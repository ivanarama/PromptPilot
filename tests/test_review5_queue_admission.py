"""Ревью reliability-v1, п.5: честная позиция в очереди.

Прежняя проекция — просто номер по ORDER BY среди ожидающих: paused-серия и
отложенная задача стояли впереди готовой, занятость рабочего дерева не
учитывалась. Новая позиция — ранг среди ГОТОВЫХ по правилам допуска воркера
(read-only worker.admission_snapshot), не-готовые получают wait_reason.
"""

from datetime import datetime, timedelta, timezone

import asyncio

import httpx
import pytest

from promptpilot import db, worker, pipeline_insights
from promptpilot.models import TaskCreate
from promptpilot.worker import admission_snapshot


def _paused_series_task(isolated_db, tmp):
    with db._connect(immediate=True) as conn:
        conn.execute(
            """INSERT INTO task_series
               (title, prompt, working_dir, base_recurrence, priority,
                created_at, updated_at, paused)
               VALUES ('paused-flow', 'paused-flow', ?, '1h', 1, ?, ?, 1)""",
            (str(tmp / "d1"), db._now(), db._now()))
        series_id = conn.execute(
            "SELECT id FROM task_series ORDER BY id DESC LIMIT 1").fetchone()[0]
    return db.create_task(TaskCreate(
        prompt="paused series task", working_dir=str(tmp / "d1"),
        priority=1, series_id=series_id))


def test_admission_snapshot_ranks_only_ready(isolated_db, tmp_path, monkeypatch):
    """Сценарий репро: paused-серия и будущая задача не занимают позиций,
    занятое рабочее дерево тоже; готовая получает честную позицию."""
    monkeypatch.setattr(worker, "CONCURRENCY", 2)
    t_paused = _paused_series_task(isolated_db, tmp_path)
    t_future = db.create_task(TaskCreate(
        prompt="future task", working_dir=str(tmp_path / "d2"),
        priority=1,
        scheduled_at=(datetime.now(timezone.utc)
                      + timedelta(hours=2)).isoformat()))
    t_busy_a = db.create_task(TaskCreate(
        prompt="same dir A", working_dir=str(tmp_path / "shared"), priority=2))
    t_busy_b = db.create_task(TaskCreate(
        prompt="same dir B", working_dir=str(tmp_path / "shared"), priority=3))
    t_ready = db.create_task(TaskCreate(
        prompt="ready task", working_dir=str(tmp_path / "d3"), priority=5))

    snap = admission_snapshot()

    assert snap[t_paused.id]["wait_reason"] == "paused_series"
    assert snap[t_paused.id]["queue_position"] is None
    assert snap[t_future.id]["wait_reason"] == "scheduled_future"
    assert snap[t_busy_a.id]["ready"] is True
    assert snap[t_busy_a.id]["queue_position"] == 1, (
        "первая задача свободного дерева — готова с позицией 1"
    )
    assert snap[t_busy_b.id]["wait_reason"] == "busy_working_dir", (
        "второй агент в занятом дереве — не готова, с причиной"
    )
    assert snap[t_ready.id]["queue_position"] == 2, (
        "готовая задача стоит сразу за первой готовой, без пропусков"
        " на paused/future/busy"
    )


def test_admission_snapshot_respects_running_lock(isolated_db, tmp_path, monkeypatch):
    """RUNNING-задача занимает свой lock_key: второй агент того же дерева
    не готов, чужое дерево — готово."""
    monkeypatch.setattr(worker, "CONCURRENCY", 2)
    running = db.create_task(TaskCreate(
        prompt="running", working_dir=str(tmp_path / "busy")))
    with db._connect(immediate=True) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?",
                     (running.id,))
    waiting_same = db.create_task(TaskCreate(
        prompt="wants same dir", working_dir=str(tmp_path / "busy")))
    waiting_other = db.create_task(TaskCreate(
        prompt="wants other dir", working_dir=str(tmp_path / "free")))

    snap = admission_snapshot()
    assert snap[waiting_same.id]["wait_reason"] == "busy_working_dir"
    assert snap[waiting_other.id]["queue_position"] == 1


def test_api_tasks_expose_wait_reason(isolated_db, tmp_path):
    import asyncio
    import httpx
    from promptpilot.api import app

    t_paused = _paused_series_task(isolated_db, tmp_path)
    t_ready = db.create_task(TaskCreate(
        prompt="ready", working_dir=str(tmp_path / "d9")))

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8420",
        ) as client:
            return await client.get("/api/tasks")

    tasks = {t["id"]: t for t in asyncio.run(run()).json()}
    assert tasks[t_paused.id]["wait_reason"] == "paused_series"
    assert tasks[t_paused.id]["queue_position"] is None
    assert tasks[t_ready.id]["queue_position"] == 1
    assert tasks[t_ready.id]["wait_reason"] is None


def test_admission_snapshot_query_count_is_constant(isolated_db, tmp_path,
                                                    monkeypatch):
    """Регрессия N+1: число SQL-запросов admission_snapshot не растёт с
    длиной очереди (замер на ревизии adde138: ~3 запроса на задачу,
    500 задач ≈ 600 запросов и ~1.2 с на каждый GET /api/tasks)."""
    import sqlite3

    counts = []
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda _sql: counts.append(1))
        return conn

    def seed(n):
        for i in range(n):
            db.create_task(TaskCreate(
                prompt=f"q{i}", working_dir=str(tmp_path / f"d{i}")))

    def measure():
        counts.clear()
        admission_snapshot()
        return len(counts)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    seed(10)
    small = measure()
    seed(50)  # +40 задач в очереди
    large = measure()
    assert small == large, (
        f"число SQL-запросов должно быть константой, а не расти с очередью: "
        f"{small} при 10 задачах против {large} при 60"
    )


@pytest.mark.parametrize("concurrency", [1, 3])
@pytest.mark.parametrize("with_running", [False, True])
def test_api_first_position_matches_real_claim(
        isolated_db, tmp_path, monkeypatch, concurrency, with_running):
    from promptpilot.api import app

    monkeypatch.setattr(worker, "CONCURRENCY", concurrency)
    # These optional policies are outside the documented base projection.
    monkeypatch.setattr(pipeline_insights, "worker_lane_policy", lambda: None)
    monkeypatch.setattr(pipeline_insights, "worker_budget_fairness_policy", lambda: None)
    _paused_series_task(isolated_db, tmp_path)
    db.create_task(TaskCreate(
        prompt="future", priority=1,
        scheduled_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
    busy = []
    if with_running:
        running = db.create_task(TaskCreate(prompt="running", working_dir=str(tmp_path / "X")))
        with db._connect(immediate=True) as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running.id,))
        if concurrency > 1:
            busy.append(worker.lock_key(running))
    first = db.create_task(TaskCreate(prompt="first", working_dir=str(tmp_path / "X"), priority=2))
    second = db.create_task(TaskCreate(prompt="second", working_dir=str(tmp_path / "X"), priority=3))
    third = db.create_task(TaskCreate(prompt="third", working_dir=str(tmp_path / "Y"), priority=5))

    async def rows():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8420",
        ) as client:
            response = await client.get("/api/tasks")
            assert response.status_code == 200
            return {row["id"]: row for row in response.json()}

    public = asyncio.run(rows())
    expected = third if with_running and concurrency > 1 else first
    assert public[expected.id]["queue_position"] == 1
    if concurrency == 1:
        assert [public[t.id]["queue_position"] for t in [first, second, third]] == [1, 2, 3]
    claimed, _lane = worker._claim_next_task(busy_keys=busy)
    assert claimed.id == expected.id
    if concurrency > 1:
        busy.append(worker.lock_key(claimed))
    public = asyncio.run(rows())
    ready = [row for row in public.values() if row["queue_position"] == 1]
    next_task, _lane = worker._claim_next_task(busy_keys=busy)
    assert (next_task.id if next_task else None) == (ready[0]["id"] if ready else None)


def test_completed_task_page_does_not_scan_pending_queue(isolated_db, tmp_path, monkeypatch):
    from promptpilot.api import app

    task = db.create_task(TaskCreate(prompt="completed"))
    db.mark_completed(task.id, "done", exit_code=0)
    db.create_task(TaskCreate(prompt="pending", working_dir=str(tmp_path)))
    monkeypatch.setattr(worker, "admission_snapshot", lambda: pytest.fail("terminal page scanned queue"))

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8420",
        ) as client:
            return await client.get("/api/tasks", params={"status": "completed"})

    response = asyncio.run(request())
    assert response.status_code == 200 and [row["id"] for row in response.json()] == [task.id]


@pytest.mark.parametrize("concurrency,expected_calls", [(1, 0), (3, 1)])
def test_snapshot_resolves_each_distinct_lock_once(
        isolated_db, tmp_path, monkeypatch, concurrency, expected_calls):
    monkeypatch.setattr(worker, "CONCURRENCY", concurrency)
    original = worker.lock_key
    calls = []

    def counted(task):
        calls.append(task.working_dir)
        return original(task)

    monkeypatch.setattr(worker, "lock_key", counted)
    for index in range(25):
        db.create_task(TaskCreate(prompt=str(index), working_dir=str(tmp_path)))
    admission_snapshot()
    assert len(calls) == expected_calls
