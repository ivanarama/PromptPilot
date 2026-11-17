"""Public budget-refresh CLI: real temporary Native DB/guards, fake GitHub I/O."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from promptpilot import entrypoint, pipeline_insights as pi
from promptpilot.models import TaskCreate
from tests.test_github_budget import _limits
from tests.test_pipeline_source_refresh import (
    continuation as continuation, ledger, targeted_merge as targeted_merge,
)

_COLLECT_RATES = pi._github_rate_limits


@pytest.fixture
def rates(continuation, monkeypatch):
    f = continuation
    f.profile["priority_control"] = {"trusted_account": "fixture-owner"}
    state = SimpleNamespace(calls=[], limits=_limits(), fault=None, after_response=None)
    monkeypatch.setattr(pi, "_github_rate_limits", _COLLECT_RATES)
    monkeypatch.setattr(pi, "_gh_executable", lambda: "fixture-gh")

    def external(args, **kwargs):
        assert args[:2] == ["fixture-gh", "api"]
        endpoint = args[2:]
        state.calls.append(endpoint)
        if endpoint == ["rate_limit"]:
            value = {"resources": {r: state.limits[r] for r in ("core", "search")}}
            if state.fault == "partial-rest":
                value["resources"].pop("search")
        else:
            assert endpoint == ["graphql", "--input", "-"]
            assert json.loads(kwargs["input"])["query"] == pi._GITHUB_RATE_LIMIT_QUERY
            q = state.limits["graphql"]
            value = {"data": {"viewer": {"login": "fixture-owner"}, "rateLimit": {
                "limit": q["limit"], "remaining": q["remaining"], "used": q["used"],
                "resetAt": datetime.fromtimestamp(q["reset"], timezone.utc).isoformat()}}}
            if state.fault == "partial-graphql":
                value["data"].pop("rateLimit")
            elif state.fault == "foreign-account":
                value["data"]["viewer"]["login"] = "foreign-account"
        if state.after_response:
            state.after_response(endpoint)
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    monkeypatch.setattr(subprocess, "run", external)
    for name in ("_analyze_without_budget", "_run_profile_health_check", "_github_search"):
        monkeypatch.setattr(pi, name, lambda *a, **kw: pytest.fail("rate-only entered Source work"))
    old = {"version": 1, "observed_at": time.time() - 200, "limits": _limits()}
    f.db.set_setting(f.db._pipeline_github_rate_snapshot_key("github-default"), json.dumps(old))
    fingerprint = pi._profile_fingerprint(f.profile)
    for key, value in {
            pi._CACHE_EPOCH_KEY: "71", pi._cache_key("example", fingerprint): "old source bytes",
            pi._published_profile_key("example"): fingerprint,
            pi._published_profile_revision_key("example"): "17",
            pi._published_revision_key("example", fingerprint): "17",
            f.db._pipeline_refresh_status_key("example"): '{"version":1,"keep":"source status"}',
            "pipeline_item_holds:v1:2": '{"111":{"reason":"preserve"}}'}.items():
        f.db.set_setting(key, value)
    return f, state


def source_image(f):
    with f.db._connect() as c:
        return {
            "settings": [tuple(r) for r in c.execute(
                "SELECT key,value FROM settings WHERE key LIKE 'pipeline_insights_%' "
                "OR key LIKE 'pipeline_github_refresh_status:%' "
                "OR key LIKE 'pipeline_item_hold%' ORDER BY key")],
            "history": [tuple(r) for r in c.execute("SELECT * FROM pipeline_snapshots ORDER BY id")],
            "series": [tuple(r) for r in c.execute("SELECT * FROM task_series ORDER BY id")],
            "task_ids": [r[0] for r in c.execute("SELECT id FROM tasks ORDER BY id")],
        }


def invoke(f, monkeypatch, capsys, profile="example"):
    for key, value in f.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PP_TASK_STARTED_AT", f.task.started_at.isoformat())
    monkeypatch.setattr(sys, "argv", ["pp", "pipelinectl", "budget-refresh", "--profile", profile])
    with pytest.raises(SystemExit) as stopped:
        entrypoint.main()
    return stopped.value.code, json.loads(capsys.readouterr().out)


def no_grants(result):
    for key in ("source_refreshed", "source_admission_granted", "native_source_admission_granted",
                "native_admission_granted", "mutable_admission_granted", "granted",
                "product_mutation_authorized"):
        assert result[key] is False


def test_public_cli_refreshes_real_rate_cache_with_full_cost_and_no_source(rates, monkeypatch, capsys):
    f, state = rates
    before, reserved = source_image(f), ledger(f)
    code, result = invoke(f, monkeypatch, capsys)
    assert code == 0 and result["state"] == "refreshed" and result["rate_snapshot_refreshed"] is True
    assert result["admitted_observation"] is True
    saved = f.db.get_pipeline_github_rate_snapshot("github-default")
    assert saved == result["rate_snapshot"] and time.time() - saved["observed_at"] < 5
    assert saved["limits"] == state.limits and len(state.calls) == 2
    assert result["budget"]["requested_cost"] == {"core": 50, "search": 10, "graphql": 100}
    assert result["budget"]["reserved_other"] == {"core": 1200, "search": 12, "graphql": 1000}
    assert result["budget"]["minimum_remaining"]["search"] == 2
    assert result["budget"]["admission_priority"] == 1
    assert result["existing_execution_reservation_reused"] is True
    assert source_image(f) == before and ledger(f) == reserved and f.checks == f.searches == []
    assert "actual-session" not in json.dumps(result) and "existing-skill" not in json.dumps(result)
    no_grants(result)


def test_targeted_route_keeps_its_exact_existing_promise_once(targeted_merge, rates, monkeypatch, capsys):
    f, _state = rates
    assert f is targeted_merge
    before, reserved = source_image(f), ledger(f)
    code, result = invoke(f, monkeypatch, capsys)
    assert code == 0 and result["admitted_route"] == "fallback_targeted"
    assert result["budget"]["reserved_other"]["search"] == 8
    assert result["budget"]["requested_cost"]["search"] == 10
    assert result["existing_skill_reservation_reused"] is False
    assert source_image(f) == before and ledger(f) == reserved
    no_grants(result)


@pytest.mark.parametrize("condition", ["lower-priority", "live-foreign", "low-quota", "busy-lease"])
def test_denial_keeps_cost_floors_foreign_promises_and_source(rates, monkeypatch, capsys, condition):
    f, state = rates
    if condition == "lower-priority":
        with f.db._connect() as c:
            c.execute("UPDATE tasks SET priority=2 WHERE id=?", (f.task.id,))
    elif condition == "live-foreign":
        f.db.create_task(TaskCreate(prompt="foreign", priority=3))
        other = f.db.get_next_runnable()
        assert f.db.reserve_pipeline_github_budget("github-default", token="foreign-reserve",
            task_id=other.id, task_started_at=other.started_at, profile_id="foreign", queue_id="other",
            route="skill", cost={"core": 100, "search": 7, "graphql": 50}, limits=_limits(),
            minimum_remaining={"core": 250, "search": 2, "graphql": 100})["allowed"]
    elif condition == "low-quota":
        state.limits = _limits(core=100)
    else:
        assert f.db.acquire_pipeline_scan_lease("github-default", "foreign-scan", 60)["acquired"]
    before, reserved = source_image(f), ledger(f)
    code, result = invoke(f, monkeypatch, capsys)
    assert code == 3 and result["state"] == "wait" and result["rate_snapshot_refreshed"] is False
    assert result["admitted_observation"] is False and result["rate_cache_write_not_excluded"] is True
    if condition == "lower-priority":
        assert result["budget"]["minimum_remaining"]["search"] == 14
    elif condition == "live-foreign":
        assert result["budget"]["reserved_other"]["search"] == 19
        assert result["budget"]["state"] == "budget_in_flight"
    elif condition == "busy-lease":
        assert state.calls == []
        with f.db._connect() as c:
            lease = json.loads(c.execute("SELECT value FROM settings WHERE key=?",
                (f.db._pipeline_scan_lease_key("github-default"),)).fetchone()[0])
        assert lease["token"] == "foreign-scan"
    if condition != "busy-lease":
        # Canonical admission collected and saved true rates before discovering
        # that the unchanged configured estimate did not fit. No false no-write claim.
        assert time.time() - f.db.get_pipeline_github_rate_snapshot("github-default")["observed_at"] < 5
    assert source_image(f) == before and ledger(f) == reserved
    no_grants(result)


@pytest.mark.parametrize("fault", ["partial-rest", "partial-graphql", "foreign-account"])
def test_partial_or_foreign_account_observation_never_replaces_rate_cache(rates, monkeypatch, capsys, fault):
    f, state = rates
    state.fault = fault
    old_rate = f.db.get_pipeline_github_rate_snapshot("github-default")
    before = source_image(f)
    code, result = invoke(f, monkeypatch, capsys)
    assert code == 2 and result["state"] == "refused"
    assert f.db.get_pipeline_github_rate_snapshot("github-default") == old_rate
    assert source_image(f) == before
    no_grants(result)


@pytest.mark.parametrize("fault", ["missing-session", "missing-reserve", "wrong-cost", "global-pause",
                                   "series-pause", "expired", "unsupported-route", "foreign-profile"])
def test_unadmitted_identity_cannot_reach_any_http(rates, monkeypatch, capsys, fault):
    f, state = rates
    if fault == "missing-session":
        with f.db._connect() as c:
            c.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (f.task.id,))
    elif fault in ("missing-reserve", "wrong-cost", "unsupported-route"):
        value = json.loads(ledger(f))
        if fault == "missing-reserve":
            value["reservations"] = []
        elif fault == "wrong-cost":
            value["reservations"][0]["cost"]["core"] += 1
        else:
            value["reservations"][0]["route"] = "tool"
        f.db.set_setting(f.db._pipeline_github_budget_reservation_key("github-default"), json.dumps(value))
    elif fault == "global-pause":
        f.db.set_setting("worker_paused", "1")
    elif fault == "series-pause":
        with f.db._connect() as c:
            c.execute("UPDATE task_series SET paused=1 WHERE id=?", (f.task.series_id,))
    elif fault == "expired":
        with f.db._connect() as c:
            c.execute("UPDATE tasks SET task_timeout=1,started_at='2000-01-01T00:00:00+00:00' WHERE id=?", (f.task.id,))
    before = source_image(f)
    code, result = invoke(f, monkeypatch, capsys, profile="foreign" if fault == "foreign-profile" else "example")
    assert code == 2 and result["state"] == "refused" and state.calls == []
    assert source_image(f) == before
    no_grants(result)


@pytest.mark.parametrize("fault", ["session", "start", "priority", "prompt", "working_dir", "provider",
                                   "rights", "model", "effort", "task_timeout", "cancel", "lease", "reservation"])
def test_transactional_rate_write_rejects_lost_exact_native_fence(rates, monkeypatch, capsys, fault):
    f, _state = rates
    old_rate = f.db.get_pipeline_github_rate_snapshot("github-default")
    before = source_image(f)
    original = f.db.record_pipeline_github_rate_snapshot

    def write(*args, **kwargs):
        if fault == "cancel":
            f.db.set_setting(f"cancel_task:{f.task.id}", "1")
        elif fault == "lease":
            f.db.set_setting(f.db._pipeline_scan_lease_key("github-default"), "{}")
        elif fault == "reservation":
            value = json.loads(ledger(f))
            value["reservations"][0]["token"] = "changed-admission"
            f.db.set_setting(f.db._pipeline_github_budget_reservation_key("github-default"), json.dumps(value))
        else:
            key, value = {"session": ("session_id", "changed-session"),
                          "start": ("started_at", datetime.now(timezone.utc).isoformat()),
                          "priority": ("priority", 2), "task_timeout": ("task_timeout", 601),
                          **{k: (k, "changed-input") for k in (
                              "prompt", "working_dir", "provider", "rights", "model", "effort")}}[fault]
            with f.db._connect() as c:
                c.execute(f"UPDATE tasks SET {key}=? WHERE id=?", (value, f.task.id))
        return original(*args, **kwargs)

    monkeypatch.setattr(f.db, "record_pipeline_github_rate_snapshot", write)
    code, result = invoke(f, monkeypatch, capsys)
    assert code == 2 and result["state"] == "refused" and result["rate_snapshot_refreshed"] is False
    assert f.db.get_pipeline_github_rate_snapshot("github-default") == old_rate
    assert source_image(f) == before
    no_grants(result)


def test_missing_identity_public_cli_does_not_create_native_data(tmp_path):
    env = dict(os.environ, PP_DATA_DIR=str(tmp_path / "no-native-data"))
    env.pop("PP_TASK_ID", None)
    result = subprocess.run([sys.executable, "-m", "promptpilot", "pipelinectl", "budget-refresh",
                             "--profile", "example"], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    value = json.loads(result.stdout)
    assert value["rate_snapshot_refreshed"] is False
    assert value["admitted_observation"] is False
    no_grants(value)
    assert not (tmp_path / "no-native-data").exists()
