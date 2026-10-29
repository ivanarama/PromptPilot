"""Public control API under contention and unrelated scheduler history."""
import asyncio
from contextlib import closing
import sqlite3

import httpx

from promptpilot.api import app
from promptpilot.models import TaskCreate


def request_action(series_id):
    async def send():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8420") as client:
            return await client.post(f"/api/schedule/{series_id}/action", json={"action": "run_now"})

    return asyncio.run(send())


def test_busy_control_refuses_without_mutation_and_closes_failed_connections(
        isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="control", recurrence="1h"))
    before = isolated_db.get_task(task.id).scheduled_at
    original_connect = sqlite3.connect
    connections, closed = [], set()

    class ObservedConnection(sqlite3.Connection):
        def close(self):
            super().close()
            closed.add(id(self))

    def connect(*args, **kwargs):
        kwargs.update(timeout=0.02, factory=ObservedConnection)
        value = original_connect(*args, **kwargs)
        connections.append(value)
        return value

    with closing(original_connect(isolated_db.DB_PATH)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(sqlite3, "connect", connect)
        response = request_action(task.series_id)
        writer.rollback()
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "состояние" in response.json()["detail"]
    assert connections and all(id(value) in closed for value in connections)
    assert isolated_db.get_task(task.id).scheduled_at == before


def test_post_commit_read_failure_does_not_replay_the_action(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="control", recurrence="1h"))
    action = isolated_db.series_action
    calls = []

    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return action(*args, **kwargs)

    def busy_read(_series_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(isolated_db, "series_action", counted)
    monkeypatch.setattr(isolated_db, "get_series", busy_read)
    response = request_action(task.series_id)
    assert response.status_code == 503
    assert "состояние" in response.json()["detail"]
    assert len(calls) == 1
    assert isolated_db.get_task(task.id).scheduled_at is not None


def test_unrelated_sql_failure_is_not_reported_as_retryable_busy(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="control", recurrence="1h"))

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such table: broken_schema")

    monkeypatch.setattr(isolated_db, "series_action", broken)
    response = request_action(task.series_id)
    assert response.status_code == 500
    assert "retry-after" not in response.headers


def test_one_series_control_response_is_bounded_independently_of_other_history(
        isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="target", recurrence="1h"))
    stamp = "2026-10-28T00:00:00+00:00"
    with isolated_db._connect() as connection:
        connection.executemany(
            """INSERT INTO task_series(id,title,prompt,base_recurrence,created_at,updated_at)
               VALUES (?,?,'unrelated','1h',?,?)""",
            ((number, f"unrelated {number}", stamp, stamp) for number in range(10000, 15000)),
        )
        connection.executemany(
            "INSERT INTO tasks(prompt,status,created_at,series_id) VALUES ('history','completed',?,?)",
            ((stamp, number) for number in range(10000, 15000)),
        )
    original_connect = sqlite3.connect
    work = [0]

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)

        def budget():
            work[0] += 1000
            return int(work[0] > 100000)

        connection.set_progress_handler(budget, 1000)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    response = request_action(task.series_id)
    # Count SQLite work rather than flaky elapsed time: acknowledging one
    # action must not scan every unrelated series and its complete history.
    assert response.status_code == 200, response.text
    series = response.json()["series"]
    assert series["id"] == task.series_id and series["next_task_id"] == task.id
    assert series["runs"] == 1
