import json
import os
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from promptpilot import herdr_exec, process_tree, worker
from promptpilot.models import TaskCreate
from promptpilot.process_tree import OwnedProcess, run_owned


SLEEP_CODE = "import time; time.sleep(120)"
SPAWN_AND_REPORT_CODE = (
    "import subprocess, sys, time; "
    "child = subprocess.Popen([sys.executable, '-c', %r]); "
    "print(child.pid, flush=True); "
    "time.sleep(120)"
) % SLEEP_CODE


def _pid_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _wait_not_running(pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _stop_unrelated(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def test_owned_process_termination_kills_descendant_not_unrelated_process():
    unrelated = subprocess.Popen([sys.executable, "-c", SLEEP_CODE])
    tree = None
    try:
        tree = OwnedProcess.start(
            [sys.executable, "-c", SPAWN_AND_REPORT_CODE],
            stdout=subprocess.PIPE,
            text=True,
        )
        descendant_pid = int(tree.process.stdout.readline().strip())
        tree.process.stdout.close()

        tree.terminate()
        tree.close()
        tree.process.wait(timeout=10)

        assert _wait_not_running(descendant_pid)
        assert unrelated.poll() is None
    finally:
        if tree is not None:
            tree.close()
            if tree.process.poll() is None:
                tree.process.kill()
                tree.process.wait(timeout=10)
        _stop_unrelated(unrelated)


def test_closing_after_root_exit_kills_lingering_descendant():
    spawn_and_exit = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', %r], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid, flush=True)"
    ) % SLEEP_CODE
    tree = OwnedProcess.start(
        [sys.executable, "-c", spawn_and_exit],
        stdout=subprocess.PIPE,
        text=True,
    )
    descendant_pid = int(tree.process.stdout.readline().strip())
    tree.process.stdout.close()
    try:
        tree.process.wait(timeout=10)
        assert _pid_is_running(descendant_pid)
        tree.close()
        assert _wait_not_running(descendant_pid)
    finally:
        tree.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object setup")
def test_windows_close_terminates_job_with_retained_handle_not_unrelated_process():
    """Normal completion must not depend on PromptPilot owning the last handle."""
    import ctypes
    from ctypes import wintypes

    duplicate_same_access = 0x00000002
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    spawn_and_exit = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', %r], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid, flush=True)"
    ) % SLEEP_CODE
    unrelated = subprocess.Popen([sys.executable, "-c", SLEEP_CODE])
    tree = OwnedProcess.start(
        [sys.executable, "-c", spawn_and_exit],
        stdout=subprocess.PIPE,
        text=True,
    )
    retained_job = wintypes.HANDLE()
    try:
        current = kernel32.GetCurrentProcess()
        assert kernel32.DuplicateHandle(
            current, tree._job, current, ctypes.byref(retained_job),
            0, False, duplicate_same_access,
        )
        descendant_pid = int(tree.process.stdout.readline().strip())
        tree.process.stdout.close()
        tree.process.wait(timeout=10)

        tree.close()

        assert _wait_not_running(descendant_pid)
        assert unrelated.poll() is None
    finally:
        tree.close()
        if retained_job:
            kernel32.CloseHandle(retained_job)
        _stop_unrelated(unrelated)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object setup")
@pytest.mark.parametrize("failure_point", ["assign", "resume"])
def test_windows_boundary_setup_failure_kills_suspended_root(
        monkeypatch, failure_point):
    events = []

    class FakeProcess:
        pid = 4242
        killed = False

        def poll(self):
            return 1 if self.killed else None

        def kill(self):
            self.killed = True
            events.append("kill")

        def wait(self, timeout=None):
            events.append("wait")
            return 1

    fake_process = FakeProcess()
    monkeypatch.setattr(process_tree, "_new_windows_job", lambda: 99)
    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda *_a, **_kw: fake_process)
    monkeypatch.setattr(
        process_tree, "_TerminateJobObject",
        lambda job, code: events.append(("terminate_job", job, code)) or True,
    )
    monkeypatch.setattr(
        process_tree, "_CloseHandle", lambda handle: events.append(("close", handle)) or True,
    )

    def assign(_job, _pid):
        if failure_point == "assign":
            raise process_tree.ProcessTreeError("assign failed")
        events.append("assign")

    def resume(_pid):
        if failure_point == "resume":
            raise process_tree.ProcessTreeError("resume failed")

    monkeypatch.setattr(process_tree, "_assign_windows_process", assign)
    monkeypatch.setattr(process_tree, "_resume_windows_process", resume)

    with pytest.raises(process_tree.ProcessTreeError):
        OwnedProcess.start(["provider.exe"])

    assert ("terminate_job", 99, 1) in events
    assert "kill" in events
    assert "wait" in events
    assert ("close", 99) in events


def test_run_owned_timeout_kills_descendant_not_unrelated_process(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    spawn_and_record = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', %r]); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(120)"
    ) % SLEEP_CODE
    unrelated = subprocess.Popen([sys.executable, "-c", SLEEP_CODE])
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_owned(
                [sys.executable, "-c", spawn_and_record, str(child_pid_file)],
                timeout=5,
            )

        descendant_pid = int(child_pid_file.read_text())
        assert _wait_not_running(descendant_pid)
        assert unrelated.poll() is None
    finally:
        _stop_unrelated(unrelated)


def test_herdr_cli_timeout_uses_owned_process_runner(monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(herdr_exec, "run_owned", timeout)

    rc, data, raw = herdr_exec._run(["agent", "get", "example"], timeout=3)

    assert rc == -1
    assert data is None
    assert raw == "herdr agent get: local timeout"
    assert calls[0][1]["timeout"] == 3


def test_herdr_default_timeout_is_operation_aware(monkeypatch):
    monkeypatch.setattr(herdr_exec, "HERDR_START_TIMEOUT_MS", 90_000)
    monkeypatch.setattr(herdr_exec, "HERDR_WORKTREE_TIMEOUT_SECONDS", 420)

    assert herdr_exec._command_timeout(["agent", "get", "pp-t42"]) == 30
    assert herdr_exec._command_timeout(["agent", "start", "pp-t42"]) == 100
    assert herdr_exec._command_timeout(["worktree", "create"]) == 420
    assert herdr_exec._command_timeout(["worktree", "open"]) == 420
    assert herdr_exec._command_timeout(["worktree", "remove"]) == 420
    assert herdr_exec._command_timeout(["agent", "get", "pp-t42"], "host") == 180


def test_herdr_run_applies_worktree_timeout(monkeypatch):
    calls = []

    def completed(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(herdr_exec, "run_owned", completed)
    monkeypatch.setattr(herdr_exec, "HERDR_WORKTREE_TIMEOUT_SECONDS", 420)

    rc, data, raw = herdr_exec._run(["worktree", "remove", "--workspace", "w1"])

    assert (rc, data, raw) == (0, {}, "{}")
    assert calls[0][1]["timeout"] == 420


@pytest.mark.parametrize("probe_data", [
    None,
    {},
    {"result": {}},
    {"result": {"agent": {"pane_id": "p1"}}},
    {"result": {"agent": {"agent_status": "unknown"}}},
])
def test_owned_herdr_close_rejects_incomplete_or_unknown_probe(
        monkeypatch, probe_data):
    def ambiguous(args, host=None, timeout=None):
        if args[:2] == ["tab", "close"]:
            return 1, None, "close failed"
        return 0, probe_data, "ambiguous"

    monkeypatch.setattr(herdr_exec, "_run", ambiguous)
    monkeypatch.setattr(herdr_exec.time, "sleep", lambda _seconds: None)

    error = herdr_exec._close_owned_session(
        "pp-t42", ["tab", "close", "owned-tab"], host=None,
    )

    assert "could not confirm" in error


def test_owned_herdr_close_failure_is_not_silently_accepted(monkeypatch):
    calls = []

    def still_running(args, host=None, timeout=None):
        calls.append((args, timeout))
        if args[:2] == ["tab", "close"]:
            return 1, None, "close failed"
        return 0, {"result": {"agent": {"agent_status": "working"}}}, "still alive"

    monkeypatch.setattr(herdr_exec, "_run", still_running)
    monkeypatch.setattr(herdr_exec.time, "sleep", lambda _seconds: None)

    error = herdr_exec._close_owned_session(
        "pp-t42", ["tab", "close", "owned-tab"], host=None,
    )

    assert "could not confirm" in error
    assert [call for call in calls if call[0][:2] == ["tab", "close"]] == [
        (["tab", "close", "owned-tab"], None),
        (["tab", "close", "owned-tab"], None),
        (["tab", "close", "owned-tab"], None),
    ]


def test_owned_herdr_close_accepts_verified_missing_agent(monkeypatch):
    responses = iter([
        (0, {"result": {}}, "closed"),
        (1, {"error": {"code": "agent_not_found"}}, "not found"),
    ])
    monkeypatch.setattr(herdr_exec, "_run", lambda *_args, **_kwargs: next(responses))

    assert herdr_exec._close_owned_session(
        "pp-t42", ["tab", "close", "owned-tab"], host=None,
    ) == ""


def test_stale_owned_session_must_be_verified_closed(monkeypatch):
    def listed(args, host=None, timeout=None):
        if args[:2] == ["tab", "list"]:
            return 0, {"result": {"tabs": [
                {"label": "pp-t42-old", "tab_id": "tab-1"},
            ]}}, ""
        if args[:2] == ["workspace", "list"]:
            return 0, {"result": {"workspaces": []}}, ""
        raise AssertionError(args)

    monkeypatch.setattr(herdr_exec, "_run", listed)
    monkeypatch.setattr(
        herdr_exec, "_close_owned_session",
        lambda *_args, **_kwargs: "owned agent is still running",
    )

    with pytest.raises(herdr_exec.HerdrError, match="could not be stopped safely"):
        herdr_exec._close_stale_tabs(42)


def test_rate_limit_is_not_requeued_when_owned_close_is_unverified(monkeypatch):
    def fake_run(args, host=None, timeout=None):
        if args[:2] == ["tab", "create"]:
            return 0, {"result": {
                "root_pane": {"pane_id": "pane-1"},
                "tab": {"tab_id": "tab-1"},
            }}, ""
        if args[:2] == ["agent", "start"]:
            return 0, {"result": {"agent": {"agent_status": "idle"}}}, ""
        if args[:2] == ["agent", "prompt"]:
            return 0, {"result": {"agent": {"agent_status": "done"}}}, ""
        if args[:2] == ["agent", "read"]:
            return 0, None, "You reached your usage limit"
        raise AssertionError(args)

    task = SimpleNamespace(
        id=42, herdr_target=None, model=None, effort=None, session_id=None,
        skip_permissions=False, detached=False, working_dir=".", worktree=False,
    )
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda _host: None)
    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", lambda *_args: None)
    monkeypatch.setattr(herdr_exec, "_run", fake_run)
    monkeypatch.setattr(
        herdr_exec, "_stabilize_workflow_completion",
        lambda *_args, **_kwargs: ("done", ""),
    )
    monkeypatch.setattr(
        herdr_exec, "_close_owned_session",
        lambda *_args, **_kwargs: "owned session is still running",
    )

    outcome = herdr_exec.run_in_herdr(
        task, {"kind": "codex"}, prompt_override="test prompt",
    )

    assert outcome["ok"] is False
    assert outcome["rate_limited"] is False
    assert "still running" in outcome["error"]


def test_pane_bookkeeping_failure_closes_tab_before_agent_start(monkeypatch):
    calls = []
    closed = []

    def fake_run(args, host=None, timeout=None):
        calls.append(args)
        if args[:2] == ["tab", "create"]:
            return 0, {"result": {
                "root_pane": {"pane_id": "pane-1"},
                "tab": {"tab_id": "tab-1"},
            }}, ""
        raise AssertionError(args)

    task = SimpleNamespace(
        id=42, herdr_target=None, model=None, effort=None, session_id=None,
        skip_permissions=False, detached=False, working_dir=".", worktree=False,
    )
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda _host: None)
    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", lambda *_args: None)
    monkeypatch.setattr(herdr_exec, "_run", fake_run)
    monkeypatch.setattr(
        herdr_exec, "_close_owned_session",
        lambda name, close_args, host: closed.append(
            (name, close_args, host)) or "",
    )

    outcome = herdr_exec.run_in_herdr(
        task, {"kind": "agy"},
        on_pane=lambda _pane: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")),
        prompt_override="test prompt",
    )

    assert outcome["ok"] is False
    assert "pane bookkeeping failed before agent start" in outcome["error"]
    assert len(closed) == 1
    assert closed[0][0].startswith("pp-t42-")
    assert closed[0][1:] == (["tab", "close", "tab-1"], None)
    assert not any(call[:2] == ["agent", "start"] for call in calls)


def test_cancel_does_not_close_foreign_herdr_target(monkeypatch):
    calls = []

    def fake_run(args, host=None, timeout=None):
        calls.append(args)
        if args[:2] == ["agent", "get"]:
            return 0, {
                "result": {"agent": {"agent_status": "working", "pane_id": "w1:p1"}}
            }, ""
        if args[:2] == ["agent", "prompt"]:
            return 1, {"error": {"code": "timeout"}}, "still working"
        raise AssertionError(args)

    task = SimpleNamespace(
        id=42, herdr_target="user-session", model=None, effort=None,
        session_id=None, skip_permissions=False, detached=False,
    )
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda _host: None)
    monkeypatch.setattr(herdr_exec, "_run", fake_run)

    outcome = herdr_exec.run_in_herdr(
        task, {"kind": "codex"}, cancel_check=lambda: True,
        prompt_override="test prompt",
    )

    assert outcome["cancelled"] is True
    assert "может продолжать работу" in outcome["cancel_note"]
    assert not any(call[:2] in (["tab", "close"], ["workspace", "close"])
                   for call in calls)


def test_worker_timeout_kills_provider_descendant(isolated_db, monkeypatch, tmp_path):
    child_pid_file = tmp_path / "provider-child.pid"
    spawn_and_record = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', %r]); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(120)"
    ) % SLEEP_CODE
    command = [sys.executable, "-c", spawn_and_record, str(child_pid_file)]
    created = isolated_db.create_task(TaskCreate(
        prompt="test owned process timeout",
        provider="process-tree-test",
        task_timeout=1,
        working_dir=str(tmp_path),
    ))
    task = isolated_db.get_next_runnable()
    assert task.id == created.id

    monkeypatch.setattr(worker, "load_providers", lambda: {"process-tree-test": {}})
    monkeypatch.setattr(worker, "build_cmd", lambda *_args, **_kwargs: command.copy())
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())

    worker._execute_task_inner(task)

    settled = isolated_db.get_task(task.id)
    descendant_pid = int(child_pid_file.read_text())
    assert settled.status.value == "failed"
    assert settled.error == "Execution timed out after 1s"
    assert _wait_not_running(descendant_pid)


def test_worker_cancel_kills_provider_descendant(isolated_db, monkeypatch, tmp_path):
    child_pid_file = tmp_path / "cancelled-provider-child.pid"
    spawn_and_record = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', %r]); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(120)"
    ) % SLEEP_CODE
    command = [sys.executable, "-c", spawn_and_record, str(child_pid_file)]
    created = isolated_db.create_task(TaskCreate(
        prompt="test owned process cancellation",
        provider="process-tree-test",
        task_timeout=0,
        working_dir=str(tmp_path),
    ))
    task = isolated_db.get_next_runnable()
    assert task.id == created.id

    monkeypatch.setattr(worker, "load_providers", lambda: {"process-tree-test": {}})
    monkeypatch.setattr(worker, "build_cmd", lambda *_args, **_kwargs: command.copy())
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())
    monkeypatch.setattr(worker.db, "is_cancel_requested", lambda _task_id: True)

    worker._execute_task_inner(task)

    settled = isolated_db.get_task(task.id)
    descendant_pid = int(child_pid_file.read_text())
    assert settled.status.value == "cancelled"
    assert _wait_not_running(descendant_pid)


def test_worker_success_kills_provider_descendant_not_unrelated(
        isolated_db, monkeypatch, tmp_path):
    child_pid_file = tmp_path / "completed-provider-child.pid"
    spawn_record_and_exit = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', %r], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "print('provider completed')"
    ) % SLEEP_CODE
    command = [
        sys.executable, "-c", spawn_record_and_exit, str(child_pid_file),
    ]
    unrelated = subprocess.Popen([sys.executable, "-c", SLEEP_CODE])
    created = isolated_db.create_task(TaskCreate(
        prompt="test owned process normal completion",
        provider="process-tree-test",
        working_dir=str(tmp_path),
    ))
    task = isolated_db.get_next_runnable()
    assert task.id == created.id

    monkeypatch.setattr(worker, "load_providers", lambda: {"process-tree-test": {}})
    monkeypatch.setattr(worker, "build_cmd", lambda *_args, **_kwargs: command.copy())
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())

    try:
        worker._execute_task_inner(task)

        settled = isolated_db.get_task(task.id)
        descendant_pid = int(child_pid_file.read_text())
        assert settled.status.value == "completed"
        assert _wait_not_running(descendant_pid)
        assert unrelated.poll() is None
    finally:
        _stop_unrelated(unrelated)


def test_headless_pipeline_success_without_closing_verdict_is_failed(
        isolated_db, monkeypatch, tmp_path):
    created = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
        provider="process-tree-test", working_dir=str(tmp_path),
    ))
    task = isolated_db.get_next_runnable()
    assert task.id == created.id

    from promptpilot import pipeline_insights

    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route",
        lambda *_args, **_kwargs: {
            "action": "prompt", "mode": "skill", "prompt": task.prompt,
            "profile_id": "example", "queue_id": "review",
        },
    )
    monkeypatch.setattr(worker, "load_providers", lambda: {"process-tree-test": {}})
    monkeypatch.setattr(
        worker, "build_cmd",
        lambda *_args, **_kwargs: [
            sys.executable, "-c", "print('work ended without verdict')",
        ],
    )
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())

    worker._execute_task_inner(task)

    settled = isolated_db.get_task(task.id)
    assert settled.status.value == "failed"
    assert "without a closing ИТОГ verdict" in settled.error


def test_headless_stream_pipeline_validates_verdict_before_stored_meta(
        isolated_db, monkeypatch, tmp_path):
    created = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
        provider="process-tree-test", working_dir=str(tmp_path),
    ))
    task = isolated_db.get_next_runnable()
    assert task.id == created.id

    from promptpilot import pipeline_insights

    events = [
        {"type": "thread.started", "thread_id": "thread-603"},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "ИТОГ: ГОТОВО (reviewed #1414)",
        }},
        {"type": "turn.completed", "usage": {
            "input_tokens": 120, "cached_input_tokens": 80,
            "output_tokens": 30, "reasoning_output_tokens": 10,
        }},
    ]
    encoded_events = json.dumps(events, ensure_ascii=True)
    script = (
        "import json; events=json.loads(" + repr(encoded_events)
        + "); [print(json.dumps(event)) for event in events]"
    )
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route",
        lambda *_args, **_kwargs: {
            "action": "prompt", "mode": "skill", "prompt": task.prompt,
            "profile_id": "example", "queue_id": "review",
        },
    )
    monkeypatch.setattr(worker, "load_providers", lambda: {"process-tree-test": {}})
    monkeypatch.setattr(
        worker, "build_cmd", lambda *_args, **_kwargs: [sys.executable, "-c", script],
    )
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())

    worker._execute_task_inner(task)

    settled = isolated_db.get_task(task.id)
    assert settled.status.value == "completed"
    assert settled.verdict == "ГОТОВО"
    assert settled.session_id == "thread-603"
    assert "--- Meta ---" in settled.result
    assert "Tokens: 120 in / 30 out" in settled.result
