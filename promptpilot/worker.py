"""Worker — executes tasks from the queue."""

import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from . import db
from .config import BASE_DELAY, DEFAULT_CLI, MAX_DELAY, POLL_INTERVAL, TASK_TIMEOUT, build_cmd

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


def execute_task(task):
    """Run CLI with the task's prompt."""
    provider = task.provider or DEFAULT_CLI
    cmd = build_cmd(provider, task.prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TASK_TIMEOUT,
            cwd=task.working_dir,
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
        db.mark_rate_limited(task.id, next_run)
        print(f"  -> Rate limited. Retry #{task.retry_count + 1} at {next_run.strftime('%H:%M:%S')}")
        return

    if result.returncode == 0:
        db.mark_completed(task.id, result.stdout, exit_code=0)
        print(f"  -> Completed ({len(result.stdout)} chars)")
    else:
        db.mark_failed(task.id, result.stderr or result.stdout, exit_code=result.returncode)
        print(f"  -> Failed (exit {result.returncode})")


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
