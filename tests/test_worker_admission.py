from concurrent.futures import Future
from datetime import datetime, timezone
import signal
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from promptpilot import worker
from promptpilot.models import TaskCreate


def test_admission_fence_blocks_next_claim_until_current_admission_finishes():
    fence = worker._AdmissionFence()

    first = fence.begin()

    assert fence.wait(0) is False
    with pytest.raises(RuntimeError, match="still pending"):
        fence.begin()

    first.set()
    assert fence.wait(0.1) is True

    second = fence.begin()
    assert second is not first
    assert fence.wait(0) is False


def test_worker_lane_policy_prefers_home_slots_then_borrows(monkeypatch):
    from promptpilot import pipeline_insights

    profile = {
        "title": "Pipeline", "repository": "owner/repo",
        "queues": [
            {"id": "triage", "series_contains": " - TRIAGE"},
            {"id": "plan", "series_contains": " - PLAN"},
            {"id": "fix", "series_contains": " - FIX"},
            {"id": "review", "series_contains": " - REVIEW"},
            {"id": "merge", "series_contains": " - MERGE"},
        ],
        "scheduler": {"lanes": [
            {"id": "integration", "queues": ["merge", "review"]},
            {"id": "production", "queues": ["review", "fix"]},
            {"id": "intake", "queues": ["triage", "plan"]},
        ]},
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"repo": profile})
    policy = pipeline_insights.worker_lane_policy()

    def task(number, stage):
        return SimpleNamespace(
            id=number, series_id=number, series_title=f"Repo - {stage}",
            prompt=f"Repo - {stage}", priority=1,
            created_at=datetime(2026, 1, number, tzinfo=timezone.utc),
        )

    assert pipeline_insights.worker_lane_rank(
        task(1, "MERGE"), policy)[1] == "repo:integration"
    assert pipeline_insights.worker_lane_rank(
        task(2, "REVIEW"), policy)[1] == "repo:production"
    assert pipeline_insights.worker_lane_rank(
        task(3, "TRIAGE"), policy)[1] == "repo:intake"
    assert pipeline_insights.worker_lane_rank(
        task(4, "REVIEW"), policy, {"repo:production"})[1] == "repo:integration"
    assert pipeline_insights.worker_lane_rank(
        task(5, "REVIEW"), policy,
        {"repo:integration", "repo:production"})[1] == "repo:intake"
    assert pipeline_insights.worker_lane_rank(
        task(6, "FIX"), policy,
        {"repo:integration", "repo:production", "repo:intake"}) is None


def test_lane_claim_is_atomic_and_queue_order_beats_fifo(
        isolated_db, monkeypatch):
    from promptpilot import pipeline_insights

    profile = {
        "title": "Pipeline", "repository": "owner/repo",
        "queues": [
            {"id": "fix", "series_contains": " - FIX"},
            {"id": "review", "series_contains": " - REVIEW"},
            {"id": "merge", "series_contains": " - MERGE"},
        ],
        "scheduler": {"lanes": [
            {"id": "integration", "queues": ["merge", "review"]},
            {"id": "production", "queues": ["review", "fix"]},
        ]},
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"repo": profile})
    fix = isolated_db.create_task(TaskCreate(prompt="Repo - FIX", recurrence="1h"))
    merge = isolated_db.create_task(TaskCreate(prompt="Repo - MERGE", recurrence="1h"))

    claimed, lane = worker._claim_next_task()

    assert claimed.id == merge.id
    assert claimed.id != fix.id
    assert lane == "repo:integration"


def test_worker_recovers_then_warms_pipeline_before_claiming(monkeypatch):
    events = []
    handlers = {}

    monkeypatch.setattr(
        worker.signal, "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    monkeypatch.setattr(
        worker, "_warm_pipeline_runtime", lambda: events.append("warm"))
    monkeypatch.setattr(
        worker, "live_task_ids", lambda: events.append("live") or set())
    monkeypatch.setattr(
        worker.db, "recover_running", lambda **_kwargs: events.append("recover"))
    monkeypatch.setattr(
        worker.db, "repair_active_series_occurrences", lambda: [])
    monkeypatch.setattr(worker.db, "touch_worker_heartbeat", lambda _pid: None)
    monkeypatch.setattr(worker.db, "mark_worker_stopped", lambda _pid: None)
    monkeypatch.setattr(worker, "_code_snapshot", lambda: None)
    monkeypatch.setattr(worker, "CONCURRENCY", 1)
    monkeypatch.setattr(worker.time, "sleep", lambda _delay: None)

    from promptpilot import workflows
    monkeypatch.setattr(workflows, "sync_all_tasks", lambda: None)

    monkeypatch.setattr(worker.db, "is_paused", lambda: False)
    monkeypatch.setattr(worker, "enough_memory", lambda: True)

    def stop_on_first_claim(**_kwargs):
        events.append("claim")
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return None

    monkeypatch.setattr(worker.db, "get_next_runnable", stop_on_first_claim)

    worker.run_worker()

    assert events[:4] == ["live", "recover", "warm", "claim"]


def test_task_opens_fence_only_after_herdr_provider_started(monkeypatch):
    admission_complete = threading.Event()
    calls = []
    task = SimpleNamespace(
        id=41,
        series_id=7,
        prompt="Example - MERGE",
        provider="test-provider",
        working_dir=None,
        machine=None,
    )

    from promptpilot import pipeline_insights

    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)

    def route(*_args, **_kwargs):
        assert admission_complete.is_set() is False
        calls.append("route")
        return {
            "action": "prompt", "mode": "skill", "prompt": "run",
            "profile_id": "onebase", "queue_id": "merge",
        }

    monkeypatch.setattr(pipeline_insights, "execution_route", route)
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: {"test-provider": {"executor": "herdr"}},
    )

    def provider(*_args, **kwargs):
        assert admission_complete.is_set() is False
        assert kwargs["require_closing_verdict"] is True
        calls.append("provider")
        worker._signal_admission_complete(kwargs["admission_complete"])
        assert admission_complete.is_set() is True

    monkeypatch.setattr(worker, "_execute_herdr_task", provider)

    worker._execute_task_body(task, admission_complete)

    assert calls == ["route", "provider"]


def test_sqlite_busy_control_write_is_retried(monkeypatch):
    attempts = []
    sleeps = []

    def operation():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "saved"

    monkeypatch.setattr(worker.time, "sleep", sleeps.append)

    assert worker._retry_sqlite_busy(operation, "test write") == "saved"
    assert attempts == [1, 2, 3]
    assert sleeps == [0.1, 0.5]


def test_sqlite_non_lock_error_is_not_retried(monkeypatch):
    attempts = []

    def operation():
        attempts.append(1)
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(
        worker.time, "sleep",
        lambda _delay: (_ for _ in ()).throw(AssertionError("unexpected retry")),
    )

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        worker._retry_sqlite_busy(operation, "test write")
    assert attempts == [1]


def test_pipeline_success_without_closing_verdict_is_rejected(monkeypatch):
    task = SimpleNamespace(
        id=42, retry_count=0, max_retries=3, tg_chat_id=None,
        keep_pane=False, machine=None,
    )
    failed = []
    completed = []

    monkeypatch.setattr(
        "promptpilot.herdr_exec.run_in_herdr",
        lambda *_args, **_kwargs: {
            "ok": True, "rate_limited": False, "retry_reason": "",
            "cancelled": False, "env_failure": "", "verdict": "",
            "output": "Проверки ещё выполняются\n⢿  Running command...",
            "error": "",
        },
    )
    monkeypatch.setattr(worker, "_effective_timeout", lambda _task: None)
    monkeypatch.setattr(worker.db, "is_cancel_requested", lambda _task_id: False)
    monkeypatch.setattr(
        worker.db, "mark_failed",
        lambda task_id, error, exit_code=None: failed.append(
            (task_id, error, exit_code)),
    )
    monkeypatch.setattr(
        worker.db, "mark_completed",
        lambda *_args, **_kwargs: completed.append((_args, _kwargs)),
    )

    worker._execute_herdr_task(
        task, {"kind": "agy"}, prompt_override="OneBase - REVIEW",
        require_closing_verdict=True,
    )

    assert failed == [(
        42, "Pipeline provider returned success without a closing ИТОГ verdict", 1,
    )]
    assert completed == []


def test_blocked_notification_failure_does_not_detach_running_agent(monkeypatch):
    task = SimpleNamespace(
        id=43, retry_count=0, max_retries=3, tg_chat_id=99,
        keep_pane=False, machine=None,
    )
    notifications = []
    failures = []

    def provider(*_args, **kwargs):
        kwargs["on_blocked"]("pane-43")
        return {
            "ok": False, "rate_limited": False, "retry_reason": "",
            "cancelled": False, "env_failure": "", "verdict": "",
            "output": "", "error": "provider stopped after approval wait",
        }

    def notification(*_args, **_kwargs):
        notifications.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("promptpilot.herdr_exec.run_in_herdr", provider)
    monkeypatch.setattr(worker, "_effective_timeout", lambda _task: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(worker.db, "is_cancel_requested", lambda _task_id: False)
    monkeypatch.setattr(worker.db, "add_notification", notification)
    monkeypatch.setattr(
        worker.db, "mark_failed",
        lambda task_id, error, exit_code=None: failures.append(
            (task_id, error, exit_code)),
    )

    worker._execute_herdr_task(task, {"kind": "agy"})

    assert len(notifications) == 3
    assert failures == [(43, "provider stopped after approval wait", 1)]


def test_pool_boundary_opens_fence_after_early_crash(monkeypatch):
    admission_complete = threading.Event()

    def crash(_task, _admission_complete):
        raise RuntimeError("before admission")

    monkeypatch.setattr(worker, "execute_task", crash)

    with pytest.raises(RuntimeError, match="before admission"):
        worker._execute_task_with_admission_fence(
            SimpleNamespace(id=42), admission_complete)

    assert admission_complete.is_set() is True


def test_task_without_provider_opens_fence_after_reconciliation(monkeypatch):
    admission_complete = threading.Event()
    task = SimpleNamespace(id=43)
    calls = []

    def before_signal(name):
        assert admission_complete.is_set() is False
        calls.append(name)

    monkeypatch.setattr(
        worker, "_execute_task_inner",
        lambda _task, _admission_complete: before_signal("body"),
    )
    monkeypatch.setattr(worker, "_recur_after_run", lambda _task: before_signal("recur"))
    monkeypatch.setattr(worker.db, "get_task", lambda _task_id: None)

    from promptpilot import workflows

    monkeypatch.setattr(workflows, "sync_task", lambda _task_id: before_signal("sync"))
    monkeypatch.setattr(
        workflows, "advance_linked_task",
        lambda _task_id: before_signal("advance"),
    )

    worker.execute_task(task, admission_complete)

    assert admission_complete.is_set() is True
    assert calls == ["sync", "body", "recur", "sync", "advance"]


def test_equal_priority_and_creation_time_claim_by_id(isolated_db):
    first = isolated_db.create_task(TaskCreate(prompt="first", priority=1))
    second = isolated_db.create_task(TaskCreate(prompt="second", priority=1))
    with isolated_db._connect() as conn:
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id IN (?, ?)",
            ("2026-09-15T00:00:00+00:00", first.id, second.id),
        )

    assert isolated_db.get_next_runnable().id == first.id


def test_unhandled_future_recovery_retries_with_backoff(monkeypatch):
    task = SimpleNamespace(
        id=44,
        started_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )
    future = Future()
    future.set_exception(RuntimeError("database is locked"))
    in_flight = {future: ("local:repo", task)}
    recoveries = {}
    attempts = []

    def recover(candidate, exc):
        attempts.append((candidate, exc))
        return len(attempts) > 2

    monkeypatch.setattr(worker, "_fail_stuck", recover)
    monkeypatch.setattr(worker, "POLL_INTERVAL", 2)
    monkeypatch.setattr(worker, "MAX_DELAY", 30)

    worker._reap_futures(in_flight, recoveries, now=100.0)

    assert in_flight == {}
    assert len(attempts) == 1
    recovery = next(iter(recoveries.values()))
    assert recovery["lock"] == "local:repo"
    assert recovery["retry_at"] == 102.0

    worker._reap_futures(in_flight, recoveries, now=101.9)
    assert len(attempts) == 1
    assert recoveries

    worker._reap_futures(in_flight, recoveries, now=102.0)
    assert len(attempts) == 2
    recovery = next(iter(recoveries.values()))
    assert recovery["retry_at"] == 106.0

    worker._reap_futures(in_flight, recoveries, now=105.9)
    assert len(attempts) == 2
    assert recoveries

    worker._reap_futures(in_flight, recoveries, now=106.0)
    assert len(attempts) == 3
    assert recoveries == {}


def test_delayed_internal_recovery_cannot_fail_newer_attempt(isolated_db):
    created = isolated_db.create_task(TaskCreate(prompt="attempt fenced"))
    first = isolated_db.get_next_runnable()
    assert first.id == created.id
    assert isolated_db.reset_task(created.id) is True
    second = isolated_db.get_next_runnable()
    assert second.id == created.id
    assert second.started_at != first.started_at

    assert worker._fail_stuck(first, RuntimeError("old crash")) is True

    current = isolated_db.get_task(created.id)
    assert current.status.value == "running"
    assert current.started_at == second.started_at
    assert current.error is None
