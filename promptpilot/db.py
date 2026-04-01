"""SQLite database layer."""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    model TEXT,
    session_id TEXT,
    parent_task_id INTEGER,
    tg_chat_id INTEGER,
    notified_at TEXT,
    recurrence TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    "ALTER TABLE tasks ADD COLUMN model TEXT",
    "ALTER TABLE tasks ADD COLUMN tg_chat_id INTEGER",
    "ALTER TABLE tasks ADD COLUMN notified_at TEXT",
    "ALTER TABLE tasks ADD COLUMN recurrence TEXT",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
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
            """INSERT INTO tasks (prompt, working_dir, provider, status, priority, scheduled_at, created_at, max_retries, skip_permissions, model, session_id, parent_task_id, tg_chat_id, recurrence)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.prompt,
                task.working_dir,
                task.provider,
                task.priority,
                task.scheduled_at.isoformat() if task.scheduled_at else None,
                _now(),
                task.max_retries,
                int(task.skip_permissions),
                task.model,
                task.session_id,
                task.parent_task_id,
                task.tg_chat_id,
                task.recurrence,
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


def get_setting(key: str, default: str = None) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def is_paused() -> bool:
    return get_setting("worker_paused", "0") == "1"


def parse_recurrence(recurrence: str) -> Optional[datetime]:
    """Parse recurrence string and return next run datetime (UTC).

    Supported formats:
      "30m"          — every 30 minutes
      "6h"           — every 6 hours
      "daily@09:00"  — every day at 09:00 UTC
    """
    if not recurrence:
        return None
    s = recurrence.strip().lower()
    # Nh or Nm
    m = re.fullmatch(r"(\d+)([mh])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(minutes=n) if unit == "m" else timedelta(hours=n)
        return datetime.now(timezone.utc) + delta
    # daily@HH:MM
    m = re.fullmatch(r"daily@(\d{1,2}):(\d{2})", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        now = datetime.now(timezone.utc)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return None


def get_cost_stats() -> dict:
    """Parse Cost lines from completed task results and aggregate."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT result, provider, completed_at FROM tasks WHERE status='completed' AND result LIKE '%Cost: $%' AND completed_at IS NOT NULL"
        ).fetchall()

    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    total = today = week = 0.0
    by_provider: dict = {}

    for row in rows:
        m = re.search(r"Cost: \$(\d+\.\d+)", row["result"] or "")
        if not m:
            continue
        cost = float(m.group(1))
        completed = row["completed_at"] or ""
        provider = row["provider"] or "claude"

        total += cost
        by_provider[provider] = round(by_provider.get(provider, 0.0) + cost, 6)
        if completed[:10] == today_str:
            today += cost
        if completed >= week_ago:
            week += cost

    return {"today": round(today, 6), "week": round(week, 6), "total": round(total, 6), "by_provider": by_provider}


def get_pending_notifications() -> list:
    """Return completed/failed tasks with a tg_chat_id that haven't been notified yet."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE tg_chat_id IS NOT NULL
                 AND notified_at IS NULL
                 AND status IN ('completed', 'failed')""",
        ).fetchall()
        return [_row_to_task(r) for r in rows]


def mark_notified(task_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET notified_at = ? WHERE id = ?",
            (_now(), task_id),
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
