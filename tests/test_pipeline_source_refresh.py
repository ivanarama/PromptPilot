"""Official continuation tool: actual Native SQLite, fake external observations."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from promptpilot import pipeline_insights as pi, pipeline_source_refresh as source
from promptpilot.models import TaskCreate
from promptpilot.task_identity import apply_task_markers
from tests.test_github_budget import _limits


@pytest.fixture
def continuation(isolated_db, monkeypatch):
    profile = {"title": "Example", "repository": "owner/example", "queues": [{
        "id": "fix", "title": "Fix", "capacity": 1, "query": "is:pr",
        "series_contains": "Example - FIX"}], "health_check": {"command": ["checker"]},
        "github_budget": {"minimum_remaining": {"core": 250, "search": 2, "graphql": 100},
            "priority_one_headroom": {"core": 1200, "search": 12, "graphql": 1000},
            "essential_snapshot_headroom": True,
            "costs": {r: {"core": 1200, "search": 12, "graphql": 1000}
                      for r in pi._GITHUB_BUDGET_ROUTES}}}
    profile["github_budget"]["costs"]["insights"] = {"core": 50, "search": 10, "graphql": 100}
    monkeypatch.setattr(pi, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pi, "_cache", {})
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: _limits())
    checks = []
    searches = []
    monkeypatch.setattr(pi, "_run_profile_health_check",
                        lambda _: checks.append(True) or {"state": "green", "findings": []})
    def search(*_args):
        pi._ensure_github_scan_lease()
        searches.append(True)
        return {"count": 0, "items": [], "membership_complete": True}
    monkeypatch.setattr(pi, "_github_search", search)
    isolated_db.create_task(TaskCreate(prompt="Example - FIX", recurrence="1h",
                                      priority=1, task_timeout=600))
    task = isolated_db.get_next_runnable()
    isolated_db.set_session_id(task.id, "actual-session")
    cost = profile["github_budget"]["costs"]["skill"]
    reserved = isolated_db.reserve_pipeline_github_budget("github-default", token="existing-skill",
        task_id=task.id, task_started_at=task.started_at, profile_id="example", queue_id="fix",
        route="skill", cost=cost, limits=_limits(), minimum_remaining={"core": 250, "search": 2, "graphql": 100})
    assert reserved["allowed"]
    return SimpleNamespace(db=isolated_db, profile=profile, task=task, checks=checks,
                           searches=searches, env={"PP_TASK_ID": str(task.id)})


def ledger(f):
    return f.db.get_setting(f.db._pipeline_github_budget_reservation_key("github-default"))


def test_priority_one_refresh_uses_scan_floor_and_existing_skill_promise_once(continuation):
    f = continuation
    before = ledger(f)
    result = source.refresh("example", f.env)
    assert result["state"] == "refreshed" and result["source_refreshed"] is True
    assert result["budget"]["minimum_remaining"]["search"] == 2
    assert result["budget"]["reserved_other"]["search"] == 12
    assert result["budget"]["requested_cost"]["search"] == 10
    assert result["budget"]["effective_after"]["search"] == 8
    assert result["budget"]["admission_priority"] == 1
    assert result["existing_skill_reservation_reused"] is True
    assert result["mutable_admission_granted"] is False
    assert result["product_mutation_authorized"] is False
    assert len(f.checks) == len(f.searches) == 1
    assert len(f.db.list_pipeline_snapshots("example")) == 1
    assert ledger(f) == before
    assert "existing-skill" not in json.dumps(result) and "actual-session" not in json.dumps(result)


def test_lower_priority_scan_retains_soft_priority_one_headroom(continuation):
    f = continuation
    with f.db._connect() as c:
        c.execute("UPDATE tasks SET priority=2 WHERE id=?", (f.task.id,))
    result = source.refresh("example", f.env)
    assert result["state"] == "wait" and result["source_refreshed"] is False
    assert result["budget"]["minimum_remaining"]["search"] == 14
    assert f.checks == f.searches == []


def test_scan_never_removes_foreign_or_stale_reservations(continuation):
    f = continuation
    key = f.db._pipeline_github_budget_reservation_key("github-default")
    value = json.loads(ledger(f))
    value["reservations"].append(dict(value["reservations"][0], token="stale-foreign",
                                      task_id=9999, task_started_at="old"))
    f.db.set_setting(key, json.dumps(value, indent=2))
    before = ledger(f)
    assert source.refresh("example", f.env)["source_refreshed"] is True
    assert ledger(f) == before


def test_live_foreign_promise_remains_fully_counted_and_unchanged(continuation):
    f = continuation
    f.db.create_task(TaskCreate(prompt="foreign work", priority=3))
    other = f.db.get_next_runnable()
    assert f.db.reserve_pipeline_github_budget("github-default", token="foreign-skill",
        task_id=other.id, task_started_at=other.started_at, profile_id="foreign", queue_id="triage",
        route="skill", cost={"core": 100, "search": 7, "graphql": 50}, limits=_limits(),
        minimum_remaining={"core": 250, "search": 2, "graphql": 100})["allowed"]
    before = ledger(f)
    result = source.refresh("example", f.env)
    assert result["state"] == "wait" and result["budget"]["state"] == "budget_in_flight"
    assert result["budget"]["reserved_other"]["search"] == 19
    assert ledger(f) == before and f.checks == f.searches == []


def test_current_full_source_query_change_during_scan_cannot_publish(continuation, monkeypatch):
    f = continuation
    def search(*_args):
        f.profile["queues"][0]["query"] = "is:issue"
        return {"count": 0, "items": [], "membership_complete": True}
    monkeypatch.setattr(pi, "_github_search", search)
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] is False
    assert f.db.list_pipeline_snapshots("example") == []


@pytest.mark.parametrize("fault", ["missing-session", "wrong-marker", "missing-reserve",
                                   "different-series", "cancelled", "expired", "foreign-profile"])
def test_unbound_attempt_is_refused_before_any_github_io(continuation, monkeypatch, fault):
    f = continuation
    if fault == "missing-session":
        with f.db._connect() as c:
            c.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (f.task.id,))
    elif fault == "wrong-marker":
        f.env["PP_TASK_STARTED_AT"] = "different-start"
    elif fault == "missing-reserve":
        f.db.release_pipeline_github_budget("github-default", token="existing-skill",
            task_id=f.task.id, task_started_at=f.task.started_at)
    elif fault == "different-series":
        with f.db._connect() as c:
            c.execute("UPDATE tasks SET series_id=NULL WHERE id=?", (f.task.id,))
    elif fault == "cancelled":
        f.db.set_setting(f"cancel_task:{f.task.id}", "1")
    elif fault == "expired":
        with f.db._connect() as c:
            c.execute("UPDATE tasks SET started_at='2000-01-01T00:00:00+00:00' WHERE id=?", (f.task.id,))
    else:
        with pytest.raises(ValueError):
            source.refresh("foreign", f.env)
        return
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: pytest.fail("unbound attempt reached GitHub"))
    with pytest.raises((ValueError, TypeError)):
        source.refresh("example", f.env)
    assert f.checks == f.searches == []


@pytest.mark.parametrize("fault", ["session", "cancel", "lease", "epoch", "start",
                                  "priority", "series", "reservation", "prompt", "working_dir",
                                  "provider", "rights", "model", "effort", "task_timeout"])
def test_drift_before_publication_cannot_publish_cache_or_history(continuation, monkeypatch, fault):
    f = continuation
    before = ledger(f)
    original = f.db.set_setting_if_newer_revision
    def publish(*args, **kwargs):
        if kwargs.get("pipeline_snapshot") is not None:
            if fault == "session":
                f.db.set_session_id(f.task.id, "different-session")
            elif fault == "cancel":
                f.db.set_setting(f"cancel_task:{f.task.id}", "1")
            elif fault == "lease":
                f.db.set_setting(f.db._pipeline_scan_lease_key("github-default"), "{}")
            elif fault == "epoch":
                f.db.increment_int_setting(pi._CACHE_EPOCH_KEY)
            elif fault == "reservation":
                key = f.db._pipeline_github_budget_reservation_key("github-default")
                value = json.loads(ledger(f))
                value["reservations"][0]["token"] = "new-attempt-admission"
                f.db.set_setting(key, json.dumps(value))
            else:
                column, value = {"start": ("started_at", datetime.now(timezone.utc).isoformat()),
                                 "priority": ("priority", 2), "series": ("series_id", None),
                                 **{key: (key, "changed-input") for key in (
                                     "prompt", "working_dir", "provider", "rights", "model", "effort")},
                                 "task_timeout": ("task_timeout", 601)}[fault]
                with f.db._connect() as c:
                    c.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, f.task.id))
        return original(*args, **kwargs)
    monkeypatch.setattr(f.db, "set_setting_if_newer_revision", publish)
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] is False
    assert f.db.list_pipeline_snapshots("example") == []
    assert f.db.get_setting(pi._cache_key("example", pi._profile_fingerprint(f.profile))) is None
    if fault != "reservation":
        assert ledger(f) == before


def test_busy_scan_lease_preserves_current_admission_without_waiter_edits(continuation):
    f = continuation
    before = ledger(f)
    assert f.db.acquire_pipeline_scan_lease("github-default", "other-scan", 60)["acquired"]
    result = source.refresh("example", f.env)
    assert result["state"] == "wait"
    assert result["budget"]["state"] == "scan_in_progress"
    with f.db._connect() as c:
        assert c.execute("SELECT budget_wait_scope FROM tasks WHERE id=?", (f.task.id,)).fetchone()[0] is None
    assert f.checks == f.searches == [] and ledger(f) == before


def test_equal_priority_waiter_does_not_demote_actual_priority_one(continuation):
    f = continuation
    f.db.create_task(TaskCreate(prompt="other", priority=1))
    other = f.db.get_next_runnable()
    f.db.arm_pipeline_github_budget_waiter("github-default", task_id=other.id,
                                          task_started_at=other.started_at)
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] is True
    assert result["budget"]["admission_priority"] == 1


def test_existing_aged_fairness_baton_is_preserved_without_scheduler_edits(continuation):
    f = continuation
    f.profile["github_budget"]["starvation_timeout_seconds"] = 60
    f.db.create_task(TaskCreate(prompt="older waiter", priority=5))
    other = f.db.get_next_runnable()
    f.db.arm_pipeline_github_budget_waiter("github-default", task_id=other.id,
                                          task_started_at=other.started_at)
    with f.db._connect() as c:
        c.execute("UPDATE tasks SET budget_wait_started_at=? WHERE id=?",
                  ((datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(), other.id))
        before = tuple(c.execute("SELECT budget_wait_scope,budget_wait_started_at FROM tasks WHERE id=?",
                                 (other.id,)).fetchone())
    result = source.refresh("example", f.env)
    assert result["state"] == "wait" and result["budget"]["state"] == "fairness_waiter"
    with f.db._connect() as c:
        assert tuple(c.execute("SELECT budget_wait_scope,budget_wait_started_at FROM tasks WHERE id=?",
                               (other.id,)).fetchone()) == before
    assert f.checks == f.searches == []


def test_herdr_pane_is_a_real_native_session_and_fenced_at_publication(continuation, monkeypatch):
    f = continuation
    with f.db._connect() as c:
        c.execute("UPDATE tasks SET session_id=NULL,herdr_pane='native-pane' WHERE id=?", (f.task.id,))
    assert source.refresh("example", f.env)["source_refreshed"] is True
    original = f.db.set_setting_if_newer_revision
    def publish(*args, **kwargs):
        if kwargs.get("pipeline_snapshot") is not None:
            with f.db._connect() as c:
                c.execute("UPDATE tasks SET herdr_pane='replaced-pane' WHERE id=?", (f.task.id,))
        return original(*args, **kwargs)
    monkeypatch.setattr(f.db, "set_setting_if_newer_revision", publish)
    assert source.refresh("example", f.env)["source_refreshed"] is False
    assert len(f.db.list_pipeline_snapshots("example")) == 1


def test_other_scope_admission_for_same_task_is_ambiguous(continuation, monkeypatch):
    f = continuation
    value = json.loads(ledger(f))
    f.db.set_setting(f.db._pipeline_github_budget_reservation_key("foreign-account"),
                     json.dumps(value))
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: pytest.fail("ambiguous admission reached GitHub"))
    with pytest.raises(ValueError, match="ambiguous"):
        source.refresh("example", f.env)


def test_retarget_between_route_matching_and_first_fence_is_refused(continuation, monkeypatch):
    f = continuation
    original = f.db.pipeline_source_refresh_state
    def observe(*args, **kwargs):
        with f.db._connect() as c:
            c.execute("UPDATE tasks SET prompt='a different queue' WHERE id=?", (f.task.id,))
        return original(*args, **kwargs)
    monkeypatch.setattr(f.db, "pipeline_source_refresh_state", observe)
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: pytest.fail("retarget reached GitHub"))
    with pytest.raises(ValueError, match="execution inputs"):
        source.refresh("example", f.env)
    assert f.db.list_pipeline_snapshots("example") == []


def test_future_series_edits_do_not_retarget_the_claimed_unchanged_task(continuation, monkeypatch):
    f = continuation
    original = f.db.set_setting_if_newer_revision
    def publish(*args, **kwargs):
        if kwargs.get("pipeline_snapshot") is not None:
            with f.db._connect() as c:
                c.execute("UPDATE task_series SET title='future title',provider='future-provider',"
                          "working_dir='future-directory' WHERE id=?", (f.task.series_id,))
        return original(*args, **kwargs)
    monkeypatch.setattr(f.db, "set_setting_if_newer_revision", publish)
    assert source.refresh("example", f.env)["source_refreshed"] is True


@pytest.mark.parametrize("failed", [True, "true"])
def test_failed_checker_is_never_reported_as_source_success(continuation, monkeypatch, failed):
    monkeypatch.setattr(pi, "_run_profile_health_check", lambda _: {"checker_failed": failed})
    result = source.refresh("example", continuation.env)
    assert result["state"] == "refused" and result["reason"] == "checker_failed"
    assert result["source_refreshed"] is False and result["snapshot_token"] is None


@pytest.mark.parametrize("started", [datetime(2026, 1, 2, tzinfo=timezone.utc), None])
def test_actual_task_markers_replace_inherited_and_provider_spoof(started):
    env = {"PP_TASK_ID": "spoof", "PP_TASK_STARTED_AT": "spoof", "OTHER": "keep"}
    task = SimpleNamespace(id=42, started_at=started)
    apply_task_markers(env, task)
    assert env["PP_TASK_ID"] == "42" and env["OTHER"] == "keep"
    assert env.get("PP_TASK_STARTED_AT") == (started.isoformat() if started else None)


def test_missing_worker_identity_is_db_free_cli(tmp_path):
    env = dict(os.environ, PP_DATA_DIR=str(tmp_path / "unused-native-data"))
    env.pop("PP_TASK_ID", None)
    result = subprocess.run([sys.executable, "-m", "promptpilot", "pipelinectl", "source-refresh",
                             "--profile", "example"], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert json.loads(result.stdout)["source_refreshed"] is False
    assert not (tmp_path / "unused-native-data").exists()


def test_ordinary_worker_forwards_actual_start_after_provider_environment(isolated_db, monkeypatch):
    from promptpilot import worker
    isolated_db.create_task(TaskCreate(prompt="ordinary", provider="fixture"))
    task = isolated_db.get_next_runnable()
    captured = {}
    class Captured(BaseException):
        pass
    def launch(*_args, **kwargs):
        captured.update(kwargs["env"])
        raise Captured()
    monkeypatch.setattr(worker, "load_providers", lambda: {"fixture": {}})
    monkeypatch.setattr(worker, "build_cmd", lambda *_args, **_kwargs: ["never-executed"])
    monkeypatch.setattr(worker, "get_provider_env", lambda *_args, **_kwargs: {
        "PP_TASK_ID": "spoof-id", "PP_TASK_STARTED_AT": "spoof-start"})
    monkeypatch.setattr(worker.OwnedProcess, "start", launch)
    with pytest.raises(Captured):
        worker._execute_task_body(task)
    assert captured["PP_TASK_ID"] == str(task.id)
    assert captured["PP_TASK_STARTED_AT"] == task.started_at.isoformat()
    assert "PP_PIPELINE_REPLICAS" not in captured


@pytest.mark.parametrize("replicas", [None, "2"])
def test_herdr_pane_forwards_actual_start_after_provider_spoof(monkeypatch, replicas):
    from promptpilot import herdr_exec
    started = datetime(2026, 1, 2, tzinfo=timezone.utc)
    task = SimpleNamespace(id=42, started_at=started, herdr_target=None, model=None,
        effort=None, session_id=None, skip_permissions=False, detached=False,
        working_dir=".", worktree=False)
    captured = {}
    class Captured(BaseException):
        pass
    def run(args, **_kwargs):
        if args[:2] == ["tab", "create"]:
            for index, value in enumerate(args[:-1]):
                if value == "--env":
                    key, data = args[index + 1].split("=", 1)
                    captured[key] = data
            raise Captured()
        return 0, {}, ""
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda *_: None)
    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", lambda *_: None)
    monkeypatch.setattr(herdr_exec, "_run", run)
    env = {"PP_TASK_ID": "spoof", "PP_TASK_STARTED_AT": "spoof"}
    if replicas:
        env["PP_PIPELINE_REPLICAS"] = replicas
    with pytest.raises(Captured):
        herdr_exec.run_in_herdr(task, {"kind": "codex", "env": env}, prompt_override="probe")
    assert captured["PP_TASK_ID"] == "42"
    assert captured["PP_TASK_STARTED_AT"] == started.isoformat()
    assert captured.get("PP_PIPELINE_REPLICAS") == replicas
