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

The same session can live on another machine: pass ``host`` (an ssh target, or
a :class:`~promptpilot.remote.Remote` when the machine speaks PowerShell) and
every herdr CLI call is executed there over ssh — the pane, the agent and its
working directory belong to that machine, and the user attaches with
``herdr --remote <host>``.

Semantics notes:
  - task timeout bounds the *active* prompt turn (herdr-side, clean abort);
    time spent in ``blocked`` is intentionally unbounded — it is human time.
  - ``detached`` submits the prompt and completes the task immediately,
    leaving the pane open (herdr keeps the session alive).
  - the interactive session produces no stream-json, so cost/session_id
    are not captured; a SIGKILL of the worker leaves the pane running.
"""

import json
import os
import re
import subprocess
import time

from . import worktree
from .config import HERDR_BIN, HERDR_KEEP_PANE, HERDR_READ_LINES, HERDR_START_TIMEOUT_MS
from .remote import (POWERSHELL, REMOTE_CALL_TIMEOUT, as_remote, ps_quote,
                     ssh_command, ssh_script)

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


def herdr_argv(args, host=None) -> list:
    """Argv for a herdr CLI call — local, or on `host` (str or Remote) over ssh.

    Remotely the binary is resolved by the machine's own PATH: a local absolute
    PP_HERDR_BIN says nothing about the other machine.
    """
    remote = as_remote(host)
    if not remote:
        return [HERDR_BIN, *[str(a) for a in args]]
    return ssh_command(remote, [os.path.basename(HERDR_BIN), *args])


def attach_hint(host=None) -> str:
    """How the user opens this herdr session by hand."""
    remote = as_remote(host)
    return f"herdr --remote {remote.host}" if remote else "herdr"


def _run(args, host=None, timeout=None):
    """Run a herdr CLI command. Returns (rc, parsed_json_or_None, raw_output)."""
    if host and timeout is None:
        timeout = REMOTE_CALL_TIMEOUT
    try:
        proc = subprocess.run(
            herdr_argv(args, host),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise HerdrError("ssh not found" if host else f"herdr CLI not found: {HERDR_BIN}")
    except subprocess.TimeoutExpired:
        where = f" on {as_remote(host).host}" if host else ""
        return -1, None, f"herdr {' '.join(str(a) for a in args[:2])}{where}: local timeout"
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


def _start_server(host=None):
    """Launch the herdr server in the background (locally or on `host`)."""
    remote = as_remote(host)
    if not remote:
        subprocess.Popen(
            [HERDR_BIN, "server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    # Detach on the remote side: a server still attached to the ssh session
    # would hold the connection open for as long as it lives.
    binary = os.path.basename(HERDR_BIN)
    if remote.shell == POWERSHELL:
        script = (f"Start-Process -FilePath {ps_quote(binary)} "
                  f"-ArgumentList 'server' -WindowStyle Hidden")
    else:
        script = f"nohup {binary} server >/dev/null 2>&1 </dev/null &"
    try:
        subprocess.run(
            ssh_script(remote, script),
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _ensure_server(host=None):
    rc, data, _ = _run(["status", "server", "--json"], host=host, timeout=30)
    if rc == 0 and _dig(data, "running", default=False):
        return
    _start_server(host)
    for _ in range(20):
        time.sleep(0.5)
        rc, data, _ = _run(["status", "server", "--json"], host=host, timeout=30)
        if rc == 0 and _dig(data, "running", default=False):
            return
    where = f" на машине {as_remote(host).host}" if host else ""
    raise HerdrError(f"herdr server{where} не запущен и не удалось его поднять "
                     f"(установлен ли herdr, доступен ли ssh?)")


def _close_stale_tabs(task_id: int, host=None):
    """Close leftover panes of this task's previous attempts (crash recovery:
    the worker may have been killed while the agent kept running).

    A worktree task owns a whole workspace, so those are swept too — closed,
    never removed: the previous attempt's checkout is where the retry resumes.
    """
    prefix = f"pp-t{task_id}-"
    rc, data, _ = _run(["tab", "list"], host=host)
    if rc == 0:
        for tab in _dig(data, "result", "tabs", default=[]) or []:
            if str(tab.get("label", "")).startswith(prefix):
                _run(["tab", "close", tab.get("tab_id", "")], host=host)
    rc, data, _ = _run(["workspace", "list"], host=host)
    if rc == 0:
        for ws in _dig(data, "result", "workspaces", default=[]) or []:
            if str(ws.get("label", "")).startswith(prefix):
                _run(["workspace", "close", ws.get("workspace_id", "")], host=host)


def _open_worktree(task, cwd, label, host=None) -> dict:
    """Give the task its own checkout — a worktree-backed herdr workspace.

    herdr owns the checkout: it lands under the worktrees root of the machine
    that actually runs the task (so this works over ssh unchanged) and shows up
    as a worktree workspace the user can open, inspect and drop from the UI.
    pp only fixes the branch — ``pp/t<id>`` — and that is what lets a retry
    resume its own work instead of starting from a half-changed tree.

    Returns the parsed create/open response plus its interesting bits, or
    ``{"error": ...}``.
    """
    if not cwd:
        return {"error": "worktree: не задана рабочая директория — "
                         "не из чего создавать ветку"}
    branch = worktree.branch_for(task.id)
    args = ["--cwd", cwd, "--branch", branch, "--label", label, "--no-focus"]
    rc, data, raw = _run(["worktree", "create", *args], host=host)
    if rc != 0 and _error_code(data) == "worktree_create_failed":
        # The checkout of a previous attempt is still on disk — reopen it.
        rc, data, raw = _run(["worktree", "open", *args], host=host)
    if rc != 0:
        if _error_code(data) == "not_git_worktree":
            return {"error": f"worktree: «{cwd}» — не git-репозиторий; "
                             f"сними галку «свой worktree» или укажи репозиторий"}
        return {"error": f"herdr worktree create failed: {raw}"}
    wt = {
        "data": data,
        "workspace_id": _dig(data, "result", "workspace", "workspace_id"),
        "path": _dig(data, "result", "worktree", "path"),
        "root": _dig(data, "result", "workspace", "worktree", "repo_root"),
        "branch": _dig(data, "result", "worktree", "branch") or branch,
    }
    # A checkout without .env is a checkout the agent cannot run anything in.
    wt["copied"] = worktree.copy_extras(wt["root"], wt["path"], host) if wt["root"] else []
    return wt


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


def _wait_settled(name, until_args, deadline, cancel_check, host=None):
    """Wait for the agent in 10s chunks so cancel/timeout stay responsive.

    Returns (state, raw): state is an agent status or one of the pseudo-states
    "__cancel__" / "__timeout__" / "__error__".
    """
    while True:
        if cancel_check and cancel_check():
            return "__cancel__", ""
        if deadline and time.monotonic() > deadline:
            return "__timeout__", ""
        rc, data, raw = _run(["agent", "wait", name, *until_args, "--timeout", "10000"],
                             host=host)
        if rc == 0:
            return _agent_status(data), raw
        if _error_code(data) != "timeout":
            return "__error__", raw


def run_in_herdr(task, provider_cfg: dict, on_blocked=None, timeout: int = None,
                 cancel_check=None, keep_pane: bool = None, host: str = None,
                 on_worktree=None) -> dict:
    """Run a task in a herdr-managed agent session.

    on_blocked(pane_id) is called once when the agent first enters ``blocked``.
    on_worktree(path, branch) is called as soon as a ``worktree`` task has its
    own checkout — long before the run ends, which is when the user wants it.
    timeout (seconds) bounds the ACTIVE prompt turn; time spent in ``blocked``
    is human time and is not counted. cancel_check() → True aborts the run
    (pane is closed) — the worker maps it to the cancelled status.
    host: ssh target of the machine to run on; None = this machine.

    Returns {"ok", "rate_limited", "cancelled", "output", "error", "pane_id"}.
    Raises nothing: all herdr failures are reported via the outcome dict.
    """
    outcome = {"ok": False, "rate_limited": False, "cancelled": False,
               "output": "", "error": "", "pane_id": "",
               "worktree_path": "", "worktree_branch": ""}
    attach = attach_hint(host)
    workspace_id = ""  # set only when the task got its own worktree workspace
    repo_root = ""
    wt_copied = []

    try:
        _ensure_server(host)

        target = getattr(task, "herdr_target", None)
        if target:
            # Target mode: send the prompt into an EXISTING session. The pane
            # belongs to the user — never close or rename it.
            rc, data, raw = _run(["agent", "get", target], host=host)
            if rc != 0 or not _agent_status(data):
                outcome["error"] = (f"herdr-сессия «{target}» не найдена — панель закрыта? "
                                    f"({raw[:200]})")
                return outcome
            name = target
            pane_id = _dig(data, "result", "agent", "pane_id") or target
            tab_id = None
            outcome["pane_id"] = pane_id
        else:
            _close_stale_tabs(task.id, host)
            name = f"pp-t{task.id}-{int(time.time()) % 100000}"
            # Remote: the working dir must exist on THAT machine; without one
            # herdr falls back to its own default (the user's home there).
            cwd = task.working_dir or (None if host else ".")
            env_args = []
            for k, v in (provider_cfg.get("env") or {}).items():
                if v:
                    env_args += ["--env", f"{k}={v}"]

            if getattr(task, "worktree", False):
                wt = _open_worktree(task, cwd, name, host)
                if wt.get("error"):
                    outcome["error"] = wt["error"]
                    return outcome
                outcome["worktree_path"] = wt["path"]
                outcome["worktree_branch"] = wt["branch"]
                workspace_id, repo_root, wt_copied = wt["workspace_id"], wt["root"], wt["copied"]
                if on_worktree:
                    on_worktree(wt["path"], wt["branch"])
                if env_args:
                    # `worktree create` takes no --env, so the agent gets its
                    # own tab inside that workspace to receive the provider's.
                    rc, data, raw = _run(["tab", "create", "--workspace", workspace_id,
                                          "--cwd", wt["path"], "--label", name,
                                          "--no-focus", *env_args], host=host)
                else:
                    rc, data, raw = 0, wt["data"], ""
            else:
                tab_cmd = ["tab", "create", "--label", name, "--no-focus", *env_args]
                if cwd:
                    tab_cmd += ["--cwd", cwd]
                rc, data, raw = _run(tab_cmd, host=host)

            pane_id = _dig(data, "result", "root_pane", "pane_id")
            tab_id = _dig(data, "result", "tab", "tab_id")
            if rc != 0 or not pane_id or not tab_id:
                outcome["error"] = f"herdr tab create failed: {raw}"
                return outcome
            outcome["pane_id"] = pane_id

        def close_tab():
            if workspace_id:
                # The checkout IS the result — drop it only when the agent
                # provably left nothing there, so failures don't litter disks.
                if repo_root and worktree.is_untouched(repo_root, outcome["worktree_path"], host):
                    _run(["worktree", "remove", "--workspace", workspace_id], host=host)
                else:
                    _run(["workspace", "close", workspace_id], host=host)
                return
            if tab_id:
                _run(["tab", "close", tab_id], host=host)

        def fail(msg, keep_pane=True):
            if keep_pane:
                outcome["error"] = f"{msg}\n(панель {pane_id} оставлена — подключись командой: {attach})"
            else:
                close_tab()
                outcome["error"] = msg
            return outcome

        # Start the agent, forwarding per-task flags to its CLI.
        # (target mode: the agent is already running — nothing to start)
        kind = provider_cfg.get("kind", "claude")
        # provider-level extra CLI args (e.g. ["--effort", "max"] for claude)
        agent_args = list(provider_cfg.get("args") or [])
        if task.model:
            agent_args += ["--model", task.model]
        if task.session_id:
            agent_args += ["--resume", task.session_id]
        if task.skip_permissions:
            agent_args.append("--dangerously-skip-permissions")
        if not target:
            cmd = ["agent", "start", name, "--kind", kind, "--pane", pane_id,
                   "--timeout", str(HERDR_START_TIMEOUT_MS)]
            if agent_args:
                cmd += ["--", *agent_args]
            # A freshly created pane needs a moment to spawn its shell; until
            # then agent start fails with agent_pane_busy — retry briefly.
            for _ in range(20):
                rc, data, raw = _run(cmd, host=host)
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
                                  "--wait", "--until", "working", "--timeout", "15000"],
                                 host=host)
            if rc != 0 and _error_code(data) not in ("timeout", "agent_prompt_stalled"):
                return fail(f"herdr agent prompt failed: {raw}")
            outcome["ok"] = True
            outcome["output"] = (f"Отправлено в сессию {name} (панель {pane_id})" if target
                                 else f"Запущен в herdr (панель {pane_id}) — подключись командой: {attach}")
            if workspace_id:
                outcome["output"] += "\n" + worktree.summary(outcome["worktree_path"],
                                                             outcome["worktree_branch"], wt_copied)
            return outcome

        # Submit the prompt. Waits are chunked (10s) so a user cancel or the
        # task timeout stays responsive; the herdr-side 10s "timeout" error
        # here just means "agent still working". On agent_prompt_stalled: if
        # the agent is already working the prompt DID land (resubmit would
        # double it) — wait instead.
        deadline = time.monotonic() + timeout if timeout else None
        prompt_cmd = ["agent", "prompt", name, task.prompt, "--wait", "--timeout", "10000"]
        state = ""
        for attempt in range(1, PROMPT_STALL_RETRIES + 1):
            rc, data, raw = _run(prompt_cmd, host=host)
            if rc == 0:
                state = _agent_status(data)
                break
            code = _error_code(data)
            if code == "timeout":
                # prompt landed, agent is busy — fall into the chunked wait
                state, raw = _wait_settled(name, [], deadline, cancel_check, host)
                break
            if code != "agent_prompt_stalled":
                return fail(f"herdr agent prompt failed: {raw}")
            rc2, data2, _ = _run(["agent", "get", name], host=host)
            if rc2 == 0 and _agent_status(data2) == "working":
                state, raw = _wait_settled(name, [], deadline, cancel_check, host)
                break
            # The text may be sitting unsent in the input box — some agent
            # builds swallow the submit key right after the paste. Press Enter
            # rather than re-sending: a resend would type the prompt twice.
            # A changed state_change_seq proves the submit landed.
            seq_before = _dig(data2, "result", "agent", "state_change_seq", default=-1)
            _run(["agent", "send-keys", name, "enter"], host=host)
            time.sleep(2)
            rc3, data3, _ = _run(["agent", "get", name], host=host)
            if rc3 == 0 and (_agent_status(data3) != "idle"
                             or _dig(data3, "result", "agent", "state_change_seq",
                                     default=-1) != seq_before):
                state, raw = _wait_settled(name, [], deadline, cancel_check, host)
                break
            if attempt == PROMPT_STALL_RETRIES:
                return fail(f"herdr: prompt stalled after {attempt} attempts: {raw}")
            time.sleep(2)

        # blocked = waiting for a human (permission dialog etc.) — no deadline.
        if state == "blocked" and on_blocked:
            on_blocked(pane_id)
        while state == "blocked":
            state, raw = _wait_settled(name, ["--until", "idle", "--until", "done"],
                                       None, cancel_check, host)

        if state == "__cancel__":
            close_tab()
            outcome["cancelled"] = True
            return outcome
        if state == "__timeout__":
            return fail(f"herdr: таймаут задачи ({timeout}s)", keep_pane=False)
        if state == "__error__":
            return fail(f"herdr agent wait failed: {raw}")

        rc, _, raw = _run(["agent", "read", name, "--source", "recent-unwrapped",
                           "--lines", str(HERDR_READ_LINES), "--format", "text"],
                          host=host)
        if rc != 0:
            return fail(f"herdr agent read failed: {raw}")

        cleaned = _trim_transcript(raw, task.prompt)

        if _looks_rate_limited(cleaned):
            close_tab()  # no-op for target mode (foreign pane)
            outcome["rate_limited"] = True
            outcome["error"] = cleaned
            return outcome

        # task checkbox decides; provider flag / env force keep-open regardless
        where = f", машина {as_remote(host).host}" if host else ""
        if target:
            outcome["ok"] = True
            outcome["output"] = (f"{cleaned}\n\n--- Meta ---\n"
                                 f"Executor: herdr (сессия {name}, pane {pane_id}{where})")
            return outcome

        # What the user needs in order to go look at the result: where the
        # branch is and whether the agent actually put anything on it.
        wt_meta = ""
        if workspace_id:
            changes = worktree.status(repo_root, outcome["worktree_path"], host) if repo_root else ""
            wt_meta = "\n" + worktree.summary(outcome["worktree_path"],
                                              outcome["worktree_branch"], wt_copied)
            if changes:
                wt_meta += f"\nИзменения: {changes}"

        keep = bool(keep_pane) or provider_cfg.get("keep_pane") or HERDR_KEEP_PANE
        if keep:
            # Keep the live session for follow-up work. Rename the agent out of
            # the pp-t* namespace so the Telegram bridge watches the continued
            # session, and relabel the tab so a task re-run won't close it as
            # stale. Both renames are best-effort.
            _run(["agent", "rename", name, f"t{task.id}"], host=host)
            _run(["tab", "rename", tab_id, f"pp-kept-{task.id}"], host=host)
            if workspace_id:
                _run(["workspace", "rename", workspace_id, f"pp-kept-{task.id}"], host=host)
            outcome["ok"] = True
            outcome["output"] = (
                f"{cleaned}\n\n--- Meta ---\nExecutor: herdr (pane {pane_id}{where}, "
                f"сессия оставлена — открой «{attach}» и продолжай в ней){wt_meta}"
            )
            return outcome

        close_tab()
        outcome["ok"] = True
        outcome["output"] = (f"{cleaned}\n\n--- Meta ---\n"
                             f"Executor: herdr (pane {pane_id}{where}){wt_meta}")
        return outcome

    except HerdrError as e:
        outcome["error"] = str(e)
        return outcome
    except Exception as e:  # never crash the worker loop over a herdr hiccup
        outcome["error"] = f"herdr executor error: {type(e).__name__}: {e}"
        return outcome
