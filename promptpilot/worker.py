"""Worker — executes tasks from the queue."""

import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db, worktree
from .config import (BASE_DELAY, CONCURRENCY, DEFAULT_CLI, MAX_DELAY, MIN_FREE_MB,
                     POLL_INTERVAL, TASK_TIMEOUT, VERDICT_REQUIRED, build_cmd,
                     get_provider_env, load_providers)
from .process_tree import OwnedProcess, ProcessTreeError

# Our quota is spent — the wait is measured in hours and nothing else will get
# through either.
RATE_LIMIT_RE = re.compile(
    r"rate[ _-]?limit"
    r"|too many requests"
    r"|(?:error|status|http|code)[\s:]*429\b"
    r"|quota exceeded"
    r"|usage limit"
    r"|hit your (?:session|usage|weekly) limit",
    re.IGNORECASE,
)

# The provider is swamped (HTTP 529/503) — nothing to do with our quota, and it
# usually clears in minutes. Same requeue, very different thing to tell a human.
OVERLOADED_RE = re.compile(
    r"overloaded"
    r"|(?:error|status|http|code)[\s:]*(?:529|503)\b"
    r"|at capacity"
    r"|try again later",
    re.IGNORECASE,
)

RETRY_RATE_LIMIT = "rate_limit"
RETRY_OVERLOAD = "overload"

# Human-facing wording per reason — a 529 reported as "упёрлась в лимит" sends
# people looking at their subscription instead of status.claude.com.
RETRY_REASON_RU = {
    RETRY_RATE_LIMIT: "упёрлась в лимит (rate limit)",
    RETRY_OVERLOAD: "API перегружен (529 overloaded)",
}
RETRY_REASON_ERR = {
    RETRY_RATE_LIMIT: "Rate limited",
    RETRY_OVERLOAD: "API overloaded",
}


def _notify_pipeline_completion(task, verdict: str | None) -> None:
    if str(verdict or "").upper() != "ГОТОВО":
        return
    try:
        from . import pipeline_insights
        woken = pipeline_insights.after_task_completed(task, verdict)
        if woken:
            print(f"  -> Pipeline stages woken: {', '.join(woken)}", flush=True)
    except Exception as exc:
        # Completion is already durable; a dashboard/wakeup failure must not
        # rewrite the successful task outcome. The periodic sampler retries it.
        print(f"  !! pipeline wake-up unavailable for #{task.id}: {exc}", flush=True)


def _pipeline_defer_time(decision: dict) -> Optional[datetime]:
    """Resolve an exact budget reset before falling back to relative intervals."""
    defer_until = decision.get("defer_until")
    if defer_until:
        try:
            parsed = datetime.fromisoformat(
                str(defer_until).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
                now = datetime.now(timezone.utc)
                return max(
                    parsed,
                    now + timedelta(seconds=max(1, POLL_INTERVAL)),
                )
        except (TypeError, ValueError):
            pass
    defer_for = decision.get("defer_for")
    return db.parse_recurrence(str(defer_for)) if defer_for else None


def retry_reason(text: str, exit_code: int) -> Optional[str]:
    """Why the run must be requeued: RETRY_RATE_LIMIT, RETRY_OVERLOAD or None.

    Match against readable text — pass stderr, or the text extracted from a
    stream-json stdout, not raw JSON, so a bare "429" or "capacity" buried in a
    payload/traceback does not masquerade as a limit. A real limit wins over
    overload: limit banners often end with "try again later" too.
    """
    if exit_code == 0:
        return None
    text = text or ""
    if RATE_LIMIT_RE.search(text):
        return RETRY_RATE_LIMIT
    if OVERLOADED_RE.search(text):
        return RETRY_OVERLOAD
    return None


def is_rate_limited(text: str, exit_code: int) -> bool:
    """Whether the run failed on a limit OR a provider overload — both mean
    'requeue and try again', which is all most callers care about."""
    return retry_reason(text, exit_code) is not None


# The run died on the environment, not on the task: the door was shut (auth,
# 403, a 5xx) or the answer was cut off mid-sentence. Blaming the task for
# these buries work that was usually already done — the last word just never
# arrived. Such a run goes back into the queue instead, still bounded by
# max_retries so a permanently broken environment cannot loop forever.
ENV_FAILURE_RE = re.compile(
    r"API Error:\s*(?:401|403|5\d\d)"
    r"|Failed to authenticate"
    r"|Connection closed mid-response"
    r"|terminal_reason[\"'\s:=]+api_error"
    r"|Connection reset by peer"
    # Socket error codes stay case-SENSITIVE: lowercased, "ENOTFOUND" hides
    # inside "ModuleNotFoundError" and every missing import becomes an outage.
    r"|(?-i:\bECONNRESET\b|\bETIMEDOUT\b|\bENOTFOUND\b|\bEAI_AGAIN\b)",
    re.IGNORECASE,
)


# The agent is asked to end with this line so a finished task says WHAT
# happened, not just that the process exited 0. Parsed whether or not we asked.
VERDICTS = (
    "ГОТОВО", "УЖЕ СДЕЛАНО", "НУЖЕН ЧЕЛОВЕК", "НЕ СМОГ", "ПУСТО",
)
VERDICT_RE = re.compile(r"^[ \t>*#-]*ИТОГ:\s*(" + "|".join(VERDICTS) + r")\b", re.M | re.I)

VERDICT_INSTRUCTION = (
    "\n\nПоследней строкой ответа напиши ровно одну из:\n"
    "ИТОГ: ГОТОВО — сделано\n"
    "ИТОГ: УЖЕ СДЕЛАНО — оказалось, что уже исправлено\n"
    "ИТОГ: НУЖЕН ЧЕЛОВЕК — нужно решение или доступ человека\n"
    "ИТОГ: НЕ СМОГ — не получилось\n"
    "ИТОГ: ПУСТО — проснулся по расписанию, а делать нечего\n"
    "После двоеточия можно коротко пояснить причину."
)


def parse_verdict(text: str) -> str:
    """The task's own last word, or "" if it never said one.

    Last match wins: the agent may quote the format earlier while explaining
    itself, and only the closing line is the verdict.
    """
    matches = VERDICT_RE.findall(text or "")
    return matches[-1].upper() if matches else ""


def effective_prompt(task) -> str:
    """The prompt as the agent should see it: task, then the human's late word.

    The note goes last and says so explicitly — it is written after the task was
    already set, usually because the run was going the wrong way, so it has to
    outrank everything above it.
    """
    prompt = task.prompt
    note = (getattr(task, "note", None) or "").strip()
    if note:
        prompt += ("\n\n<приписка>\n"
                   "Это дописано человеком ПОСЛЕ постановки задачи выше и главнее её.\n"
                   f"{note}\n</приписка>")
    if VERDICT_REQUIRED:
        prompt += VERDICT_INSTRUCTION
    return prompt


def live_task_ids() -> set:
    """Tasks whose agent process is demonstrably still alive.

    Found by the marker every run carries in its environment, so a run is
    recognised by the process itself rather than by our own bookkeeping — that
    is what makes it survive the worker dying. Linux-only and best effort: where
    it can't be read, nothing is claimed to be alive and the old behaviour holds.
    """
    ids = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        pids = None
    if pids is None:
        if os.name != "posix":
            return ids
        try:
            result = subprocess.run(
                ["ps", "eww", "-axo", "pid=,command="],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired):
            return ids
        if result.returncode:
            return ids
        for match in re.finditer(r"(?:^|\s)PP_TASK_ID=(\d+)(?=\s|$)", result.stdout):
            ids.add(int(match.group(1)))
        return ids
    for pid in pids:
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read()
        except OSError:
            continue  # not ours, or gone between listdir and open
        for part in raw.split(b"\0"):
            if part.startswith(b"PP_TASK_ID="):
                try:
                    ids.add(int(part.split(b"=", 1)[1]))
                except ValueError:
                    pass
    return ids


def _marked_task_process_groups(task_id: int, task_started_at: str,
                                reservation_token: str) -> tuple[set[int], str]:
    """Find POSIX provider groups carrying one unguessable exact-run marker."""
    if os.name != "posix":
        return set(), ""
    if (not task_started_at
            or not re.fullmatch(r"[0-9a-f]{32}", reservation_token or "")):
        return set(), "recovered provider has no exact reservation marker"
    pids = []
    try:
        proc_entries = [value for value in os.listdir("/proc") if value.isdigit()]
    except OSError:
        proc_entries = None
    if proc_entries is not None:
        markers = {
            f"PP_TASK_ID={task_id}".encode(),
            f"PP_TASK_STARTED_AT={task_started_at}".encode(),
            f"PP_PIPELINE_TARGET_TOKEN={reservation_token}".encode(),
        }
        for value in proc_entries:
            try:
                with open(f"/proc/{value}/environ", "rb") as stream:
                    if markers.issubset(set(stream.read().split(b"\0"))):
                        pids.append(int(value))
            except OSError:
                continue
    else:
        try:
            result = subprocess.run(
                ["ps", "eww", "-axo", "pid=,command="],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return set(), f"could not inspect orphan provider processes: {exc}"
        if result.returncode:
            return set(), (
                "could not inspect orphan provider processes: "
                + (result.stderr or f"ps exit {result.returncode}").strip())
        markers = [re.compile(
            rf"(?:^|\s){re.escape(value)}(?=\s|$)") for value in (
                f"PP_TASK_ID={int(task_id)}",
                f"PP_TASK_STARTED_AT={task_started_at}",
                f"PP_PIPELINE_TARGET_TOKEN={reservation_token}",
            )]
        for line in result.stdout.splitlines():
            stripped = line.lstrip()
            pid_text, separator, command = stripped.partition(" ")
            if (separator and pid_text.isdigit()
                    and all(marker.search(command) for marker in markers)):
                pids.append(int(pid_text))
    groups = set()
    for pid in pids:
        try:
            groups.add(os.getpgid(pid))
        except ProcessLookupError:
            continue
        except OSError as exc:
            return set(), f"could not inspect provider process {pid}: {exc}"
    own_group = os.getpgrp()
    if own_group in groups:
        return set(), "refusing to stop a provider in the worker process group"
    return groups, ""


def _cleanup_orphan_headless_provider(task_id: int, task_started_at: str,
                                      reservation_token: str) -> str:
    """Stop and verify a POSIX provider left by an earlier worker process."""
    if os.name == "nt":
        # Headless Windows providers live in a private KILL_ON_JOB_CLOSE Job.
        # Losing the worker's handle is already a kernel-enforced cleanup.
        return ""
    groups, error = _marked_task_process_groups(
        task_id, task_started_at, reservation_token)
    if error:
        return error
    for group_id in groups:
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return f"could not stop orphan provider group {group_id}: {exc}"
    deadline = time.monotonic() + 10
    while groups and time.monotonic() < deadline:
        time.sleep(0.1)
        groups, error = _marked_task_process_groups(
            task_id, task_started_at, reservation_token)
        if error:
            return error
    return ("orphan provider processes are still alive: "
            + ", ".join(str(value) for value in sorted(groups))) if groups else ""


def _recovered_provider_cleanup(task, reservation: dict):
    """Build an idempotent cleanup for a reserved attempt after worker loss."""
    started_at = getattr(task, "started_at", None)
    expected_attempt = (
        started_at.astimezone(timezone.utc).isoformat()
        if isinstance(started_at, datetime) else "")
    if (not expected_attempt
            or reservation.get("task_started_at") != expected_attempt):
        return lambda: "recovered reservation belongs to another task attempt"
    ownership_kind = str(reservation.get("ownership_kind") or "")
    if ownership_kind not in {"headless", "herdr"}:
        return lambda: "recovered reservation has no trusted provider ownership kind"
    if ownership_kind == "herdr":
        state = str(reservation.get("herdr_session_state") or "")
        if state == "reserved":
            # Election was durable, but session creation never began. The
            # ordered state machine proves no Herdr provider can exist yet.
            return lambda: ""
        if state not in {"creating", "owned"}:
            return lambda: (
                "recovered Herdr reservation predates the durable "
                "session descriptor")
        host = None
        if getattr(task, "machine", None):
            from .config import load_machines, machine_remote
            machine = load_machines().get(task.machine)
            if not machine or not machine.get("host"):
                return lambda: f"machine {task.machine!r} is unavailable for orphan cleanup"
            host = machine_remote(machine)

        def cleanup_herdr():
            from .herdr_exec import (
                _close_owned_session, _close_stale_tabs, _ensure_server,
            )
            try:
                _ensure_server(host)
                pane_id = str(reservation.get("herdr_pane_id") or "")
                tab_id = str(reservation.get("herdr_tab_id") or "")
                workspace_id = str(
                    reservation.get("herdr_workspace_id") or "")
                if state == "owned":
                    if not pane_id or not tab_id:
                        return "recovered Herdr descriptor is incomplete"
                    close_args = (
                        ["workspace", "close", workspace_id]
                        if workspace_id else ["tab", "close", tab_id])
                    error = _close_owned_session(
                        f"recovered task #{task.id}", close_args, host,
                        pane_id=pane_id)
                    if error:
                        return error
                elif state == "creating":
                    # The agent start is ordered strictly after descriptor CAS.
                    # A crash here can leave only an idle shell/tab; labels are
                    # hygiene, not the proof used for an owned provider.
                    _close_stale_tabs(task.id, host)
            except Exception as exc:
                return f"could not close recovered herdr provider: {type(exc).__name__}: {exc}"
            return ""

        return cleanup_herdr
    if getattr(task, "machine", None):
        return lambda: "cannot prove a recovered remote headless provider stopped"
    task_started_at = str(reservation.get("task_started_at") or "")
    reservation_token = str(reservation.get("token") or "")
    return lambda: _cleanup_orphan_headless_provider(
        task.id, task_started_at, reservation_token)


def _reconcile_recovered_attempt(task) -> None:
    """Repair projections after an orphan becomes pending or cancelled."""
    try:
        # For a recovered cancellation this also consumes a latched pipeline
        # wake, matching the normal execute_task finally path.
        _recur_after_run(task)
        from . import workflows
        workflows.sync_task(task.id)
        workflows.advance_linked_task(task.id)
    except Exception as exc:
        print(
            f"  !! could not reconcile recovered attempt #{task.id}: {exc}",
            flush=True,
        )


def _reconcile_reserved_running_tasks(alive: set[int]):
    """Clean provider orphans before expired target fences become reclaimable."""
    reservations = db.list_pipeline_target_reservations()
    by_task = {int(item["task_id"]): item for item in reservations}
    running = db.list_tasks(statuses=["running"], limit=100000)
    quarantines = []
    keep = set(alive)
    for task in running:
        reservation = by_task.get(task.id)
        if reservation is None:
            continue
        cleanup = _recovered_provider_cleanup(task, reservation)
        error = cleanup()
        if error:
            keep.add(task.id)
            quarantines.append((task, RecoveredProviderOwnershipError(
                f"recovered provider ownership is uncertain: {error}", cleanup)))
        else:
            if db.recover_running_attempt(task.id, task.started_at):
                _reconcile_recovered_attempt(task)
                keep.discard(task.id)
            else:
                fresh = db.get_task(task.id)
                if (fresh is not None and fresh.status.value == "running"
                        and db.task_has_live_pipeline_target_reservation(task.id)):
                    keep.add(task.id)
                    quarantines.append((task, RecoveredProviderOwnershipError(
                        "recovered provider stopped, but exact attempt requeue was rejected",
                        cleanup,
                    )))
    return keep, quarantines


def env_failure(text: str) -> str:
    """The bit of text proving the environment failed, or "" if it did not."""
    m = ENV_FAILURE_RE.search(text or "")
    return m.group(0) if m else ""


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
                meta["session_id"] = event["sessionID"]
            tokens = part.get("tokens", {})
            if tokens:
                meta["input_tokens"] = tokens.get("input")
                meta["output_tokens"] = tokens.get("output")
                meta["total_tokens"] = tokens.get("total")

        elif etype == "thread.started":
            # Codex exec --json: the thread id is the resumable session id.
            if event.get("thread_id"):
                meta["session_id"] = event["thread_id"]

        elif etype == "item.completed":
            # Codex exec --json: only the final agent_message belongs in the
            # human-readable result. Command/reasoning items stay structured.
            item = event.get("item", {})
            if item.get("type") == "agent_message" and item.get("text"):
                text_parts.append(item["text"])
            elif item.get("type") == "error" and item.get("message"):
                text_parts.append(f"Codex warning: {item['message']}")

        elif etype == "turn.completed":
            # Codex counts cached input inside input_tokens and exposes the
            # cached part separately. Keep both so the UI does not invent an
            # estimate from wall clock time.
            usage = event.get("usage", {})
            if usage:
                meta["input_tokens"] = usage.get("input_tokens")
                meta["cached_input_tokens"] = usage.get("cached_input_tokens")
                meta["output_tokens"] = usage.get("output_tokens")
                meta["reasoning_output_tokens"] = usage.get("reasoning_output_tokens")
                if usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
                    meta["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

        elif etype in ("turn.failed", "error"):
            error = event.get("error", event)
            message = error.get("message") if isinstance(error, dict) else str(error)
            if message:
                text_parts.append(f"Codex error: {message}")

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
        if meta.get("cached_input_tokens") is not None:
            parts.append(f"Cached input: {meta['cached_input_tokens']}")
        if meta.get("reasoning_output_tokens") is not None:
            parts.append(f"Reasoning output: {meta['reasoning_output_tokens']}")
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


def _remember_stream_session(task_id: int, line: str) -> None:
    """Commit a resumable session as soon as the provider announces it."""
    try:
        event = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return
    session_id = None
    if event.get("type") == "thread.started":
        session_id = event.get("thread_id")
    elif event.get("type") == "system":
        session_id = event.get("session_id")
    if session_id:
        db.set_session_id(task_id, session_id)


def _read_process_pipe(pipe, chunks: list[str], task_id: int | None = None) -> None:
    """Drain one provider pipe without blocking the cancellation poll loop."""
    try:
        for line in iter(pipe.readline, ""):
            chunks.append(line)
            if task_id is not None:
                _remember_stream_session(task_id, line)
    finally:
        pipe.close()


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


def _stop_owned_process(tree: OwnedProcess) -> None:
    """End an owned process tree, then release its lifetime boundary."""
    try:
        tree.terminate()
    except (ProcessLookupError, PermissionError, OSError) as exc:
        print(f"  !! could not terminate full task process tree: {exc}", flush=True)
        try:
            tree.process.kill()
        except OSError:
            pass
    finally:
        # On Windows this closes a KILL_ON_JOB_CLOSE handle, an independent
        # second guarantee that every process assigned to this task is ended.
        tree.close()


_active_provider_trees: dict[tuple[str, int], OwnedProcess] = {}
_active_provider_trees_lock = threading.Lock()


class ProviderOwnershipError(RuntimeError):
    """A provider may still be live; retry cleanup before terminal release."""

    def __init__(self, message: str, cleanup, heartbeat=None, on_cleaned=None):
        super().__init__(message)
        self.cleanup = cleanup
        self.heartbeat = heartbeat
        self.on_cleaned = on_cleaned

    def retry_cleanup(self) -> bool:
        heartbeat_error = ""
        if callable(self.heartbeat):
            try:
                self.heartbeat(force=True)
            except Exception as exc:
                heartbeat_error = f"{type(exc).__name__}: {exc}"
        try:
            error = self.cleanup()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        if error:
            detail = error
            if heartbeat_error:
                detail += f"; target heartbeat failed: {heartbeat_error}"
            print(f"  !! provider ownership still uncertain: {detail}", flush=True)
            return False
        return True

    def finish_after_cleanup(self) -> bool:
        if not callable(self.on_cleaned):
            return False
        try:
            return bool(self.on_cleaned())
        except Exception as exc:
            print(
                f"  !! provider terminal intent could not be committed: "
                f"{type(exc).__name__}: {exc}", flush=True,
            )
            return False


class RecoveredProviderOwnershipError(ProviderOwnershipError):
    """A pre-existing orphan should be requeued, not counted as a failed run."""


class PipelineTargetLeaseLost(RuntimeError):
    """The exact target fence disappeared while its provider was still live."""


PIPELINE_TARGET_HEARTBEAT_TTL = 600
PIPELINE_TARGET_HEARTBEAT_INTERVAL = 30.0


class _PipelineTargetHeartbeat:
    """Exact lease heartbeat spanning admission, provider, and quarantine."""

    def __init__(self, reservation: dict, task_id: int, *, task_started_at: str,
                 ownership_kind: str, ttl_seconds: int, interval_seconds: float):
        if not isinstance(reservation, dict):
            raise PipelineTargetLeaseLost(
                "replicated pipeline route has no exact target reservation")
        if reservation.get("task_id") != task_id:
            raise PipelineTargetLeaseLost(
                "pipeline target reservation belongs to another task")
        if reservation.get("task_started_at") != task_started_at:
            raise PipelineTargetLeaseLost(
                "pipeline target reservation belongs to another task attempt")
        if reservation.get("ownership_kind") != ownership_kind:
            raise PipelineTargetLeaseLost(
                "pipeline target reservation ownership kind changed")
        self.reservation = reservation
        self.task_id = task_id
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self.last_touch = None
        self.failure = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def __call__(self, *, force: bool = False):
        now = time.monotonic()
        with self.lock:
            if self.failure is not None:
                raise PipelineTargetLeaseLost(
                    f"pipeline target heartbeat failed: {self.failure}")
            if (not force and self.last_touch is not None
                    and now - self.last_touch < self.interval_seconds):
                return self.reservation
            renewed = _retry_sqlite_busy(
                lambda: db.renew_pipeline_target_reservation(
                    self.reservation, self.ttl_seconds),
                f"pipeline target heartbeat for task #{self.task_id}",
            )
            if renewed is None:
                raise PipelineTargetLeaseLost(
                    "pipeline target reservation expired or was released")
            self.reservation = renewed
            self.last_touch = now
            return renewed

    def start(self):
        """Fence immediately, then keep renewing independent of provider polls."""
        self(force=True)
        if self.thread is None:
            self.thread = threading.Thread(
                target=self._run,
                name=f"pp-target-heartbeat-{self.task_id}",
                daemon=True,
            )
            self.thread.start()
        return self

    def begin_herdr_session(self):
        """Record that no Herdr agent may start until exact IDs are bound."""
        with self.lock:
            if self.failure is not None:
                raise PipelineTargetLeaseLost(
                    f"pipeline target heartbeat failed: {self.failure}")
            updated = db.begin_pipeline_target_herdr_session(self.reservation)
            if updated is None:
                raise PipelineTargetLeaseLost(
                    "pipeline target could not enter Herdr creation state")
            self.reservation = updated
            return updated

    def bind_herdr_session(self, pane_id: str, tab_id: str,
                           workspace_id: str = ""):
        """Persist immutable Herdr IDs before agent start."""
        with self.lock:
            if self.failure is not None:
                raise PipelineTargetLeaseLost(
                    f"pipeline target heartbeat failed: {self.failure}")
            updated = db.bind_pipeline_target_herdr_session(
                self.reservation, pane_id, tab_id, workspace_id)
            if updated is None:
                raise PipelineTargetLeaseLost(
                    "pipeline target rejected its Herdr ownership descriptor")
            self.reservation = updated
            return updated

    def _run(self):
        while not self.stop_event.wait(self.interval_seconds):
            try:
                task = db.get_task(self.task_id)
                if task is None or task.status.value != "running":
                    return
                self(force=True)
            except Exception as exc:
                with self.lock:
                    if self.failure is None:
                        self.failure = f"{type(exc).__name__}: {exc}"
                return

    def stop(self):
        self.stop_event.set()


def _pipeline_target_heartbeater(reservation: dict, task_id: int,
                                 *, task_started_at: str, ownership_kind: str,
                                 ttl_seconds: int = PIPELINE_TARGET_HEARTBEAT_TTL,
                                 interval_seconds: float = PIPELINE_TARGET_HEARTBEAT_INTERVAL):
    return _PipelineTargetHeartbeat(
        reservation, task_id, task_started_at=task_started_at,
        ownership_kind=ownership_kind,
        ttl_seconds=ttl_seconds,
        interval_seconds=interval_seconds)


def _renew_quarantined_target(task_id: int) -> int | None:
    """Keep any target fenced while provider cleanup remains uncertain."""
    try:
        return db.renew_task_pipeline_target_reservations(
            task_id, PIPELINE_TARGET_HEARTBEAT_TTL)
    except Exception as exc:
        print(
            f"  !! pipeline target quarantine heartbeat #{task_id}: {exc}",
            flush=True,
        )
        return None


def _provider_tree_key(task_id: int) -> tuple[str, int]:
    return os.path.realpath(str(db.DB_PATH)), int(task_id)


def _register_provider_tree(task_id: int, tree: OwnedProcess) -> None:
    key = _provider_tree_key(task_id)
    with _active_provider_trees_lock:
        duplicate = key in _active_provider_trees
        if not duplicate:
            _active_provider_trees[key] = tree
    if duplicate:
        # The new child already exists but is not registered. End it before
        # surfacing the duplicate; the old registered owner remains intact.
        _stop_owned_process(tree)
        raise RuntimeError(f"task #{task_id} already owns a provider tree")


def _forget_provider_tree(task_id: int, tree: OwnedProcess) -> None:
    key = _provider_tree_key(task_id)
    with _active_provider_trees_lock:
        if _active_provider_trees.get(key) is tree:
            _active_provider_trees.pop(key, None)


def _close_registered_provider_tree(task_id: int) -> bool:
    """Stop a child before any caller can release its exact target lease."""
    key = _provider_tree_key(task_id)
    with _active_provider_trees_lock:
        tree = _active_provider_trees.get(key)
    if tree is None:
        return True
    try:
        _stop_owned_process(tree)
    except Exception as exc:
        print(
            f"  !! provider tree cleanup #{task_id} is not confirmed: {exc}",
            flush=True,
        )
        return False
    _forget_provider_tree(task_id, tree)
    return True


def _effective_timeout(task):
    """Per-task timeout in seconds; None = no limit (0 disables the global one)."""
    if task.task_timeout == 0:
        return None
    if task.task_timeout is not None:
        return task.task_timeout
    return None if TASK_TIMEOUT == 0 else TASK_TIMEOUT


def _recur_after_run(task):
    """Продлить расписание после завершившегося прогона — успешного или нет.

    Раньше следующее вхождение создавалось только на успешном пути, и первое же
    падение убивало серию навсегда и молча: 2026-08-25 merge-shepherd исчез из
    очереди на 13 часов из-за одного rate limit. Расписание — это намерение
    «делай каждые 2 часа», а не награда за удачный прогон.

    Статус перечитываем из базы: в памяти он остался тем, с которым задачу
    забрали. rate_limited/running сюда не попадают (прогон ещё продолжится), а
    отменённая серия не продлевается — отмена это воля человека.
    """
    if not task.recurrence:
        return
    fresh = db.get_task(task.id)
    if not fresh or fresh.status.value not in ("completed", "failed"):
        # A pending/rate-limited retry is still the same occurrence. Preserve
        # a wake that arrived while it was running so a second useful
        # occurrence follows after the retry; only explicit cancellation
        # abandons that intent.
        if (fresh and fresh.series_id
                and fresh.status.value == "cancelled"):
            db.clear_pipeline_series_wake(fresh.series_id)
        return
    _maybe_recur(fresh, failed=fresh.status.value == "failed")
    if fresh.series_id:
        db.consume_pipeline_series_wake(fresh.series_id)


def _pipeline_repeat_guard(task) -> dict | None:
    """Apply the repeat-blocker circuit breaker to configured pipeline work."""
    if not task.series_id:
        return None
    try:
        from . import pipeline_insights
        if pipeline_insights._matching_queue(task) is None:
            return None
        state = db.pause_pipeline_series_on_repeated_blocker(
            task.series_id, task.id)
    except Exception as exc:
        # Profile visibility is optional infrastructure. Do not silently kill
        # an otherwise healthy generic schedule when it cannot be classified.
        print(
            f"  !! repeat-blocker guard unavailable for #{task.id}: {exc}",
            flush=True,
        )
        return None
    if not state.get("suppress_recurrence"):
        return state
    if state.get("newly_paused"):
        print(
            f"  -> Series #{task.series_id} paused: tasks "
            f"#{state['previous_task_id']} and #{task.id} reported the same blocker",
            flush=True,
        )
        if task.tg_chat_id:
            try:
                db.add_notification(
                    task.tg_chat_id,
                    f"⏸ Серия «{task.prompt.splitlines()[0]}» приостановлена: "
                    "два запуска подряд вернули одинаковый блокер. "
                    "После устранения причины нажмите Resume.",
                    task_id=task.id,
                )
            except Exception as exc:
                print(f"  -> notify repeat-blocker pause failed: {exc}")
    return state


def _maybe_recur(task, failed: bool = False):
    """Enqueue the next occurrence of a recurring task."""
    if not task.recurrence:
        return
    repeat_guard = _pipeline_repeat_guard(task)
    if repeat_guard and repeat_guard.get("suppress_recurrence"):
        return
    series = db.prepare_series_recurrence(task.series_id, task.verdict) if task.series_id else None
    recurrence = series["effective_recurrence"] if series else task.recurrence
    if task.series_id and series is None:  # series was explicitly ended
        return
    # The durable series latch allows one immediate re-election after a
    # validated targeted gate-fallback. Consecutive stale results use the
    # ordinary cadence, so a moving target cannot create an unbounded hot loop.
    next_dt = (datetime.now(timezone.utc)
               if series and series.get("stale_reselect_immediate")
               else db.parse_recurrence(recurrence))
    if not next_dt:
        return
    from .models import TaskCreate
    # The prompt as stored, never the one this run was handed: a one-off note
    # must not be baked into every future occurrence.
    stored = db.get_task(task.id)
    db.create_series_occurrence_if_idle(TaskCreate(
        prompt=(stored.prompt if stored else task.prompt),
        working_dir=task.working_dir,
        provider=series["provider"] if series else task.provider,
        priority=series["priority"] if series else task.priority,
        scheduled_at=next_dt,
        max_retries=task.max_retries,
        skip_permissions=task.skip_permissions,
        model=series["model"] if series else task.model,
        effort=series["effort"] if series else task.effort,
        recurrence=series["base_recurrence"] if series else task.recurrence,
        series_id=task.series_id,
        tg_chat_id=task.tg_chat_id,
        task_timeout=series["task_timeout"] if series else task.task_timeout,
        detached=task.detached,
        # Where and how it ran is part of the schedule, not of one occurrence:
        # without these a recurring task silently drifts back to this machine,
        # the shared work tree and a closing pane on its second run.
        machine=task.machine,
        keep_pane=task.keep_pane,
        worktree=task.worktree,
    ))
    print(f"  -> Recurring: next run at {next_dt.strftime('%Y-%m-%d %H:%M UTC')}"
          f"{' (после падения)' if failed else ''}")
    # Продлили — но человек должен узнать, что серия работает вхолостую: молча
    # повторять падение каждые два часа ничем не лучше молчаливой смерти.
    if failed and task.tg_chat_id:
        try:
            when = next_dt.astimezone().strftime("%d.%m %H:%M")
            db.add_notification(
                task.tg_chat_id,
                f"🔁 Задача #{task.id} упала, но расписание продолжено — "
                f"следующий запуск в {when}.\n"
                f"Если падает подряд, серию стоит починить или отменить.",
                task_id=task.id,
            )
        except Exception as e:
            print(f"  -> notify recur-after-fail failed: {e}")


def _requeue_env_failure(task, marker: str, detail: str):
    """Hand the task back to the queue: the environment failed, not the task.

    Accounted against max_retries like a rate limit, so an environment that is
    broken for good ends up failing the task instead of retrying forever.
    """
    if task.retry_count >= task.max_retries:
        changed = _mark_failed(
            task,
            f"Срыв по вине среды ({marker}), "
            f"попытки исчерпаны ({task.max_retries}).\n{detail}",
        )
        if changed:
            print(f"  -> Env failure ({marker}), retries exhausted")
        return changed
    next_run = compute_next_run(task.retry_count)
    changed = _mark_rate_limited(
        task, next_run,
        error=f"Срыв по вине среды ({marker}) — "
              f"задача возвращена в очередь.\n{detail}",
    )
    if changed:
        _notify_requeued(task, next_run, f"срыв среды ({marker})")
        print(f"  -> Env failure ({marker}). Retry #{task.retry_count + 1} "
              f"at {next_run.strftime('%H:%M:%S')}")
    return changed


def _notify_requeued(task, next_run, reason: str):
    """Queue a Telegram note when a task silently leaves the fast path —
    without it a rate-limited task just looks 'running' for hours."""
    if not task.tg_chat_id:
        return
    try:
        when = next_run.astimezone().strftime("%d.%m %H:%M") if next_run else "позже"
        db.add_notification(
            task.tg_chat_id,
            f"⏸ Задача #{task.id}: {reason} — продолжу в {when}.",
            task_id=task.id,
        )
    except Exception as e:
        print(f"  -> notify requeue failed: {e}")


def _finalize_herdr_outcome(task, outcome: dict, *,
                            require_closing_verdict: bool,
                            allow_targeted_stale: bool) -> bool:
    """Commit a verified-stopped Herdr result for this exact task attempt."""
    if outcome.get("cancelled"):
        changed = _mark_cancelled(
            task,
            outcome.get("cancel_note") or "Отменена пользователем во время выполнения",
        )
        if changed:
            print("  -> Cancelled by user")
        return changed

    if outcome["rate_limited"]:
        reason = outcome.get("retry_reason") or RETRY_RATE_LIMIT
        label = RETRY_REASON_ERR.get(reason, RETRY_REASON_ERR[RETRY_RATE_LIMIT])
        if task.retry_count >= task.max_retries:
            return _mark_failed(
                task,
                f"{label}, max retries ({task.max_retries}) exceeded.\n"
                f"{outcome['error']}",
            )
        next_run = compute_next_run(task.retry_count)
        changed = _mark_rate_limited(
            task, next_run, error=outcome["error"] or label)
        if changed:
            _notify_requeued(
                task,
                next_run,
                RETRY_REASON_RU.get(reason, RETRY_REASON_RU[RETRY_RATE_LIMIT]),
            )
            print(
                f"  -> {label}. Retry #{task.retry_count + 1} "
                f"at {next_run.strftime('%H:%M:%S')}")
        return changed

    if outcome.get("env_failure"):
        return _requeue_env_failure(
            task, outcome["env_failure"], outcome["error"])

    if not outcome["ok"]:
        changed = _retry_sqlite_busy(
            lambda: _mark_failed(task, outcome["error"], exit_code=1),
            f"завершение herdr-задачи #{task.id} с ошибкой",
        )
        if changed:
            print("  -> Failed (herdr)")
        return changed

    verdict = outcome.get("verdict") or parse_verdict(outcome["output"])
    if verdict == "УСТАРЕЛО" and not allow_targeted_stale:
        verdict = "НЕ СМОГ"
    if require_closing_verdict and not outcome.get("verdict"):
        changed = _retry_sqlite_busy(
            lambda: _mark_failed(
                task,
                "Pipeline provider returned success without a closing ИТОГ verdict",
                exit_code=1,
            ),
            f"отклонение неполного результата herdr-задачи #{task.id}",
        )
        if changed:
            print("  -> Failed (pipeline result has no closing verdict)")
        return changed
    changed = _retry_sqlite_busy(
        lambda: _mark_completed(
            task, outcome["output"], exit_code=0,
            verdict=verdict or None,
        ),
        f"завершение herdr-задачи #{task.id}",
    )
    if changed:
        text_preview = outcome["output"][:80].replace("\n", " ").strip()
        print(f"  -> Completed: {text_preview}")
    return changed


def _commit_terminal_or_defer(task, label: str, commit) -> bool:
    """Retry a known provider result instead of replacing it with failure."""
    try:
        changed = commit()
    except Exception as exc:
        raise ProviderOwnershipError(
            f"{label} is waiting for durable commit: "
            f"{type(exc).__name__}: {exc}",
            lambda: "",
            on_cleaned=commit,
        ) from exc
    if changed is False:
        fresh = db.get_task(task.id)
        if (fresh is not None and fresh.status.value == "running"
                and fresh.started_at == getattr(task, "started_at", None)):
            raise ProviderOwnershipError(
                f"{label} CAS was rejected for its live attempt",
                lambda: "",
                on_cleaned=commit,
            )
    return changed is not False


def _commit_herdr_outcome_or_defer(task, outcome: dict, *,
                                   require_closing_verdict: bool,
                                   allow_targeted_stale: bool) -> None:
    """Do not turn a durable Herdr result into a generic worker failure."""
    _commit_terminal_or_defer(
        task,
        "Herdr terminal outcome",
        lambda: _finalize_herdr_outcome(
            task,
            outcome,
            require_closing_verdict=require_closing_verdict,
            allow_targeted_stale=allow_targeted_stale,
        ),
    )


def _execute_herdr_task(task, provider_cfg, host=None, machine=None, prompt_override=None,
                        admission_complete=None, require_closing_verdict=False,
                        allow_targeted_stale=False, target_heartbeat=None):
    """Run the task in a live herdr session (providers with executor=herdr).

    host is the ssh target of the machine the session lives on (None = local).
    """
    from .herdr_exec import attach_hint, run_in_herdr

    attach = attach_hint(host)
    where = f" на машине {machine}" if machine else ""

    def on_blocked(pane_id):
        print(f"  -> Blocked, waiting for approval in pane {pane_id}{where}")
        if task.tg_chat_id:
            # pane_id/machine ride along so the bot can attach confirm/screen/
            # reply buttons — approving one's OWN task from the phone used to
            # be impossible (the message only suggested ssh).
            try:
                _retry_sqlite_busy(
                    lambda: db.add_notification(
                        task.tg_chat_id,
                        f"⏸ Задача #{task.id} ждёт подтверждения в herdr{where} "
                        f"(панель {pane_id}).\nПодтверди кнопкой ниже, или "
                        f"подключись: {attach}",
                        task_id=task.id,
                        pane_id=pane_id,
                        machine=machine,
                    ),
                    f"уведомление о блокировке задачи #{task.id}",
                )
            except Exception as exc:
                # A convenience notification is never allowed to detach the
                # worker's bookkeeping from an already-running owned agent.
                print(f"  !! blocked notification failed: {exc}", flush=True)

    def on_pane(pane_id):
        _retry_sqlite_busy(
            lambda: db.set_task_pane(task.id, pane_id),
            f"сохранение панели задачи #{task.id}",
        )

    def on_worktree(path, branch):
        # Recorded the moment the checkout exists, not when the task ends: the
        # user watching a long run wants the branch name now.
        _retry_sqlite_busy(
            lambda: db.set_worktree(task.id, path, branch),
            f"сохранение worktree задачи #{task.id}",
        )
        print(f"  -> Worktree {path} ({branch})")

    def on_started(_pane_id):
        # Do not admit another pipeline subprocess while this run is between
        # creating its owned tab and durably recording/starting the provider.
        _signal_admission_complete(admission_complete)

    def on_session(pane_id, tab_id, workspace_id):
        if target_heartbeat is None:
            return
        target_heartbeat.bind_herdr_session(
            pane_id, tab_id, workspace_id or "")

    def cancel_or_heartbeat():
        if callable(target_heartbeat):
            target_heartbeat()
        return db.is_cancel_requested(task.id)

    if callable(target_heartbeat):
        target_heartbeat(force=True)
        if not hasattr(target_heartbeat, "begin_herdr_session"):
            raise PipelineTargetLeaseLost(
                "replicated Herdr route has no durable session binder")
        target_heartbeat.begin_herdr_session()

    outcome = run_in_herdr(task, provider_cfg, on_blocked=on_blocked,
                           timeout=_effective_timeout(task),
                           cancel_check=cancel_or_heartbeat,
                           keep_pane=task.keep_pane, host=host,
                           on_worktree=on_worktree, on_pane=on_pane,
                           on_session=on_session,
                           on_started=on_started,
                           prompt_override=prompt_override,
                           require_closing_verdict=require_closing_verdict,
                           allow_targeted_stale=allow_targeted_stale)

    ownership_cleanup = outcome.pop("_ownership_cleanup", None)
    terminal_intent = outcome.pop("_terminal_intent", None)
    if outcome.get("ownership_uncertain"):
        if not callable(ownership_cleanup):
            ownership_cleanup = lambda: "missing herdr ownership cleanup"
        settled_outcome = dict(outcome)
        settled_outcome.pop("ownership_uncertain", None)
        if isinstance(terminal_intent, dict):
            settled_outcome.update(terminal_intent)
        raise ProviderOwnershipError(
            outcome.get("error") or "herdr provider ownership is uncertain",
            ownership_cleanup,
            heartbeat=target_heartbeat,
            on_cleaned=lambda: _finalize_herdr_outcome(
                task,
                settled_outcome,
                require_closing_verdict=require_closing_verdict,
                allow_targeted_stale=allow_targeted_stale,
            ),
        )

    _commit_herdr_outcome_or_defer(
        task,
        outcome,
        require_closing_verdict=require_closing_verdict,
        allow_targeted_stale=allow_targeted_stale,
    )


def _wrap_ssh(remote, cmd, env_extra):
    """Run the command on a remote machine: the executable is resolved by that
    machine's PATH, the provider env is passed along in its shell's dialect."""
    from .remote import ssh_command
    argv = [os.path.basename(cmd[0]), *cmd[1:]]
    return ssh_command(remote, argv, env_extra)


def _signal_admission_complete(admission_complete):
    """Open the next DB claim once this task has chosen its provider route."""
    if admission_complete is not None:
        admission_complete.set()


def _retry_sqlite_busy(operation, label: str):
    """Retry a short control-plane write after transient SQLite contention.

    sqlite's own busy timeout covers ordinary overlap.  A frozen helper used to
    run schema initialisation concurrently and could outlive that timeout.  The
    helper no longer opens the DB, but bounded retries keep pane/final-state
    bookkeeping durable under any remaining writer burst.  Non-lock failures
    are never retried or hidden.
    """
    delays = (0.1, 0.5)
    for attempt in range(len(delays) + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == len(delays):
                raise
            delay = delays[attempt]
            print(
                f"  !! SQLite занят: {label}; повтор через {delay:g} с",
                flush=True,
            )
            time.sleep(delay)


def _mark_completed(task, *args, **kwargs):
    kwargs["expected_started_at"] = getattr(task, "started_at", None)
    return db.mark_completed(task.id, *args, **kwargs)


def _mark_failed(task, *args, **kwargs):
    kwargs["expected_started_at"] = getattr(task, "started_at", None)
    return db.mark_failed(task.id, *args, **kwargs)


def _mark_rate_limited(task, *args, **kwargs):
    kwargs["expected_started_at"] = getattr(task, "started_at", None)
    return db.mark_rate_limited(task.id, *args, **kwargs)


def _defer_task(task, *args, **kwargs):
    kwargs["expected_started_at"] = getattr(task, "started_at", None)
    return db.defer_task(task.id, *args, **kwargs)


def _mark_cancelled(task, *args, **kwargs):
    kwargs["expected_started_at"] = getattr(task, "started_at", None)
    return db.mark_cancelled(task.id, *args, **kwargs)


def _execute_task_body(task, admission_complete=None):
    """Run CLI with the task's prompt."""
    if task.series_id:
        # Optional profile-owned gates are deterministic and token-free. They
        # stop empty stages before a provider is launched and defer dependent
        # stages (for example MERGE while REVIEW owns a single-flight barrier).
        try:
            from . import pipeline_insights
            gate = pipeline_insights.dispatch_gate(task)
        except Exception as exc:
            gate = None
            print(f"  !! pipeline dispatch gate unavailable for #{task.id}: {exc}", flush=True)
        if gate:
            reason = gate["reason"]
            if gate["action"] == "defer":
                next_run = _pipeline_defer_time(gate)
                if next_run:
                    _retry_sqlite_busy(
                        lambda: _defer_task(task, next_run, reason),
                        f"отложить pipeline-задачу #{task.id}",
                    )
                    print(f"  -> Deferred without agent: {reason}")
                    return
                print("  !! invalid pipeline defer time", flush=True)
            elif gate["action"] == "complete_empty":
                _retry_sqlite_busy(
                    lambda: _mark_completed(
                        task,
                        f"Предварительная проверка PromptPilot: {reason}\n"
                        "Провайдер не запускался, токены не потрачены.\n\n"
                        f"ИТОГ: ПУСТО ({reason})",
                        exit_code=0, verdict="ПУСТО",
                    ),
                    f"завершение пустой pipeline-задачи #{task.id}",
                )
                print(f"  -> Completed without agent: {reason}")
                return

    agent_prompt = effective_prompt(task)
    require_closing_verdict = False
    allow_targeted_stale = False
    pipeline_replicas = None
    pipeline_data_dir = None
    pipeline_lease_key_file = None
    pipeline_task_started_at = None
    pipeline_provider_ownership_kind = None
    pipeline_target_token = None
    target_heartbeat = None
    if task.series_id:
        try:
            from . import pipeline_insights
            route = pipeline_insights.execution_route(
                task, agent_prompt, getattr(task, "working_dir", None),
                retain_budget=True)
        except Exception as exc:
            route = {
                "action": "defer", "mode": "skill", "defer_for": "5m",
                "reason": f"pipeline execution admission unavailable: {exc}",
            }
            print(f"  !! pipeline execution route unavailable for #{task.id}: {exc}", flush=True)
        if route["action"] == "block":
            reason = route["reason"]
            _retry_sqlite_busy(
                lambda: _mark_completed(
                    task,
                    f"Предварительная проверка PromptPilot: {reason}\n"
                    "Провайдер не запускался, токены не потрачены.\n\n"
                    f"ИТОГ: НУЖЕН ЧЕЛОВЕК ({reason})",
                    exit_code=0, verdict="НУЖЕН ЧЕЛОВЕК",
                ),
                f"завершение заблокированной pipeline-задачи #{task.id}",
            )
            print(f"  -> Blocked without agent: {reason}")
            return
        if route["action"] == "defer":
            reason = route["reason"]
            next_run = _pipeline_defer_time(route)
            if next_run:
                _retry_sqlite_busy(
                    lambda: _defer_task(
                        task, next_run, reason,
                        hard_not_before=(
                            route.get("defer_policy") == "hard_not_before"),
                        budget_wait_scope=route.get("budget_wait_scope"),
                        budget_wait_revision=route.get("budget_wait_revision"),
                    ),
                    f"отложить pipeline-задачу #{task.id}",
                )
                print(f"  -> Pipeline preflight deferred without agent: {reason}")
                return
            _retry_sqlite_busy(
                lambda: _mark_completed(
                    task,
                    f"Pipeline preflight PromptPilot: {reason}\n"
                    "Некорректное время повтора pipeline preflight\n\n"
                    f"ИТОГ: НУЖЕН ЧЕЛОВЕК ({reason})",
                    exit_code=0, verdict="НУЖЕН ЧЕЛОВЕК",
                ),
                f"завершение ошибочной pipeline-задачи #{task.id}",
            )
            return
        if route["action"] == "complete_empty":
            reason = route["reason"]
            verdict = str(route.get("verdict") or "ПУСТО").strip() or "ПУСТО"
            # Defence in depth: execution_route normally normalizes this, but
            # persistence must not trust an injected/custom route.  УСТАРЕЛО is
            # reserved for the validated targeted fallback provider path.
            if verdict.upper() == "УСТАРЕЛО":
                verdict = "НЕ СМОГ"
            _retry_sqlite_busy(
                lambda: _mark_completed(
                    task,
                    f"Pipeline preflight PromptPilot: {reason}\n"
                    "Провайдер не запускался, токены не потрачены.\n\n"
                    f"ИТОГ: {verdict} ({reason})",
                    exit_code=0, verdict=verdict,
                ),
                f"завершение pipeline-задачи #{task.id} без провайдера",
            )
            print(f"  -> Pipeline preflight completed without agent: {reason}")
            return
        agent_prompt = route["prompt"]
        raw_replicas = route.get("pipeline_replicas")
        if type(raw_replicas) is int and 2 <= raw_replicas <= 16:
            pipeline_replicas = str(raw_replicas)
        raw_data_dir = route.get("pipeline_data_dir")
        if (pipeline_replicas is not None and isinstance(raw_data_dir, str)
                and os.path.isabs(raw_data_dir)):
            pipeline_data_dir = os.path.realpath(raw_data_dir)
        if pipeline_replicas is not None and pipeline_data_dir is None:
            raise RuntimeError(
                "replicated pipeline route has no absolute scheduler data directory")
        raw_lease_key = route.get("pipeline_lease_key_file")
        if (pipeline_replicas is not None and isinstance(raw_lease_key, str)
                and os.path.isabs(raw_lease_key)):
            pipeline_lease_key_file = os.path.realpath(raw_lease_key)
        if pipeline_replicas is not None and pipeline_lease_key_file is None:
            raise RuntimeError(
                "replicated pipeline route has no absolute scheduler lease key")
        if pipeline_replicas is not None:
            raw_started_at = route.get("pipeline_task_started_at")
            task_started_at = getattr(task, "started_at", None)
            expected_started_at = (
                task_started_at.astimezone(timezone.utc).isoformat()
                if isinstance(task_started_at, datetime) else None)
            if (not isinstance(raw_started_at, str)
                    or raw_started_at != expected_started_at):
                raise RuntimeError(
                    "replicated pipeline route belongs to another task attempt")
            pipeline_task_started_at = raw_started_at
            raw_ownership_kind = route.get("pipeline_provider_ownership_kind")
            if raw_ownership_kind not in {"headless", "herdr"}:
                raise RuntimeError(
                    "replicated pipeline route has no provider ownership kind")
            pipeline_provider_ownership_kind = raw_ownership_kind
            target_heartbeat = _pipeline_target_heartbeater(
                route.get("pipeline_target_reservation"), task.id,
                task_started_at=pipeline_task_started_at,
                ownership_kind=pipeline_provider_ownership_kind)
            target_heartbeat.start()
            raw_target_token = target_heartbeat.reservation.get("token")
            if (not isinstance(raw_target_token, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", raw_target_token)):
                raise RuntimeError(
                    "replicated pipeline route has no exact target token")
            pipeline_target_token = raw_target_token
        require_closing_verdict = bool(
            route.get("profile_id") and route.get("queue_id")
        )
        gate_command = route.get("gate_command")
        allow_targeted_stale = bool(
            require_closing_verdict
            and route.get("next_already_run") is True
            and isinstance(gate_command, list)
            and gate_command
            and all(isinstance(value, str) and value for value in gate_command)
        )
        if require_closing_verdict:
            from .herdr_exec import ensure_closing_verdict_contract
            agent_prompt = ensure_closing_verdict_contract(
                agent_prompt, allow_targeted_stale=allow_targeted_stale)
        if route.get("fallback_reason"):
            if route.get("next_already_run"):
                print("  -> Pipeline tool selected target; continuing full skill: "
                      f"{route['fallback_reason']}")
            else:
                print(f"  -> Pipeline tool unavailable, using skill: {route['fallback_reason']}")
        elif route.get("mode") == "tool":
            print(f"  -> Pipeline tool route: {route['queue_id']}")

    provider = task.provider or DEFAULT_CLI

    provider_cfg = load_providers().get(provider, {})
    if pipeline_replicas is not None:
        actual_ownership_kind = (
            "herdr" if provider_cfg.get("executor") == "herdr" else "headless")
        if actual_ownership_kind != pipeline_provider_ownership_kind:
            raise RuntimeError(
                "replicated pipeline provider ownership changed after election")
        # The provider may invoke pipelinectl again for the signed completion
        # gate. Keep replica mode in its environment too: otherwise an
        # accidental repeated `next` would silently fall back to the legacy,
        # unreserved election path.
        provider_cfg = dict(provider_cfg)
        provider_cfg["strict_owned_session"] = True
        provider_cfg["env"] = {
            **(provider_cfg.get("env") or {}),
            "PP_PIPELINE_REPLICAS": pipeline_replicas,
            "PP_TASK_STARTED_AT": pipeline_task_started_at,
            "PP_PROVIDER_OWNERSHIP_KIND": pipeline_provider_ownership_kind,
            "PP_PIPELINE_TARGET_TOKEN": pipeline_target_token,
            "PP_DATA_DIR": pipeline_data_dir,
            "PP_PIPELINE_LEASE_KEY_FILE": pipeline_lease_key_file,
        }
    machine = getattr(task, "machine", None)

    host = None
    if machine:
        from .config import load_machines, machine_remote
        m = load_machines().get(machine)
        if not m or not m.get("host"):
            _mark_failed(task, f"Машина «{machine}» не найдена в реестре")
            return
        host = machine_remote(m)

    if provider_cfg.get("executor") == "herdr":
        # herdr sessions work the same way on any machine: the CLI calls go
        # over ssh, the pane lives there (attach with `herdr --remote <host>`).
        try:
            _execute_herdr_task(
                task, provider_cfg, host=host, machine=machine,
                prompt_override=agent_prompt,
                admission_complete=admission_complete,
                require_closing_verdict=require_closing_verdict,
                allow_targeted_stale=allow_targeted_stale,
                target_heartbeat=target_heartbeat,
            )
        finally:
            # Startup failures have no on_started callback.  Once their durable
            # outcome is recorded they must not strand the admission fence.
            _signal_admission_complete(admission_complete)
        return

    # The GitHub scan lease is gone and a provider-spanning budget reservation,
    # when needed, is already durable.  Non-herdr providers have no intermediate
    # pane bookkeeping, so a lower-priority task may begin admission now.
    _signal_admission_complete(admission_complete)

    # The task's own checkout, when it asked for one. Done before the CLI starts
    # so the agent only ever sees the isolated tree.
    run_dir, wt_note = task.working_dir, ""
    if getattr(task, "worktree", False):
        if machine:
            # A headless remote command runs in the ssh login directory, so a
            # worktree over there would simply never be entered. herdr-based
            # providers place the agent in the checkout and do support this.
            _mark_failed(task, f"worktree на машине «{machine}» поддерживается только "
                               f"через herdr-провайдер (executor: herdr)")
            return
        try:
            wt = worktree.prepare(task.working_dir, task.id)
        except worktree.WorktreeError as e:
            _mark_failed(task, f"worktree: {e}")
            print(f"  -> Failed (worktree): {e}")
            return
        run_dir = wt["path"]
        db.set_worktree(task.id, wt["path"], wt["branch"])
        wt_note = "\n\n" + worktree.summary(wt["path"], wt["branch"], wt["copied"])
        print(f"  -> Worktree {wt['path']} ({wt['branch']})")

    cmd = build_cmd(provider, agent_prompt, skip_permissions=task.skip_permissions,
                    session_id=task.session_id, model=task.model, guard=not machine,
                    effort=task.effort)
    prompt_stdin = agent_prompt if provider_cfg.get("prompt_stdin") else None

    env = get_provider_env(provider)
    # Marks the run in its own environment, inherited by the agent process. That
    # is what lets a live run be found by process rather than by our bookkeeping.
    env.pop("PP_PIPELINE_REPLICAS", None)
    env.pop("PP_TASK_STARTED_AT", None)
    env.pop("PP_PROVIDER_OWNERSHIP_KIND", None)
    env.pop("PP_PIPELINE_TARGET_TOKEN", None)
    env["PP_TASK_ID"] = str(task.id)
    if pipeline_replicas is not None:
        env["PP_PIPELINE_REPLICAS"] = pipeline_replicas
        env["PP_TASK_STARTED_AT"] = pipeline_task_started_at
        env["PP_PROVIDER_OWNERSHIP_KIND"] = pipeline_provider_ownership_kind
        env["PP_PIPELINE_TARGET_TOKEN"] = pipeline_target_token
        env["PP_DATA_DIR"] = pipeline_data_dir
        env["PP_PIPELINE_LEASE_KEY_FILE"] = pipeline_lease_key_file

    if machine:
        if task.detached:
            _mark_failed(task, "Фоновый запуск (detached) на удалённой машине пока не поддерживается")
            return
        cmd = _wrap_ssh(host, cmd, provider_cfg.get("env"))
        env = os.environ.copy()
        # The marker rides the local ssh process too, so a live remote run is
        # found by live_task_ids() and recover_running() won't relaunch it into
        # a second concurrent run in the same remote directory.
        env["PP_TASK_ID"] = str(task.id)
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
        kwargs = {"cwd": run_dir, "env": env, "stdin": subprocess.DEVNULL}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **kwargs)
            _mark_completed(task, f"Запущен в фоне (PID {proc.pid}){wt_note}", exit_code=0)
            print(f"  -> Detached (PID {proc.pid})")
        except FileNotFoundError:
            _mark_failed(task, f"Command not found: {cmd[0]}", exit_code=-1)
        return

    effective_timeout = _effective_timeout(task)

    if callable(target_heartbeat):
        target_heartbeat(force=True)
    try:
        tree = OwnedProcess.start(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=run_dir,
            stdin=subprocess.PIPE if prompt_stdin is not None else subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        _mark_failed(task, f"CLI '{provider}' not found. Is it installed and in PATH?", exit_code=-1)
        return
    except ProcessTreeError as exc:
        # Fail closed: without a lifetime boundary a timed-out agent can keep
        # changing the checkout after the queue has already moved on.
        _mark_failed(task, f"Could not isolate CLI process tree: {exc}", exit_code=-1)
        return
    _register_provider_tree(task.id, tree)
    proc = tree.process

    if prompt_stdin is not None:
        # Write once and detach the handle before polling communicate(). This
        # avoids resending after TimeoutExpired and preserves newlines through
        # Windows .CMD provider shims.
        try:
            proc.stdin.write(prompt_stdin)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            proc.stdin = None

    # Drain both pipes while polling. Besides avoiding pipe deadlocks, the
    # stdout reader persists Codex/Claude session ids immediately, so a worker
    # crash can resume the same conversation instead of mistaking its own dirty
    # checkout for foreign work on a fresh run.
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_thread = threading.Thread(
        target=_read_process_pipe, args=(proc.stdout, stdout_parts, task.id), daemon=True)
    stderr_thread = threading.Thread(
        target=_read_process_pipe, args=(proc.stderr, stderr_parts), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    # Poll instead of blocking: allows user-requested cancellation of a
    # RUNNING task (Web UI/bot) and the per-task timeout.
    started = time.monotonic()
    while True:
        try:
            proc.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            if callable(target_heartbeat):
                target_heartbeat()
            if db.is_cancel_requested(task.id):
                commit_cancel = lambda: _mark_cancelled(
                    task, "Отменена пользователем во время выполнения")
                try:
                    _stop_owned_process(tree)
                except Exception as exc:
                    raise ProviderOwnershipError(
                        "headless cancellation is waiting for process cleanup: "
                        f"{type(exc).__name__}: {exc}",
                        lambda: "",
                        on_cleaned=commit_cancel,
                    ) from exc
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                stdout_thread.join(timeout=10)
                stderr_thread.join(timeout=10)
                changed = _commit_terminal_or_defer(
                    task,
                    "headless cancellation",
                    commit_cancel,
                )
                if changed:
                    print("  -> Cancelled by user")
                return
            if effective_timeout and time.monotonic() - started > effective_timeout:
                commit_timeout = lambda: _mark_failed(
                    task, f"Execution timed out after {effective_timeout}s",
                    exit_code=-1)
                try:
                    _stop_owned_process(tree)
                except Exception as exc:
                    raise ProviderOwnershipError(
                        "headless timeout is waiting for process cleanup: "
                        f"{type(exc).__name__}: {exc}",
                        lambda: "",
                        on_cleaned=commit_timeout,
                    ) from exc
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                stdout_thread.join(timeout=10)
                stderr_thread.join(timeout=10)
                _commit_terminal_or_defer(
                    task,
                    "headless timeout",
                    commit_timeout,
                )
                return

    # A successful wrapper may exit while a child still owns the pipes. Close
    # the task boundary before joining readers so such a child cannot survive
    # (or hold this worker forever).
    tree.close()
    _forget_provider_tree(task.id, tree)
    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)

    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    # A rate/usage limit can land on stderr, or — for stream-json CLIs like
    # Claude Code — inside the stdout result event with a non-zero exit. Check
    # both, matching the readable text extracted from stream-json rather than
    # raw JSON. Missing the stdout case sent the task to failed forever, which
    # defeats the whole point of the queue.
    stdout_text = parse_stream_json(result.stdout).get("text", "") if is_stream_json(result.stdout) else ""
    reason = (retry_reason(result.stderr, result.returncode)
              or retry_reason(stdout_text, result.returncode))
    if reason:
        # Extract readable error from stream-json if possible
        rl_error = result.stderr or result.stdout
        if is_stream_json(result.stdout):
            parsed = parse_stream_json(result.stdout)
            rl_error = format_result(parsed) or rl_error
        elif is_stream_json(result.stderr):
            parsed = parse_stream_json(result.stderr)
            rl_error = format_result(parsed) or rl_error
        label = RETRY_REASON_ERR[reason]
        if task.retry_count >= task.max_retries:
            _commit_terminal_or_defer(
                task,
                "headless exhausted retry outcome",
                lambda: _mark_failed(
                    task,
                    f"{label}, max retries ({task.max_retries}) exceeded.\n"
                    f"{rl_error}"),
            )
            return
        next_run = compute_next_run(task.retry_count)
        def commit_rate_limit():
            changed = _mark_rate_limited(
                task, next_run, error=rl_error or label)
            if changed:
                _notify_requeued(task, next_run, RETRY_REASON_RU[reason])
                print(
                    f"  -> {label}. Retry #{task.retry_count + 1} "
                    f"at {next_run.strftime('%H:%M:%S')}")
            return changed
        _commit_terminal_or_defer(
            task, "headless rate-limit outcome", commit_rate_limit)
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
        marker = env_failure(result.stderr) or env_failure(result.stdout)
        if marker:
            _commit_terminal_or_defer(
                task,
                "headless environment-failure outcome",
                lambda: _requeue_env_failure(task, marker, error_text),
            )
            return
        changed = _commit_terminal_or_defer(
            task,
            "headless failure outcome",
            lambda: _mark_failed(
                task, error_text, exit_code=result.returncode),
        )
        if changed:
            print(f"  -> Failed (exit {result.returncode})")
        return

    # Parse output
    model_used = None
    session_id = None
    if is_stream_json(result.stdout):
        parsed = parse_stream_json(result.stdout)
        output = format_result(parsed)
        verdict_source = parsed["text"]
        model_used = parsed["meta"].get("model")
        session_id = parsed["meta"].get("session_id")
        # Check for rate limit in stream events — only if no text was returned
        rl = parsed.get("rate_limit_info")
        if rl and not parsed["text"]:
            if task.retry_count >= task.max_retries:
                _commit_terminal_or_defer(
                    task,
                    "headless exhausted stream retry outcome",
                    lambda: _mark_failed(
                        task, f"{RETRY_REASON_ERR[RETRY_RATE_LIMIT]}.\n{output}"),
                )
                return
            next_run = compute_next_run(task.retry_count)
            def commit_stream_rate_limit():
                changed = _mark_rate_limited(
                    task, next_run,
                    error=output or RETRY_REASON_ERR[RETRY_RATE_LIMIT])
                if changed:
                    _notify_requeued(
                        task, next_run, RETRY_REASON_RU[RETRY_RATE_LIMIT])
                    print(
                        "  -> Rate limited (stream event). Retry at "
                        f"{next_run.strftime('%H:%M:%S')}")
                return changed
            _commit_terminal_or_defer(
                task, "headless stream rate-limit outcome",
                commit_stream_rate_limit)
            return
    else:
        # Plain text output (non-Claude CLIs)
        output = result.stdout
        verdict_source = output

    if require_closing_verdict:
        from .herdr_exec import _closing_workflow_verdict
        verdict = _closing_workflow_verdict(
            verdict_source, allow_targeted_stale=allow_targeted_stale)
        if not verdict:
            changed = _commit_terminal_or_defer(
                task,
                "headless missing-verdict outcome",
                lambda: _mark_failed(
                    task,
                    "Pipeline provider returned success without a closing ИТОГ verdict\n"
                    + output[-4000:],
                    exit_code=1,
                ),
            )
            if changed:
                print("  -> Failed (pipeline result has no closing verdict)")
            return
    else:
        verdict = parse_verdict(output)
    changes = None
    if wt_note:
        output += wt_note
        changes = worktree.status(wt["root"], wt["path"])
        if changes and changes != worktree.NO_CHANGES:
            output += f"\nИзменения: {changes}"

    changed = _commit_terminal_or_defer(
        task,
        "headless successful outcome",
        lambda: _mark_completed(
            task, output, exit_code=0, model_used=model_used,
            session_id=session_id, verdict=verdict or None,
        ),
    )
    if changed:
        text_preview = output[:80].replace("\n", " ").strip()
        print(f"  -> Completed: {text_preview}")

    # An empty checkout (no commits, no dirty files) is just clutter — remove it
    # so .pp-worktrees doesn't grow without bound. The branch stays regardless;
    # this mirrors what the herdr executor already does for its own checkouts.
    if changes == worktree.NO_CHANGES:
        if worktree.remove(wt["root"], wt["path"]):
            print(f"  -> Removed empty worktree {wt['path']}")


def _execute_task_inner(task, admission_complete=None):
    """Run one task and always release its exact GitHub budget reservation."""
    try:
        if admission_complete is None:
            return _execute_task_body(task)
        return _execute_task_body(task, admission_complete)
    finally:
        try:
            from . import pipeline_insights
            pipeline_insights.release_execution_admission()
        except Exception as exc:
            print(
                f"  !! GitHub budget reservation cleanup #{task.id}: {exc}",
                flush=True)


def execute_task(task, admission_complete=None):
    """Execute a queue task and reconcile an optional W1 workflow link.

    Reconciliation is deliberately repeatable. If the process dies between the
    queue update and this callback, ``sync_all_tasks`` at worker startup repairs
    the projection from the durable task row.
    """
    from . import workflows

    try:
        workflows.sync_task(task.id)
    except Exception as exc:
        print(f"  !! workflow start sync #{task.id}: {exc}", flush=True)
    try:
        if admission_complete is None:
            return _execute_task_inner(task)
        return _execute_task_inner(task, admission_complete)
    finally:
        # Project-pipeline election reserves an exact target before any
        # provider can start. Every attempt exit (success, failure, defer,
        # cancellation, startup error, or unexpected exception) releases that
        # task's reservation; a hard process crash is recovered by the lease
        # TTL in the database.
        provider_stopped = _close_registered_provider_tree(task.id)
        if not provider_stopped:
            _renew_quarantined_target(task.id)
        try:
            fresh_for_cleanup = db.get_task(task.id)
            if (provider_stopped and fresh_for_cleanup is not None
                    and fresh_for_cleanup.status.value != "running"):
                db.release_pipeline_target_reservations(task.id)
        except Exception as exc:
            print(
                f"  !! pipeline target reservation cleanup #{task.id}: {exc}",
                flush=True,
            )
        try:
            _recur_after_run(task)
        except Exception as exc:
            print(f"  !! не смог продлить расписание #{task.id}: {exc}", flush=True)
        try:
            fresh = db.get_task(task.id)
            if fresh and fresh.status.value == "completed":
                _notify_pipeline_completion(fresh, fresh.verdict)
        except Exception as exc:
            print(f"  !! pipeline wake-up after recurrence #{task.id}: {exc}", flush=True)
        try:
            workflows.sync_task(task.id)
            workflows.advance_linked_task(task.id)
        except Exception as exc:
            print(f"  !! workflow final sync #{task.id}: {exc}", flush=True)
        # A dispatch/preflight path that did not launch a provider opens the
        # fence only after its recurrence and workflow projections are durable.
        # The pool wrapper remains the final backstop for failures above.
        _signal_admission_complete(admission_complete)


def _execute_task_with_admission_fence(task, admission_complete):
    """Pool boundary that cannot strand a claim fence on an early crash."""
    try:
        return execute_task(task, admission_complete)
    finally:
        _signal_admission_complete(admission_complete)


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


def free_mb():
    """Memory actually available for one more agent, or None if unmeasurable.

    Unmeasurable must not mean "blocked": on a platform where this cannot be
    read the queue has to keep moving.
    """
    try:  # Linux: MemAvailable is the honest number, MemFree is not
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    try:  # Windows, without dragging in psutil
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _Status()
        st.dwLength = ctypes.sizeof(_Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullAvailPhys) // (1024 * 1024)
    except Exception:
        pass
    return None


def enough_memory() -> bool:
    """False only when we measured the memory and it is genuinely short."""
    if MIN_FREE_MB <= 0:
        return True
    free = free_mb()
    return free is None or free >= MIN_FREE_MB


def _fail_stuck(task, exc) -> bool:
    """An unexpected crash in execute_task left a task stranded in 'running'.

    execute_task reports task-level failures itself, so reaching here is a bug
    (a KeyError in parsing, a 'database is locked'); mark it failed so the queue
    keeps moving instead of a task stuck forever and — in the pool — its lock
    key freed while a second task walks into the same directory. Only touch its
    exact attempt: the crash may have happened after mark_completed, or a
    delayed recovery may race a newer reclaimed attempt.

    Return False only when the durable transition could not be attempted, so
    the worker loop can retry it with backoff. A fenced no-op is terminal: the
    task already moved on and must not be changed by this old recovery.
    """
    if not _close_registered_provider_tree(task.id):
        _renew_quarantined_target(task.id)
        return False
    if isinstance(exc, ProviderOwnershipError) and not exc.retry_cleanup():
        _renew_quarantined_target(task.id)
        return False
    if isinstance(exc, RecoveredProviderOwnershipError):
        try:
            changed = db.recover_running_attempt(task.id, task.started_at)
        except Exception as error:
            print(
                f"  !! could not requeue recovered provider #{task.id}: {error}",
                flush=True,
            )
            return False
        if changed:
            _reconcile_recovered_attempt(task)
            return True
        fresh = db.get_task(task.id)
        return fresh is None or fresh.status.value != "running"
    if isinstance(exc, ProviderOwnershipError) and callable(exc.on_cleaned):
        if not exc.finish_after_cleanup():
            fresh = db.get_task(task.id)
            if (fresh is None or fresh.status.value != "running"
                    or fresh.started_at != task.started_at):
                # The exact attempt moved on concurrently. Its stale
                # continuation is a fenced no-op, not a reason to retain a
                # quarantine around a newer attempt.
                return True
            _renew_quarantined_target(task.id)
            return False
        try:
            # execute_task's finally ran while the exact attempt was still
            # fenced as running. Complete the projections only after cleanup
            # has made the delayed terminal transition safe.
            _recur_after_run(task)
            fresh = db.get_task(task.id)
            if fresh and fresh.status.value == "completed":
                _notify_pipeline_completion(fresh, fresh.verdict)
            from . import workflows
            workflows.sync_task(task.id)
            workflows.advance_linked_task(task.id)
        except Exception as error:
            # The terminal row is already durable. Startup reconciliation and
            # the periodic pipeline sampler repair these idempotent projections.
            print(
                f"  !! could not finish deferred terminal reconciliation "
                f"#{task.id}: {error}", flush=True,
            )
        return True
    try:
        changed = db.fail_running_attempt(
            task.id,
            task.started_at,
            f"Внутренняя ошибка воркера: {type(exc).__name__}: {exc}",
        )
    except Exception as e:  # never let recovery itself take down the loop
        print(f"  !! не удалось пометить #{task.id} failed: {e}", flush=True)
        return False

    if not changed:
        return True

    try:
        # execute_task's finally block could not extend the series while this
        # row was still running. Once recovery marks it failed, do the same
        # recurrence handoff as the normal failure path so one unexpected
        # exception cannot leave a durable schedule broken.
        _recur_after_run(task)
        from . import workflows
        workflows.sync_task(task.id)
        workflows.advance_linked_task(task.id)
    except Exception as e:
        # The failure row is already durable. Startup reconciliation repairs
        # projections/series if this best-effort follow-up is interrupted.
        print(f"  !! не удалось завершить восстановление #{task.id}: {e}", flush=True)
    return True


def _stuck_recovery_delay(failures: int) -> float:
    """Bounded backoff for durable recovery, independent of provider retries."""
    base = max(1, POLL_INTERVAL)
    return min(base * (2 ** max(0, failures - 1)), MAX_DELAY)


def _queue_stuck_recovery(recoveries: dict, task, lock: str, exc) -> None:
    """Remember one failed execution attempt until its state is durable."""
    key = (task.id, task.started_at)
    try:
        target_fenced = db.task_has_live_pipeline_target_reservation(task.id)
    except Exception as fence_exc:
        print(
            f"  !! could not inspect pipeline target fence #{task.id}: "
            f"{fence_exc}", flush=True,
        )
        target_fenced = isinstance(exc, ProviderOwnershipError)
    recoveries.setdefault(key, {
        "task": task,
        "lock": lock,
        "exc": exc,
        "failures": 0,
        "retry_at": 0.0,
        "target_fenced": target_fenced,
        "heartbeat_at": 0.0,
    })


def _drain_stuck_recoveries(recoveries: dict, now: float | None = None) -> None:
    """Run due recoveries and retain failed writes for a later loop pass."""
    now = time.monotonic() if now is None else now
    for key, item in list(recoveries.items()):
        if item.get("target_fenced") and item.get("heartbeat_at", 0.0) <= now:
            renewed = _renew_quarantined_target(item["task"].id)
            item["heartbeat_at"] = now + PIPELINE_TARGET_HEARTBEAT_INTERVAL
            if renewed != 1:
                # The fence is already lost or SQLite could not confirm its
                # renewal. Do not wait through exponential backoff while a
                # provider may still be mutating the old target.
                print(
                    f"  !! pipeline target fence lost during cleanup "
                    f"#{item['task'].id}; retrying ownership cleanup now",
                    flush=True,
                )
                item["retry_at"] = 0.0
        if item["retry_at"] > now:
            continue
        if _fail_stuck(item["task"], item["exc"]):
            recoveries.pop(key, None)
            continue
        item["failures"] += 1
        item["retry_at"] = now + _stuck_recovery_delay(item["failures"])


def _reap_futures(in_flight: dict, recoveries: dict,
                  now: float | None = None) -> None:
    """Collect completed pool work and durably recover unhandled crashes."""
    for fut in [candidate for candidate in in_flight if candidate.done()]:
        lock, task = in_flight.pop(fut)
        exc = fut.exception()
        if exc:  # execute_task already reports task failures; this is a bug
            print(
                f"  !! исполнение задачи #{task.id} упало: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            _queue_stuck_recovery(recoveries, task, lock, exc)
    _drain_stuck_recoveries(recoveries, now=now)


def _active_worker_task_ids(in_flight: dict, recoveries: dict) -> set[int]:
    """Tasks that still own either execution or cleanup capacity."""
    return ({task.id for _lock, task in in_flight.values()}
            | {item["task"].id for item in recoveries.values()})


def _occupied_worker_slots(in_flight: dict, recoveries: dict) -> int:
    """A quarantined provider keeps its slot until ownership is resolved."""
    return len(in_flight) + len(recoveries)


def lock_key(task) -> str:
    """What a task must not share with another task running at the same time.

    Two agents in one work tree overwrite each other's edits, so a task locks
    the directory it will edit. A task with its own worktree locks nothing —
    that is the whole point of it. A task aimed at an existing herdr session
    locks the session instead: there the directory belongs to the user.

    Empty string means "no conflict possible".
    """
    where = getattr(task, "machine", None) or "local"
    if getattr(task, "herdr_target", None):
        return f"{where}:session:{task.herdr_target}"
    if getattr(task, "worktree", False):
        return ""
    path = task.working_dir or os.getcwd()
    if not getattr(task, "machine", None):
        path = os.path.realpath(path)
        try:
            stat_result = os.stat(path)
            return (
                f"{where}:dir-id:{int(stat_result.st_dev)}:"
                f"{int(stat_result.st_ino)}")
        except OSError:
            path = os.path.abspath(path)
    path = os.path.normcase(path.rstrip("/\\"))
    return f"{where}:dir:{path}"


class _AdmissionFence:
    """Serialize DB claims until the preceding task finishes admission.

    Provider execution remains concurrent: the event opens as soon as routing
    and its durable GitHub budget reservation are complete.
    """

    def __init__(self):
        self._pending = None

    def wait(self, timeout: float) -> bool:
        return self._pending is None or self._pending.wait(timeout)

    def begin(self):
        if not self.wait(0):
            raise RuntimeError("previous provider admission is still pending")
        self._pending = threading.Event()
        return self._pending


def _claim_next_task(busy_keys=(), busy_lane_ids=()):
    """Atomically claim by configured lane preference, with safe fallback.

    Invalid optional lane configuration must be visible but must not freeze the
    legacy queue.  The project preflight still owns every external mutation;
    this function only chooses which already-runnable local task gets a slot.
    """
    from . import pipeline_insights

    try:
        policy = pipeline_insights.worker_lane_policy()
    except (AttributeError, OSError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"  !! pipeline lane scheduler unavailable: {exc}", flush=True)
        policy = None
    try:
        budget_fairness = pipeline_insights.worker_budget_fairness_policy()
    except (AttributeError, OSError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"  !! pipeline budget fairness unavailable: {exc}", flush=True)
        budget_fairness = None
    fairness_kwargs = ({
        "budget_wait_scope": budget_fairness["scope"],
        "budget_starvation_timeout_seconds": (
            budget_fairness["starvation_timeout_seconds"]),
    } if budget_fairness is not None else {})
    if policy is None:
        return db.get_next_runnable(
            busy_keys=busy_keys, key_fn=lock_key,
            **fairness_kwargs), None

    assignments = {}

    def rank(task):
        candidate = pipeline_insights.worker_lane_rank(
            task, policy, busy_lane_ids)
        if candidate is None:
            return None
        score, lane_id = candidate
        assignments[task.id] = lane_id
        return score

    task = db.get_next_runnable(
        busy_keys=busy_keys, key_fn=lock_key, order_key_fn=rank,
        **fairness_kwargs)
    return task, assignments.get(task.id) if task is not None else None


def _warm_pipeline_runtime():
    """Load pipeline routing code before a runnable task is claimed.

    Frozen executables import this relatively large module lazily.  On a busy
    disk Windows can spend minutes paging it in.  Doing that after the atomic
    claim makes a healthy pending task look like a hung running task and holds
    the admission fence even though no provider or mutation has started.
    """
    from . import pipeline_insights
    return pipeline_insights


def run_worker():
    """Main worker loop.

    With PP_CONCURRENCY=1 (the default) this is the plain sequential worker it
    has always been. Above that, tasks run in a thread pool and the queue is
    walked past anything that would collide with a task already in flight —
    see lock_key(). One worker process is still the assumption: a second one
    would reset this one's running tasks on startup (recover_running).
    """
    running = True

    def stop(signum, frame):
        nonlocal running
        print("\nShutting down worker...")
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    heartbeat_stop = threading.Event()

    def publish_heartbeat():
        while not heartbeat_stop.is_set():
            try:
                db.touch_worker_heartbeat(os.getpid())
            except Exception as exc:
                print(f"Не удалось записать heartbeat worker: {exc}", file=sys.stderr, flush=True)
            heartbeat_stop.wait(max(1, min(POLL_INTERVAL, 10)))

    heartbeat_thread = threading.Thread(
        target=publish_heartbeat, name="pp-worker-heartbeat", daemon=True)
    heartbeat_thread.start()

    # Recover tasks stuck in 'running' from a previous crash — but leave alone
    # any whose agent is still working: the worker dying doesn't kill the agent.
    alive = live_task_ids()
    alive, recovered_quarantines = _reconcile_reserved_running_tasks(alive)
    if alive:
        print(f"Живые прогоны найдены по метке в окружении, не трогаю: {sorted(alive)}")
    db.recover_running(keep_ids=alive)

    # Import/page-in routing after stale attempts have been recovered but
    # before terminal-gap repair. Repair is intentionally generic; only
    # profile-matched project series opt into repeat-blocker pausing.
    pipeline_runtime = _warm_pipeline_runtime()
    try:
        repeat_guard_series_ids = pipeline_runtime.repeat_guard_series_ids(
            db.list_series())
    except Exception as exc:
        print(
            f"  !! repeat-blocker startup classification unavailable: {exc}",
            flush=True,
        )
        repeat_guard_series_ids = ()
    repaired_series = db.repair_active_series_occurrences(
        repeat_guard_series_ids=repeat_guard_series_ids)
    if repaired_series:
        print(
            "Восстановлены потерянные в аварийном окне серии: "
            + ", ".join(str(value) for value in repaired_series),
            flush=True,
        )
    # A crash may happen after a queue task commits its final status but before
    # the W1 workflow projection observes it. Reconciliation is idempotent and
    # also maps reset running tasks back to pending runs.
    try:
        from . import workflows
        workflows.sync_all_tasks()
    except Exception as exc:
        print(f"Не удалось синхронизировать workflow после восстановления: {exc}")

    code_snapshot = _code_snapshot()

    print(f"PromptPilot worker started (poll every {POLL_INTERVAL}s)")
    print(f"Timeout: {'no limit' if TASK_TIMEOUT == 0 else f'{TASK_TIMEOUT}s'} | Backoff: {BASE_DELAY}-{MAX_DELAY}s"
          + (f" | Параллельно: {CONCURRENCY}" if CONCURRENCY > 1 else ""))
    print("Waiting for tasks...\n")

    pool = None
    in_flight = {}  # Future -> (lock key, exact claimed task attempt)
    task_lanes = {}  # task id -> configured scheduler lane
    stuck_recoveries = {}  # exact attempt -> retry state; keeps its lock key
    for orphan_task, orphan_error in recovered_quarantines:
        _queue_stuck_recovery(
            stuck_recoveries, orphan_task, lock_key(orphan_task), orphan_error)
    admission_fence = _AdmissionFence()
    short_on_memory = False
    if CONCURRENCY > 1:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="pp-task")

    def reap():
        _reap_futures(in_flight, stuck_recoveries)
        active = _active_worker_task_ids(in_flight, stuck_recoveries)
        for task_id in list(task_lanes):
            if task_id not in active:
                task_lanes.pop(task_id, None)

    while running:
        reap()

        # Auto-reload: pick up code updates between tasks (dev-friendly —
        # a stale worker silently ignoring new features is worse than a restart)
        if code_snapshot is not None and _code_snapshot() != code_snapshot:
            if in_flight or stuck_recoveries:
                # Restarting now would orphan live agents — let them finish.
                time.sleep(POLL_INTERVAL)
                continue
            print("Код обновился — перезапускаю worker...", flush=True)
            os.execv(sys.executable, [sys.executable, "-m", "promptpilot", "worker"])

        if (db.is_paused()
                or _occupied_worker_slots(in_flight, stuck_recoveries) >= CONCURRENCY):
            time.sleep(POLL_INTERVAL)
            continue

        if not enough_memory():
            # Say it once per shortage, not every poll — the log is for reading.
            if not short_on_memory:
                short_on_memory = True
                print(f"Мало памяти ({free_mb()} МБ < {MIN_FREE_MB}) — новые задачи не берём",
                      flush=True)
            time.sleep(POLL_INTERVAL)
            continue
        short_on_memory = False

        # Claim order is the DB priority order.  Do not claim a second task
        # until the first has finished scan/preflight and durably reserved the
        # selected provider route; pool thread scheduling is deliberately not
        # used as an ordering mechanism.
        if pool is not None and not admission_fence.wait(POLL_INTERVAL):
            continue

        busy_keys = [lk for lk, _task in in_flight.values() if lk]
        busy_keys.extend(
            item["lock"] for item in stuck_recoveries.values() if item["lock"])
        task, lane_id = _claim_next_task(
            busy_keys=busy_keys,
            busy_lane_ids=task_lanes.values(),
        )
        if task is None:
            time.sleep(POLL_INTERVAL)
            continue

        provider = task.provider or DEFAULT_CLI
        prompt_preview = task.prompt[:60].replace("\n", " ")
        print(f"[#{task.id}] [{provider}] Running: {prompt_preview}...")
        if pool is None:
            try:
                execute_task(task)
            except Exception as exc:  # an unhandled crash must not stop the loop
                print(f"  !! исполнение задачи #{task.id} упало: {type(exc).__name__}: {exc}", flush=True)
                _queue_stuck_recovery(
                    stuck_recoveries, task, lock_key(task), exc)
                _drain_stuck_recoveries(stuck_recoveries)
        else:
            admission_complete = admission_fence.begin()
            if lane_id is not None:
                task_lanes[task.id] = lane_id
                print(f"  -> Scheduler lane: {lane_id}", flush=True)
            in_flight[pool.submit(
                _execute_task_with_admission_fence,
                task, admission_complete)] = (lock_key(task), task)

    if pool is not None:
        if in_flight:
            print(f"Жду завершения задач в работе: {len(in_flight)}...")
        pool.shutdown(wait=True)
    heartbeat_stop.set()
    heartbeat_thread.join(timeout=2)
    db.mark_worker_stopped(os.getpid())
    print("Worker stopped.")
