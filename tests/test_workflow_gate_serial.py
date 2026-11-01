"""Public admission, crash/replay, ownership ambiguity and recovery contract."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from click.testing import CliRunner
import pytest

from promptpilot import gate_execution, process_tree, workflows
from promptpilot.cli import cli
from promptpilot.models import WorkflowCreate, WorkflowStartRequest, WorkflowRoundCreate
from test_workflow_gate_failures import command
from test_workflow_pause_race import request


def gating(db, tmp_path, slug="serial", commands=None, **gate_options):
    item = db.create_workflow(WorkflowCreate(
        slug=slug, objective="Serial gate fixture", repository_path=str(tmp_path),
        candidate_branch="feature/fixture", config={
            "automation": {"enabled": True, "auto_dispatch_reviewer": False,
                           "auto_resume_revision": False},
            "gate": {"commands": commands or [command("print('gate OK')")], **gate_options},
        },
    ))
    workflows.start_workflow(item.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(item.id)
    run = db.list_workflow_runs(db.list_workflow_rounds(item.id)[-1].id)[-1]
    workflows.sync_task(run.task_id)
    db.mark_completed(run.task_id, "executor fixture complete", exit_code=0)
    db.set_verdict(run.task_id, "ГОТОВО")
    workflows.sync_task(run.task_id)
    result = db.get_workflow(item.id)
    assert result.status.value == "gating"
    return result


def journal(db, workflow_id):
    with db._connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM workflow_gate_executions WHERE workflow_id=? ORDER BY created_at",
            (workflow_id,),
        )]


def post(item, suffix="advance", **body):
    return request("POST", f"/api/workflows/{item.id}/{suffix}", **({"json": body} if body else {}))


def launch(db, item, phase="normal", marker=None):
    env = dict(os.environ, PP_DATA_DIR=str(db.DB_DIR), PYTHONIOENCODING="utf-8")
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("gate_serial_child.py")), phase, item.id,
         *([str(marker)] if marker else [])],
        cwd=str(db.DB_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )


def finish(child, expected=0):
    try:
        out, err = child.communicate(timeout=25)
        assert child.returncode == expected, (out, err)
        assert not err, err
        return json.loads(out) if out else None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def wait_for_file(path):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.05)
    assert path.exists(), f"fixture did not create {path}"


def assert_fenced(db, item):
    current = db.get_workflow(item.id)
    version = current.state_version
    for suffix, body in [
        ("gate", {"verdict": "PASS", "execution_id": "forged", "token": "forged"}),
        ("review", {"verdict": "PASS", "allow_no_diff": True}),
        ("human-input", {"text": "ordinary resume", "resume": True}),
        ("human-input", {"text": "note", "note": "edit context"}),
        ("human-input", {"text": "checkpoint bypass", "restart_checkpoint": True}),
        ("dispatch", {"role": "executor", "prompt": "do not launch"}),
    ]:
        response = post(item, suffix, expected_version=version, **body)
        assert response.status_code == 409, (suffix, response.text)
    response = request("PATCH", f"/api/workflows/{item.id}", json={
        "expected_version": version, "config": current.config, "allow_terminal_config": True,
    })
    assert response.status_code == 409, response.text
    with pytest.raises(db.WorkflowConflictError):
        db.create_workflow_round(WorkflowRoundCreate(workflow_id=item.id, round_no=2))
    with pytest.raises(sqlite3.IntegrityError):
        with db._connect(immediate=True) as conn:
            conn.execute("DELETE FROM workflows WHERE id=?", (item.id,))
    with pytest.raises(sqlite3.IntegrityError, match="history is retained"):
        with db._connect(immediate=True) as conn:
            conn.execute("DELETE FROM workflow_gate_executions WHERE workflow_id=?", (item.id,))
    assert db.get_workflow(item.id).state_version == version


def test_two_process_public_advance_and_unrelated_primary_writes(isolated_db, tmp_path):
    marker, release = tmp_path / "effects", tmp_path / "release"
    source = (
        "from pathlib import Path; import time; "
        f"p=Path({str(marker)!r}); stream=p.open('a'); stream.write('x'); stream.close(); "
        f"release=Path({str(release)!r}); "
        "deadline=time.monotonic()+12\n"
        "while not release.exists() and time.monotonic()<deadline: time.sleep(.05)\n"
    )
    item = gating(isolated_db, tmp_path, commands=[command(source)])
    other = gating(isolated_db, tmp_path, slug="other")
    first = launch(isolated_db, item)
    try:
        wait_for_file(marker)
        claimed = journal(isolated_db, item.id)[0]
        assert claimed["state"] == "STARTED"
        scope = json.loads(claimed["scope_json"])
        assert scope["executor"]["id"] and scope["executor"]["output_sha256"]
        assert claimed["admitted_version"] == claimed["observed_version"] + 1
        version = isolated_db.get_workflow(item.id).state_version
        started = time.monotonic()
        second = finish(launch(isolated_db, item))
        assert time.monotonic() - started < 6
        assert second["code"] == 200 and second["body"]["state_version"] == version
        assert journal(isolated_db, item.id)[0]["state"] == "STARTED"
        # These transactions would block if a primary writer were held by the shell.
        with isolated_db._connect(immediate=True) as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES ('fixture-write','ok')")
        assert post(other).json()["status"] == "reviewing"
        assert_fenced(isolated_db, item)
        assert post(item).json()["state_version"] == version
    finally:
        release.write_text("release", encoding="utf-8")
        result = finish(first)
    assert result["code"] == 200 and result["body"]["status"] == "reviewing"
    assert marker.read_text() == "x"
    assert journal(isolated_db, item.id)[0]["state"] == "APPLIED"
    assert gate_execution.lock_path(item.id).exists()
    events = isolated_db.list_workflow_events(item.id, limit=1000)
    assert sum(e.event_type == "gate.passed" for e in events) == 1
    assert not any(e.event_type in {"automation.paused", "gate.execution_unknown"} for e in events)


@pytest.mark.parametrize("phase,initial,effects", [
    ("before_commit", None, 0), ("started", "STARTED", 0),
    ("effects", "STARTED", 1), ("live_effects", "STARTED", 1),
    ("settled", "SETTLED", 1), ("applied", "APPLIED", 1),
])
def test_crash_windows_through_public_advance(isolated_db, tmp_path, phase, initial, effects):
    marker = tmp_path / "count"
    source = f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    if phase == "live_effects":
        source += "; import time; time.sleep(2)"
    item = gating(isolated_db, tmp_path, commands=[command(source)])
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(15)"],
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    try:
        finish(launch(isolated_db, item, phase, marker), expected=17)
        assert unrelated.poll() is None
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)
    rows = journal(isolated_db, item.id)
    assert (rows[0]["state"] if rows else None) == initial
    assert (len(marker.read_text()) if marker.exists() else 0) == effects
    first = post(item)
    second = post(item)
    assert first.status_code == second.status_code == 200
    assert first.json()["state_version"] == second.json()["state_version"]
    if initial == "STARTED":
        assert first.json()["status"] == "awaiting_human"
        assert journal(isolated_db, item.id)[0]["state"] == "UNKNOWN"
        assert (len(marker.read_text()) if marker.exists() else 0) == effects
        assert_fenced(isolated_db, item)
    else:
        assert first.json()["status"] == "reviewing"
        assert marker.read_text() == "x"
        assert journal(isolated_db, item.id)[0]["state"] == "APPLIED"
    events = isolated_db.list_workflow_events(item.id, limit=1000)
    assert sum(e.event_type == "gate.passed" for e in events) == (0 if initial == "STARTED" else 1)
    unknown = [e for e in events if e.event_type == "gate.execution_unknown"]
    assert len(unknown) == (1 if initial == "STARTED" else 0)
    if unknown:
        assert unknown[0].idempotency_key == f"gate.execution_unknown:{rows[0]['execution_id']}"


def test_one_use_exact_engineering_recovery_and_cli(isolated_db, tmp_path):
    marker = tmp_path / "recovered"
    item = gating(isolated_db, tmp_path, commands=[command(f"from pathlib import Path; Path({str(marker)!r}).write_text('new gate')")])
    finish(launch(isolated_db, item, "started"), expected=17)
    unknown = post(item).json()
    old = journal(isolated_db, item.id)[0]
    body = {"execution_id": old["execution_id"], "recovery_nonce": old["recovery_nonce"],
            "expected_version": unknown["state_version"], "reason": "Fixture owner exited; inspected private effects",
            "acknowledge_gate_effects_reviewed": True}
    for changes in [{"execution_id": "wrong"}, {"recovery_nonce": "wrong"},
                    {"expected_version": body["expected_version"] - 1}, {"reason": " "},
                    {"acknowledge_gate_effects_reviewed": False}]:
        assert post(item, "gate/recover", **(body | changes)).status_code == 409
    with gate_execution.execution_lock(item.id) as acquired:
        assert acquired
        assert post(item, "gate/recover", **body).status_code == 409
    recovered = CliRunner().invoke(cli, [
        "workflow", "recover-gate", item.id, "--execution-id", body["execution_id"],
        "--recovery-nonce", body["recovery_nonce"], "--expected-version", str(body["expected_version"]),
        "--reason", body["reason"], "--acknowledge-gate-effects-reviewed",
    ])
    assert recovered.exit_code == 0, recovered.output
    assert isolated_db.get_workflow(item.id).status.value == "gating"
    assert not marker.exists()
    resolved = journal(isolated_db, item.id)[0]
    resolution = json.loads(resolved["resolution_json"])
    assert resolved["state"] == "RESOLVED" and resolved["cleanup_json"] is None
    assert resolution["cleanup_confirmed"] is False and "server did not verify" in resolution["operator_acknowledgement"]
    assert post(item, "gate/recover", **body).status_code == 409
    assert post(item).json()["status"] == "reviewing"
    rows = journal(isolated_db, item.id)
    assert len(rows) == 2 and rows[1]["execution_id"] != old["execution_id"]
    assert rows[1]["state"] == "APPLIED" and marker.read_text() == "new gate"
    assert post(item, "gate/recover", **(body | {"expected_version": isolated_db.get_workflow(item.id).state_version})).status_code == 409


@pytest.mark.parametrize("boundary", ["factory", "cleanup", "timeout_cleanup", "base_exception", "settle"])
def test_uncertain_owned_boundaries_never_project_or_retry(isolated_db, tmp_path, monkeypatch, boundary):
    class FixtureInterruption(BaseException):
        pass

    # Factory/interruption probes deliberately lose the adapter return value.
    # Keep their finite private child alive until this fixture's retained owner
    # closes it, rather than racing an already exited process group on macOS.
    source = ("import time; time.sleep(30)" if boundary in {"factory", "base_exception"}
              else "import time; time.sleep(2)" if boundary == "timeout_cleanup" else "print('ok')")
    item = gating(isolated_db, tmp_path, timeout_seconds=1, commands=[command(source)])
    factories, owned = [], []
    original_start = process_tree.OwnedProcess.start
    original_close = process_tree.OwnedProcess.close

    def start(*args, **kwargs):
        # Durable admission is observable from another connection before factory.
        assert journal(isolated_db, item.id)[0]["state"] == "STARTED"
        factories.append(True)
        tree = original_start(*args, **kwargs)
        owned.append(tree)
        if boundary == "factory":
            raise OSError("factory raised after creating its finite private child")
        if boundary == "base_exception":
            raise FixtureInterruption("owned factory interrupted")
        return tree

    def close(tree):
        if boundary in {"cleanup", "timeout_cleanup"}:
            raise process_tree.ProcessTreeError("close evidence is uncertain")
        return original_close(tree)

    monkeypatch.setattr(process_tree.OwnedProcess, "start", start)
    monkeypatch.setattr(process_tree.OwnedProcess, "close", close)
    if boundary == "settle":
        def fail_settle(*args):
            raise sqlite3.OperationalError("outcome persistence failed")
        monkeypatch.setattr(gate_execution, "_settle_gate", fail_settle)
    try:
        if boundary == "base_exception":
            with pytest.raises(FixtureInterruption):
                # Exercise the public coordinator directly: ASGI task groups
                # wrap BaseException differently across supported Python versions.
                workflows.advance_workflow(item.id)
        else:
            response = post(item)
            assert response.status_code == 200
        response = post(item)
        assert response.json()["status"] == "awaiting_human"
        assert len(factories) == 1
        row = journal(isolated_db, item.id)[0]
        assert row["state"] == "UNKNOWN" and row["decision_json"] is None
        assert not any(e.event_type == "gate.passed" for e in isolated_db.list_workflow_events(item.id))
    finally:
        # Keep exact fixture handles and clean only these processes, even for
        # the deliberately ambiguous adapter cases; no PID-based takeover.
        for tree in owned:
            original_close(tree)
            tree.process.communicate(timeout=5)


def test_projection_rollback_and_replay_are_atomic(isolated_db, tmp_path, monkeypatch):
    marker = tmp_path / "atomic"
    item = gating(isolated_db, tmp_path, commands=[command(f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')")])
    original = workflows._record_gate

    def fail_after_projection(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("projection commit interrupted")

    monkeypatch.setattr(workflows, "_record_gate", fail_after_projection)
    assert post(item).json()["status"] == "gating"
    assert journal(isolated_db, item.id)[0]["state"] == "SETTLED"
    assert not any(e.event_type == "gate.passed" for e in isolated_db.list_workflow_events(item.id))
    monkeypatch.setattr(workflows, "_record_gate", original)
    assert post(item).json()["status"] == "reviewing"
    assert post(item).json()["status"] == "reviewing"
    assert marker.read_text() == "x"
    assert journal(isolated_db, item.id)[0]["state"] == "APPLIED"
    assert sum(e.event_type == "gate.passed" for e in isolated_db.list_workflow_events(item.id)) == 1


@pytest.mark.parametrize("transaction", ["claim", "settle", "projection"])
@pytest.mark.parametrize("committed", [False, True])
def test_primary_commit_errors_never_repeat_effects(isolated_db, tmp_path, monkeypatch, transaction, committed):
    marker = tmp_path / "commit-effect"
    item = gating(isolated_db, tmp_path, commands=[command(
        f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')")])
    original = isolated_db._connect
    failed = []

    @contextmanager
    def failing_connect(*args, **kwargs):
        matched = []

        class Connection:
            def execute(self, sql, *parameters):
                if ((transaction == "claim" and sql.startswith("INSERT INTO workflow_gate_executions"))
                        or (transaction == "settle" and "SET state='SETTLED'" in sql)
                        or (transaction == "projection" and "SET state='APPLIED'" in sql)):
                    matched.append(True)
                return conn.execute(sql, *parameters)

        with original(*args, **kwargs) as conn:
            yield Connection()
            if matched and not failed and not committed:
                failed.append(True)
                raise sqlite3.OperationalError("fixture commit failed before durability")
        if matched and not failed and committed:
            failed.append(True)
            raise sqlite3.OperationalError("fixture commit succeeded but acknowledgement was lost")

    monkeypatch.setattr(isolated_db, "_connect", failing_connect)
    first = post(item)
    assert first.status_code == 200 and failed
    monkeypatch.setattr(isolated_db, "_connect", original)
    if transaction == "claim":
        assert not marker.exists()
        if committed:
            assert journal(isolated_db, item.id)[0]["state"] == "STARTED"
            assert post(item).json()["status"] == "awaiting_human"
            assert journal(isolated_db, item.id)[0]["state"] == "UNKNOWN"
        else:
            assert not journal(isolated_db, item.id)
    else:
        assert marker.read_text() == "x"
        second = post(item)
        state = journal(isolated_db, item.id)[0]["state"]
        if transaction == "settle" and not committed:
            assert state == "UNKNOWN" and second.json()["status"] == "awaiting_human"
        else:
            assert state == "APPLIED" and second.json()["status"] == "reviewing"
        assert marker.read_text() == "x"


def test_cancel_preserves_uncertainty_and_no_old_pass(isolated_db, tmp_path):
    marker, release = tmp_path / "cancel-effects", tmp_path / "release"
    forbidden = tmp_path / "must-not-start-after-cancel"
    source = (f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('effect'); "
              f"p=Path({str(release)!r}); deadline=time.monotonic()+10\n"
              "while not p.exists() and time.monotonic()<deadline: time.sleep(.05)\n")
    item = gating(isolated_db, tmp_path, commands=[command(source), command(
        f"from pathlib import Path; Path({str(forbidden)!r}).touch()")])
    child = launch(isolated_db, item)
    try:
        wait_for_file(marker)
        current = isolated_db.get_workflow(item.id)
        assert post(item, "cancel", expected_version=current.state_version).json()["status"] == "cancelled"
    finally:
        release.write_text("release")
        result = finish(child)
    assert result["body"]["status"] == "cancelled"
    assert not forbidden.exists()
    assert post(item).json()["status"] == "cancelled"
    row = journal(isolated_db, item.id)[0]
    assert row["state"] == "UNKNOWN" and row["decision_json"] is not None
    assert json.loads(row["cleanup_json"])["cleanup_confirmed"] is True
    assert_fenced(isolated_db, item)


def test_scope_drift_after_settlement_cannot_apply_old_pass(isolated_db, tmp_path):
    item = gating(isolated_db, tmp_path)
    finish(launch(isolated_db, item, "settled"), expected=17)
    # Simulate a historical/non-cooperating writer without changing version.
    with isolated_db._connect(immediate=True) as conn:
        conn.execute("UPDATE workflow_runs SET output_sha256='changed' WHERE workflow_id=?", (item.id,))
    assert post(item).json()["status"] == "awaiting_human"
    row = journal(isolated_db, item.id)[0]
    assert row["state"] == "UNKNOWN" and row["projected_version"] is None
    assert not any(e.event_type == "gate.passed" for e in isolated_db.list_workflow_events(item.id))


def test_old_generation_fences_new_version_round_and_disabled_automation(isolated_db, tmp_path):
    item = gating(isolated_db, tmp_path)
    finish(launch(isolated_db, item, "started"), expected=17)
    old = journal(isolated_db, item.id)[0]
    # Model a legacy writer that bypassed admission. Neither the new version,
    # round nor automation flag is permission to discard the old ownership.
    with isolated_db._connect(immediate=True) as conn:
        conn.execute(
            "INSERT INTO workflow_rounds(id,workflow_id,round_no,status,started_at) "
            "VALUES ('fixture-new-round',?,2,'gating',?)", (item.id, isolated_db._now()),
        )
        config = item.config | {"automation": item.config["automation"] | {"enabled": False}}
        conn.execute("UPDATE workflows SET current_round=2,state_version=state_version+100,config_json=? WHERE id=?",
                     (json.dumps(config), item.id))
    unknown = post(item)
    assert unknown.status_code == 200 and unknown.json()["status"] == "awaiting_human"
    row = journal(isolated_db, item.id)[0]
    assert row["execution_id"] == old["execution_id"] and row["state"] == "UNKNOWN"
    assert post(item).json()["state_version"] == unknown.json()["state_version"]
    assert_fenced(isolated_db, item)
    assert post(item, "gate/recover", expected_version=unknown.json()["state_version"],
                execution_id=row["execution_id"], recovery_nonce=row["recovery_nonce"],
                reason="cannot relabel old round", acknowledge_gate_effects_reviewed=True).status_code == 409


@pytest.mark.parametrize("outcome", ["disabled", "empty", "invalid_cwd", "timeout", "fail", "pass"])
def test_public_outcomes_settle_with_honest_cleanup(isolated_db, tmp_path, monkeypatch, outcome):
    from promptpilot.models import WorkflowUpdate

    item = gating(isolated_db, tmp_path, timeout_seconds=1, commands=[command(
        "import time; time.sleep(2)" if outcome == "timeout" else
        "raise SystemExit(7)" if outcome == "fail" else "print('ok')")])
    if outcome in {"disabled", "empty"}:
        config = item.config | {"gate": item.config["gate"] | {
            "enabled": outcome != "disabled", "commands": [],
        }}
        item = isolated_db.update_workflow(item.id, WorkflowUpdate(
            expected_version=item.state_version, config=config))
    if outcome == "invalid_cwd":
        with isolated_db._connect(immediate=True) as conn:
            conn.execute("UPDATE workflows SET repository_path=? WHERE id=?", (str(tmp_path / "missing"), item.id))
    if outcome in {"disabled", "empty", "invalid_cwd"}:
        def unexpected_factory(*args, **kwargs):
            raise AssertionError("pre-factory outcome must never create a process")
        monkeypatch.setattr(process_tree.OwnedProcess, "start", unexpected_factory)
    response = post(item)
    assert response.status_code == 200
    expected = ("reviewing" if outcome in {"disabled", "pass"} else
                "revision_required" if outcome in {"fail", "timeout"} else "awaiting_human")
    assert response.json()["status"] == expected
    row = journal(isolated_db, item.id)[0]
    assert row["state"] == "APPLIED" and json.loads(row["cleanup_json"])["cleanup_confirmed"] is True
    assert row["projected_version"] == row["admitted_version"] + 1
    event_type = "gate.passed" if expected == "reviewing" else "gate.failed" if expected == "revision_required" else "gate.human_required"
    events = [e for e in isolated_db.list_workflow_events(item.id) if e.event_type == event_type]
    assert len(events) == 1 and events[0].payload["execution_id"] == row["execution_id"]


def test_stale_observation_is_not_re_admitted(isolated_db, tmp_path, monkeypatch):
    marker = tmp_path / "stale"
    item = gating(isolated_db, tmp_path, commands=[command(f"from pathlib import Path; Path({str(marker)!r}).touch()")])
    original = gate_execution.execution_lock

    @contextmanager
    def changed_before_admission(workflow_id):
        current = isolated_db.get_workflow(workflow_id)
        from promptpilot.models import WorkflowUpdate
        isolated_db.update_workflow(workflow_id, WorkflowUpdate(expected_version=current.state_version,
                                                               config=current.config))
        with original(workflow_id) as acquired:
            yield acquired

    monkeypatch.setattr(gate_execution, "execution_lock", changed_before_admission)
    assert post(item).json()["status"] == "gating"
    assert not marker.exists() and not journal(isolated_db, item.id)


def test_same_process_connections_contend_and_lock_is_stable(isolated_db, tmp_path):
    item = gating(isolated_db, tmp_path)
    with gate_execution.execution_lock(item.id) as acquired:
        assert acquired
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(post, item).result(timeout=10)
        assert response.status_code == 200 and response.json()["state_version"] == item.state_version
        assert not journal(isolated_db, item.id)
    path = gate_execution.lock_path(item.id)
    assert path.exists()
    assert post(item).json()["status"] == "reviewing"
    assert path.exists()


def test_interrupted_lock_acquisition_closes_its_connection(isolated_db, tmp_path, monkeypatch):
    class Interrupted(BaseException):
        pass

    item = gating(isolated_db, tmp_path)
    original = sqlite3.connect
    closed = []

    class InterruptedConnection:
        def execute(self, sql):
            raise Interrupted("interrupted before auxiliary admission")

        def close(self):
            closed.append(True)

    def connect(path, *args, **kwargs):
        if path == str(gate_execution.lock_path(item.id)):
            return InterruptedConnection()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(Interrupted):
        workflows.advance_workflow(item.id)
    assert closed == [True] and not journal(isolated_db, item.id)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_established_lock_failure_is_closed(isolated_db, tmp_path, damage):
    item = gating(isolated_db, tmp_path)
    finish(launch(isolated_db, item, "started"), expected=17)
    path = gate_execution.lock_path(item.id)
    if damage == "missing":
        path.unlink()  # Deliberate fixture damage; production never unlinks it.
    else:
        path.write_bytes(b"not sqlite")
    response = post(item)
    assert response.status_code == 409
    assert journal(isolated_db, item.id)[0]["state"] == "STARTED"
