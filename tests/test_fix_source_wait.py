from datetime import datetime, timedelta, timezone
import json

import pytest

from promptpilot import fix_source_wait as wait, pipeline_insights as pi
from promptpilot.models import TaskCreate
from tests.test_required_snapshot_refresh import gate as gate


@pytest.fixture
def source(gate, monkeypatch):
    db, profile, task, _, _ = gate
    profile["repository"] = "ivanarama/onebase"
    monkeypatch.setattr(pi, "_profiles", lambda: {"onebase": profile})
    first = pi.analyze("onebase", db.list_series(), use_cache=False, refresh_diagnostics=True)
    hint = wait.wait_hint("onebase", profile, profile["queues"][0], first, "refresh_blocked=scan_in_progress")
    assert hint and hint["revision"] == first["cache"]["token"]["revision"]
    return db, profile, task, hint


def row(db, task):
    with db._connect() as conn:
        return conn.execute("SELECT * FROM pipeline_fix_source_waits WHERE task_id=?", (task.id,)).fetchone()


def defer(db, task, hint, seconds=90):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return db.defer_task(task.id, deadline, "snapshot contention", expected_started_at=task.started_at, source_wait=hint)


def publish(db):
    return pi.analyze("onebase", db.list_series(), use_cache=False, refresh_diagnostics=True)


def test_oldest_proven_waiter_gets_the_single_wake_even_with_lower_normal_priority(source):
    db, profile, first, hint = source
    assert defer(db, first, hint)
    second = db.create_task(TaskCreate(prompt="same queue", series_id=first.series_id, priority=1))
    running = db.get_next_runnable()
    assert running.id == second.id and defer(db, running, hint)
    assert row(db, first)["waiting_since"] <= row(db, second)["waiting_since"]
    data = publish(db)
    assert row(db, first)["ready_guard"] and row(db, second)["ready_guard"] is None
    assert wait.wake("onebase", profile, data, db.list_series()) is None
    assert db.get_next_runnable().id == first.id
    assert row(db, first)["ready_guard"] is None
    assert wait.wake_cached("onebase") is None
    assert db.get_next_runnable() is None


def test_age_survives_preflight_retries_but_never_applies_to_new_prerequisite(source):
    db, profile, task, hint = source
    assert defer(db, task, hint, seconds=-1)
    began = row(db, task)["waiting_since"]
    invalidated = {**hint, "create": False}
    running = db.get_next_runnable()
    assert not defer(db, task, invalidated)
    assert defer(db, running, invalidated, seconds=-1)
    running = db.get_next_runnable()
    assert defer(db, running, hint, seconds=-1)
    assert row(db, task)["waiting_since"] == began
    running = db.get_next_runnable()
    hard = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert db.defer_task(running.id, hard, "quota", hard_not_before=True, expected_started_at=running.started_at)
    assert row(db, task) is None
    publish(db)
    assert db.get_task(task.id).next_run_at == hard
    assert db.get_next_runnable() is None


def test_readiness_never_preempts_the_budget_fairness_winner(source):
    db, _, fix, hint = source
    assert defer(db, fix, hint)
    publish(db)
    expense = db.create_task(TaskCreate(prompt="budget waiter", priority=8))
    with db._connect() as conn:
        conn.execute("UPDATE tasks SET budget_wait_scope=?, budget_wait_started_at=? WHERE id=?",
                     ("github-default", "2025-01-01T00:00:00+00:00", expense.id))
    assert db.get_next_runnable(budget_wait_scope="github-default", budget_starvation_timeout_seconds=1).id == expense.id
    assert db.get_next_runnable().id == fix.id


@pytest.mark.parametrize("change", ["stale", "profile", "damaged", "priority", "reschedule", "cancel", "edited"])
def test_changed_or_ineligible_source_waiter_never_receives_claim_preference(source, change):
    db, _, fix, hint = source
    assert defer(db, fix, hint)
    publish(db)
    other = db.create_task(TaskCreate(prompt="normally first", priority=1))
    if change == "profile":
        db.set_setting(pi._published_profile_key("onebase"), "new profile")
    elif change == "stale":
        pi._discard_cache("onebase")
    elif change == "damaged":
        guard = json.loads(row(db, fix)["ready_guard"])
        guard["revision"] = float("inf")
        with db._connect() as conn:
            conn.execute("UPDATE pipeline_fix_source_waits SET ready_guard=? WHERE task_id=?",
                         (json.dumps(guard), fix.id))
    elif change == "priority":
        assert db.update_priority(fix.id, 9)
    elif change == "edited":
        assert db.update_task_fields(fix.id, {"model": "other"})
    elif change == "reschedule":
        deadline = datetime.now(timezone.utc) + timedelta(hours=2)
        assert db.update_task_fields(fix.id, {"scheduled_at": deadline})
        publish(db)
        assert db.get_task(fix.id).scheduled_at == deadline
    else:
        assert db.cancel_task(fix.id)
    assert db.get_next_runnable().id == other.id


def test_empty_wrong_and_malformed_snapshot_hints(source):
    _, profile, _, _ = source
    queue = profile["queues"][0]
    no_cache = {"cache": {"available": False}}
    busy = "refresh_blocked=scan_in_progress"
    assert wait.wait_hint("onebase", profile, queue, no_cache, busy)["revision"] == 0
    assert not wait.wait_hint("other", profile, queue, no_cache, busy)
    assert not wait.wait_hint("onebase", profile, {**queue, "execution": {"mode": "auto"}}, no_cache, busy)
    assert not wait.wait_hint("onebase", profile, queue, no_cache, "refresh_blocked=low")
    assert not wait.wait_hint("onebase", profile, queue, {"cache": {"token": []}}, busy)
