import sqlite3

import pytest

from promptpilot import db


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
