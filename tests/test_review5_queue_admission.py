"""Ревью reliability-v1, п.5: честная позиция в очереди.

Прежняя проекция — просто номер по ORDER BY среди ожидающих: paused-серия и
отложенная задача стояли впереди готовой, занятость рабочего дерева не
учитывалась. Новая позиция — ранг среди ГОТОВЫХ по правилам допуска воркера
(read-only worker.admission_snapshot), не-готовые получают wait_reason.
"""

from datetime import datetime, timedelta, timezone

from promptpilot import db
from promptpilot.models import TaskCreate
from promptpilot.worker import admission_snapshot


def _paused_series_task(isolated_db, tmp):
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
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


def test_admission_snapshot_ranks_only_ready(isolated_db, tmp_path):
    """Сценарий репро: paused-серия и будущая задача не занимают позиций,
    занятое рабочее дерево тоже; готовая получает честную позицию."""
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


def test_admission_snapshot_respects_running_lock(isolated_db, tmp_path):
    """RUNNING-задача занимает свой lock_key: второй агент того же дерева
    не готов, чужое дерево — готово."""
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
            transport=transport, base_url="http://t",
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
