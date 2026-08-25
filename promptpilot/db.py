"""SQLite database layer."""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import DB_DIR, DB_PATH
from .models import (
    FindingStatus,
    Stats,
    TaskCreate,
    TaskInDB,
    TaskStatus,
    WorkflowArtifactCreate,
    WorkflowArtifactInDB,
    WorkflowCreate,
    WorkflowEventCreate,
    WorkflowEventInDB,
    WorkflowFindingInDB,
    WorkflowFindingUpsert,
    WorkflowInDB,
    WorkflowRoundCreate,
    WorkflowRoundInDB,
    WorkflowRunCreate,
    WorkflowRunInDB,
    WorkflowUpdate,
)

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

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    candidate_branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    current_round INTEGER NOT NULL DEFAULT 0,
    state_version INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_rounds (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    round_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    base_sha TEXT,
    candidate_sha TEXT,
    audit_sha TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT,
    UNIQUE(workflow_id, round_no)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    round_id TEXT NOT NULL REFERENCES workflow_rounds(id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    task_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    input_sha256 TEXT NOT NULL,
    output_sha256 TEXT,
    output_json TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(round_id, role, attempt_no)
);

CREATE TABLE IF NOT EXISTS workflow_findings (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    fingerprint TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen_round INTEGER NOT NULL,
    last_seen_round INTEGER NOT NULL,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(workflow_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS workflow_artifacts (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    round_id TEXT NOT NULL REFERENCES workflow_rounds(id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(round_id, kind, sha256)
);

CREATE TABLE IF NOT EXISTS workflow_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    round_id TEXT REFERENCES workflow_rounds(id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES workflow_runs(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS workflow_events_no_update
BEFORE UPDATE ON workflow_events
BEGIN
    SELECT RAISE(ABORT, 'workflow_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS workflow_events_no_delete
BEFORE DELETE ON workflow_events
BEGIN
    SELECT RAISE(ABORT, 'workflow_events is append-only');
END;

CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_rounds_workflow ON workflow_rounds(workflow_id, round_no);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_round ON workflow_runs(round_id, role, attempt_no);
CREATE INDEX IF NOT EXISTS idx_workflow_findings_workflow ON workflow_findings(workflow_id, status, severity);
CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_round ON workflow_artifacts(round_id, kind);
CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow ON workflow_events(workflow_id, seq);
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

WORKFLOW_SCHEMA_VERSION = "workflow_orchestrator_w0_v1"


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
        # W0 is the first versioned schema addition. The tables themselves use
        # CREATE IF NOT EXISTS so this safely upgrades both fresh and legacy
        # databases; the marker gives future workflow migrations an explicit,
        # queryable baseline instead of guessing from column presence.
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (WORKFLOW_SCHEMA_VERSION, _now()),
        )


def _insert_task(conn: sqlite3.Connection, task: TaskCreate) -> TaskInDB:
    """Insert a queue task inside the caller's transaction."""
    cur = conn.execute(
        """INSERT INTO tasks (prompt, working_dir, provider, status, priority,
           scheduled_at, created_at, max_retries, skip_permissions, model,
           session_id, parent_task_id, tg_chat_id, recurrence, task_timeout,
           detached, keep_pane, herdr_target, machine, worktree)
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


def create_task(task: TaskCreate) -> TaskInDB:
    with _connect() as conn:
        return _insert_task(conn, task)


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


# --- Workflow orchestrator storage (W0) -----------------------------------


class WorkflowConflictError(RuntimeError):
    """Optimistic-lock or idempotency conflict in workflow storage."""


class WorkflowNotFoundError(LookupError):
    """The requested workflow aggregate does not exist."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json_dump(value) -> str:
    """Canonical JSON used for hashes, equality and durable DB payloads."""
    return json.dumps(value if value is not None else {}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


_JSON_MISSING = object()


def _json_load(value: Optional[str], default=_JSON_MISSING):
    if not value:
        return {} if default is _JSON_MISSING else default
    return json.loads(value)


def _row_to_workflow(row: sqlite3.Row) -> WorkflowInDB:
    data = dict(row)
    data["config"] = _json_load(data.pop("config_json"))
    for field in ("created_at", "updated_at", "completed_at"):
        data[field] = _parse_dt(data[field])
    return WorkflowInDB(**data)


def _row_to_workflow_round(row: sqlite3.Row) -> WorkflowRoundInDB:
    data = dict(row)
    data["summary"] = _json_load(data.pop("summary_json"), None)
    for field in ("started_at", "completed_at"):
        data[field] = _parse_dt(data[field])
    return WorkflowRoundInDB(**data)


def _row_to_workflow_run(row: sqlite3.Row) -> WorkflowRunInDB:
    data = dict(row)
    data["output"] = _json_load(data.pop("output_json"), None)
    for field in ("started_at", "completed_at"):
        data[field] = _parse_dt(data[field])
    return WorkflowRunInDB(**data)


def _row_to_workflow_finding(row: sqlite3.Row) -> WorkflowFindingInDB:
    data = dict(row)
    data["payload"] = _json_load(data.pop("payload_json"))
    return WorkflowFindingInDB(**data)


def _row_to_workflow_artifact(row: sqlite3.Row) -> WorkflowArtifactInDB:
    data = dict(row)
    data["metadata"] = _json_load(data.pop("metadata_json"))
    return WorkflowArtifactInDB(**data)


def _row_to_workflow_event(row: sqlite3.Row) -> WorkflowEventInDB:
    data = dict(row)
    data["payload"] = _json_load(data.pop("payload_json"))
    data["created_at"] = _parse_dt(data["created_at"])
    return WorkflowEventInDB(**data)


def _append_workflow_event(conn: sqlite3.Connection,
                           event: WorkflowEventCreate) -> WorkflowEventInDB:
    """Insert exactly once, rejecting reuse of a key for different content."""
    payload_json = _json_dump(event.payload)
    existing = conn.execute(
        "SELECT * FROM workflow_events WHERE idempotency_key = ?",
        (event.idempotency_key,),
    ).fetchone()
    if existing:
        same = (
            existing["workflow_id"] == event.workflow_id
            and existing["round_id"] == event.round_id
            and existing["run_id"] == event.run_id
            and existing["event_type"] == event.event_type
            and existing["payload_json"] == payload_json
        )
        if not same:
            raise WorkflowConflictError(
                f"idempotency key {event.idempotency_key!r} already belongs "
                "to a different workflow event"
            )
        return _row_to_workflow_event(existing)

    cur = conn.execute(
        """INSERT INTO workflow_events
           (workflow_id, round_id, run_id, event_type, payload_json,
            idempotency_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (event.workflow_id, event.round_id, event.run_id, event.event_type,
         payload_json, event.idempotency_key, _now()),
    )
    row = conn.execute(
        "SELECT * FROM workflow_events WHERE seq = ?", (cur.lastrowid,)
    ).fetchone()
    return _row_to_workflow_event(row)


def append_workflow_event(event: WorkflowEventCreate) -> WorkflowEventInDB:
    """Public append-only event writer with strict idempotency semantics."""
    with _connect(immediate=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM workflows WHERE id = ?", (event.workflow_id,)
        ).fetchone():
            raise WorkflowNotFoundError(event.workflow_id)
        return _append_workflow_event(conn, event)


def create_workflow(data: WorkflowCreate, workflow_id: str = None) -> WorkflowInDB:
    workflow_id = workflow_id or _new_id("wf")
    now = _now()
    with _connect(immediate=True) as conn:
        try:
            conn.execute(
                """INSERT INTO workflows
                   (id, slug, objective, repository_path, candidate_branch,
                    status, current_round, state_version, config_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'draft', 0, 0, ?, ?, ?)""",
                (workflow_id, data.slug, data.objective, data.repository_path,
                 data.candidate_branch, _json_dump(data.config), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflictError(
                f"workflow id or slug already exists: {workflow_id}/{data.slug}"
            ) from exc
        _append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=workflow_id,
            event_type="workflow.created",
            idempotency_key=f"workflow.created:{workflow_id}",
            payload={
                "slug": data.slug,
                "objective": data.objective,
                "repository_path": data.repository_path,
                "candidate_branch": data.candidate_branch,
                "config": data.config,
            },
        ))
        row = conn.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        return _row_to_workflow(row)


def get_workflow(workflow_id: str, *, conn=None) -> Optional[WorkflowInDB]:
    def _query(c):
        row = c.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        return _row_to_workflow(row) if row else None

    if conn is not None:
        return _query(conn)
    with _connect() as c:
        return _query(c)


def get_workflow_by_ref(reference: str) -> Optional[WorkflowInDB]:
    """Resolve a workflow by its opaque id or human-friendly unique slug."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workflows WHERE id = ?",
            (reference,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM workflows WHERE slug = ?", (reference,)
            ).fetchone()
        return _row_to_workflow(row) if row else None


def list_workflows(status: str = None, limit: int = 50,
                   offset: int = 0) -> list[WorkflowInDB]:
    with _connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT * FROM workflows WHERE status = ?
                   ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workflows ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_workflow(row) for row in rows]


def update_workflow(workflow_id: str, update: WorkflowUpdate) -> WorkflowInDB:
    changes = update.model_dump(exclude={"expected_version"}, exclude_none=True)
    if not changes:
        raise ValueError("workflow update contains no changed fields")
    if "config" in changes:
        changes["config_json"] = _json_dump(changes.pop("config"))

    allowed = {"objective", "repository_path", "candidate_branch", "config_json"}
    if not set(changes).issubset(allowed):
        raise ValueError("unsupported workflow update field")

    with _connect(immediate=True) as conn:
        current = conn.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        if not current:
            raise WorkflowNotFoundError(workflow_id)
        if current["status"] != "draft":
            raise WorkflowConflictError(
                "workflow metadata can only be edited while status is draft"
            )
        if current["state_version"] != update.expected_version:
            raise WorkflowConflictError(
                f"workflow version is {current['state_version']}, "
                f"expected {update.expected_version}"
            )

        new_version = current["state_version"] + 1
        assignments = [f"{field} = ?" for field in changes]
        args = list(changes.values())
        assignments.extend(["state_version = ?", "updated_at = ?"])
        args.extend([new_version, _now(), workflow_id, update.expected_version])
        cur = conn.execute(
            f"UPDATE workflows SET {', '.join(assignments)} "
            "WHERE id = ? AND state_version = ?",
            args,
        )
        if cur.rowcount != 1:
            raise WorkflowConflictError("workflow changed concurrently")

        event_changes = dict(changes)
        if "config_json" in event_changes:
            event_changes["config"] = _json_load(event_changes.pop("config_json"))
        _append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=workflow_id,
            event_type="workflow.updated",
            idempotency_key=f"workflow.updated:{workflow_id}:v{new_version}",
            payload={"version": new_version, "changes": event_changes},
        ))
        row = conn.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        return _row_to_workflow(row)


def create_workflow_round(data: WorkflowRoundCreate,
                          round_id: str = None) -> WorkflowRoundInDB:
    round_id = round_id or _new_id("round")
    with _connect(immediate=True) as conn:
        workflow = conn.execute(
            "SELECT * FROM workflows WHERE id = ?", (data.workflow_id,)
        ).fetchone()
        if not workflow:
            raise WorkflowNotFoundError(data.workflow_id)
        try:
            conn.execute(
                """INSERT INTO workflow_rounds
                   (id, workflow_id, round_no, status, base_sha, started_at)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (round_id, data.workflow_id, data.round_no, data.base_sha, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflictError(
                f"round {data.round_no} already exists for {data.workflow_id}"
            ) from exc
        conn.execute(
            """UPDATE workflows
               SET current_round = MAX(current_round, ?),
                   state_version = state_version + 1, updated_at = ?
               WHERE id = ?""",
            (data.round_no, _now(), data.workflow_id),
        )
        _append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=data.workflow_id,
            round_id=round_id,
            event_type="round.created",
            idempotency_key=f"round.created:{round_id}",
            payload={"round_no": data.round_no, "base_sha": data.base_sha},
        ))
        row = conn.execute(
            "SELECT * FROM workflow_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        return _row_to_workflow_round(row)


def get_workflow_round(round_id: str) -> Optional[WorkflowRoundInDB]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_rounds WHERE id = ?", (round_id,)
        ).fetchone()
        return _row_to_workflow_round(row) if row else None


def list_workflow_rounds(workflow_id: str) -> list[WorkflowRoundInDB]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM workflow_rounds WHERE workflow_id = ?
               ORDER BY round_no""",
            (workflow_id,),
        ).fetchall()
        return [_row_to_workflow_round(row) for row in rows]


def create_workflow_run(data: WorkflowRunCreate,
                        run_id: str = None) -> WorkflowRunInDB:
    run_id = run_id or _new_id("run")
    with _connect(immediate=True) as conn:
        round_row = conn.execute(
            """SELECT workflow_id FROM workflow_rounds
               WHERE id = ?""",
            (data.round_id,),
        ).fetchone()
        if not round_row or round_row["workflow_id"] != data.workflow_id:
            raise WorkflowNotFoundError(data.round_id)
        try:
            conn.execute(
                """INSERT INTO workflow_runs
                   (id, workflow_id, round_id, role, attempt_no, task_id,
                    status, input_sha256)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (run_id, data.workflow_id, data.round_id, data.role.value,
                 data.attempt_no, data.task_id, data.input_sha256),
            )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflictError(
                f"run already exists for role {data.role.value} "
                f"attempt {data.attempt_no}"
            ) from exc
        _append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=data.workflow_id,
            round_id=data.round_id,
            run_id=run_id,
            event_type="run.created",
            idempotency_key=f"run.created:{run_id}",
            payload={
                "role": data.role.value,
                "attempt_no": data.attempt_no,
                "task_id": data.task_id,
                "input_sha256": data.input_sha256,
            },
        ))
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _row_to_workflow_run(row)


def get_workflow_run(run_id: str) -> Optional[WorkflowRunInDB]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _row_to_workflow_run(row) if row else None


def list_workflow_runs(round_id: str) -> list[WorkflowRunInDB]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM workflow_runs WHERE round_id = ?
               ORDER BY role, attempt_no""",
            (round_id,),
        ).fetchall()
        return [_row_to_workflow_run(row) for row in rows]


def upsert_workflow_finding(data: WorkflowFindingUpsert) -> WorkflowFindingInDB:
    """Materialise current finding state while preserving every change as an event."""
    with _connect(immediate=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM workflows WHERE id = ?", (data.workflow_id,)
        ).fetchone():
            raise WorkflowNotFoundError(data.workflow_id)
        current = conn.execute(
            """SELECT * FROM workflow_findings
               WHERE workflow_id = ? AND fingerprint = ?""",
            (data.workflow_id, data.fingerprint),
        ).fetchone()
        payload_json = _json_dump(data.payload)
        if current:
            unchanged = (
                current["severity"] == data.severity.value
                and current["category"] == data.category
                and current["title"] == data.title
                and current["status"] == data.status.value
                and current["last_seen_round"] == data.round_no
                and current["payload_json"] == payload_json
            )
            if unchanged:
                return _row_to_workflow_finding(current)
            reopened = (
                current["status"] in (
                    FindingStatus.RESOLVED.value,
                    FindingStatus.ACCEPTED_RISK.value,
                )
                and data.status in (FindingStatus.OPEN, FindingStatus.REOPENED)
            )
            reopen_count = current["reopen_count"] + int(reopened)
            conn.execute(
                """UPDATE workflow_findings
                   SET severity = ?, category = ?, title = ?, status = ?,
                       last_seen_round = ?, reopen_count = ?, payload_json = ?
                   WHERE id = ?""",
                (data.severity.value, data.category, data.title,
                 data.status.value, data.round_no, reopen_count, payload_json,
                 current["id"]),
            )
            finding_id = current["id"]
            event_type = "finding.reopened" if reopened else "finding.updated"
        else:
            finding_id = _new_id("finding")
            conn.execute(
                """INSERT INTO workflow_findings
                   (id, workflow_id, fingerprint, severity, category, title,
                    status, first_seen_round, last_seen_round, reopen_count,
                    payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (finding_id, data.workflow_id, data.fingerprint,
                 data.severity.value, data.category, data.title,
                 data.status.value, data.round_no, data.round_no, payload_json),
            )
            event_type = "finding.created"

        event_payload = {
            "finding_id": finding_id,
            "fingerprint": data.fingerprint,
            "severity": data.severity.value,
            "status": data.status.value,
            "round_no": data.round_no,
            "payload": data.payload,
        }
        event_digest = hashlib.sha256(
            _json_dump(event_payload).encode("utf-8")
        ).hexdigest()
        _append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=data.workflow_id,
            event_type=event_type,
            idempotency_key=f"finding:{finding_id}:{event_digest}",
            payload=event_payload,
        ))
        row = conn.execute(
            "SELECT * FROM workflow_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        return _row_to_workflow_finding(row)


def list_workflow_findings(workflow_id: str,
                           status: str = None) -> list[WorkflowFindingInDB]:
    with _connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT * FROM workflow_findings
                   WHERE workflow_id = ? AND status = ?
                   ORDER BY first_seen_round, fingerprint""",
                (workflow_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM workflow_findings WHERE workflow_id = ?
                   ORDER BY first_seen_round, fingerprint""",
                (workflow_id,),
            ).fetchall()
        return [_row_to_workflow_finding(row) for row in rows]


def create_workflow_artifact(data: WorkflowArtifactCreate,
                             artifact_id: str = None) -> WorkflowArtifactInDB:
    artifact_id = artifact_id or _new_id("artifact")
    with _connect(immediate=True) as conn:
        round_row = conn.execute(
            "SELECT workflow_id FROM workflow_rounds WHERE id = ?",
            (data.round_id,),
        ).fetchone()
        if not round_row or round_row["workflow_id"] != data.workflow_id:
            raise WorkflowNotFoundError(data.round_id)
        if data.run_id:
            run_row = conn.execute(
                """SELECT workflow_id, round_id FROM workflow_runs
                   WHERE id = ?""",
                (data.run_id,),
            ).fetchone()
            if (not run_row or run_row["workflow_id"] != data.workflow_id
                    or run_row["round_id"] != data.round_id):
                raise WorkflowNotFoundError(data.run_id)

        existing = conn.execute(
            """SELECT * FROM workflow_artifacts
               WHERE round_id = ? AND kind = ? AND sha256 = ?""",
            (data.round_id, data.kind, data.sha256),
        ).fetchone()
        metadata_json = _json_dump(data.metadata)
        if existing:
            same = (
                existing["workflow_id"] == data.workflow_id
                and existing["run_id"] == data.run_id
                and existing["path"] == data.path
                and existing["size_bytes"] == data.size_bytes
                and existing["metadata_json"] == metadata_json
            )
            if not same:
                raise WorkflowConflictError(
                    "artifact identity already exists with different metadata"
                )
            return _row_to_workflow_artifact(existing)

        conn.execute(
            """INSERT INTO workflow_artifacts
               (id, workflow_id, round_id, run_id, kind, path, sha256,
                size_bytes, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact_id, data.workflow_id, data.round_id, data.run_id,
             data.kind, data.path, data.sha256, data.size_bytes, metadata_json),
        )
        _append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=data.workflow_id,
            round_id=data.round_id,
            run_id=data.run_id,
            event_type="artifact.created",
            idempotency_key=f"artifact.created:{artifact_id}",
            payload={
                "artifact_id": artifact_id,
                "kind": data.kind,
                "path": data.path,
                "sha256": data.sha256,
                "size_bytes": data.size_bytes,
                "metadata": data.metadata,
            },
        ))
        row = conn.execute(
            "SELECT * FROM workflow_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return _row_to_workflow_artifact(row)


def list_workflow_artifacts(workflow_id: str,
                            round_id: str = None) -> list[WorkflowArtifactInDB]:
    with _connect() as conn:
        if round_id:
            rows = conn.execute(
                """SELECT * FROM workflow_artifacts
                   WHERE workflow_id = ? AND round_id = ? ORDER BY kind, id""",
                (workflow_id, round_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM workflow_artifacts
                   WHERE workflow_id = ? ORDER BY round_id, kind, id""",
                (workflow_id,),
            ).fetchall()
        return [_row_to_workflow_artifact(row) for row in rows]


def list_workflow_events(workflow_id: str, after_seq: int = 0,
                         limit: int = 200) -> list[WorkflowEventInDB]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM workflow_events
               WHERE workflow_id = ? AND seq > ?
               ORDER BY seq LIMIT ?""",
            (workflow_id, after_seq, limit),
        ).fetchall()
        return [_row_to_workflow_event(row) for row in rows]


# Auto-init on import
init_db()
