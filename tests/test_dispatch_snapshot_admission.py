"""A required Native observation must refuse before loading a provider."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from promptpilot import pipeline_insights as pi, worker
from promptpilot.models import TaskCreate


@pytest.fixture
def dispatch(isolated_db, monkeypatch):
    created = isolated_db.create_task(TaskCreate(
        prompt="Example - FIX", recurrence="4h", provider="fixture-provider"))
    task = isolated_db.get_next_runnable()
    profile = {"title": "Example", "repository": "owner/example", "queues": [{
        "id": "fix", "title": "Fix", "series_contains": "Example - FIX",
        "dispatch_gate": {"require_fresh_snapshot": True},
    }]}
    data = {"cache": {"available": True, "complete": True, "stale": False,
                      "invalidated": False},
            "queues": [{"id": "fix", "backlog": 1, "membership_complete": True}], "diagnostics": {}}
    calls = []
    monkeypatch.setattr(pi, "_profiles", lambda: {"example": profile})
    def cached(*_):
        observed = deepcopy(data)
        observed["cache"].setdefault("profile_hash", pi._profile_fingerprint(profile))
        return observed
    monkeypatch.setattr(pi, "read_cached", cached)

    def forbidden_scan(*_args, **_kwargs):
        raise OSError("fixture admitted snapshot refresh is unavailable")

    monkeypatch.setattr(pi, "analyze", forbidden_scan)

    def route(*_args, **_kwargs):
        calls.append("execution-admission")
        return {"action": "prompt", "mode": "skill", "prompt": task.prompt}

    def providers():
        calls.append("load-provider")
        return {"fixture-provider": {"executor": "herdr"}}

    monkeypatch.setattr(pi, "execution_route", route)
    monkeypatch.setattr(worker, "load_providers", providers)
    monkeypatch.setattr(worker, "_execute_herdr_task",
                        lambda *_args, **_kwargs: calls.append("provider"))
    # Admission must not use the series resume/hold-clearing path.
    isolated_db.set_setting("pipeline_item_holds:v1:77", '{"42":{"reason":"keep"}}')
    isolated_db.set_setting("pipeline_item_hold_mode:v1:77", '"unchanged"')
    return isolated_db, created, task, profile, data, calls


@pytest.mark.parametrize("cache", [
    {"stale": True},
    {"invalidated": True},
    {"refresh_blocked": "budget_in_flight"},
    {"complete": False},
    {"available": False},
    {},  # A missing observation is not an empty queue or permission to launch.
])
def test_worker_defers_required_observation_before_execution_or_provider(dispatch, cache):
    db, created, task, _profile, data, calls = dispatch
    if cache:
        data["cache"].update(cache)
    else:
        data["cache"] = {}
    before = db.get_settings_snapshot([
        "pipeline_item_holds:v1:77", "pipeline_item_hold_mode:v1:77"])

    worker._execute_task_inner(task)

    saved = db.get_task(created.id)
    assert calls == []  # No provider discovery, project CLI, or budget reservation.
    assert saved.status.value == "pending"
    assert saved.scheduled_at > datetime.now(timezone.utc)
    assert db.get_next_runnable() is None
    assert saved.result is None
    assert "snapshot" in saved.error.lower()
    assert db.get_settings_snapshot(list(before)) == before


def test_worker_defers_required_observation_when_cached_read_raises(dispatch, monkeypatch):
    db, created, task, _profile, _data, calls = dispatch

    def unavailable(*_args):
        raise OSError("fixture cache unavailable")

    monkeypatch.setattr(pi, "read_cached", unavailable)
    worker._execute_task_inner(task)
    assert db.get_task(created.id).status.value == "pending"
    assert calls == []


@pytest.mark.parametrize("malformed", [None, [], {"cache": []}])
def test_worker_defers_malformed_required_observation(dispatch, monkeypatch, malformed):
    db, created, task, _profile, _data, calls = dispatch
    monkeypatch.setattr(pi, "read_cached", lambda *_: malformed)
    worker._execute_task_inner(task)
    assert db.get_task(created.id).status.value == "pending"
    assert calls == []


@pytest.mark.parametrize("option", [False, "absent"])
def test_legacy_generic_stale_fallback_still_reaches_provider(dispatch, option):
    _db, _created, task, profile, data, calls = dispatch
    config = profile["queues"][0]["dispatch_gate"]
    if option == "absent":
        del config["require_fresh_snapshot"]
    else:
        config["require_fresh_snapshot"] = option
    data["cache"]["stale"] = True
    worker._execute_task_inner(task)
    assert calls == ["execution-admission", "load-provider", "provider"]


def test_fresh_required_observation_keeps_existing_provider_path(dispatch):
    _db, _created, task, _profile, _data, calls = dispatch
    worker._execute_task_inner(task)
    assert calls == ["execution-admission", "load-provider", "provider"]


def test_cache_retry_is_bounded_and_can_use_the_next_fresh_window(dispatch, monkeypatch):
    db, created, task, _profile, data, calls = dispatch
    data["cache"]["stale"] = True
    before = datetime.now(timezone.utc)
    worker._execute_task_inner(task)
    saved = db.get_task(created.id)
    assert 29 <= (saved.scheduled_at - before).total_seconds() <= 31
    assert calls == []

    # Exercise the real scheduler boundary without sleeping or starting scans.
    monkeypatch.setattr(db, "_now", lambda: (saved.scheduled_at - timedelta(seconds=1)).isoformat())
    assert db.get_next_runnable() is None
    data["cache"]["stale"] = False
    monkeypatch.setattr(db, "_now", lambda: (saved.scheduled_at + timedelta(seconds=1)).isoformat())
    retry = db.get_next_runnable()
    assert retry is not None and retry.id == created.id
    worker._execute_task_inner(retry)
    assert calls == ["execution-admission", "load-provider", "provider"]


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", "false", [], {}])
def test_invalid_opt_in_is_deferred_as_configuration_error(dispatch, invalid):
    db, created, task, profile, _data, calls = dispatch
    profile["queues"][0]["dispatch_gate"]["require_fresh_snapshot"] = invalid
    worker._execute_task_inner(task)
    assert db.get_task(created.id).status.value == "pending"
    assert "boolean" in db.get_task(created.id).error
    assert calls == []


def test_required_refresh_failure_still_defers_with_backpressure(dispatch):
    _db, _created, task, profile, data, calls = dispatch
    profile["queues"][0]["dispatch_gate"]["backpressure"] = {"max_active": 1}
    data["cache"]["stale"] = True
    gate = pi.dispatch_gate(task)
    assert gate["action"] == "defer"
    assert gate["profile_id"] == "example" and gate["queue_id"] == "fix"
    assert calls == []


def test_required_snapshot_does_not_bypass_fresh_backpressure(dispatch):
    _db, _created, task, profile, data, _calls = dispatch
    profile["queues"][0]["dispatch_gate"]["backpressure"] = {
        "max_active": 1, "active_fields": ["review_backlog"],
        "candidate_field": "fix_candidates"}
    data["diagnostics"] = {"review_backlog": [{"number": 42, "head": "a"}],
                           "fix_candidates": []}
    gate = pi.dispatch_gate(task)
    assert gate["action"] == "defer" and "WIP" in gate["reason"]
