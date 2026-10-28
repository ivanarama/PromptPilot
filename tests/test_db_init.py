import sqlite3

import pytest

from promptpilot import db
from promptpilot.models import TaskCreate


def test_series_covering_index_upgrades_existing_database(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="retained", recurrence="1h"))
    with isolated_db._connect() as conn:
        conn.execute("DROP INDEX idx_tasks_series_summary")
        conn.execute("CREATE INDEX idx_tasks_series ON tasks(series_id, id)")
    before = isolated_db.list_series()

    isolated_db.init_db()

    assert isolated_db.list_series() == before
    assert isolated_db.get_task(task.id).prompt == "retained"
    with isolated_db._connect() as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(tasks)")}
    assert "idx_tasks_series_summary" in indexes
    assert "idx_tasks_series" not in indexes



def test_init_db_does_not_rebuild_the_replaced_series_index(isolated_db, monkeypatch):
    # Migrations replay on every start: creating the old index before dropping
    # it would scan the whole task table on each server/worker/bot start.
    statements = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)
    isolated_db.init_db()

    assert not any("idx_tasks_series ON" in statement for statement in statements)
    assert any("idx_tasks_series_summary" in statement for statement in statements)

def test_init_db_retries_transient_busy(monkeypatch):
    attempts = []
    sleeps = []

    def initialize():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        if len(attempts) == 2:
            raise sqlite3.OperationalError("database is busy")
        return "ready"

    monkeypatch.setattr(db, "_init_db_once", initialize)
    monkeypatch.setattr(db, "INIT_DB_BUSY_DELAYS", (0.1, 0.5, 1.0))
    monkeypatch.setattr(db.time, "sleep", sleeps.append)

    assert db.init_db() == "ready"
    assert len(attempts) == 3
    assert sleeps == [0.1, 0.5]


def test_init_db_does_not_mask_non_lock_operational_error(monkeypatch):
    monkeypatch.setattr(
        db, "_init_db_once",
        lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk I/O error")),
    )
    monkeypatch.setattr(
        db.time, "sleep",
        lambda _delay: (_ for _ in ()).throw(
            AssertionError("non-lock failure must not be retried")),
    )

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db.init_db()


def test_init_db_stops_after_bounded_busy_retries(monkeypatch):
    attempts = []
    sleeps = []

    def locked():
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_init_db_once", locked)
    monkeypatch.setattr(db, "INIT_DB_BUSY_DELAYS", (0.1, 0.5))
    monkeypatch.setattr(db.time, "sleep", sleeps.append)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.init_db()

    assert len(attempts) == 3
    assert sleeps == [0.1, 0.5]
