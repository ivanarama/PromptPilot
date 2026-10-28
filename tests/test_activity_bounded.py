from contextlib import contextmanager
import sqlite3

import pytest

from promptpilot import db


@pytest.fixture
def activity_store(tmp_path, monkeypatch):
    path = tmp_path / "activity.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE tasks(
            id INTEGER PRIMARY KEY, series_id INTEGER, status TEXT, verdict TEXT,
            started_at TEXT, completed_at TEXT, result TEXT)""")
        conn.execute("""CREATE INDEX activity_metadata ON tasks(
            series_id, id, status, started_at, completed_at, verdict)""")
        start = "2026-10-27T08:00:00+00:00"
        end = "2026-10-27T08:00:05+00:00"
        rows = [
            (i, 1 if i % 2 else 2, "completed", "ПУСТО", start, end,
             "historical " + "x" * 4096)
            for i in range(1, 401)
        ]
        rows.extend([
            (1000, 1, "failed", "НЕ СМОГ", start, end,
             "first\nИТОГ: НЕ СМОГ (kept)\n\n--- Meta ---\nTokens: 11 in / 7 out"),
            (1001, 2, "cancelled", None, start, None, "cancelled result"),
            (1100, 1, "running", None, start, None, "unfinished"),
            (1200, 3, "pending", None, None, None, "no terminal"),
        ])
        conn.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", rows)

    observed = {"rows": 0, "calls": 0}

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            observed["rows"] += len(rows)
            return rows

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, *args):
            observed["calls"] += 1
            return Cursor(self.conn.execute(*args))

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield Connection(conn)
        finally:
            conn.close()

    monkeypatch.setattr(db, "_connect", connect)
    return observed


def test_activity_reads_only_latest_terminal_payload_per_series(activity_store):
    values = db.pipeline_series_activity([1, 2, 3])

    assert {key: value["task_id"] for key, value in values.items()} == {
        1: 1000, 2: 1001,
    }
    assert activity_store["rows"] == 2, (
        "Historical result payloads must not be loaded and discarded in Python"
    )
    assert activity_store["calls"] == 1


def test_activity_preserves_summary_duration_usage_and_cancelled_state(activity_store):
    values = db.pipeline_series_activity([1, 2])

    assert values[1] == {
        "task_id": 1000, "status": "failed", "verdict": "НЕ СМОГ",
        "at": "2026-10-27T08:00:05+00:00", "duration_seconds": 5,
        "summary": "ИТОГ: НЕ СМОГ (kept)", "input_tokens": 11, "output_tokens": 7,
    }
    assert values[2] == {
        "task_id": 1001, "status": "cancelled", "verdict": None, "at": None,
        "duration_seconds": None, "summary": "cancelled result",
        "input_tokens": None, "output_tokens": None,
    }


def test_activity_empty_input_performs_no_query(activity_store):
    assert db.pipeline_series_activity([]) == {}
    assert activity_store == {"rows": 0, "calls": 0}


def test_activity_normalizes_requested_ids_and_ignores_unfinished_or_missing(activity_store):
    values = db.pipeline_series_activity([2, 2, None, 3, 999])

    assert list(values) == [2]
    assert values[2]["task_id"] == 1001
    assert activity_store["rows"] == 1
