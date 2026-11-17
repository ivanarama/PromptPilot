"""The public worker survives only a proved pre-claim SQLite refusal."""
from contextlib import contextmanager
import signal
import sqlite3

from click.testing import CliRunner
import pytest

from promptpilot import pipeline_insights, worker
from promptpilot.cli import cli
from promptpilot.models import TaskCreate


@pytest.mark.parametrize("ranked", [False, True])
@pytest.mark.parametrize("pause_after_wait", [False, True])
def test_public_worker_retries_real_begin_contention_without_claim_or_provider(
        isolated_db, monkeypatch, ranked, pause_after_wait):
    task = isolated_db.create_task(TaskCreate(prompt="TEMP contention fixture"))
    real_connect = sqlite3.connect
    writer = real_connect(str(isolated_db.DB_PATH), timeout=0)
    real_claim = isolated_db.get_next_runnable
    handlers, claims, sleeps, executions, statements = {}, [], [], [], []
    locked = False

    def connect(*args, **kwargs):
        # Exercise real SQLITE_BUSY promptly; production's timeout is unchanged.
        kwargs["timeout"] = 0
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(isolated_db.sqlite3, "connect", connect)
    monkeypatch.setattr(worker.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(worker, "live_task_ids", lambda: set())
    monkeypatch.setattr(worker, "_code_snapshot", lambda: None)
    monkeypatch.setattr(worker, "enough_memory", lambda: True)
    monkeypatch.setattr(worker, "CONCURRENCY", 1)
    monkeypatch.setattr(isolated_db, "touch_worker_heartbeat", lambda _pid: None)
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {})
    monkeypatch.setattr(pipeline_insights, "worker_lane_policy", lambda: {} if ranked else None)
    monkeypatch.setattr(pipeline_insights, "worker_lane_rank", lambda *_args: ((0,), "fixture:lane"))

    def claim(**kwargs):
        nonlocal locked
        claims.append(kwargs)
        if len(claims) == 1:
            writer.execute("BEGIN IMMEDIATE")
            locked = True
        return real_claim(**kwargs)

    def sleep(delay):
        nonlocal locked
        sleeps.append(delay)
        if locked:
            # The failed attempt never entered the election body or mutated a task.
            row = writer.execute("SELECT status,started_at FROM tasks WHERE id=?", (task.id,)).fetchone()
            assert row == ("pending", None)
            assert executions == []
            assert not any("UPDATE tasks SET status = 'running'" in value for value in statements)
            writer.rollback()
            locked = False
            if pause_after_wait:
                with real_connect(str(isolated_db.DB_PATH)) as control:
                    control.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('worker_paused','1')")
        elif pause_after_wait:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        else:
            pytest.fail("worker failed to claim after the lock was released")

    def execute(claimed):
        assert not locked
        executions.append(claimed.id)
        isolated_db.mark_completed(claimed.id, "TEMP execution fixture", exit_code=0)
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(isolated_db, "get_next_runnable", claim)
    monkeypatch.setattr(worker.time, "sleep", sleep)
    monkeypatch.setattr(worker, "execute_task", execute)
    try:
        result = CliRunner().invoke(cli, ["worker"])
    finally:
        writer.rollback()
        writer.close()
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert sleeps and all(delay == worker.POLL_INTERVAL for delay in sleeps)
    retained = isolated_db.get_task(task.id)
    if pause_after_wait:
        assert len(claims) == 1 and executions == []
        assert retained.status.value == "pending" and retained.started_at is None
    else:
        assert len(claims) == 2 and executions == [task.id]
        assert retained.status.value == "completed"


def test_busy_from_inside_claim_body_is_not_a_retryable_begin_failure(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="TEMP body failure"))

    def rank(_task):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError) as error:
        isolated_db.get_next_runnable(order_key_fn=rank)
    assert type(error.value) is sqlite3.OperationalError
    assert isolated_db.get_task(task.id).status.value == "pending"


def test_commit_failure_is_not_wrapped_as_retryable_begin_contention(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="TEMP unknown commit failure"))
    original = isolated_db._connect

    @contextmanager
    def fail_after_body(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            yield connection
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(isolated_db, "_connect", fail_after_body)
    with pytest.raises(sqlite3.OperationalError) as error:
        isolated_db.get_next_runnable()
    assert type(error.value) is sqlite3.OperationalError
    monkeypatch.setattr(isolated_db, "_connect", original)
    assert isolated_db.get_task(task.id).status.value == "pending"


def test_unknown_error_after_commit_cannot_be_retried_as_an_unstarted_claim(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="TEMP committed but lost outcome"))
    real_connect = sqlite3.connect

    class CommitThenRaise(sqlite3.Connection):
        def commit(self):
            super().commit()
            raise sqlite3.OperationalError("database is locked")

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=CommitThenRaise)

    monkeypatch.setattr(isolated_db.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError) as error:
        isolated_db.get_next_runnable()
    assert type(error.value) is sqlite3.OperationalError
    monkeypatch.setattr(isolated_db.sqlite3, "connect", real_connect)
    retained = isolated_db.get_task(task.id)
    assert retained.status.value == "running" and retained.started_at is not None


def test_actual_shared_cache_locked_begin_is_typed_before_any_election_write(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="TEMP SQLITE_LOCKED fixture"))
    real_connect = sqlite3.connect
    uri = isolated_db.DB_PATH.as_uri() + "?cache=shared"
    writer = real_connect(uri, uri=True, timeout=0)

    def connect(_path, **_kwargs):
        return real_connect(uri, uri=True, timeout=0)

    monkeypatch.setattr(isolated_db.sqlite3, "connect", connect)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(isolated_db.TaskClaimBusy) as error:
            isolated_db.get_next_runnable()
        if hasattr(error.value, "sqlite_errorcode"):
            assert error.value.sqlite_errorcode & 0xff == 6  # Actual LOCKED, not BUSY.
        assert writer.execute("SELECT status,started_at FROM tasks WHERE id=?", (task.id,)).fetchone() == ("pending", None)
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("code,expected", [(5, True), (6, True), (261, True), (262, True), (10, False), (11, False)])
def test_primary_or_extended_code_takes_precedence_over_message(isolated_db, code, expected):
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code
    assert isolated_db._is_sqlite_claim_contention(error) is expected


@pytest.mark.parametrize("message,expected", [
    ("database is locked", True), ("database table is locked: tasks", True),
    ("database schema is locked: main", True), ("disk I/O error: database is locked", False),
    ("not a database", False), ("database is locked unexpectedly", False),
])
def test_python310_without_sqlite_code_uses_only_canonical_lock_messages(isolated_db, message, expected):
    error = sqlite3.OperationalError(message)
    assert not hasattr(error, "sqlite_errorcode")
    assert isolated_db._is_sqlite_claim_contention(error) is expected


@pytest.mark.parametrize("message", ["disk I/O error", "database disk image is malformed", "unable to open database file"])
def test_non_contention_begin_error_remains_an_error(isolated_db, monkeypatch, message):
    @contextmanager
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError(message)
        yield  # pragma: no cover

    monkeypatch.setattr(isolated_db, "_connect", fail)
    with pytest.raises(sqlite3.OperationalError, match=message) as error:
        isolated_db.get_next_runnable()
    assert type(error.value) is sqlite3.OperationalError
