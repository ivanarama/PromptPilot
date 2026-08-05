"""Worker — executes tasks from the queue."""

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from . import db
from .config import BASE_DELAY, DEFAULT_CLI, MAX_DELAY, POLL_INTERVAL, TASK_TIMEOUT, build_cmd, get_provider_env, load_providers

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
    """Parse stream-json output from Claude CLI or OpenCode.

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
            # Claude Code: Extract text content from assistant messages
            msg = event.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])

        elif etype == "text":
            # OpenCode: Extract text from part.text
            part = event.get("part", {})
            if part.get("text"):
                text_parts.append(part["text"])

        elif etype == "result":
            # Claude Code: Final result event — metadata
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
            # Extract result text (always for errors, fallback for empty output)
            if event.get("result"):
                if event.get("is_error") or not text_parts:
                    text_parts.append(event["result"])
            for d in event.get("permission_denials", []):
                desc = d.get("tool_input", {}).get("description") or d.get("tool_input", {}).get("command", "")
                denials.append(f"[{d.get('tool_name', '?')}] {desc}")

        elif etype == "step_finish":
            # OpenCode: Final step event — metadata
            part = event.get("part", {})
            if part.get("cost") is not None:
                meta["cost"] = part["cost"]
            if event.get("sessionID"):
                meta["session_id"] = event["session_id"]
            tokens = part.get("tokens", {})
            if tokens:
                meta["input_tokens"] = tokens.get("input")
                meta["output_tokens"] = tokens.get("output")
                meta["total_tokens"] = tokens.get("total")

        elif etype == "system":
            # System events (api_retry, errors, etc.)
            subtype = event.get("subtype", "")
            error_msg = event.get("error", "")
            if subtype == "api_retry" and error_msg:
                attempt = event.get("attempt", "?")
                status = event.get("error_status", "")
                text_parts.append(f"API retry #{attempt} (status {status}): {error_msg}")

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


def _kill_process_tree(proc):
    """Kill the task's process and its children (process group on POSIX)."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _effective_timeout(task):
    """Per-task timeout in seconds; None = no limit (0 disables the global one)."""
    if task.task_timeout == 0:
        return None
    if task.task_timeout is not None:
        return task.task_timeout
    return None if TASK_TIMEOUT == 0 else TASK_TIMEOUT


def _maybe_recur(task):
    """After a successful run, enqueue the next occurrence of a recurring task."""
    if not task.recurrence:
        return
    next_dt = db.parse_recurrence(task.recurrence)
    if not next_dt:
        return
    from .models import TaskCreate
    db.create_task(TaskCreate(
        prompt=task.prompt,
        working_dir=task.working_dir,
        provider=task.provider,
        priority=task.priority,
        scheduled_at=next_dt,
        max_retries=task.max_retries,
        skip_permissions=task.skip_permissions,
        model=task.model,
        recurrence=task.recurrence,
        tg_chat_id=task.tg_chat_id,
        task_timeout=task.task_timeout,
        detached=task.detached,
    ))
    print(f"  -> Recurring: next run at {next_dt.strftime('%Y-%m-%d %H:%M UTC')}")


def _execute_herdr_task(task, provider_cfg, host=None, machine=None):
    """Run the task in a live herdr session (providers with executor=herdr).

    host is the ssh target of the machine the session lives on (None = local).
    """
    from .herdr_exec import attach_hint, run_in_herdr

    attach = attach_hint(host)
    where = f" на машине {machine}" if machine else ""

    def on_blocked(pane_id):
        print(f"  -> Blocked, waiting for approval in pane {pane_id}{where}")
        if task.tg_chat_id:
            db.add_notification(
                task.tg_chat_id,
                f"⏸ Задача #{task.id} ждёт подтверждения в herdr{where} (панель {pane_id}).\n"
                f"Подключись командой: {attach} — подтверди действие, задача продолжится.",
                task_id=task.id,
            )

    outcome = run_in_herdr(task, provider_cfg, on_blocked=on_blocked,
                           timeout=_effective_timeout(task),
                           cancel_check=lambda: db.is_cancel_requested(task.id),
                           keep_pane=task.keep_pane, host=host)

    if outcome.get("cancelled"):
        db.clear_cancel_request(task.id)
        db.mark_cancelled(task.id, "Отменена пользователем во время выполнения")
        print("  -> Cancelled by user")
        return

    if outcome["rate_limited"]:
        if task.retry_count >= task.max_retries:
            db.mark_failed(task.id, f"Rate limited, max retries ({task.max_retries}) exceeded.\n{outcome['error']}")
            return
        next_run = compute_next_run(task.retry_count)
        db.mark_rate_limited(task.id, next_run, error=outcome["error"] or "Rate limited")
        print(f"  -> Rate limited. Retry #{task.retry_count + 1} at {next_run.strftime('%H:%M:%S')}")
        return

    if not outcome["ok"]:
        db.mark_failed(task.id, outcome["error"], exit_code=1)
        print("  -> Failed (herdr)")
        return

    db.mark_completed(task.id, outcome["output"], exit_code=0)
    text_preview = outcome["output"][:80].replace("\n", " ").strip()
    print(f"  -> Completed: {text_preview}")
    _maybe_recur(task)


def _wrap_ssh(host, cmd, env_extra):
    """Run the command on a remote machine: basename resolved by the remote
    login-shell PATH, provider env passed via `env K=V`."""
    import shlex
    remote = list(cmd)
    remote[0] = os.path.basename(remote[0])
    envp = [f"{k}={v}" for k, v in (env_extra or {}).items() if v]
    inner = " ".join(shlex.quote(x) for x in ((["env", *envp] if envp else []) + remote))
    # ssh flattens argv into one remote command line — quote the -lc payload
    return ["ssh", "-o", "BatchMode=yes", host, "bash", "-lc", shlex.quote(inner)]


def execute_task(task):
    """Run CLI with the task's prompt."""
    provider = task.provider or DEFAULT_CLI

    provider_cfg = load_providers().get(provider, {})
    machine = getattr(task, "machine", None)

    host = None
    if machine:
        from .config import load_machines
        m = load_machines().get(machine)
        if not m or not m.get("host"):
            db.mark_failed(task.id, f"Машина «{machine}» не найдена в реестре")
            return
        host = m["host"]

    if provider_cfg.get("executor") == "herdr":
        # herdr sessions work the same way on any machine: the CLI calls go
        # over ssh, the pane lives there (attach with `herdr --remote <host>`).
        _execute_herdr_task(task, provider_cfg, host=host, machine=machine)
        return

    cmd = build_cmd(provider, task.prompt, skip_permissions=task.skip_permissions, session_id=task.session_id, model=task.model)

    env = get_provider_env(provider)

    if machine:
        if task.detached:
            db.mark_failed(task.id, "Фоновый запуск (detached) на удалённой машине пока не поддерживается")
            return
        cmd = _wrap_ssh(host, cmd, provider_cfg.get("env"))
        env = os.environ.copy()
    else:
        # On Windows, .cmd/.bat wrappers (e.g. npm-installed CLIs like qwen) are
        # invisible to subprocess without shell=True.  shutil.which() resolves the
        # full path including extension so subprocess can find and run them directly.
        resolved = shutil.which(cmd[0], path=env.get("PATH"))
        if resolved:
            cmd[0] = resolved

    # Detached mode: start process and return immediately — for servers/bots that run forever
    if task.detached:
        import platform
        kwargs = {"cwd": task.working_dir, "env": env, "stdin": subprocess.DEVNULL}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **kwargs)
            db.mark_completed(task.id, f"Запущен в фоне (PID {proc.pid})", exit_code=0)
            print(f"  -> Detached (PID {proc.pid})")
        except FileNotFoundError:
            db.mark_failed(task.id, f"Command not found: {cmd[0]}", exit_code=-1)
        return

    effective_timeout = _effective_timeout(task)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=task.working_dir,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError:
        db.mark_failed(task.id, f"CLI '{provider}' not found. Is it installed and in PATH?", exit_code=-1)
        return

    # Poll instead of blocking: allows user-requested cancellation of a
    # RUNNING task (Web UI/bot) and the per-task timeout.
    started = time.monotonic()
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=2)
            break
        except subprocess.TimeoutExpired:
            if db.is_cancel_requested(task.id):
                _kill_process_tree(proc)
                proc.communicate()
                db.clear_cancel_request(task.id)
                db.mark_cancelled(task.id, "Отменена пользователем во время выполнения")
                print("  -> Cancelled by user")
                return
            if effective_timeout and time.monotonic() - started > effective_timeout:
                _kill_process_tree(proc)
                proc.communicate()
                db.mark_failed(task.id, f"Execution timed out after {effective_timeout}s", exit_code=-1)
                return

    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    if is_rate_limited(result.stderr, result.returncode):
        # Extract readable error from stream-json if possible
        rl_error = result.stderr or result.stdout
        if is_stream_json(rl_error):
            parsed = parse_stream_json(rl_error)
            rl_error = format_result(parsed) or rl_error
        elif is_stream_json(result.stdout):
            parsed = parse_stream_json(result.stdout)
            rl_error = format_result(parsed) or rl_error
        if task.retry_count >= task.max_retries:
            db.mark_failed(task.id, f"Rate limited, max retries ({task.max_retries}) exceeded.\n{rl_error}")
            return
        next_run = compute_next_run(task.retry_count)
        db.mark_rate_limited(task.id, next_run, error=rl_error or "Rate limited")
        print(f"  -> Rate limited. Retry #{task.retry_count + 1} at {next_run.strftime('%H:%M:%S')}")
        return

    if result.returncode != 0:
        error_text = result.stderr or result.stdout
        # Try to extract readable error from stream-json output
        if is_stream_json(error_text):
            parsed = parse_stream_json(error_text)
            error_text = format_result(parsed) or error_text
        elif is_stream_json(result.stdout):
            parsed = parse_stream_json(result.stdout)
            error_text = format_result(parsed) or error_text
        db.mark_failed(task.id, error_text, exit_code=result.returncode)
        print(f"  -> Failed (exit {result.returncode})")
        return

    # Parse output
    model_used = None
    session_id = None
    if is_stream_json(result.stdout):
        parsed = parse_stream_json(result.stdout)
        output = format_result(parsed)
        model_used = parsed["meta"].get("model")
        session_id = parsed["meta"].get("session_id")
        # Check for rate limit in stream events — only if no text was returned
        rl = parsed.get("rate_limit_info")
        if rl and not parsed["text"]:
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

    db.mark_completed(task.id, output, exit_code=0, model_used=model_used, session_id=session_id)
    text_preview = output[:80].replace("\n", " ").strip()
    print(f"  -> Completed: {text_preview}")

    _maybe_recur(task)


def _code_snapshot():
    """Mtimes of the package's .py files; None when frozen (code can't change)."""
    if getattr(sys, "frozen", False):
        return None
    from pathlib import Path
    base = Path(__file__).parent
    try:
        return {str(f): f.stat().st_mtime for f in base.glob("*.py")}
    except OSError:
        return None


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

    code_snapshot = _code_snapshot()

    print(f"PromptPilot worker started (poll every {POLL_INTERVAL}s)")
    print(f"Timeout: {'no limit' if TASK_TIMEOUT == 0 else f'{TASK_TIMEOUT}s'} | Backoff: {BASE_DELAY}-{MAX_DELAY}s")
    print("Waiting for tasks...\n")

    while running:
        # Auto-reload: pick up code updates between tasks (dev-friendly —
        # a stale worker silently ignoring new features is worse than a restart)
        if code_snapshot is not None and _code_snapshot() != code_snapshot:
            print("Код обновился — перезапускаю worker...", flush=True)
            os.execv(sys.executable, [sys.executable, "-m", "promptpilot", "worker"])

        if db.is_paused():
            time.sleep(POLL_INTERVAL)
            continue

        task = db.get_next_runnable()
        if task is None:
            time.sleep(POLL_INTERVAL)
            continue

        provider = task.provider or DEFAULT_CLI
        prompt_preview = task.prompt[:60].replace("\n", " ")
        print(f"[#{task.id}] [{provider}] Running: {prompt_preview}...")
        execute_task(task)

    print("Worker stopped.")
