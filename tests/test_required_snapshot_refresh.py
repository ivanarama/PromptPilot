"""Required Source freshness is obtained before any provider, not by its cadence."""
from copy import deepcopy

import pytest

from promptpilot import pipeline_insights as pi, worker
from promptpilot.models import TaskCreate
from tests.test_github_budget import _limits
from tests.source_cost_fixtures import checker_receipt, declare_checker, observed_rates


@pytest.fixture
def gate(isolated_db, monkeypatch, tmp_path):
    profile = {"title": "Example", "repository": "owner/example", "health_check": {"command": ["checker"]},
        "queues": [{"id": "fix", "title": "Fix", "query": "is:pr", "capacity": 1,
                    "series_contains": "Example - FIX", "dispatch_gate": {"require_fresh_snapshot": True}}],
        "github_budget": {"minimum_remaining": {"core": 250, "search": 2, "graphql": 100},
            "essential_snapshot_headroom": True,
            "costs": {r: {"core": 50, "search": 10, "graphql": 100} for r in pi._GITHUB_BUDGET_ROUTES}}}
    declare_checker(profile, tmp_path)
    monkeypatch.setattr(pi, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pi, "_cache", {})
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits()))
    calls = []
    def checker(_):
        pi._ensure_github_scan_lease()
        calls.append("checker")
        return checker_receipt({"state": "green", "findings": []})
    def search(*_):
        pi._ensure_github_scan_lease()
        calls.append("search")
        return {"count": 1, "items": [{"key": "pr:1", "number": 1, "kind": "pr", "head": "a" * 40}],
                "membership_complete": True}
    monkeypatch.setattr(pi, "_run_profile_health_check", checker)
    monkeypatch.setattr(pi, "_github_search", search)
    created = isolated_db.create_task(TaskCreate(prompt="Example - FIX", recurrence="4h", provider="fixture"))
    task = isolated_db.get_next_runnable()
    def route(*_, **__):
        calls.append("execution-admission")
        return {"action": "prompt", "mode": "skill", "prompt": task.prompt}
    def providers():
        calls.append("load-provider")
        return {"fixture": {"executor": "herdr"}}
    monkeypatch.setattr(pi, "execution_route", route)
    monkeypatch.setattr(worker, "load_providers", providers)
    monkeypatch.setattr(worker, "_execute_herdr_task", lambda *_, **__: calls.append("provider"))
    return isolated_db, profile, task, created, calls


def full_data(profile):
    return {"cache": {"available": True, "complete": True, "stale": False, "invalidated": False,
                      "profile_hash": pi._profile_fingerprint(profile)},
            "queues": [{"id": "fix", "title": "Fix", "backlog": 1, "membership_complete": True}],
            "diagnostics": {"state": "green"}}


def test_cold_required_source_gets_real_admitted_scan_before_provider(gate):
    db, _profile, task, _created, calls = gate
    worker._execute_task_inner(task)
    assert calls == ["checker", "search", "execution-admission", "load-provider", "provider"]
    assert len(db.list_pipeline_snapshots("example")) == 1
    assert pi.read_cached("example", db.list_series())["cache"]["ttl_seconds"] == 300


def test_two_due_gate_calls_reuse_one_complete_publication(gate):
    db, _profile, task, _created, calls = gate
    assert pi.dispatch_gate(task) is None
    assert pi.dispatch_gate(task) is None
    assert calls == ["checker", "search"]
    assert len(db.list_pipeline_snapshots("example")) == 1


def test_raced_cold_read_reuses_winner_under_canonical_refresh_lock(gate, monkeypatch):
    db, _profile, task, _created, calls = gate
    pi.analyze("example", db.list_series(), use_cache=False, refresh_diagnostics=True)
    original = pi.read_cached
    first = True
    def raced(*args):
        nonlocal first
        if first:
            first = False
            return {"cache": {"stale": True}}
        return original(*args)
    monkeypatch.setattr(pi, "read_cached", raced)
    assert pi.dispatch_gate(task) is None
    assert calls == ["checker", "search"]
    assert len(db.list_pipeline_snapshots("example")) == 1


def test_manual_force_refresh_keeps_its_existing_scan_semantics(gate):
    db, _profile, _task, _created, calls = gate
    for _ in range(2):
        pi.analyze("example", db.list_series(), use_cache=False, refresh_diagnostics=True)
    assert calls == ["checker", "search", "checker", "search"]
    assert len(db.list_pipeline_snapshots("example")) == 2


@pytest.mark.parametrize("fault", ["partial", "stale", "invalidated", "denied", "unavailable",
    "incomplete", "wrong-profile", "checker-failed", "checker-errors", "malformed", "error"])
def test_unusable_refreshed_source_stays_pending_without_provider(gate, monkeypatch, fault):
    db, profile, task, created, calls = gate
    data = full_data(profile)
    if fault == "partial":
        data["queues"][0]["membership_complete"] = False
    elif fault in {"stale", "invalidated"}:
        data["cache"][fault] = True
    elif fault == "denied":
        data["cache"]["refresh_blocked"] = "low"
    elif fault in {"unavailable", "incomplete"}:
        data["cache"]["available" if fault == "unavailable" else "complete"] = False
    elif fault == "wrong-profile":
        data["cache"]["profile_hash"] = "old-profile"
    elif fault == "checker-failed":
        data["diagnostics"]["checker_failed"] = True
    elif fault == "checker-errors":
        data["diagnostics"]["errors"] = ["checker unavailable"]
    elif fault == "malformed":
        data = {"cache": []}
    def refresh(*_, **options):
        assert options == {"use_cache": False, "refresh_diagnostics": True,
                           "reuse_fresh": True, "snapshot_queue_id": "fix"}
        if fault == "error":
            raise OSError("fixture scan unavailable")
        return deepcopy(data)
    monkeypatch.setattr(pi, "analyze", refresh)
    worker._execute_task_inner(task)
    assert db.get_task(created.id).status.value == "pending"
    assert calls == []


@pytest.mark.parametrize("fault", ["pause", "profile"])
def test_pause_or_profile_change_during_refresh_cannot_launch(gate, monkeypatch, fault):
    db, profile, task, created, calls = gate
    data = full_data(profile)
    def refresh(*_, **__):
        if fault == "pause":
            db.set_setting("worker_paused", "1")
        else:
            profile["queues"][0]["query"] = "a changed Source contract"
            data["cache"]["profile_hash"] = pi._profile_fingerprint(profile)
        return data
    monkeypatch.setattr(pi, "analyze", refresh)
    worker._execute_task_inner(task)
    assert db.get_task(created.id).status.value == "pending" and calls == []


def test_normal_budget_denial_runs_no_checker_or_provider(gate, monkeypatch):
    db, _profile, task, created, calls = gate
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: _limits(search=1))
    worker._execute_task_inner(task)
    assert db.get_task(created.id).status.value == "pending" and calls == []
    assert "refresh_blocked=" in db.get_task(created.id).error


def test_unrelated_partial_tail_and_human_red_diagnostics_are_not_new_global_gates(gate, monkeypatch):
    _db, profile, task, _created, calls = gate
    profile["queues"].append({"id": "tail", "title": "Tail", "query": "is:issue"})
    data = full_data(profile)
    data["queues"].append({"id": "tail", "membership_complete": False})
    data["diagnostics"] = {"state": "red", "findings": [{"severity": "red", "code": "human_decision"}]}
    monkeypatch.setattr(pi, "analyze", lambda *_, **__: data)
    worker._execute_task_inner(task)
    assert calls == ["execution-admission", "load-provider", "provider"]
