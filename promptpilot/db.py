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
    recurrence TEXT,
    task_timeout INTEGER,
    detached INTEGER NOT NULL DEFAULT 0,
    keep_pane INTEGER NOT NULL DEFAULT 1,
    herdr_target TEXT,
    machine TEXT,
    worktree INTEGER NOT NULL DEFAULT 0,
    worktree_path TEXT,
    worktree_branch TEXT,
    note TEXT,
    verdict TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    tg_chat_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    pane_id TEXT,
    machine TEXT
);

CREATE TABLE IF NOT EXISTS prompt_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    machine TEXT,
    pane_id TEXT,
    agent TEXT,
    agent_session TEXT,
    project TEXT,
    prompt TEXT NOT NULL,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_runnable ON tasks(status, priority, next_run_at);
CREATE INDEX IF NOT EXISTS idx_prompt_log_project ON prompt_log(project);
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
    "ALTER TABLE tasks ADD COLUMN task_timeout INTEGER",
    "ALTER TABLE tasks ADD COLUMN detached INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN keep_pane INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE tasks ADD COLUMN herdr_target TEXT",
    "ALTER TABLE tasks ADD COLUMN machine TEXT",
    """CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        tg_chat_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        sent_at TEXT
    )""",
    "ALTER TABLE tasks ADD COLUMN worktree INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN worktree_path TEXT",
    "ALTER TABLE tasks ADD COLUMN worktree_branch TEXT",
    "ALTER TABLE tasks ADD COLUMN note TEXT",
    "ALTER TABLE tasks ADD COLUMN verdict TEXT",
    "ALTER TABLE tasks ADD COLUMN herdr_pane TEXT",
    "ALTER TABLE notifications ADD COLUMN pane_id TEXT",
    "ALTER TABLE notifications ADD COLUMN machine TEXT",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Canonical aware-UTC ISO string for the queue's string comparison.

    A naive datetime is read as local time — what a user typing
    '2026-08-13T15:00' means — then converted to UTC, so scheduled_at and
    next_run_at compare correctly against _now() ('...+00:00') regardless of
    who wrote them (CLI naive, bot aware, API with a 'Z' suffix).
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


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
def _connect(immediate: bool = False):
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if immediate:
        # Take the write lock before reading: a claim that decides on a stale
        # snapshot would hand the same task to two workers.
        conn.execute("BEGIN IMMEDIATE")
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
            """INSERT INTO tasks (prompt, working_dir, provider, status, priority, scheduled_at, created_at, max_retries, skip_permissions, model, session_id, parent_task_id, tg_chat_id, recurrence, task_timeout, detached, keep_pane, herdr_target, machine, worktree)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.prompt,
                task.working_dir,
                task.provider,
                task.priority,
                _to_utc_iso(task.scheduled_at),
                _now(),
                task.max_retries,
                int(task.skip_permissions),
                task.model,
                task.session_id,
                task.parent_task_id,
                task.tg_chat_id,
                task.recurrence,
                task.task_timeout,
                int(task.detached),
                int(task.keep_pane),
                task.herdr_target,
                task.machine,
                int(task.worktree),
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
    statuses: Optional[list] = None,
):
    """statuses — a list of TaskStatus/str values for multi-status filters
    (e.g. the bot's «Активные» view = pending+running+rate_limited)."""
    with _connect() as conn:
        if statuses:
            vals = [s.value if hasattr(s, "value") else s for s in statuses]
            marks = ",".join("?" * len(vals))
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE status IN ({marks})"
                " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*vals, limit, offset),
            ).fetchall()
        elif status:
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


def recent_working_dirs(limit: int = 8, machine: Optional[str] = None) -> list:
    """Distinct working_dirs of past tasks, most recent first — candidates for
    the bot's directory picker. Scoped to one machine (None = local): a path
    used on another host is no suggestion here."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT working_dir, MAX(id) AS mid FROM tasks"
            " WHERE working_dir IS NOT NULL AND working_dir != '' AND machine IS ?"
            " GROUP BY working_dir ORDER BY mid DESC LIMIT ?",
            (machine, limit),
        ).fetchall()
        return [r["working_dir"] for r in rows]


def get_next_runnable(busy_keys=(), key_fn=None) -> Optional[TaskInDB]:
    """Claim the highest-priority runnable task and mark it running.

    The whole select-then-claim runs under the write lock, so several workers
    (threads of one worker, or separate processes) never claim the same row.

    busy_keys/key_fn — when the caller already runs tasks, candidates whose
    key_fn(task) is in busy_keys are passed over: that is how two agents are
    kept out of one work tree while the queue keeps moving.
    """
    now = _now()
    busy = set(busy_keys or ())
    with _connect(immediate=True) as conn:
        # Sequential worker takes just the top task. When some keys are busy we
        # walk the whole runnable queue in priority order until a non-colliding
        # task is found — a hard LIMIT could hide a free task behind a wall of
        # conflicting ones. Rows are materialised before any UPDATE so claiming
        # one doesn't disturb the iteration.
        limit_clause = "" if busy else " LIMIT 1"
        rows = conn.execute(
            f"""SELECT * FROM tasks
               WHERE status IN ('pending', 'rate_limited')
                 AND (scheduled_at IS NULL OR scheduled_at <= ?)
                 AND (next_run_at IS NULL OR next_run_at <= ?)
               ORDER BY priority ASC, created_at ASC{limit_clause}""",
            (now, now),
        ).fetchall()
        for row in rows:
            task = _row_to_task(row)
            if busy and key_fn and key_fn(task) in busy:
                continue
            cur = conn.execute(
                """UPDATE tasks SET status = 'running', started_at = ?
                   WHERE id = ? AND status IN ('pending', 'rate_limited')""",
                (_now(), task.id),
            )
            if cur.rowcount:
                return task
        return None


def set_note(task_id: int, text: str) -> bool:
    """Attach the human's late word to a task — or clear it with an empty text.

    Lives on the task, not on the run: a task that comes back from a rate limit
    or an environment failure must carry the note into its next attempt, and a
    note written while a run is in flight has to survive that run being killed.
    """
    with _connect() as conn:
        cur = conn.execute("UPDATE tasks SET note = ? WHERE id = ?",
                           (text or None, task_id))
        return cur.rowcount > 0


def clear_note(task_id: int):
    """Drop the note once the task reached a verdict — it was for that attempt.

    Kept on requeue (rate limit, environment failure): there the attempt never
    got to act on it.
    """
    with _connect() as conn:
        conn.execute("UPDATE tasks SET note = NULL WHERE id = ?", (task_id,))


def set_verdict(task_id: int, verdict: str):
    with _connect() as conn:
        conn.execute("UPDATE tasks SET verdict = ? WHERE id = ?", (verdict or None, task_id))


def set_worktree(task_id: int, path: str, branch: str):
    """Record where a task's checkout landed, so the UI can point at the diff."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET worktree_path = ?, worktree_branch = ? WHERE id = ?",
            (path, branch, task_id),
        )


def _drop_cancel_flag(conn, task_id: int):
    """Clear a stale cancel request as a task leaves 'running'.

    Otherwise a cancel that lands just before the run finishes on its own (rate
    limit, env failure) leaves the flag set, and the task's next attempt — or a
    manual reset — is killed on sight by ghost of the old request."""
    conn.execute("DELETE FROM settings WHERE key = ?", (f"cancel_task:{task_id}",))


def mark_completed(task_id: int, result: str, exit_code: int = 0, model_used: str = None, session_id: str = None):
    clear_note(task_id)
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'completed', result = ?, exit_code = ?, completed_at = ?, model_used = ?, session_id = COALESCE(?, session_id) WHERE id = ?",
            (result, exit_code, _now(), model_used, session_id, task_id),
        )
        _drop_cancel_flag(conn, task_id)


def mark_failed(task_id: int, error: str, exit_code: int = 1):
    clear_note(task_id)
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'failed', error = ?, exit_code = ?, completed_at = ? WHERE id = ?",
            (error, exit_code, _now(), task_id),
        )
        _drop_cancel_flag(conn, task_id)


def mark_rate_limited(task_id: int, next_run_at: datetime, error: str = None):
    with _connect() as conn:
        conn.execute(
            """UPDATE tasks
               SET status = 'rate_limited',
                   next_run_at = ?,
                   retry_count = retry_count + 1,
                   error = COALESCE(?, error)
               WHERE id = ?""",
            (_to_utc_iso(next_run_at), error, task_id),
        )
        _drop_cancel_flag(conn, task_id)


def request_cancel(task_id: int) -> bool:
    """Ask the worker to kill a RUNNING task's process (worker polls this)."""
    # immediate: the read-then-write must not race a concurrent writer, or WAL
    # returns SQLITE_BUSY on the upgrade instead of waiting out the busy timeout.
    with _connect(immediate=True) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row or row["status"] != "running":
            return False
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
            (f"cancel_task:{task_id}",),
        )
        return True


def is_cancel_requested(task_id: int) -> bool:
    return get_setting(f"cancel_task:{task_id}") == "1"


def clear_cancel_request(task_id: int):
    with _connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (f"cancel_task:{task_id}",))


def mark_cancelled(task_id: int, note: str = None):
    clear_note(task_id)
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', completed_at = ?, error = COALESCE(?, error) WHERE id = ?",
            (_now(), note, task_id),
        )
        _drop_cancel_flag(conn, task_id)


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


def add_notification(tg_chat_id: int, message: str, task_id: int = None,
                     pane_id: str = None, machine: str = None):
    """Queue a free-form message for the bot's notify loop (e.g. blocked agent).

    pane_id/machine let the bot attach herdr action buttons (confirm/screen/
    reply) to the delivered message instead of sending bare text."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO notifications (task_id, tg_chat_id, message, created_at, pane_id, machine)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, tg_chat_id, message, _now(), pane_id, machine),
        )


def add_prompt_log(prompt: str, pane_id: str = None, machine: str = None,
                   agent: str = None, agent_session: str = None,
                   project: str = None, source: str = None):
    """Journal a prompt sent straight into a herdr pane (outside the task
    queue). Stores the project (pane cwd) and agent_session — a pointer into
    the agent's own transcript store (~/.claude/projects for Claude Code) —
    never the reply text: duplicating transcripts here would only be a worse
    copy of what the agent already keeps."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO prompt_log (created_at, machine, pane_id, agent,"
            " agent_session, project, prompt, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), machine, pane_id, agent, agent_session, project, prompt, source),
        )


def list_prompt_log(project: str = None, limit: int = 50) -> list:
    """Direct-to-pane prompts, newest first; project filters by pane cwd."""
    sql = "SELECT * FROM prompt_log"
    args = []
    if project:
        sql += " WHERE project = ?"
        args.append(project)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def set_task_pane(task_id: int, pane_id: str):
    """Remember the herdr pane a running task lives in — the bot's task card
    uses it for the «📺 Экран» button."""
    with _connect() as conn:
        conn.execute("UPDATE tasks SET herdr_pane = ? WHERE id = ?", (pane_id, task_id))


def get_unsent_notifications() -> list:
    """Return queued notifications not yet delivered, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE sent_at IS NULL ORDER BY id",
        ).fetchall()
        return [dict(r) for r in rows]


def mark_notification_sent(notification_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE notifications SET sent_at = ? WHERE id = ?",
            (_now(), notification_id),
        )


def recover_running(keep_ids=()):
    """Reset 'running' tasks back to 'pending' (crash recovery).

    keep_ids — tasks whose agent is demonstrably still running. Without it a
    worker restart yanks the queue out from under live agents, and a second
    worker process steals the first one's work on startup.
    """
    keep = [int(i) for i in keep_ids or ()]
    sql = "UPDATE tasks SET status = 'pending', started_at = NULL WHERE status = 'running'"
    if keep:
        sql += f" AND id NOT IN ({','.join('?' * len(keep))})"
    with _connect() as conn:
        conn.execute(sql, keep)


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
        conn.execute(
            "DELETE FROM notifications WHERE sent_at IS NOT NULL AND sent_at < ?",
            (cutoff,),
        )
        return cur.rowcount


# Auto-init on import
init_db()
