"""Website orders through HTTP dispatch and the actual owning worker."""
import asyncio
import copy
import io
import json
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from promptpilot import api, config, db, worker, workorder_guard, workorder_private

_REAL_GUARD_OPEN = workorder_guard._open


@pytest.fixture
def order(isolated_db, monkeypatch, tmp_path):
    providers = copy.deepcopy(config.load_providers())
    providers["guarded"] = {"cmd": "claude -p {prompt}", "kind": "claude", "env": {
        "WORKORDERS_WORKER_TOKEN": "not-for-child", "WORKORDERS_SITE_URL": "not-for-child"}}
    providers["session"] = {"cmd": "unused", "executor": "herdr"}
    for module in (api, config, worker):
        monkeypatch.setattr(module, "load_providers", lambda: providers)
    monkeypatch.setattr(config, "GUARD", "off")
    monkeypatch.setattr(worker.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_active_provider_trees", {})
    monkeypatch.setattr(api, "API_TOKEN", "test-api-token")
    protected = []
    monkeypatch.setattr(workorder_private, "protect_directory", lambda path: protected.append(path))
    repository = tmp_path / "repository"
    repository.mkdir()
    case = SimpleNamespace(providers=providers, protected=protected, repository=repository,
                           requests=[], workflow=None, task=None, before_response=None)

    def request(method, path, data=None):
        async def perform():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                         base_url="http://localhost",
                                         headers={"Authorization": "Bearer test-api-token"}) as client:
                return await client.request(method, path, json=data)
        return asyncio.run(perform())

    def rpc(request, timeout):
        assert 0 < timeout <= 8
        operation = request.full_url.rstrip("/").rsplit("/", 1)[1]
        case.requests.append((operation, json.loads(request.data)))
        if case.before_response is not None:
            case.before_response(operation)
        accepted = case.approve(operation)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return request.full_url

            def read(self, _limit):
                return json.dumps({"approved" if operation == "reserve" else "continue": accepted}).encode()
        return Response()

    def create(guard=None):
        guard = guard if guard is not None else {
            "site_url": "https://orders.invalid", "run_id": "run-1",
            "worker_key": "test-worker", "lease_token": "test-lease"}
        monkeypatch.setenv("WORKORDERS_WORKER_TOKEN", "test-worker-token")
        monkeypatch.setenv("WORKORDERS_SITE_URL", guard["site_url"])
        result = request("POST", "/api/integrations/workorders/ensure", {
            "request_id": "test-order", "workflow": {
                "slug": "test-order", "objective": "Approved work", "repository_path": str(repository),
                "candidate_branch": "main", "config": {
                    "automation": {"enabled": False}, "workorder_guard": guard}}})
        if result.status_code == 200:
            case.workflow = result.json()
            started = request("POST", f"/api/workflows/{case.workflow['id']}/start",
                              {"expected_version": case.workflow["state_version"]})
            assert started.status_code == 200, started.text
            case.workflow = started.json()
        return result

    def dispatch(**changes):
        result = request("POST", f"/api/workflows/{case.workflow['id']}/dispatch", {
            "expected_version": case.workflow["state_version"], "role": "executor",
            "prompt": "Approved task", "provider": "guarded", "max_retries": 0,
            "task_timeout": 60, **changes})
        if result.status_code == 200:
            case.workflow = result.json()["workflow"]
            case.task = db.get_task(result.json()["task"]["id"])
        return result

    def claim():
        task = db.get_next_runnable()
        assert task is not None and task.id == case.task.id
        case.task = task
        return task

    case.request, case.create, case.dispatch, case.claim = request, create, dispatch, claim
    case.approve = lambda _operation: True
    monkeypatch.setattr(workorder_guard, "_open", rpc)
    return case


def install_owned_fake(monkeypatch, *, pulse=None, cleanup_fails=False):
    state = {"calls": [], "events": [], "cleanup_fails": cleanup_fails, "pulse": pulse}

    class Process:
        pid = 987654
        returncode = None

        def __init__(self):
            self.stdout = io.StringIO("ИТОГ: ГОТОВО\n")
            self.stderr = io.StringIO()
            self.stdin = None

        def wait(self, timeout=None):
            if state["pulse"] is not None:
                current, state["pulse"] = state["pulse"], None
                current()
                raise subprocess.TimeoutExpired("fake-owned", timeout)
            self.returncode = 0
            return 0

    class Tree:
        def __init__(self):
            self.process = Process()

        def terminate(self):
            state["events"].append("stop")

        def close(self):
            state["events"].append("close")
            if state["cleanup_fails"]:
                raise RuntimeError("private unconfirmed cleanup")

    def factory(command, **kwargs):
        state["calls"].append((command, kwargs))
        return Tree()

    monkeypatch.setattr(worker.OwnedProcess, "start", factory)
    return state


@pytest.mark.parametrize("changes", [
    {"rights": "full"}, {"execution_mode": "external"}, {"skip_permissions": True},
    {"provider": "session"}, {"worktree": True}, {"max_retries": 1},
])
def test_public_dispatch_refuses_unsafe_workorder_execution(order, changes):
    assert order.create().status_code == 200
    assert order.dispatch(**changes).status_code == 409
    assert not db.list_tasks()


@pytest.mark.parametrize("change", ["provider", "working_dir", "model", "provider_policy"])
def test_actual_worker_rejects_bound_policy_drift_before_factory(order, monkeypatch, tmp_path, change):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    if change == "provider_policy":
        order.providers["guarded"]["executor"] = "herdr"
    else:
        values = {"provider": "session", "working_dir": str(tmp_path), "model": "another-model"}
        result = order.request("PATCH", f"/api/tasks/{order.task.id}", {change: values[change]})
        assert result.status_code == 200, result.text
    task = order.claim()
    state = install_owned_fake(monkeypatch)
    monkeypatch.setattr(worker, "_execute_herdr_task", lambda *_args, **_kwargs: pytest.fail("guard bypass"))
    worker.execute_task(task)
    assert not state["calls"] and not order.requests
    assert db.get_task(task.id).status.value == "failed"


def test_public_config_patch_cannot_remove_or_replace_guard(order):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    value = copy.deepcopy(order.workflow["config"])
    value.pop("workorder_guard")
    result = order.request("PATCH", f"/api/workflows/{order.workflow['id']}", {
        "expected_version": order.workflow["state_version"], "config": value})
    assert result.status_code == 409


def test_worker_preserves_guard_origin_even_if_current_config_is_damaged(order, monkeypatch):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    with db._connect(immediate=True) as connection:
        value = copy.deepcopy(order.workflow["config"])
        value.pop("workorder_guard")
        connection.execute("UPDATE workflows SET config_json=? WHERE id=?",
                           (json.dumps(value), order.workflow["id"]))
    state = install_owned_fake(monkeypatch)
    worker.execute_task(order.claim())
    assert not state["calls"] and not order.requests
    assert db.get_task(order.task.id).status.value == "failed"


def test_real_worker_launch_is_narrow_scrubbed_and_exactly_reserved(order, monkeypatch):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    state = install_owned_fake(monkeypatch)
    worker.execute_task(order.claim())
    assert len(state["calls"]) == 1
    command, options = state["calls"][0]
    assert "--restricted" in command and "--dangerously-skip-permissions" not in command
    assert options["cwd"] == str(order.repository)
    assert not any(key.startswith("WORKORDERS_") for key in options["env"])
    assert "PP_API_TOKEN" not in options["env"] and "PP_TG_TOKEN" not in options["env"]
    assert [operation for operation, _ in order.requests] == ["reserve", "check", "check"]
    assert {payload["stage_key"] for _, payload in order.requests} == {
        f"task:{order.task.id}:{order.task.started_at.isoformat()}"}
    assert db.get_task(order.task.id).status.value == "completed"


def test_profile_change_during_authorization_refuses_spawn(order, monkeypatch):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    state = install_owned_fake(monkeypatch)
    def change_profile(operation):
        if operation == "check":
            order.providers["guarded"]["executor"] = "herdr"
    order.before_response = change_profile
    worker.execute_task(order.claim())
    assert not state["calls"]
    assert db.get_task(order.task.id).status.value == "failed"


@pytest.mark.parametrize("which", ["argv", "environment"])
def test_prepared_provider_buffers_use_the_bound_snapshot(order, monkeypatch, which):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    approved = copy.deepcopy(order.providers["guarded"])
    name = "build_cmd" if which == "argv" else "get_provider_env"
    original = getattr(worker, name)

    def aba(*args, **kwargs):
        order.providers["guarded"] = {
            **approved, "cmd": "C:/private/unapproved/claude.exe -p {prompt}",
            "env": {**approved["env"], "UNAPPROVED_PROVIDER_SECRET": "synthetic"}}
        try:
            return original(*args, **kwargs)
        finally:
            order.providers["guarded"] = copy.deepcopy(approved)

    monkeypatch.setattr(worker, name, aba)
    state = install_owned_fake(monkeypatch)
    worker.execute_task(order.claim())
    assert len(state["calls"]) == 1
    command, options = state["calls"][0]
    assert not command[0].startswith("C:/private/unapproved/")
    assert "UNAPPROVED_PROVIDER_SECRET" not in options["env"]
    assert db.get_task(order.task.id).status.value == "completed"


def test_public_guarded_task_cannot_become_recurring(order):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    result = order.request("PATCH", f"/api/tasks/{order.task.id}", {"recurrence": "1h"})
    assert result.status_code == 409
    assert not db.get_task(order.task.id).recurrence


def test_restart_does_not_backfill_or_execute_guarded_recurrence(order, monkeypatch):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    with db._connect() as conn:
        conn.execute("UPDATE tasks SET recurrence='1h' WHERE id=?", (order.task.id,))
    db.init_db()
    assert db.get_task(order.task.id).series_id is None
    state = install_owned_fake(monkeypatch)
    worker.execute_task(order.claim())
    assert not state["calls"]
    assert db.get_task(order.task.id).status.value == "failed"
    assert len(db.list_tasks()) == 1


@pytest.mark.parametrize("retention", ["delete", "purge"])
def test_legacy_guarded_series_stays_refused_after_root_retention(order, monkeypatch, retention):
    from promptpilot.models import TaskCreate

    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    # Simulate a pre-fix legacy series, including an orphan successor with no run.
    root_id = order.task.id
    with db._connect() as conn:
        series_id = db._create_series(conn, TaskCreate(prompt="legacy", recurrence="1h"))
        conn.execute("UPDATE tasks SET series_id=?, recurrence='1h' WHERE id=?",
                     (series_id, root_id))
    follower = db.create_task(TaskCreate(prompt="legacy successor", provider="guarded",
                                        recurrence="1h", series_id=series_id))
    db.mark_failed(root_id, "legacy terminal")
    if retention == "delete":
        assert db.delete_task(root_id)
    else:
        with db._connect() as conn:
            conn.execute("UPDATE tasks SET completed_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                         (root_id,))
        assert db.purge_old() == 1
    db.init_db()
    state = install_owned_fake(monkeypatch)
    order.approve = lambda _operation: False
    task = db.get_next_runnable()
    assert task.id == follower.id
    worker.execute_task(task)
    assert not state["calls"] and not order.requests
    assert db.get_task(task.id).status.value == "failed"
    assert db.create_series_occurrence_if_idle(TaskCreate(
        prompt="legacy successor", recurrence="1h", series_id=series_id)) is None
    assert db.repair_active_series_occurrences() == []
    assert len(db.list_tasks()) == 1


@pytest.mark.parametrize("failure", ["cleanup", "terminal-write"])
def test_lease_loss_reaches_owning_worker_quarantine_before_terminal(order, monkeypatch, failure):
    assert order.create().status_code == 200
    assert order.dispatch().status_code == 200
    real_clock = time.monotonic
    offset = [0]
    monkeypatch.setattr(workorder_guard.time, "monotonic", lambda: real_clock() + offset[0])
    state = install_owned_fake(monkeypatch, pulse=lambda: offset.__setitem__(0, 11),
                               cleanup_fails=failure == "cleanup")
    order.approve = lambda operation: operation == "reserve" or sum(
        name == "check" for name, _ in order.requests) == 1
    real_fail = db.mark_failed
    writes = []
    def mark_failed(*args, **kwargs):
        writes.append(list(state["events"]))
        if failure == "terminal-write" and len(writes) == 1:
            raise sqlite3.OperationalError("private writer busy")
        return real_fail(*args, **kwargs)
    monkeypatch.setattr(db, "mark_failed", mark_failed)
    task = order.claim()
    with pytest.raises(worker.ProviderOwnershipError) as pending:
        worker.execute_task(task)
    assert len(state["calls"]) == 1
    assert sum(operation == "check" for operation, _ in order.requests) == 2
    assert "stop" in state["events"]
    assert db.get_task(task.id).status.value == "running"
    if failure == "cleanup":
        assert not writes
        assert not worker._fail_stuck(task, pending.value)
        state["cleanup_fails"] = False
    assert worker._fail_stuck(task, pending.value)
    assert all("stop" in before_write for before_write in writes)
    assert db.get_task(task.id).status.value == "failed"


@pytest.mark.parametrize("changes", [
    {"site_url": "http://remote.invalid"}, {"site_url": "ftp://localhost"},
    {"site_url": "https://user:password@orders.invalid"}, {"site_url": "https://orders.invalid?a=b"},
    {"run_id": "../other"}, {"lease_token": "value\nInjected: header"}, {"extra": "unknown"},
])
def test_public_creation_validates_guard_before_private_storage_changes(order, changes):
    guard = {"site_url": "https://orders.invalid", "run_id": "run-1",
             "worker_key": "test-worker", "lease_token": "test-lease", **changes}
    assert order.create(guard).status_code == 422
    assert not order.protected and not db.list_workflows()


@pytest.mark.parametrize("code", [302, 307, 308])
def test_authorization_redirect_never_sends_credentials_to_another_origin(order, monkeypatch, code):
    received = []
    destination = [""]

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"approved":true,"continue":true}')

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(code)
            self.send_header("Location", destination[0])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Target) as target, \
            ThreadingHTTPServer(("127.0.0.1", 0), Redirect) as redirect:
        destination[0] = f"http://127.0.0.1:{target.server_port}/not-the-approved-origin/"
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (target, redirect)]
        for thread in threads:
            thread.start()
        try:
            guard = {"site_url": f"http://127.0.0.1:{redirect.server_port}", "run_id": "run-1",
                     "worker_key": "test-worker", "lease_token": "test-lease"}
            assert order.create(guard).status_code == 200
            assert order.dispatch().status_code == 200
            monkeypatch.setattr(workorder_guard, "_open", _REAL_GUARD_OPEN)
            with pytest.raises(workorder_guard.WorkorderAuthorizationError):
                workorder_guard.check_task(order.task.id, reserve=True)
            assert not received
        finally:
            target.shutdown()
            redirect.shutdown()
            for thread in threads:
                thread.join(timeout=2)
                assert not thread.is_alive()
