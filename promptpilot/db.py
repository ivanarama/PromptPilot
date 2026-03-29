"""SQLite database layer."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .config import DB_DIR, DB_PATH
from .models import Stats, TaskCreate, TaskInDB, TaskStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt TEXT NOT NULL,
    working_dir TEXT,
    provider TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 5,
    scheduled_at TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    exit_code INTEGER,
    model_used TEXT,
    skip_permissions INTEGER DEFAULT 0,
    session_id TEXT,
    parent_task_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_runnable ON tasks(status, priority, next_run_at);
"""

MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN provider TEXT",
    "ALTER TABLE tasks ADD COLUMN model_used TEXT",
    "ALTER TABLE tasks ADD COLUMN skip_permissions INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN session_id TEXT",
    "ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    if val is None:
        return None
    return datetime.fromisoformat(val)


def _row_to_task(row: sqlite3.Row) -> TaskInDB:
    d = dict(row)
    for field in ("scheduled_at", "next_run_at", "created_at", "started_at", "completed_at"):
        d[field] = _parse_dt(d[field])
    return TaskInDB(**d)


@contextmanager
def _connect():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # Run migrations for existing databases
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists


def create_task(task: TaskCreate) -> TaskInDB:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (prompt, working_dir, provider, status, priority, scheduled_at, created_at, max_retries, skip_permissions, session_id, parent_task_id)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.prompt,
                task.working_dir,
                task.provider,
                task.priority,
                task.scheduled_at.isoformat() if task.scheduled_at else None,
                _now(),
                task.max_retries,
                int(task.skip_permissions),
                task.session_id,
                task.parent_task_id,
            ),
        )
        return get_task(cur.lastrowid, conn=conn)


def get_task(task_id: int, *, conn=None) -> Optional[TaskInDB]:
    def _query(c):
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    if conn:
        return _query(conn)
    with _connect() as c:
        return _query(c)


def list_tasks(
    status: Optional[TaskStatus] = None,
    limit: int = 50,
    offset: int = 0,
):
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status.value, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_task(r) for r in rows]


def get_next_runnable() -> Optional[TaskInDB]:
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM tasks
               WHERE status IN ('pending', 'rate_limited')
                 AND (scheduled_at IS NULL OR scheduled_at <= ?)
                 AND (next_run_at IS NULL OR next_run_at <= ?)
               ORDER BY priority ASC, created_at ASC
               LIMIT 1""",
            (now, now),
        ).fetchone()
        if row:
            task = _row_to_task(row)
            conn.execute(
                "UPDATE tasks SET status = 'running', started_at = ? WHERE id = ?",
                (_now(), task.id),
            )
            return task
        return None


def mark_completed(task_id: int, result: str, exit_code: int = 0, model_used: str = None, session_id: str = None):
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'completed', result = ?, exit_code = ?, completed_at = ?, model_used = ?, session_id = COALESCE(?, session_id) WHERE id = ?",
            (result, exit_code, _now(), model_used, session_id, task_id),
        )


def mark_failed(task_id: int, error: str, exit_code: int = 1):
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'failed', error = ?, exit_code = ?, completed_at = ? WHERE id = ?",
            (error, exit_code, _now(), task_id),
        )


def mark_rate_limited(task_id: int, next_run_at: datetime, error: str = None):
    with _connect() as conn:
        conn.execute(
            """UPDATE tasks
               SET status = 'rate_limited',
                   next_run_at = ?,
                   retry_count = retry_count + 1,
                   error = COALESCE(?, error)
               WHERE id = ?""",
            (next_run_at.isoformat(), error, task_id),
        )


def cancel_task(task_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'cancelled', completed_at = ? WHERE id = ? AND status IN ('pending', 'rate_limited')",
            (_now(), task_id),
        )
        return cur.rowcount > 0


def update_priority(task_id: int, priority: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ? AND status IN ('pending', 'rate_limited')",
            (priority, task_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


def get_stats() -> Stats:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        data = {row["status"]: row["cnt"] for row in rows}
        total = sum(data.values())
        return Stats(
            pending=data.get("pending", 0),
            running=data.get("running", 0),
            completed=data.get("completed", 0),
            failed=data.get("failed", 0),
            rate_limited=data.get("rate_limited", 0),
            cancelled=data.get("cancelled", 0),
            total=total,
        )


def recover_running():
    """Reset any 'running' tasks back to 'pending' (crash recovery)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'pending', started_at = NULL WHERE status = 'running'"
        )


def reset_task(task_id: int) -> bool:
    """Reset a single stuck 'running' task back to 'pending'."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'pending', started_at = NULL WHERE id = ? AND status = 'running'",
            (task_id,),
        )
        return cur.rowcount > 0


def purge_old(before_days: int = 7) -> int:
    with _connect() as conn:
        cutoff = datetime.now(timezone.utc)
        from datetime import timedelta
        cutoff = (cutoff - timedelta(days=before_days)).isoformat()
        cur = conn.execute(
            "DELETE FROM tasks WHERE status IN ('completed', 'failed', 'cancelled') AND completed_at < ?",
            (cutoff,),
        )
        return cur.rowcount


# Auto-init on import
init_db()
