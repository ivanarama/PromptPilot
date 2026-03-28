"""Worker — executes tasks from the queue."""

import json
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from . import db
from .config import BASE_DELAY, DEFAULT_CLI, MAX_DELAY, POLL_INTERVAL, TASK_TIMEOUT, build_cmd, get_provider_env

RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "ratelimit",
    "overloaded",
    "too many requests",
    "429",
    "quota exceeded",
    "capacity",
    "try again later",
]


def is_rate_limited(stderr: str, exit_code: int) -> bool:
    if exit_code == 0:
        return False
    text = stderr.lower()
    return any(p in text for p in RATE_LIMIT_PATTERNS)


def compute_next_run(retry_count: int) -> datetime:
    delay = min(BASE_DELAY * (2 ** retry_count), MAX_DELAY)
    jitter = delay * 0.1 * (random.random() * 2 - 1)
    return datetime.now(timezone.utc) + timedelta(seconds=delay + jitter)


def parse_stream_json(stdout: str) -> dict:
    """Parse stream-json output from Claude CLI.

    Extracts text from assistant messages, metadata from result event,
    and rate limit info.
    """
    text_parts = []
    meta = {}
    rate_limit_info = None
    denials = []

    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Not JSON line — treat as plain text
            text_parts.append(line)
            continue

        etype = event.get("type")

        if etype == "assistant":
            # Extract text content from assistant messages
            msg = event.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])

        elif etype == "result":
            # Final result event — metadata
            meta["cost"] = event.get("total_cost_usd")
            meta["session_id"] = event.get("session_id")
            meta["duration_ms"] = event.get("duration_ms")
            meta["num_turns"] = event.get("num_turns")
            meta["is_error"] = event.get("is_error")
            meta["subtype"] = event.get("subtype")
            usage = event.get("usage", {})
            meta["input_tokens"] = usage.get("input_tokens")
            meta["output_tokens"] = usage.get("output_tokens")
            model_usage = event.get("modelUsage", {})
            if model_usage:
                meta["model"] = list(model_usage.keys())[0]
            # If result has text and we didn't capture any
            if event.get("result") and not text_parts:
                text_parts.append(event["result"])
            for d in event.get("permission_denials", []):
                desc = d.get("tool_input", {}).get("description") or d.get("tool_input", {}).get("command", "")
                denials.append(f"[{d.get('tool_name', '?')}] {desc}")

        elif etype == "rate_limit_event":
            rate_limit_info = event.get("rate_limit_info", {})
            meta["rate_limit"] = rate_limit_info

    text = "\n".join(text_parts).strip()
    if denials:
        meta["denials"] = denials

    return {"text": text, "meta": meta, "rate_limit_info": rate_limit_info}


def format_result(parsed: dict) -> str:
    """Format parsed result for storage — human-readable text + JSON meta."""
    parts = []

    if parsed["text"]:
        parts.append(parsed["text"])

    meta = parsed["meta"]
    if meta:
        parts.append("")
        parts.append("--- Meta ---")
        if meta.get("model"):
            parts.append(f"Model: {meta['model']}")
        if meta.get("cost") is not None:
            parts.append(f"Cost: ${meta['cost']:.4f}")
        if meta.get("duration_ms") is not None:
            parts.append(f"Time: {meta['duration_ms'] / 1000:.1f}s")
        if meta.get("input_tokens") is not None:
            parts.append(f"Tokens: {meta['input_tokens']} in / {meta.get('output_tokens', '?')} out")
        if meta.get("session_id"):
            parts.append(f"Session: {meta['session_id']}")
        if meta.get("rate_limit"):
            rl = meta["rate_limit"]
            resets = rl.get("resetsAt")
            if resets:
                dt = datetime.fromtimestamp(resets)
                parts.append(f"Rate limit resets: {dt.strftime('%Y-%m-%d %H:%M')}")
        if meta.get("denials"):
            parts.append(f"\nPermission denials ({len(meta['denials'])}):")
            for d in meta["denials"]:
                parts.append(f"  {d}")

    return "\n".join(parts)


def is_stream_json(stdout: str) -> bool:
    """Check if output looks like stream-json (multiple JSON lines)."""
    if not stdout:
        return False
    first_line = stdout.strip().split("\n", 1)[0].strip()
    if not first_line:
        return False
    try:
        data = json.loads(first_line)
        return isinstance(data, dict) and "type" in data
    except (json.JSONDecodeError, TypeError):
        return False


def execute_task(task):
    """Run CLI with the task's prompt."""
    provider = task.provider or DEFAULT_CLI
    cmd = build_cmd(provider, task.prompt)

    env = get_provider_env(provider)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TASK_TIMEOUT,
            cwd=task.working_dir,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        db.mark_failed(task.id, "Execution timed out", exit_code=-1)
        return
    except FileNotFoundError:
        db.mark_failed(task.id, f"CLI '{provider}' not found. Is it installed and in PATH?", exit_code=-1)
        return

    if is_rate_limited(result.stderr, result.returncode):
        if task.retry_count >= task.max_retries:
            db.mark_failed(task.id, f"Rate limited, max retries ({task.max_retries}) exceeded.\n{result.stderr}")
            return
        next_run = compute_next_run(task.retry_count)
        db.mark_rate_limited(task.id, next_run, error=result.stderr or "Rate limited")
        print(f"  -> Rate limited. Retry #{task.retry_count + 1} at {next_run.strftime('%H:%M:%S')}")
        return

    if result.returncode != 0:
        db.mark_failed(task.id, result.stderr or result.stdout, exit_code=result.returncode)
        print(f"  -> Failed (exit {result.returncode})")
        return

    # Parse output
    model_used = None
    if is_stream_json(result.stdout):
        parsed = parse_stream_json(result.stdout)
        output = format_result(parsed)
        model_used = parsed["meta"].get("model")
        # Check for rate limit in stream events
        rl = parsed.get("rate_limit_info")
        if rl and rl.get("status") != "allowed":
            if task.retry_count >= task.max_retries:
                db.mark_failed(task.id, f"Rate limited.\n{output}")
                return
            next_run = compute_next_run(task.retry_count)
            db.mark_rate_limited(task.id, next_run, error=output or "Rate limited")
            print(f"  -> Rate limited (stream event). Retry at {next_run.strftime('%H:%M:%S')}")
            return
    else:
        # Plain text output (non-Claude CLIs)
        output = result.stdout

    db.mark_completed(task.id, output, exit_code=0, model_used=model_used)
    text_preview = output[:80].replace("\n", " ").strip()
    print(f"  -> Completed: {text_preview}")


def run_worker():
    """Main worker loop."""
    running = True

    def stop(signum, frame):
        nonlocal running
        print("\nShutting down worker...")
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # Recover any tasks stuck in 'running' from a previous crash
    db.recover_running()

    print(f"PromptPilot worker started (poll every {POLL_INTERVAL}s)")
    print(f"Timeout: {TASK_TIMEOUT}s | Backoff: {BASE_DELAY}-{MAX_DELAY}s")
    print("Waiting for tasks...\n")

    while running:
        task = db.get_next_runnable()
        if task is None:
            time.sleep(POLL_INTERVAL)
            continue

        provider = task.provider or DEFAULT_CLI
        prompt_preview = task.prompt[:60].replace("\n", " ")
        print(f"[#{task.id}] [{provider}] Running: {prompt_preview}...")
        execute_task(task)

    print("Worker stopped.")
