"""Claim/defer must not invalidate the observation needed by their own gate."""
from datetime import datetime, timedelta, timezone

from promptpilot import pipeline_insights as pi, worker
from promptpilot.models import TaskCreate


def test_real_scheduler_and_worker_preserve_canonical_snapshot_epoch(
        isolated_db, monkeypatch):
    db = isolated_db
    profile = {"title": "Example", "repository": "owner/example", "queues": [{
        "id": "fix", "title": "Fix", "query": "is:pr",
        "capacity": 1, "series_contains": "Example - FIX",
        "dispatch_gate": {"require_fresh_snapshot": True},
    }]}
    monkeypatch.setattr(pi, "_profiles", lambda: {"example": profile})
    # External results are fixture-owned; publishing and reading the canonical
    # durable cache, scheduler claims, worker and SQLite triggers remain real.
    monkeypatch.setattr(pi, "_run_profile_health_check", lambda *_: {
        "state": "green", "findings": []})
    monkeypatch.setattr(pi, "_github_search", lambda *_: {
        "count": 0, "items": [],
        "membership_complete": True})
    created = db.create_task(TaskCreate(
        prompt="Example - FIX", recurrence="4h", provider="fixture-provider"))
    fresh = pi.analyze("example", db.list_series(), use_cache=False)
    assert fresh["cache"]["stale"] is False
    epoch = pi._cache_epoch()
    pi._cache.clear()  # Exercise restart-compatible durable reads, not a mock.

    calls = []

    def route(*_args, **_kwargs):
        calls.append("execution-admission")
        return {"action": "prompt", "mode": "skill", "prompt": created.prompt}

    monkeypatch.setattr(pi, "execution_route", route)
    monkeypatch.setattr(worker, "load_providers", lambda: {
        "fixture-provider": {"executor": "herdr"}})
    monkeypatch.setattr(worker, "_execute_herdr_task",
                        lambda *_args, **_kwargs: calls.append("provider"))

    def no_scan(*_args, **_kwargs):
        raise AssertionError("claim/defer/cache polling must not rescan GitHub")

    monkeypatch.setattr(pi, "analyze", no_scan)
    task = db.get_next_runnable()
    observed = pi.read_cached("example", db.list_series())
    assert observed["cache"]["source"] == "durable"
    assert observed["cache"]["invalidated"] is False
    assert pi._cache_epoch() == epoch  # Claiming running does not bump it.
    worker._execute_task_inner(task)
    assert calls == ["execution-admission", "provider"]

    # Local waiting is not productive external progress and cannot invalidate
    # its own required observation or call the success wake/invalidation path.
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert db.defer_task(task.id, deadline, "fixture local wait",
                         expected_started_at=task.started_at)
    worker._recur_after_run(task)
    assert pi._cache_epoch() == epoch
    assert pi.read_cached("example", db.list_series())["cache"]["stale"] is False

    # A genuine external invalidation still refuses the next occurrence, and
    # the new gate's own pending defer must not advance the epoch again.
    db.increment_int_setting("pipeline_insights_cache_epoch:v1")
    invalidated_epoch = pi._cache_epoch()
    monkeypatch.setattr(db, "_now", lambda: (deadline + timedelta(seconds=1)).isoformat())
    task = db.get_next_runnable()
    calls.clear()
    worker._execute_task_inner(task)
    deferred = db.get_task(created.id)
    assert deferred.status.value == "pending" and calls == []
    worker._recur_after_run(task)
    assert pi._cache_epoch() == invalidated_epoch
    assert pi.read_cached("example", db.list_series())["cache"]["invalidated"] is True
