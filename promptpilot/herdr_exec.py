"""herdr executor — runs tasks in live herdr-managed agent sessions.

Providers opt in via ``"executor": "herdr"`` in providers.json::

    "claude-herdr": {
        "executor": "herdr",
        "kind": "claude",
        "description": "Claude Code в herdr-сессии",
        "supports_skills": true
    }

Instead of a headless subprocess, the task runs in a visible herdr pane:
the user can attach (``herdr``), watch the agent work and, when the agent
hits a permission dialog (state ``blocked``), approve it by hand — the task
keeps running instead of failing.

Semantics notes:
  - task timeout bounds the *active* prompt turn (herdr-side, clean abort);
    time spent in ``blocked`` is intentionally unbounded — it is human time.
  - ``detached`` submits the prompt and completes the task immediately,
    leaving the pane open (herdr keeps the session alive).
  - the interactive session produces no stream-json, so cost/session_id
    are not captured; a SIGKILL of the worker leaves the pane running.
"""

import json
import re
import subprocess
import time

from .config import HERDR_BIN, HERDR_READ_LINES, HERDR_START_TIMEOUT_MS

# Distinctive in-session usage/rate-limit banners (interactive sessions exit 0
# even when the provider is limited, so detection is textual by necessity).
RATE_LIMIT_RE = re.compile(
    r"reached your usage limit|usage limit reached|rate_limit_error"
    r"|overloaded_error|too many requests|quota exceeded|hit your session limit",
    re.IGNORECASE,
)

PROMPT_STALL_RETRIES = 3


class HerdrError(Exception):
    pass


def _run(args, timeout=None):
    """Run a herdr CLI command. Returns (rc, parsed_json_or_None, raw_output)."""
    try:
        proc = subprocess.run(
            [HERDR_BIN, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise HerdrError(f"herdr CLI not found: {HERDR_BIN}")
    except subprocess.TimeoutExpired:
        return -1, None, f"herdr {' '.join(args[:2])}: local timeout"
    raw = (proc.stdout.strip() or proc.stderr.strip())
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        data = None
    return proc.returncode, data, raw


def _dig(data, *keys, default=""):
    """Safe nested dict access: _dig(d, 'result', 'agent', 'agent_status')."""
    cur = data
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _error_code(data) -> str:
    return _dig(data, "error", "code")


def _agent_status(data) -> str:
    return _dig(data, "result", "agent", "agent_status")


def _ensure_server():
    rc, data, _ = _run(["status", "server", "--json"])
    if rc == 0 and _dig(data, "running", default=False):
        return
    subprocess.Popen(
        [HERDR_BIN, "server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        time.sleep(0.5)
        rc, data, _ = _run(["status", "server", "--json"])
        if rc == 0 and _dig(data, "running", default=False):
            return
    raise HerdrError("herdr server is not running and could not be started")


def _close_stale_tabs(task_id: int):
    """Close leftover panes of this task's previous attempts (crash recovery:
    the worker may have been killed while the agent kept running)."""
    rc, data, _ = _run(["tab", "list"])
    if rc != 0:
        return
    prefix = f"pp-t{task_id}-"
    for tab in _dig(data, "result", "tabs", default=[]) or []:
        if str(tab.get("label", "")).startswith(prefix):
            _run(["tab", "close", tab.get("tab_id", "")])


def _trim_transcript(raw: str, prompt: str) -> str:
    """Keep the transcript from the last echo of our prompt on, drop UI chrome."""
    lines = raw.splitlines()

    start = 0
    probe = prompt.strip().splitlines()[0][:60] if prompt.strip() else ""
    for i, line in enumerate(lines):
        if probe and line.lstrip().startswith("❯") and probe in line:
            start = i

    # The bottom input box begins at the first full-width ─ separator after start.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        s = lines[i].strip()
        if s and set(s) <= {"─"}:
            end = i
            break

    def chrome(s: str) -> bool:
        s = s.strip()
        return (not s or s == "❯" or s.startswith("⏸")
                or "? for shortcuts" in s or "· /effort" in s)

    while end > start and chrome(lines[end - 1]):
        end -= 1

    out = "\n".join(lines[start:end]).strip()
    return out if out else raw.strip()


def _looks_rate_limited(cleaned: str) -> bool:
    """Scan the response for limit banners, skipping echoed prompt lines
    (a prompt that merely mentions 'too many requests' must not trigger)."""
    response_only = "\n".join(
        l for l in cleaned.splitlines() if not l.lstrip().startswith("❯")
    )
    return bool(RATE_LIMIT_RE.search(response_only))


def run_in_herdr(task, provider_cfg: dict, on_blocked=None, timeout: int = None) -> dict:
    """Run a task in a herdr-managed agent session.

    on_blocked(pane_id) is called once when the agent first enters ``blocked``.
    timeout (seconds) bounds the prompt turn (herdr-side, clean abort).

    Returns {"ok", "rate_limited", "output", "error", "pane_id"}.
    Raises nothing: all herdr failures are reported via the outcome dict.
    """
    outcome = {"ok": False, "rate_limited": False, "output": "", "error": "", "pane_id": ""}

    try:
        _ensure_server()
        _close_stale_tabs(task.id)

        name = f"pp-t{task.id}-{int(time.time()) % 100000}"
        cwd = task.working_dir or "."
        tab_cmd = ["tab", "create", "--cwd", cwd, "--label", name, "--no-focus"]
        for k, v in (provider_cfg.get("env") or {}).items():
            if v:
                tab_cmd += ["--env", f"{k}={v}"]
        rc, data, raw = _run(tab_cmd)
        pane_id = _dig(data, "result", "root_pane", "pane_id")
        tab_id = _dig(data, "result", "tab", "tab_id")
        if rc != 0 or not pane_id or not tab_id:
            outcome["error"] = f"herdr tab create failed: {raw}"
            return outcome
        outcome["pane_id"] = pane_id

        def close_tab():
            _run(["tab", "close", tab_id])

        def fail(msg, keep_pane=True):
            if keep_pane:
                outcome["error"] = f"{msg}\n(панель {pane_id} оставлена — подключись командой: herdr)"
            else:
                close_tab()
                outcome["error"] = msg
            return outcome

        # Start the agent, forwarding per-task flags to its CLI.
        kind = provider_cfg.get("kind", "claude")
        agent_args = []
        if task.model:
            agent_args += ["--model", task.model]
        if task.session_id:
            agent_args += ["--resume", task.session_id]
        if task.skip_permissions:
            agent_args.append("--dangerously-skip-permissions")
        cmd = ["agent", "start", name, "--kind", kind, "--pane", pane_id,
               "--timeout", str(HERDR_START_TIMEOUT_MS)]
        if agent_args:
            cmd += ["--", *agent_args]
        # A freshly created pane needs a moment to spawn its shell; until then
        # agent start fails with agent_pane_busy — retry briefly.
        for _ in range(20):
            rc, data, raw = _run(cmd)
            if rc == 0 or _error_code(data) != "agent_pane_busy":
                break
            time.sleep(0.5)
        if rc != 0:
            close_tab()
            outcome["error"] = f"herdr agent start failed: {raw}"
            return outcome

        # Detached: submit the prompt, confirm it landed, leave the pane open.
        if task.detached:
            rc, data, raw = _run(["agent", "prompt", name, task.prompt,
                                  "--wait", "--until", "working", "--timeout", "15000"])
            if rc != 0 and _error_code(data) not in ("timeout", "agent_prompt_stalled"):
                return fail(f"herdr agent prompt failed: {raw}")
            outcome["ok"] = True
            outcome["output"] = f"Запущен в herdr (панель {pane_id}) — подключись командой: herdr"
            return outcome

        # Submit the prompt. On agent_prompt_stalled: if the agent is already
        # working the prompt DID land (resubmit would double it) — wait instead.
        prompt_cmd = ["agent", "prompt", name, task.prompt, "--wait"]
        if timeout:
            prompt_cmd += ["--timeout", str(timeout * 1000)]
        state = ""
        for attempt in range(1, PROMPT_STALL_RETRIES + 1):
            rc, data, raw = _run(prompt_cmd)
            if rc == 0:
                state = _agent_status(data)
                break
            code = _error_code(data)
            if code == "timeout":
                return fail(f"herdr: таймаут задачи ({timeout}s)", keep_pane=False)
            if code != "agent_prompt_stalled":
                return fail(f"herdr agent prompt failed: {raw}")
            rc2, data2, _ = _run(["agent", "get", name])
            if rc2 == 0 and _agent_status(data2) == "working":
                rc3, data3, raw3 = _run(["agent", "wait", name])
                if rc3 != 0:
                    return fail(f"herdr agent wait failed: {raw3}")
                state = _agent_status(data3)
                break
            if attempt == PROMPT_STALL_RETRIES:
                return fail(f"herdr: prompt stalled after {attempt} attempts: {raw}")
            time.sleep(2)

        # blocked = waiting for a human (permission dialog etc.).
        if state == "blocked" and on_blocked:
            on_blocked(pane_id)
        while state == "blocked":
            rc, data, raw = _run(["agent", "wait", name, "--until", "idle", "--until", "done"])
            if rc != 0:
                return fail(f"herdr wait after blocked failed: {raw}")
            state = _agent_status(data)

        rc, _, raw = _run(["agent", "read", name, "--source", "recent-unwrapped",
                           "--lines", str(HERDR_READ_LINES), "--format", "text"])
        if rc != 0:
            return fail(f"herdr agent read failed: {raw}")

        cleaned = _trim_transcript(raw, task.prompt)

        if _looks_rate_limited(cleaned):
            close_tab()
            outcome["rate_limited"] = True
            outcome["error"] = cleaned
            return outcome

        close_tab()
        outcome["ok"] = True
        outcome["output"] = f"{cleaned}\n\n--- Meta ---\nExecutor: herdr (pane {pane_id})"
        return outcome

    except HerdrError as e:
        outcome["error"] = str(e)
        return outcome
    except Exception as e:  # never crash the worker loop over a herdr hiccup
        outcome["error"] = f"herdr executor error: {type(e).__name__}: {e}"
        return outcome
