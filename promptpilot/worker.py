"""Worker — executes tasks from the queue."""

import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db, worktree
from .config import (BASE_DELAY, CONCURRENCY, DEFAULT_CLI, MAX_DELAY, MIN_FREE_MB,
                     POLL_INTERVAL, TASK_TIMEOUT, VERDICT_REQUIRED, build_cmd,
                     get_provider_env, load_providers)

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
VERDICTS = ("ГОТОВО", "УЖЕ СДЕЛАНО", "НУЖЕН ЧЕЛОВЕК", "НЕ СМОГ", "ПУСТО")
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
            # proc.kill() ends only the top process. An npm-installed CLI on
            # Windows is a .cmd wrapper (cmd.exe) that spawns node — killing the
            # wrapper leaves the real agent editing files. taskkill /T takes the
            # whole tree.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _drain(proc, timeout=10):
    """Collect a killed process's final output without hanging forever.

    A grandchild the agent detached into its own session (setsid) survives the
    process-group SIGKILL and keeps the inherited stdout/stderr pipe open, so a
    bare communicate() would block until it too dies. Give up after a timeout
    and return whatever was read.
    """
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            return proc.communicate(timeout=1)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return ("", "")


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
        return
    _maybe_recur(fresh, failed=fresh.status.value == "failed")


def _maybe_recur(task, failed: bool = False):
    """Enqueue the next occurrence of a recurring task."""
    if not task.recurrence:
        return
    series = db.prepare_series_recurrence(task.series_id, task.verdict) if task.series_id else None
    recurrence = series["effective_recurrence"] if series else task.recurrence
    if task.series_id and series is None:  # series was explicitly ended
        return
    next_dt = db.parse_recurrence(recurrence)
    if not next_dt:
        return
    from .models import TaskCreate
    # The prompt as stored, never the one this run was handed: a one-off note
    # must not be baked into every future occurrence.
    stored = db.get_task(task.id)
    db.create_task(TaskCreate(
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
        db.mark_failed(task.id, f"Срыв по вине среды ({marker}), "
                                f"попытки исчерпаны ({task.max_retries}).\n{detail}")
        print(f"  -> Env failure ({marker}), retries exhausted")
        return
    next_run = compute_next_run(task.retry_count)
    db.mark_rate_limited(task.id, next_run,
                         error=f"Срыв по вине среды ({marker}) — "
                               f"задача возвращена в очередь.\n{detail}")
    _notify_requeued(task, next_run, f"срыв среды ({marker})")
    print(f"  -> Env failure ({marker}). Retry #{task.retry_count + 1} "
          f"at {next_run.strftime('%H:%M:%S')}")


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
            # pane_id/machine ride along so the bot can attach confirm/screen/
            # reply buttons — approving one's OWN task from the phone used to
            # be impossible (the message only suggested ssh).
            db.add_notification(
                task.tg_chat_id,
                f"⏸ Задача #{task.id} ждёт подтверждения в herdr{where} (панель {pane_id}).\n"
                f"Подтверди кнопкой ниже, или подключись: {attach}",
                task_id=task.id,
                pane_id=pane_id,
                machine=machine,
            )

    def on_pane(pane_id):
        db.set_task_pane(task.id, pane_id)

    def on_worktree(path, branch):
        # Recorded the moment the checkout exists, not when the task ends: the
        # user watching a long run wants the branch name now.
        db.set_worktree(task.id, path, branch)
        print(f"  -> Worktree {path} ({branch})")

    outcome = run_in_herdr(task, provider_cfg, on_blocked=on_blocked,
                           timeout=_effective_timeout(task),
                           cancel_check=lambda: db.is_cancel_requested(task.id),
                           keep_pane=task.keep_pane, host=host,
                           on_worktree=on_worktree, on_pane=on_pane)

    if outcome.get("cancelled"):
        db.clear_cancel_request(task.id)
        db.mark_cancelled(task.id, "Отменена пользователем во время выполнения")
        print("  -> Cancelled by user")
        return

    if outcome["rate_limited"]:
        reason = outcome.get("retry_reason") or RETRY_RATE_LIMIT
        label = RETRY_REASON_ERR.get(reason, RETRY_REASON_ERR[RETRY_RATE_LIMIT])
        if task.retry_count >= task.max_retries:
            db.mark_failed(task.id, f"{label}, max retries ({task.max_retries}) exceeded.\n{outcome['error']}")
            return
        next_run = compute_next_run(task.retry_count)
        db.mark_rate_limited(task.id, next_run, error=outcome["error"] or label)
        _notify_requeued(task, next_run,
                         RETRY_REASON_RU.get(reason, RETRY_REASON_RU[RETRY_RATE_LIMIT]))
        print(f"  -> {label}. Retry #{task.retry_count + 1} at {next_run.strftime('%H:%M:%S')}")
        return

    if outcome.get("env_failure"):
        _requeue_env_failure(task, outcome["env_failure"], outcome["error"])
        return

    if not outcome["ok"]:
        db.mark_failed(task.id, outcome["error"], exit_code=1)
        print("  -> Failed (herdr)")
        return

    verdict = parse_verdict(outcome["output"])
    if verdict:
        db.set_verdict(task.id, verdict)
    db.mark_completed(task.id, outcome["output"], exit_code=0)
    text_preview = outcome["output"][:80].replace("\n", " ").strip()
    print(f"  -> Completed: {text_preview}")


def _wrap_ssh(remote, cmd, env_extra):
    """Run the command on a remote machine: the executable is resolved by that
    machine's PATH, the provider env is passed along in its shell's dialect."""
    from .remote import ssh_command
    argv = [os.path.basename(cmd[0]), *cmd[1:]]
    return ssh_command(remote, argv, env_extra)


def _execute_task_inner(task):
    """Run CLI with the task's prompt."""
    provider = task.provider or DEFAULT_CLI

    provider_cfg = load_providers().get(provider, {})
    machine = getattr(task, "machine", None)

    host = None
    if machine:
        from .config import load_machines, machine_remote
        m = load_machines().get(machine)
        if not m or not m.get("host"):
            db.mark_failed(task.id, f"Машина «{machine}» не найдена в реестре")
            return
        host = machine_remote(m)

    if provider_cfg.get("executor") == "herdr":
        # herdr sessions work the same way on any machine: the CLI calls go
        # over ssh, the pane lives there (attach with `herdr --remote <host>`).
        _execute_herdr_task(task, provider_cfg, host=host, machine=machine)
        return

    # The task's own checkout, when it asked for one. Done before the CLI starts
    # so the agent only ever sees the isolated tree.
    run_dir, wt_note = task.working_dir, ""
    if getattr(task, "worktree", False):
        if machine:
            # A headless remote command runs in the ssh login directory, so a
            # worktree over there would simply never be entered. herdr-based
            # providers place the agent in the checkout and do support this.
            db.mark_failed(task.id, f"worktree на машине «{machine}» поддерживается только "
                                    f"через herdr-провайдер (executor: herdr)")
            return
        try:
            wt = worktree.prepare(task.working_dir, task.id)
        except worktree.WorktreeError as e:
            db.mark_failed(task.id, f"worktree: {e}")
            print(f"  -> Failed (worktree): {e}")
            return
        run_dir = wt["path"]
        db.set_worktree(task.id, wt["path"], wt["branch"])
        wt_note = "\n\n" + worktree.summary(wt["path"], wt["branch"], wt["copied"])
        print(f"  -> Worktree {wt['path']} ({wt['branch']})")

    agent_prompt = effective_prompt(task)
    cmd = build_cmd(provider, agent_prompt, skip_permissions=task.skip_permissions,
                    session_id=task.session_id, model=task.model, guard=not machine,
                    effort=task.effort)
    prompt_stdin = agent_prompt if provider_cfg.get("prompt_stdin") else None

    env = get_provider_env(provider)
    # Marks the run in its own environment, inherited by the agent process. That
    # is what lets a live run be found by process rather than by our bookkeeping.
    env["PP_TASK_ID"] = str(task.id)

    if machine:
        if task.detached:
            db.mark_failed(task.id, "Фоновый запуск (detached) на удалённой машине пока не поддерживается")
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
            db.mark_completed(task.id, f"Запущен в фоне (PID {proc.pid}){wt_note}", exit_code=0)
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
            cwd=run_dir,
            stdin=subprocess.PIPE if prompt_stdin is not None else subprocess.DEVNULL,
            env=env,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError:
        db.mark_failed(task.id, f"CLI '{provider}' not found. Is it installed and in PATH?", exit_code=-1)
        return

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
                _drain(proc)
                db.clear_cancel_request(task.id)
                db.mark_cancelled(task.id, "Отменена пользователем во время выполнения")
                print("  -> Cancelled by user")
                return
            if effective_timeout and time.monotonic() - started > effective_timeout:
                _kill_process_tree(proc)
                _drain(proc)
                db.mark_failed(task.id, f"Execution timed out after {effective_timeout}s", exit_code=-1)
                return

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
            db.mark_failed(task.id, f"{label}, max retries ({task.max_retries}) exceeded.\n{rl_error}")
            return
        next_run = compute_next_run(task.retry_count)
        db.mark_rate_limited(task.id, next_run, error=rl_error or label)
        _notify_requeued(task, next_run, RETRY_REASON_RU[reason])
        print(f"  -> {label}. Retry #{task.retry_count + 1} at {next_run.strftime('%H:%M:%S')}")
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
            _requeue_env_failure(task, marker, error_text)
            return
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
                db.mark_failed(task.id, f"{RETRY_REASON_ERR[RETRY_RATE_LIMIT]}.\n{output}")
                return
            next_run = compute_next_run(task.retry_count)
            db.mark_rate_limited(task.id, next_run,
                                 error=output or RETRY_REASON_ERR[RETRY_RATE_LIMIT])
            _notify_requeued(task, next_run, RETRY_REASON_RU[RETRY_RATE_LIMIT])
            print(f"  -> Rate limited (stream event). Retry at {next_run.strftime('%H:%M:%S')}")
            return
    else:
        # Plain text output (non-Claude CLIs)
        output = result.stdout

    verdict = parse_verdict(output)
    if verdict:
        db.set_verdict(task.id, verdict)

    changes = None
    if wt_note:
        output += wt_note
        changes = worktree.status(wt["root"], wt["path"])
        if changes and changes != worktree.NO_CHANGES:
            output += f"\nИзменения: {changes}"

    db.mark_completed(task.id, output, exit_code=0, model_used=model_used, session_id=session_id)
    text_preview = output[:80].replace("\n", " ").strip()
    print(f"  -> Completed: {text_preview}")

    # An empty checkout (no commits, no dirty files) is just clutter — remove it
    # so .pp-worktrees doesn't grow without bound. The branch stays regardless;
    # this mirrors what the herdr executor already does for its own checkouts.
    if changes == worktree.NO_CHANGES:
        if worktree.remove(wt["root"], wt["path"]):
            print(f"  -> Removed empty worktree {wt['path']}")


def execute_task(task):
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
        return _execute_task_inner(task)
    finally:
        try:
            _recur_after_run(task)
        except Exception as exc:
            print(f"  !! не смог продлить расписание #{task.id}: {exc}", flush=True)
        try:
            workflows.sync_task(task.id)
            workflows.advance_linked_task(task.id)
        except Exception as exc:
            print(f"  !! workflow final sync #{task.id}: {exc}", flush=True)


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


def _fail_stuck(task_id, exc):
    """An unexpected crash in execute_task left a task stranded in 'running'.

    execute_task reports task-level failures itself, so reaching here is a bug
    (a KeyError in parsing, a 'database is locked'); mark it failed so the queue
    keeps moving instead of a task stuck forever and — in the pool — its lock
    key freed while a second task walks into the same directory. Only touch it
    if it is still running: the crash may have happened after mark_completed.
    """
    try:
        t = db.get_task(task_id)
        if t and t.status.value == "running":
            db.mark_failed(task_id, f"Внутренняя ошибка воркера: {type(exc).__name__}: {exc}")
            from . import workflows
            workflows.sync_task(task_id)
            workflows.advance_linked_task(task_id)
    except Exception as e:  # never let recovery itself take down the loop
        print(f"  !! не удалось пометить #{task_id} failed: {e}", flush=True)


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
        path = os.path.abspath(path)
    path = os.path.normcase(path.rstrip("/\\"))
    return f"{where}:dir:{path}"


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

    # Recover tasks stuck in 'running' from a previous crash — but leave alone
    # any whose agent is still working: the worker dying doesn't kill the agent.
    alive = live_task_ids()
    if alive:
        print(f"Живые прогоны найдены по метке в окружении, не трогаю: {sorted(alive)}")
    db.recover_running(keep_ids=alive)
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
    in_flight = {}  # Future -> lock key held while it runs
    short_on_memory = False
    if CONCURRENCY > 1:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="pp-task")

    def reap():
        for fut in [f for f in in_flight if f.done()]:
            _lock, tid = in_flight.pop(fut)
            exc = fut.exception()
            if exc:  # execute_task already reports task failures; this is a bug
                print(f"  !! исполнение задачи #{tid} упало: {type(exc).__name__}: {exc}", flush=True)
                _fail_stuck(tid, exc)

    while running:
        reap()

        # Auto-reload: pick up code updates between tasks (dev-friendly —
        # a stale worker silently ignoring new features is worse than a restart)
        if code_snapshot is not None and _code_snapshot() != code_snapshot:
            if in_flight:
                # Restarting now would orphan live agents — let them finish.
                time.sleep(POLL_INTERVAL)
                continue
            print("Код обновился — перезапускаю worker...", flush=True)
            os.execv(sys.executable, [sys.executable, "-m", "promptpilot", "worker"])

        if db.is_paused() or len(in_flight) >= CONCURRENCY:
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

        task = db.get_next_runnable(
            busy_keys=[lk for lk, _tid in in_flight.values() if lk], key_fn=lock_key)
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
                _fail_stuck(task.id, exc)
        else:
            in_flight[pool.submit(execute_task, task)] = (lock_key(task), task.id)

    if pool is not None:
        if in_flight:
            print(f"Жду завершения задач в работе: {len(in_flight)}...")
        pool.shutdown(wait=True)
    print("Worker stopped.")
