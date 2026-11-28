import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from promptpilot import pipeline_budget_status as status, pipeline_insights as pi
from promptpilot.models import TaskCreate
from tests.test_github_budget import _limits
from tests.test_pipeline_source_refresh import continuation as continuation


@pytest.fixture
def budget(continuation, monkeypatch):
    f = continuation
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: pytest.fail("passive observation contacted GitHub"))
    f.policy = status.policy_sha256({"example": f.profile}, "example")
    f.observe = lambda: status.observe("example", f.task.series_id, f.policy, f.env)
    return f


def test_ready_receipt_uses_actual_rates_and_charges_existing_cost_once(budget):
    f = budget
    assert f.db.record_pipeline_github_rate_snapshot("github-default", _limits())
    with f.db._connect() as conn:
        before = list(conn.iterdump())
    receipt = f.observe()
    assert receipt["state"] == "ready" and receipt["ready"] is True
    assert receipt["proof_kind"] == "running" and receipt["budget_observation_scope"] == "running-budget-policy-v4"
    assert receipt["started_at"] == f.task.started_at.isoformat() and receipt["task_id"] == f.task.id
    assert receipt["profile_hash"] == pi._profile_fingerprint(f.profile) and len(receipt["receipt_id"]) == 32
    decision = receipt["decision"]
    assert decision["budget_route"] == "skill" and decision["requested_cost"]["search"] == 0
    assert decision["reserved_other"]["search"] == 12 and decision["effective_after"]["search"] == 18
    assert decision["minimum_remaining"]["search"] == 12 and decision["allowed"] is True
    assert all(receipt[k] is False for k in ("granted", "native_admission_granted", "source_snapshot_observed",
                                              "native_source_admission_granted", "mutable_admission_granted"))
    assert "existing-skill" not in json.dumps(receipt) and "actual-session" not in json.dumps(receipt)
    with f.db._connect() as conn:
        assert before == list(conn.iterdump())


def test_active_foreign_promises_are_counted_and_low_budget_waits(budget):
    f = budget
    f.db.create_task(TaskCreate(prompt="foreign", priority=3))
    other = f.db.get_next_runnable()
    limits = _limits()
    assert f.db.reserve_pipeline_github_budget("github-default", token="foreign-promise",
        task_id=other.id, task_started_at=other.started_at, profile_id="other", queue_id="review",
        route="skill", cost={"core": 20, "search": 8, "graphql": 20}, limits=limits,
        minimum_remaining={"core": 1, "search": 1, "graphql": 1})["allowed"]
    assert f.db.record_pipeline_github_rate_snapshot("github-default", limits)
    receipt = f.observe()
    assert receipt["state"] == "wait" and receipt["decision"]["state"] == "budget_in_flight"
    assert receipt["decision"]["reserved_other"]["search"] == 20 and receipt["ready"] is False
    assert "foreign-promise" not in json.dumps(receipt)


@pytest.mark.parametrize("fault,reason", [
    ("no_rate", "rate_snapshot_stale"), ("old_rate", "rate_snapshot_stale"),
    ("future_rate", "rate_snapshot_stale"), ("reset", "rate_window_expired"),
    ("started", "worker_start_environment_changed"), ("series", "running_task_identity_changed"),
    ("priority", "running_task_identity_changed"), ("policy", "effective_budget_policy_changed"),
    ("own", "observation_unavailable"), ("corrupt_ledger", "observation_unavailable"),
    ("session", "observation_unavailable")])
def test_stale_rates_and_changed_attempt_cannot_report_ready(budget, fault, reason):
    f = budget
    now = time.time()
    rates = _limits(reset=int(now) - 1 if fault == "reset" else None)
    if fault != "no_rate":
        assert f.db.record_pipeline_github_rate_snapshot("github-default", rates,
            observed_at=now + {"old_rate": -61, "future_rate": 60}.get(fault, 0))
    series_id = f.task.series_id
    if fault == "started":
        f.env["PP_TASK_STARTED_AT"] = "other"
    elif fault == "series":
        series_id += 1
    elif fault == "priority":
        with f.db._connect() as conn:
            conn.execute("UPDATE tasks SET priority=2 WHERE id=?", (f.task.id,))
    elif fault == "policy":
        f.profile["github_budget"]["minimum_remaining"]["search"] += 1
    elif fault == "own":
        assert f.db.release_pipeline_github_budget("github-default", token="existing-skill",
            task_id=f.task.id, task_started_at=f.task.started_at)
    elif fault == "corrupt_ledger":
        f.db.set_setting(f.db._pipeline_github_budget_reservation_key("github-default"), "private-invalid")
    elif fault == "session":
        with f.db._connect() as conn:
            conn.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (f.task.id,))
    result = status.observe("example", series_id, f.policy, f.env)
    assert result["state"] == "refused" and result["reason"] == reason and result["ready"] is False
    assert "receipt_id" not in result and "private-invalid" not in json.dumps(result)


def test_policy_pin_includes_other_profiles_sharing_account(budget):
    other = {"github_budget": {"minimum_remaining": {"core": 4999}}}
    assert budget.policy != status.policy_sha256({"example": budget.profile, "other": other}, "example")


@pytest.mark.parametrize("resource", ["core", "search", "graphql"])
def test_physically_impossible_stored_rate_cannot_report_ready(budget, resource):
    f = budget
    assert f.db.record_pipeline_github_rate_snapshot("github-default", _limits())
    key = f.db._pipeline_github_rate_snapshot_key("github-default")
    snapshot = json.loads(f.db.get_setting(key))
    snapshot["limits"][resource]["used"] += 1
    raw = json.dumps(snapshot)
    f.db.set_setting(key, raw)
    receipt = f.observe()
    assert receipt["state"] == "refused" and receipt["ready"] is False
    assert receipt["reason"] == "rate_snapshot_invalid" and "receipt_id" not in receipt
    assert f.db.get_setting(key) == raw


def test_cold_cli_keeps_database_read_only_and_normal_imports_still_initialize(tmp_path, budget):
    data = tmp_path / "cold"
    data.mkdir()
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"profiles": {"example": budget.profile}}), encoding="utf-8")
    env = {**os.environ, "PP_DATA_DIR": str(data), "PP_PIPELINE_PROFILES": str(profiles), "PP_TASK_ID": "1"}
    command = [sys.executable, "main.py", "pipelinectl", "budget-status", "--profile", "example",
               "--series-id", "1", "--policy-sha256", budget.policy]
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(command, capture_output=True, cwd=root, env=env, check=False)
    assert result.returncode == 2 and json.loads(result.stdout)["reason"] == "observation_unavailable"
    assert list(data.iterdir()) == []
    database = data / "promptpilot.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sentinel(marker)")
    before = database.read_bytes()
    assert subprocess.run(command, capture_output=True, cwd=root, env=env).returncode == 2
    assert database.read_bytes() == before and list(data.iterdir()) == [database]
    # An embedded client may first inspect policy and later open a regular Native service.
    script = ("import json,os; from pathlib import Path; from promptpilot.pipeline_budget_status import policy_sha256; "
              "policy_sha256(json.loads(Path(os.environ['PP_PIPELINE_PROFILES']).read_bytes())['profiles'],'example'); "
              "from promptpilot import db; assert db.get_setting('after-passive-import') is None")
    subprocess.run([sys.executable, "-c", script], cwd=root, env=env, check=True, capture_output=True)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone()
