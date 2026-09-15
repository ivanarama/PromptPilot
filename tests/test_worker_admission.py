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


def test_task_opens_fence_after_route_and_before_provider(monkeypatch):
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
        return {"action": "prompt", "mode": "skill", "prompt": "run"}

    monkeypatch.setattr(pipeline_insights, "execution_route", route)
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: {"test-provider": {"executor": "herdr"}},
    )

    def provider(*_args, **_kwargs):
        assert admission_complete.is_set() is True
        calls.append("provider")

    monkeypatch.setattr(worker, "_execute_herdr_task", provider)

    worker._execute_task_body(task, admission_complete)

    assert calls == ["route", "provider"]


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
