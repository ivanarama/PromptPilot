"""SQLite database layer."""

import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
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
    WorkflowPlanInDB,
    WorkflowStageInDB,
    WorkflowStageSpec,
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
    budget_wait_scope TEXT,
    budget_wait_started_at TEXT,
    pipeline_priority_restore INTEGER,
    verdict TEXT
    ,series_id INTEGER REFERENCES task_series(id)
);

CREATE TABLE IF NOT EXISTS task_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    working_dir TEXT,
    base_recurrence TEXT NOT NULL,
    temporary_recurrence TEXT,
    temporary_until TEXT,
    temporary_empty_limit INTEGER,
    temporary_empty_count INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT,
    effort TEXT,
    priority INTEGER NOT NULL DEFAULT 5,
    task_timeout INTEGER,
    paused INTEGER NOT NULL DEFAULT 0,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS pipeline_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_target_reservations (
    repository TEXT NOT NULL,
    stage TEXT NOT NULL,
    number INTEGER NOT NULL,
    head TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    task_started_at TEXT NOT NULL DEFAULT '',
    ownership_kind TEXT NOT NULL DEFAULT '',
    herdr_session_state TEXT NOT NULL DEFAULT '',
    herdr_pane_id TEXT NOT NULL DEFAULT '',
    herdr_tab_id TEXT NOT NULL DEFAULT '',
    herdr_workspace_id TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (repository, stage, number, head)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_runnable ON tasks(status, priority, next_run_at);
CREATE INDEX IF NOT EXISTS idx_prompt_log_project ON prompt_log(project);
CREATE INDEX IF NOT EXISTS idx_pipeline_snapshots_profile_time
    ON pipeline_snapshots(profile_id, captured_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_target_reservations_task
    ON pipeline_target_reservations(repository, task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_target_reservations_task_global
    ON pipeline_target_reservations(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_target_reservations_pr
    ON pipeline_target_reservations(repository, number);
CREATE INDEX IF NOT EXISTS idx_pipeline_target_reservations_expiry
    ON pipeline_target_reservations(expires_at);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    candidate_branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    current_round INTEGER NOT NULL DEFAULT 0,
    current_stage_id TEXT,
    state_version INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_plans (
    workflow_id TEXT PRIMARY KEY REFERENCES workflows(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'draft',
    planner_task_id INTEGER,
    input_sha256 TEXT,
    output_sha256 TEXT,
    output_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_stages (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    stage_type TEXT NOT NULL DEFAULT 'implementation',
    status TEXT NOT NULL DEFAULT 'draft',
    spec_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(workflow_id, position),
    UNIQUE(workflow_id, code)
);

CREATE TABLE IF NOT EXISTS workflow_rounds (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    round_no INTEGER NOT NULL,
    stage_id TEXT REFERENCES workflow_stages(id) ON DELETE RESTRICT,
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
CREATE INDEX IF NOT EXISTS idx_workflow_stages_workflow ON workflow_stages(workflow_id, position);
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
    "ALTER TABLE tasks ADD COLUMN effort TEXT",
    "ALTER TABLE notifications ADD COLUMN pane_id TEXT",
    "ALTER TABLE notifications ADD COLUMN machine TEXT",
    "ALTER TABLE workflows ADD COLUMN current_stage_id TEXT",
    """CREATE TABLE IF NOT EXISTS workflow_plans (
        workflow_id TEXT PRIMARY KEY REFERENCES workflows(id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'draft', planner_task_id INTEGER,
        input_sha256 TEXT, output_sha256 TEXT, output_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, approved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_stages (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
        position INTEGER NOT NULL, code TEXT NOT NULL, title TEXT NOT NULL,
        objective TEXT NOT NULL, stage_type TEXT NOT NULL DEFAULT 'implementation',
        status TEXT NOT NULL DEFAULT 'draft', spec_json TEXT NOT NULL DEFAULT '{}',
        summary_json TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
        UNIQUE(workflow_id, position), UNIQUE(workflow_id, code)
    )""",
    "ALTER TABLE workflow_rounds ADD COLUMN stage_id TEXT REFERENCES workflow_stages(id) ON DELETE RESTRICT",
    "CREATE INDEX IF NOT EXISTS idx_workflow_stages_workflow ON workflow_stages(workflow_id, position)",
    "ALTER TABLE tasks ADD COLUMN series_id INTEGER REFERENCES task_series(id)",
    """CREATE TABLE IF NOT EXISTS task_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        prompt TEXT NOT NULL, working_dir TEXT, base_recurrence TEXT NOT NULL,
        temporary_recurrence TEXT, temporary_until TEXT,
        temporary_empty_limit INTEGER, temporary_empty_count INTEGER NOT NULL DEFAULT 0,
        provider TEXT, model TEXT, effort TEXT, priority INTEGER NOT NULL DEFAULT 5,
        task_timeout INTEGER, paused INTEGER NOT NULL DEFAULT 0, ended_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tasks_series ON tasks(series_id, id)",
    """CREATE TABLE IF NOT EXISTS pipeline_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id TEXT NOT NULL, repository TEXT NOT NULL,
        captured_at TEXT NOT NULL, payload_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_snapshots_profile_time ON pipeline_snapshots(profile_id, captured_at)",
    """CREATE TABLE IF NOT EXISTS pipeline_target_reservations (
        repository TEXT NOT NULL, stage TEXT NOT NULL, number INTEGER NOT NULL,
        head TEXT NOT NULL, task_id INTEGER NOT NULL, token TEXT NOT NULL,
        reserved_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        PRIMARY KEY (repository, stage, number, head)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_target_reservations_task ON pipeline_target_reservations(repository, task_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_target_reservations_task_global ON pipeline_target_reservations(task_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_target_reservations_pr ON pipeline_target_reservations(repository, number)",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_target_reservations_expiry ON pipeline_target_reservations(expires_at)",
    "ALTER TABLE pipeline_target_reservations ADD COLUMN task_started_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE pipeline_target_reservations ADD COLUMN ownership_kind TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE pipeline_target_reservations ADD COLUMN herdr_session_state TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE pipeline_target_reservations ADD COLUMN herdr_pane_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE pipeline_target_reservations ADD COLUMN herdr_tab_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE pipeline_target_reservations ADD COLUMN herdr_workspace_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN budget_wait_scope TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tasks_budget_wait ON tasks(budget_wait_scope, status, priority)",
    "ALTER TABLE tasks ADD COLUMN pipeline_priority_restore INTEGER",
    "ALTER TABLE tasks ADD COLUMN budget_wait_started_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_tasks_budget_wait_fairness ON tasks(budget_wait_scope, status, budget_wait_started_at, priority, id)",
]

WORKFLOW_SCHEMA_VERSION = "workflow_orchestrator_w0_v1"
WORKFLOW_STAGE_SCHEMA_VERSION = "workflow_stage_planner_w3_v1"
PIPELINE_TARGET_RESERVATION_SCHEMA_VERSION = "pipeline_target_reservations_v1"
PIPELINE_TARGET_ATTEMPT_SCHEMA_VERSION = "pipeline_target_reservations_attempt_v2"
PIPELINE_TARGET_OWNERSHIP_SCHEMA_VERSION = "pipeline_target_reservations_ownership_v3"
PIPELINE_TARGET_HERDR_SCHEMA_VERSION = "pipeline_target_reservations_herdr_v4"
PIPELINE_BUDGET_WAITER_FAIRNESS_SCHEMA_VERSION = \
    "pipeline_github_budget_waiter_fairness_v1"


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
    # Internal scheduler metadata is deliberately not part of the public task
    # model/API. It identifies interruptible budget waits and a temporary
    # route-derived priority that must not leak into recurring successors.
    d.pop("budget_wait_scope", None)
    d.pop("budget_wait_started_at", None)
    d.pop("pipeline_priority_restore", None)
    for field in ("scheduled_at", "next_run_at", "created_at", "started_at", "completed_at"):
        d[field] = _parse_dt(d[field])
    return TaskInDB(**d)


@contextmanager
def _connect(immediate: bool = False):
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
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


INIT_DB_BUSY_DELAYS = (0.1, 0.5, 1.0, 2.0, 4.0)


def _init_db_once():
    with _connect() as conn:
        # Journal mode is persistent database state, not a per-connection
        # setting. Reasserting it on every connection needlessly turns an
        # otherwise read-only open into a lock-taking operation and can make a
        # reader fail while another connection owns the writer transaction.
        # Bootstrap it once; foreign_keys remains per-connection in _connect.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Run migrations for existing databases
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as exc:
                # ALTER ADD COLUMN is intentionally replayable for legacy
                # databases.  Do not misclassify SQLITE_BUSY/LOCKED, I/O or
                # corruption as an already-applied migration.
                if "duplicate column name" not in str(exc).lower():
                    raise
        # W0 is the first versioned schema addition. The tables themselves use
        # CREATE IF NOT EXISTS so this safely upgrades both fresh and legacy
        # databases; the marker gives future workflow migrations an explicit,
        # queryable baseline instead of guessing from column presence.
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (WORKFLOW_SCHEMA_VERSION, _now()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (WORKFLOW_STAGE_SCHEMA_VERSION, _now()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (PIPELINE_TARGET_RESERVATION_SCHEMA_VERSION, _now()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (PIPELINE_TARGET_ATTEMPT_SCHEMA_VERSION, _now()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (PIPELINE_TARGET_OWNERSHIP_SCHEMA_VERSION, _now()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, applied_at)
               VALUES (?, ?)""",
            (PIPELINE_TARGET_HERDR_SCHEMA_VERSION, _now()),
        )
        fairness_migrated = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (PIPELINE_BUDGET_WAITER_FAIRNESS_SCHEMA_VERSION,),
        ).fetchone()
        if fairness_migrated is None:
            migration_now = _now()
            # Existing durable reservation handoffs predate the age column.
            # Give active legacy waiters one common baseline instead of
            # leaving them permanently ineligible for the fairness baton.
            conn.execute(
                """UPDATE tasks SET budget_wait_started_at = ?
                   WHERE budget_wait_scope IS NOT NULL
                     AND budget_wait_started_at IS NULL
                     AND status IN ('pending', 'running')""",
                (migration_now,),
            )
            conn.execute(
                """INSERT INTO schema_migrations (version, applied_at)
                   VALUES (?, ?)""",
                (PIPELINE_BUDGET_WAITER_FAIRNESS_SCHEMA_VERSION,
                 migration_now),
            )
        _backfill_task_series(conn)


def init_db():
    """Initialize or migrate the database despite concurrent process startup.

    The tray launches worker and server together.  Both import this module and
    may reach the persistent ``journal_mode``/schema transaction at the same
    time; some SQLite PRAGMAs report BUSY immediately instead of honoring the
    connection timeout.  Retry the whole idempotent transaction, while still
    surfacing I/O, corruption and every other OperationalError unchanged.
    """
    for attempt in range(len(INIT_DB_BUSY_DELAYS) + 1):
        try:
            return _init_db_once()
        except sqlite3.OperationalError as exc:
            busy = any(marker in str(exc).lower() for marker in ("locked", "busy"))
            if not busy or attempt == len(INIT_DB_BUSY_DELAYS):
                raise
            time.sleep(INIT_DB_BUSY_DELAYS[attempt])


def _pipeline_target_identity(repository: str, stage: str, number: int,
                              head: str, task_id: int) -> tuple[str, str, int, str, int]:
    repository = str(repository or "").strip().lower()
    stage = str(stage or "").strip().lower()
    head = str(head or "").strip().lower()
    if (not repository or "/" not in repository or not stage
            or isinstance(number, bool) or not isinstance(number, int) or number <= 0
            or not re.fullmatch(r"[0-9a-f]{40}", head)
            or isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0):
        raise ValueError("invalid pipeline target reservation identity")
    return repository, stage, number, head, task_id


def _pipeline_reservation_ttl(ttl_seconds: int) -> int:
    if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)
            or not 60 <= ttl_seconds <= 86400):
        raise ValueError("pipeline target reservation TTL must be 60..86400 seconds")
    return ttl_seconds


def _reservation_dict(row: sqlite3.Row) -> dict:
    return {
        "repository": row["repository"], "stage": row["stage"],
        "number": int(row["number"]), "head": row["head"],
        "task_id": int(row["task_id"]),
        "task_started_at": row["task_started_at"],
        "ownership_kind": row["ownership_kind"],
        "herdr_session_state": row["herdr_session_state"],
        "herdr_pane_id": row["herdr_pane_id"],
        "herdr_tab_id": row["herdr_tab_id"],
        "herdr_workspace_id": row["herdr_workspace_id"],
        "token": row["token"],
        "reserved_at": row["reserved_at"], "expires_at": row["expires_at"],
    }


def reserve_pipeline_target(repository: str, stage: str, number: int,
                            head: str, task_id: int, ttl_seconds: int,
                            *, task_started_at: str | None = None,
                            ownership_kind: str | None = None,
                            now: Optional[datetime] = None) -> Optional[dict]:
    """Atomically reserve one exact project-pipeline target for a task.

    A task owns at most one target per repository. Repeating election for
    the same task is idempotent and renews its existing token; another task
    never steals a live target. Expired rows are crash-recovery leases.
    """
    repository, stage, number, head, task_id = _pipeline_target_identity(
        repository, stage, number, head, task_id)
    ttl_seconds = _pipeline_reservation_ttl(ttl_seconds)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_value = current.isoformat()
    expires_at = (current + timedelta(seconds=ttl_seconds)).isoformat()
    ownership_kind = str(ownership_kind or "").strip().lower()
    if ownership_kind not in {"", "headless", "herdr"}:
        raise ValueError("invalid pipeline provider ownership kind")
    with _connect(immediate=True) as conn:
        attempt = ""
        if task_started_at is not None:
            attempt = str(task_started_at).strip()
            try:
                parsed_attempt = datetime.fromisoformat(attempt)
            except (TypeError, ValueError):
                raise ValueError("invalid pipeline task attempt timestamp")
            if parsed_attempt.tzinfo is None:
                raise ValueError("pipeline task attempt timestamp must include timezone")
            task_row = conn.execute(
                "SELECT status, started_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if (task_row is None or task_row["status"] != "running"
                    or task_row["started_at"] != attempt):
                return None
        conn.execute(
            """DELETE FROM pipeline_target_reservations
               WHERE expires_at <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND (pipeline_target_reservations.task_started_at = ''
                            OR tasks.started_at = pipeline_target_reservations.task_started_at)
                 )""",
            (now_value,),
        )
        # A PR is the mutation boundary. Stage and head belong to its immutable
        # election envelope, but a transition between them must not admit a
        # second provider while the first exact attempt is still live.
        existing = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE repository = ? AND number = ?""",
            (repository, number),
        ).fetchone()
        if existing is not None:
            if (existing["stage"] != stage or existing["head"] != head
                    or int(existing["task_id"]) != task_id
                    or (attempt and existing["task_started_at"] != attempt)
                    or (ownership_kind and existing["ownership_kind"] != ownership_kind)):
                return None
            conn.execute(
                """UPDATE pipeline_target_reservations SET expires_at = ?
                   WHERE repository = ? AND stage = ? AND number = ? AND head = ?
                     AND task_id = ? AND token = ?""",
                (expires_at, repository, stage, number, head, task_id,
                 existing["token"]),
            )
            existing = conn.execute(
                """SELECT * FROM pipeline_target_reservations
                   WHERE repository = ? AND stage = ? AND number = ? AND head = ?""",
                (repository, stage, number, head),
            ).fetchone()
            return _reservation_dict(existing)

        owned = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE task_id = ?""",
            (task_id,),
        ).fetchone()
        if owned is not None:
            # A live task may call election more than once (or two injected
            # calls may race). Never swap its immutable target envelope. A
            # terminal transition releases the old row before a real retry.
            return None
        token = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO pipeline_target_reservations
               (repository, stage, number, head, task_id, task_started_at,
                ownership_kind, herdr_session_state, token, reserved_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (repository, stage, number, head, task_id, attempt, ownership_kind,
             "reserved" if ownership_kind == "herdr" else "", token,
             now_value, expires_at),
        )
        row = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?""",
            (repository, stage, number, head),
        ).fetchone()
        return _reservation_dict(row)


def _exact_pipeline_reservation_parts(reservation: dict):
    if not isinstance(reservation, dict):
        raise ValueError("invalid pipeline target reservation")
    repository, stage, number, head, task_id = _pipeline_target_identity(
        reservation.get("repository"), reservation.get("stage"),
        reservation.get("number"), reservation.get("head"),
        reservation.get("task_id"),
    )
    token = reservation.get("token")
    attempt = reservation.get("task_started_at")
    if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token)
            or not isinstance(attempt, str) or not attempt):
        raise ValueError("invalid exact pipeline target reservation")
    return repository, stage, number, head, task_id, token, attempt


def begin_pipeline_target_herdr_session(reservation: dict) -> Optional[dict]:
    """Persist the crash-safe pre-start state for one exact Herdr attempt."""
    parts = _exact_pipeline_reservation_parts(reservation)
    repository, stage, number, head, task_id, token, attempt = parts
    with _connect(immediate=True) as conn:
        row = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?
                 AND task_id = ? AND token = ? AND task_started_at = ?
                 AND ownership_kind = 'herdr'
                 AND EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND tasks.started_at = pipeline_target_reservations.task_started_at
                 )""",
            (repository, stage, number, head, task_id, token, attempt),
        ).fetchone()
        if row is None:
            return None
        state = row["herdr_session_state"]
        if state == "creating":
            if row["herdr_pane_id"] or row["herdr_tab_id"] or row["herdr_workspace_id"]:
                return None
            return _reservation_dict(row)
        if state != "reserved":
            return None
        conn.execute(
            """UPDATE pipeline_target_reservations
               SET herdr_session_state = 'creating'
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?
                 AND task_id = ? AND token = ? AND task_started_at = ?
                 AND herdr_session_state = 'reserved'""",
            (repository, stage, number, head, task_id, token, attempt),
        )
        row = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?""",
            (repository, stage, number, head),
        ).fetchone()
        return _reservation_dict(row) if row is not None else None


def bind_pipeline_target_herdr_session(
        reservation: dict, pane_id: str, tab_id: str,
        workspace_id: str = "") -> Optional[dict]:
    """Durably bind immutable Herdr IDs before its agent may be started."""
    parts = _exact_pipeline_reservation_parts(reservation)
    repository, stage, number, head, task_id, token, attempt = parts
    values = (pane_id, tab_id, workspace_id)
    if any(not isinstance(value, str) or len(value) > 512 for value in values):
        raise ValueError("invalid Herdr ownership descriptor")
    if not pane_id or not tab_id:
        raise ValueError("Herdr ownership descriptor requires pane and tab ids")
    with _connect(immediate=True) as conn:
        row = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?
                 AND task_id = ? AND token = ? AND task_started_at = ?
                 AND ownership_kind = 'herdr'
                 AND EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND tasks.started_at = pipeline_target_reservations.task_started_at
                 )""",
            (repository, stage, number, head, task_id, token, attempt),
        ).fetchone()
        if row is None:
            return None
        state = row["herdr_session_state"]
        descriptor = (
            row["herdr_pane_id"], row["herdr_tab_id"],
            row["herdr_workspace_id"],
        )
        if state == "owned":
            return _reservation_dict(row) if descriptor == values else None
        if state != "creating" or any(descriptor):
            return None
        conn.execute(
            """UPDATE pipeline_target_reservations
               SET herdr_session_state = 'owned', herdr_pane_id = ?,
                   herdr_tab_id = ?, herdr_workspace_id = ?
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?
                 AND task_id = ? AND token = ? AND task_started_at = ?
                 AND herdr_session_state = 'creating'""",
            (pane_id, tab_id, workspace_id, repository, stage, number, head,
             task_id, token, attempt),
        )
        row = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?""",
            (repository, stage, number, head),
        ).fetchone()
        return _reservation_dict(row) if row is not None else None


def renew_pipeline_target_reservation(reservation: dict, ttl_seconds: int,
                                      *, now: Optional[datetime] = None) -> Optional[dict]:
    """Renew only the still-live exact lease represented by ``reservation``."""
    if not isinstance(reservation, dict) or not isinstance(reservation.get("token"), str):
        raise ValueError("invalid pipeline target reservation")
    identity = _pipeline_target_identity(
        reservation.get("repository"), reservation.get("stage"),
        reservation.get("number"), reservation.get("head"),
        reservation.get("task_id"),
    )
    repository, stage, number, head, task_id = identity
    token = reservation["token"]
    task_started_at = reservation.get("task_started_at", "")
    if not isinstance(task_started_at, str):
        raise ValueError("invalid pipeline target reservation attempt")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid pipeline target reservation token")
    ttl_seconds = _pipeline_reservation_ttl(ttl_seconds)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_value = current.isoformat()
    expires_at = (current + timedelta(seconds=ttl_seconds)).isoformat()
    with _connect(immediate=True) as conn:
        updated = conn.execute(
            """UPDATE pipeline_target_reservations
               SET expires_at = CASE WHEN expires_at < ? THEN ? ELSE expires_at END
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?
                 AND task_id = ? AND token = ?
                 AND (expires_at > ? OR EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND (pipeline_target_reservations.task_started_at = ''
                            OR tasks.started_at = pipeline_target_reservations.task_started_at)
                 ))
                 AND pipeline_target_reservations.task_started_at = ?
                 AND (pipeline_target_reservations.task_started_at = '' OR EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND tasks.started_at = pipeline_target_reservations.task_started_at
                 ))""",
            (expires_at, expires_at, repository, stage, number, head, task_id, token,
             now_value, task_started_at),
        )
        if not updated.rowcount:
            conn.execute(
                """DELETE FROM pipeline_target_reservations
                   WHERE expires_at <= ?
                     AND NOT EXISTS (
                         SELECT 1 FROM tasks
                         WHERE tasks.id = pipeline_target_reservations.task_id
                           AND tasks.status = 'running'
                           AND (pipeline_target_reservations.task_started_at = ''
                                OR tasks.started_at = pipeline_target_reservations.task_started_at)
                     )""",
                (now_value,),
            )
            return None
        row = conn.execute(
            """SELECT * FROM pipeline_target_reservations
               WHERE repository = ? AND stage = ? AND number = ? AND head = ?""",
            (repository, stage, number, head),
        ).fetchone()
        return _reservation_dict(row)


def renew_task_pipeline_target_reservations(
        task_id: int, ttl_seconds: int = 600,
        *, now: Optional[datetime] = None) -> int:
    """Heartbeat every still-live target owned by one running task.

    Expired rows are never resurrected, and a shorter heartbeat never reduces
    an operator-configured longer TTL. This keeps a live/uncertain provider's
    target fenced while preserving TTL recovery after a hard process crash.
    """
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        return 0
    ttl_seconds = _pipeline_reservation_ttl(ttl_seconds)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_value = current.isoformat()
    heartbeat_expiry = (current + timedelta(seconds=ttl_seconds)).isoformat()
    with _connect(immediate=True) as conn:
        updated = conn.execute(
            """UPDATE pipeline_target_reservations
               SET expires_at = CASE WHEN expires_at < ? THEN ? ELSE expires_at END
               WHERE task_id = ?
                 AND (expires_at > ? OR EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND (pipeline_target_reservations.task_started_at = ''
                            OR tasks.started_at = pipeline_target_reservations.task_started_at)
                 ))
                 AND (task_started_at = '' OR EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND tasks.started_at = pipeline_target_reservations.task_started_at
                 ))""",
            (heartbeat_expiry, heartbeat_expiry, task_id, now_value),
        )
        conn.execute(
            """DELETE FROM pipeline_target_reservations
               WHERE expires_at <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM tasks
                     WHERE tasks.id = pipeline_target_reservations.task_id
                       AND tasks.status = 'running'
                       AND (pipeline_target_reservations.task_started_at = ''
                            OR tasks.started_at = pipeline_target_reservations.task_started_at)
                 )""",
            (now_value,),
        )
        return updated.rowcount


def release_pipeline_target_reservations(task_id: int) -> int:
    """Release every target owned by one completed/aborted task attempt."""
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        return 0
    with _connect(immediate=True) as conn:
        deleted = conn.execute(
            "DELETE FROM pipeline_target_reservations WHERE task_id = ?",
            (task_id,),
        )
        return deleted.rowcount


def list_pipeline_target_reservations(*, repository: str | None = None,
                                      stage: str | None = None,
                                      now: Optional[datetime] = None) -> list[dict]:
    """Return live reservations for diagnostics without extending their TTL."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    clauses = ["(expires_at > ? OR EXISTS ("
               "SELECT 1 FROM tasks "
               "WHERE tasks.id = pipeline_target_reservations.task_id "
               "AND tasks.status = 'running' "
               "AND (pipeline_target_reservations.task_started_at = '' "
               "OR tasks.started_at = pipeline_target_reservations.task_started_at)))"]
    values: list[object] = [current]
    if repository is not None:
        clauses.append("repository = ?")
        values.append(str(repository).strip().lower())
    if stage is not None:
        clauses.append("stage = ?")
        values.append(str(stage).strip().lower())
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline_target_reservations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY repository, stage, number, head",
            values,
        ).fetchall()
        return [_reservation_dict(row) for row in rows]


def _series_title(prompt: str) -> str:
    return (prompt or "").splitlines()[0].strip()[:160] or "Повторяющаяся задача"


def _create_series(conn: sqlite3.Connection, task: TaskCreate) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO task_series
           (title, prompt, working_dir, base_recurrence, provider, model, effort,
            priority, task_timeout, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_series_title(task.prompt), task.prompt, task.working_dir, task.recurrence,
         task.provider, task.model, task.effort, task.priority, task.task_timeout,
         now, now),
    )
    return cur.lastrowid


def _backfill_task_series(conn: sqlite3.Connection):
    """Give legacy recurrence chains a stable identity without changing runs."""
    rows = conn.execute(
        """SELECT * FROM tasks
           WHERE recurrence IS NOT NULL AND recurrence != '' AND series_id IS NULL
           ORDER BY id"""
    ).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row["prompt"], row["working_dir"] or ""), []).append(row)
    for chain in groups.values():
        head = next((r for r in reversed(chain)
                     if r["status"] in ("pending", "rate_limited", "running")), chain[-1])
        now = _now()
        cur = conn.execute(
            """INSERT INTO task_series
               (title, prompt, working_dir, base_recurrence, provider, model, effort,
                priority, task_timeout, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_series_title(head["prompt"]), head["prompt"], head["working_dir"],
             head["recurrence"], head["provider"], head["model"], head["effort"],
             head["priority"], head["task_timeout"], chain[0]["created_at"], now),
        )
        conn.executemany("UPDATE tasks SET series_id = ? WHERE id = ?",
                         [(cur.lastrowid, r["id"]) for r in chain])


def _insert_task(conn: sqlite3.Connection, task: TaskCreate) -> TaskInDB:
    """Insert a queue task inside the caller's transaction."""
    series_id = task.series_id
    if task.recurrence and series_id is None:
        series_id = _create_series(conn, task)
    cur = conn.execute(
        """INSERT INTO tasks (prompt, working_dir, provider, status, priority,
           scheduled_at, created_at, max_retries, skip_permissions, model,
           session_id, parent_task_id, tg_chat_id, recurrence, task_timeout,
           detached, keep_pane, herdr_target, machine, worktree, effort, series_id)
           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            task.effort,
            series_id,
        ),
    )
    return get_task(cur.lastrowid, conn=conn)


def create_task(task: TaskCreate) -> TaskInDB:
    with _connect() as conn:
        return _insert_task(conn, task)


def create_series_occurrence_if_idle(task: TaskCreate) -> Optional[TaskInDB]:
    """Insert one recurrence only when its series has no live occurrence."""
    if not task.series_id:
        raise ValueError("series occurrence requires series_id")
    with _connect(immediate=True) as conn:
        series = conn.execute(
            "SELECT ended_at FROM task_series WHERE id = ?",
            (task.series_id,),
        ).fetchone()
        if not series or series["ended_at"]:
            return None
        existing = conn.execute(
            """SELECT 1 FROM tasks WHERE series_id = ?
               AND status IN ('pending', 'running', 'rate_limited') LIMIT 1""",
            (task.series_id,),
        ).fetchone()
        if existing is not None:
            return None
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
    select = (
        "SELECT tasks.*, task_series.title AS series_title, "
        "COALESCE(task_series.paused, 0) AS series_paused FROM tasks "
        "LEFT JOIN task_series ON task_series.id = tasks.series_id"
    )
    with _connect() as conn:
        if statuses:
            vals = [s.value if hasattr(s, "value") else s for s in statuses]
            marks = ",".join("?" * len(vals))
            rows = conn.execute(
                f"{select} WHERE tasks.status IN ({marks})"
                " ORDER BY tasks.created_at DESC LIMIT ? OFFSET ?",
                (*vals, limit, offset),
            ).fetchall()
        elif status:
            rows = conn.execute(
                f"{select} WHERE tasks.status = ? "
                "ORDER BY tasks.created_at DESC LIMIT ? OFFSET ?",
                (status.value, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                f"{select} ORDER BY tasks.created_at DESC LIMIT ? OFFSET ?",
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


def get_next_runnable(
        busy_keys=(), key_fn=None, order_key_fn=None, *,
        budget_wait_scope: str | None = None,
        budget_starvation_timeout_seconds: int = 0,
        budget_fairness_now: float | None = None) -> Optional[TaskInDB]:
    """Claim the highest-priority runnable task and mark it running.

    The whole select-then-claim runs under the write lock, so several workers
    (threads of one worker, or separate processes) never claim the same row.

    busy_keys/key_fn — when the caller already runs tasks, candidates whose
    key_fn(task) is in busy_keys are passed over: that is how two agents are
    kept out of one work tree while the queue keeps moving.

    order_key_fn — optional policy rank evaluated while the same write
    transaction owns the runnable snapshot. Returning ``None`` excludes a
    candidate; otherwise the smallest key wins before the stable DB order.
    """
    now = _now()
    busy = set(busy_keys or ())
    with _connect(immediate=True) as conn:
        # The admission baton must reach a worker slot too. Otherwise an
        # endless stream of higher-priority runnable tasks can repeatedly be
        # claimed and denied while the selected waiter never reaches preflight.
        fairness_task_id = None
        if budget_starvation_timeout_seconds:
            if (not isinstance(budget_wait_scope, str)
                    or not budget_wait_scope.strip()):
                raise ValueError(
                    "budget wait scope is required for scheduler fairness")
            fairness = _pipeline_github_budget_waiter_summary(
                conn, budget_wait_scope,
                starvation_timeout_seconds=(
                    budget_starvation_timeout_seconds),
                now=budget_fairness_now).get("fairness_waiter")
            if isinstance(fairness, dict):
                fairness_task_id = fairness.get("task_id")
                # A reservation wait normally carries a short retry deadline.
                # Once its baton matures, make the selected pending attempt
                # runnable in this same claim transaction; otherwise it could
                # block new admissions while sleeping until that deadline.
                conn.execute(
                    """UPDATE tasks SET scheduled_at = ?, next_run_at = NULL
                       WHERE id = ? AND status = 'pending'
                         AND budget_wait_scope = ?""",
                    (now, fairness_task_id, budget_wait_scope),
                )
        # Sequential worker takes just the top task. When some keys are busy we
        # walk the whole runnable queue in priority order until a non-colliding
        # task is found — a hard LIMIT could hide a free task behind a wall of
        # conflicting ones. Rows are materialised before any UPDATE so claiming
        # one doesn't disturb the iteration.
        limit_clause = "" if (busy or order_key_fn is not None
                              or fairness_task_id is not None) else " LIMIT 1"
        rows = conn.execute(
            f"""SELECT * FROM tasks
               WHERE status IN ('pending', 'rate_limited')
                 AND (scheduled_at IS NULL OR scheduled_at <= ?)
                 AND (next_run_at IS NULL OR next_run_at <= ?)
                 AND (series_id IS NULL OR EXISTS (
                       SELECT 1 FROM task_series s WHERE s.id = tasks.series_id
                         AND s.paused = 0 AND s.ended_at IS NULL))
               ORDER BY priority ASC, created_at ASC, id ASC{limit_clause}""",
            (now, now),
        ).fetchall()
        candidates = []
        for position, row in enumerate(rows):
            task = _row_to_task(row)
            if busy and key_fn and key_fn(task) in busy:
                continue
            policy_rank = order_key_fn(task) if order_key_fn is not None else ()
            if order_key_fn is not None and policy_rank is None:
                continue
            fairness_rank = 0 if task.id == fairness_task_id else 1
            candidates.append(((fairness_rank, policy_rank), position, task))
        if order_key_fn is not None or fairness_task_id is not None:
            candidates.sort(key=lambda item: (item[0], item[1]))
        for _rank, _position, task in candidates:
            started_at = _now()
            cur = conn.execute(
                """UPDATE tasks SET status = 'running', started_at = ?,
                                  error = NULL, next_run_at = NULL
                   WHERE id = ? AND status IN ('pending', 'rate_limited')""",
                (started_at, task.id),
            )
            if cur.rowcount:
                return get_task(task.id, conn=conn)
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


def set_session_id(task_id: int, session_id: str) -> bool:
    """Persist a resumable provider session while its process is still alive.

    Waiting until normal completion loses the session exactly when it matters:
    after a worker/process crash.  The next retry can safely resume only when
    the first stream event was committed independently of the final result.
    """
    if not session_id:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ? AND status = 'running'",
            (session_id, task_id),
        )
        return cur.rowcount > 0


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


def _attempt_iso(started_at) -> str | None:
    if isinstance(started_at, datetime):
        return _to_utc_iso(started_at)
    return str(started_at) if started_at else None


def _release_task_pipeline_targets(conn, task_id: int, started_at=None):
    attempt = _attempt_iso(started_at)
    if attempt is None:
        conn.execute(
            "DELETE FROM pipeline_target_reservations WHERE task_id = ?",
            (task_id,),
        )
    else:
        conn.execute(
            """DELETE FROM pipeline_target_reservations
               WHERE task_id = ?
                 AND (task_started_at = '' OR task_started_at = ?)""",
            (task_id, attempt),
        )


def mark_completed(task_id: int, result: str, exit_code: int = 0,
                   model_used: str = None, session_id: str = None,
                   verdict: str = None, *, expected_started_at=None) -> bool:
    """Finalize a task, optionally committing its verdict atomically."""
    with _connect() as conn:
        attempt = _attempt_iso(expected_started_at)
        where = "id = ?"
        values = [result, exit_code, _now(), model_used, session_id, verdict, task_id]
        if attempt is not None:
            where += " AND status = 'running' AND started_at = ?"
            values.append(attempt)
        cur = conn.execute(
            "UPDATE tasks SET status = 'completed', result = ?, error = NULL, "
            "next_run_at = NULL, exit_code = ?, completed_at = ?, model_used = ?, "
            "session_id = COALESCE(?, session_id), "
            f"verdict = COALESCE(?, verdict), note = NULL, "
            f"budget_wait_scope = NULL, budget_wait_started_at = NULL "
            f"WHERE {where}",
            values,
        )
        if cur.rowcount:
            _drop_cancel_flag(conn, task_id)
            _release_task_pipeline_targets(conn, task_id, attempt)
        return cur.rowcount > 0


def mark_failed(task_id: int, error: str, exit_code: int = 1,
                *, expected_started_at=None) -> bool:
    with _connect() as conn:
        attempt = _attempt_iso(expected_started_at)
        where = "id = ?"
        values = [error, exit_code, _now(), task_id]
        if attempt is not None:
            where += " AND status = 'running' AND started_at = ?"
            values.append(attempt)
        cur = conn.execute(
            "UPDATE tasks SET status = 'failed', error = ?, exit_code = ?, "
            f"completed_at = ?, note = NULL, budget_wait_scope = NULL, "
            f"budget_wait_started_at = NULL "
            f"WHERE {where}",
            values,
        )
        if cur.rowcount:
            _drop_cancel_flag(conn, task_id)
            _release_task_pipeline_targets(conn, task_id, attempt)
        return cur.rowcount > 0


def fail_running_attempt(task_id: int, started_at, error: str,
                         exit_code: int = 1) -> bool:
    """Fail exactly one claimed attempt after an internal worker crash.

    Recovery can be delayed by SQLite contention. The task may be reset and
    claimed again before that delay clears, so task id alone is not a safe
    fence: only the exact still-running ``started_at`` attempt may be changed.
    """
    if isinstance(started_at, datetime):
        started_at = _to_utc_iso(started_at)
    if not started_at:
        return False
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE tasks
               SET status = 'failed', error = ?, exit_code = ?,
                   completed_at = ?, note = NULL, budget_wait_scope = NULL,
                   budget_wait_started_at = NULL
               WHERE id = ? AND status = 'running' AND started_at = ?""",
            (error, exit_code, _now(), task_id, started_at),
        )
        if cur.rowcount:
            _drop_cancel_flag(conn, task_id)
            _release_task_pipeline_targets(conn, task_id, started_at)
        return cur.rowcount > 0


def mark_rate_limited(task_id: int, next_run_at: datetime, error: str = None,
                      *, expected_started_at=None) -> bool:
    with _connect() as conn:
        attempt = _attempt_iso(expected_started_at)
        where = "id = ?"
        values = [_to_utc_iso(next_run_at), error, task_id]
        if attempt is not None:
            where += " AND status = 'running' AND started_at = ?"
            values.append(attempt)
        cur = conn.execute(
            """UPDATE tasks
               SET status = 'rate_limited',
                   next_run_at = ?,
                   retry_count = retry_count + 1,
                   error = COALESCE(?, error), budget_wait_scope = NULL,
                   budget_wait_started_at = NULL
                WHERE """ + where,
            values,
        )
        if cur.rowcount:
            _drop_cancel_flag(conn, task_id)
            _release_task_pipeline_targets(conn, task_id, attempt)
        return cur.rowcount > 0


def defer_task(task_id: int, next_run_at: datetime, reason: str = None,
               *, hard_not_before: bool = False,
               budget_wait_scope: str | None = None,
               budget_wait_revision: int | None = None,
               expected_started_at=None) -> bool:
    """Return a claimed task to pending without consuming a retry attempt.

    Used by deterministic pipeline dependency gates. This is waiting, not a
    provider failure and not a completed run, so retry and run metrics must not
    move.  A hard deadline is additionally stored in ``next_run_at`` so
    automatic queue wake-ups cannot spend GitHub quota before the exact reset;
    an explicit human ``run_now`` still clears that barrier intentionally.

    ``budget_wait_scope`` marks the distinct, interruptible case where live
    quota is sufficient but another task temporarily owns a reservation. The
    ledger release wakes that waiter immediately instead of leaving a
    high-priority task asleep for the whole busy-retry interval.
    """
    if budget_wait_scope is not None:
        if (not isinstance(budget_wait_scope, str)
                or not budget_wait_scope.strip()
                or len(budget_wait_scope) > 256):
            raise ValueError("budget wait scope must contain 1..256 characters")
        if (isinstance(budget_wait_revision, bool)
                or not isinstance(budget_wait_revision, int)
                or budget_wait_revision < 0):
            raise ValueError(
                "budget wait revision must be a non-negative integer")
        if hard_not_before:
            raise ValueError(
                "budget reservation wait cannot also be a hard deadline")
    elif budget_wait_revision is not None:
        raise ValueError("budget wait revision requires a scope")
    deadline = _to_utc_iso(next_run_at)
    with _connect(immediate=budget_wait_scope is not None) as conn:
        # Close the release-before-defer race. If the reservation ledger moved
        # after admission observed contention, the sole wake may already have
        # happened. Requeue immediately, but preserve the durable priority
        # handoff until this task actually reserves budget.
        if (budget_wait_scope is not None
                and _pipeline_github_budget_revision(
                    conn, budget_wait_scope) != budget_wait_revision):
            deadline = _now()
        attempt = _attempt_iso(expected_started_at)
        where = "id = ? AND status = 'running'"
        values = [deadline, deadline if hard_not_before else None, reason,
                  budget_wait_scope, budget_wait_scope, _now(),
                  budget_wait_scope, task_id]
        if attempt is not None:
            where += " AND started_at = ?"
            values.append(attempt)
        cur = conn.execute(
            """UPDATE tasks SET status = 'pending', scheduled_at = ?,
                      next_run_at = ?, started_at = NULL,
                      error = COALESCE(?, error),
                      budget_wait_started_at = CASE
                        WHEN ? IS NULL THEN NULL
                        WHEN budget_wait_scope = ?
                          AND budget_wait_started_at IS NOT NULL
                        THEN budget_wait_started_at
                        ELSE ?
                      END,
                      budget_wait_scope = ?
               WHERE """ + where,
            values,
        )
        if cur.rowcount:
            _drop_cancel_flag(conn, task_id)
            _release_task_pipeline_targets(conn, task_id, attempt)
        return cur.rowcount > 0


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


def mark_cancelled(task_id: int, note: str = None,
                   *, expected_started_at=None) -> bool:
    with _connect() as conn:
        attempt = _attempt_iso(expected_started_at)
        where = "id = ?"
        values = [_now(), note, task_id]
        if attempt is not None:
            where += " AND status = 'running' AND started_at = ?"
            values.append(attempt)
        cur = conn.execute(
            "UPDATE tasks SET status = 'cancelled', completed_at = ?, "
            f"error = COALESCE(?, error), note = NULL, "
            f"budget_wait_scope = NULL, budget_wait_started_at = NULL "
            f"WHERE {where}",
            values,
        )
        if cur.rowcount:
            _drop_cancel_flag(conn, task_id)
            _release_task_pipeline_targets(conn, task_id, attempt)
        return cur.rowcount > 0


def cancel_task(task_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'cancelled', completed_at = ?, "
            "budget_wait_scope = NULL, budget_wait_started_at = NULL "
            "WHERE id = ? "
            "AND status IN ('pending', 'rate_limited')",
            (_now(), task_id),
        )
        if cur.rowcount:
            _release_task_pipeline_targets(conn, task_id)
        return cur.rowcount > 0


def update_priority(task_id: int, priority: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE tasks SET priority = ?, pipeline_priority_restore = NULL,
                                  budget_wait_started_at = CASE
                                    WHEN budget_wait_scope IS NOT NULL THEN ?
                                    ELSE NULL
                                  END
               WHERE id = ? AND status IN ('pending', 'rate_limited')""",
            (priority, _now(), task_id),
        )
        return cur.rowcount > 0


def promote_running_attempt_priority(task_id: int, started_at,
                                     priority: int) -> int | None:
    """Promote one exact running attempt without changing its series.

    A routed pipeline occurrence can become more urgent only after its signed
    target has been inspected.  Keep that temporary promotion on the task row
    so budget-wait ordering observes it, while the recurring successor still
    inherits the configured series priority.
    """
    if (isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0
            or isinstance(priority, bool) or not isinstance(priority, int)
            or not 1 <= priority <= 10):
        raise ValueError("invalid running task priority promotion")
    attempt = _attempt_iso(started_at)
    if attempt is None:
        raise ValueError("running task attempt is required for priority promotion")
    with _connect(immediate=True) as conn:
        row = conn.execute(
            """SELECT priority, pipeline_priority_restore FROM tasks
               WHERE id = ? AND status = 'running' AND started_at = ?""",
            (task_id, attempt),
        ).fetchone()
        if row is None:
            return None
        current = int(row["priority"])
        restore = row["pipeline_priority_restore"]
        if restore is not None and not 1 <= int(restore) <= 10:
            raise ValueError("stored pipeline priority restore value is invalid")
        if current > priority:
            conn.execute(
                """UPDATE tasks
                   SET priority = ?, pipeline_priority_restore = COALESCE(
                       pipeline_priority_restore, priority)
                   WHERE id = ? AND status = 'running' AND started_at = ?
                     AND priority > ?""",
                (priority, task_id, attempt, priority),
            )
            return priority
        return current


def restore_running_attempt_priority(task_id: int, started_at) -> int | None:
    """Drop a temporary routed promotion after fresh preflight disproves it."""
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise ValueError("invalid running task priority restoration")
    attempt = _attempt_iso(started_at)
    if attempt is None:
        raise ValueError("running task attempt is required for priority restoration")
    with _connect(immediate=True) as conn:
        row = conn.execute(
            """SELECT priority, pipeline_priority_restore FROM tasks
               WHERE id = ? AND status = 'running' AND started_at = ?""",
            (task_id, attempt),
        ).fetchone()
        if row is None:
            return None
        restore = row["pipeline_priority_restore"]
        if restore is None:
            return int(row["priority"])
        restored = int(restore)
        if not 1 <= restored <= 10:
            raise ValueError("stored pipeline priority restore value is invalid")
        conn.execute(
            """UPDATE tasks
               SET priority = ?, pipeline_priority_restore = NULL
               WHERE id = ? AND status = 'running' AND started_at = ?""",
            (restored, task_id, attempt),
        )
        return restored


def _effective_series_recurrence(row: dict, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    temporary = row.get("temporary_recurrence")
    until = _parse_dt(row.get("temporary_until"))
    if temporary and (until is None or until > now):
        return temporary
    return row["base_recurrence"]


def list_series() -> list:
    """Durable recurring series with current occurrence and health counters."""
    with _connect() as conn:
        # The summary and its sparse occurrence details must describe the same
        # queue snapshot even if a worker completes a run between SELECTs.
        conn.execute("BEGIN")
        series_rows = [dict(r) for r in conn.execute(
            """SELECT s.id, s.title, s.prompt, s.working_dir,
                      s.base_recurrence, s.temporary_recurrence,
                      s.temporary_until, s.temporary_empty_limit,
                      s.temporary_empty_count, s.provider, s.model, s.effort,
                      s.priority, s.task_timeout, s.paused, s.ended_at,
                      auto_pause.value AS auto_pause_data
               FROM task_series AS s
               LEFT JOIN settings AS auto_pause
                 ON auto_pause.key = ? || CAST(s.id AS TEXT)
               ORDER BY s.id DESC""",
            (_PIPELINE_REPEAT_BLOCKER_PAUSE_PREFIX,))]
        # Keep this read deliberately narrow.  Task result/prompt/note payloads
        # can be very large and none of them contributes to series health.
        # Fetching all series histories in one pass also avoids the old N+1
        # SELECT pattern when the schedule page is refreshed.
        task_rows_by_series = {}
        for row in conn.execute(
                """SELECT t.id, t.series_id, t.status, t.started_at,
                          t.completed_at, t.verdict
                   FROM tasks AS t
                   INNER JOIN task_series AS s ON s.id = t.series_id
                   ORDER BY t.series_id DESC, t.id DESC"""):
            task_rows_by_series.setdefault(row["series_id"], []).append(dict(row))

        # Only the current active occurrence exposes its error/schedule, and
        # only the active or last occurrence supplies ``machine``.  Read those
        # details for all series with one additional query instead of loading
        # those potentially sizeable columns for every historical run.
        task_details = {}
        for row in conn.execute(
                """WITH picked AS (
                       SELECT series_id,
                              MAX(CASE WHEN status IN
                                  ('pending', 'rate_limited', 'running')
                                  THEN id END) AS active_id,
                              MAX(CASE WHEN status IN
                                  ('completed', 'failed', 'cancelled')
                                  THEN id END) AS last_id
                       FROM tasks
                       WHERE series_id IS NOT NULL
                       GROUP BY series_id
                   )
                   SELECT 'active' AS kind, t.series_id, t.id, t.machine,
                          t.scheduled_at, t.error, t.worktree, t.detached,
                          t.herdr_target
                   FROM tasks AS t
                   INNER JOIN picked ON picked.active_id = t.id
                   INNER JOIN task_series AS s ON s.id = t.series_id
                   UNION ALL
                   SELECT 'last' AS kind, t.series_id, t.id, t.machine,
                          NULL AS scheduled_at, NULL AS error,
                          NULL AS worktree, NULL AS detached,
                          NULL AS herdr_target
                   FROM tasks AS t
                   INNER JOIN picked ON picked.last_id = t.id
                   INNER JOIN task_series AS s ON s.id = t.series_id"""):
            task_details[(row["kind"], row["series_id"])] = dict(row)

        out = []
        for s in series_rows:
            tasks = task_rows_by_series.get(s["id"], [])
            active = next((r for r in tasks
                           if r["status"] in ("pending", "rate_limited", "running")), None)
            last = next((r for r in tasks
                         if r["status"] in ("completed", "failed", "cancelled")), None)
            active_detail = task_details.get(("active", s["id"]))
            last_detail = task_details.get(("last", s["id"]))
            completed = [r for r in tasks if r["status"] in ("completed", "failed")]
            failures = sum(r["status"] == "failed" for r in completed)
            empties = sum((r["verdict"] or "").upper() == "ПУСТО" for r in completed)
            durations = []
            for r in completed:
                if r["started_at"] and r["completed_at"]:
                    durations.append((_parse_dt(r["completed_at"]) -
                                      _parse_dt(r["started_at"])).total_seconds())
            auto_pause_reason = None
            try:
                auto_pause = json.loads(s["auto_pause_data"])
                reason = auto_pause.get("reason")
                if (auto_pause.get("kind") == "repeated_blocker"
                        and isinstance(reason, str) and reason.strip()):
                    auto_pause_reason = reason.strip()
            except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
                pass
            out.append({
                "id": s["id"], "title": s["title"], "prompt": s["prompt"],
                "working_dir": s["working_dir"], "runs": len(tasks),
                "recurrence": s["base_recurrence"],
                "effective_recurrence": _effective_series_recurrence(s),
                "temporary_recurrence": s["temporary_recurrence"],
                "temporary_until": s["temporary_until"],
                "temporary_empty_limit": s["temporary_empty_limit"],
                "temporary_empty_count": s["temporary_empty_count"],
                "provider": s["provider"], "model": s["model"], "effort": s["effort"],
                "priority": s["priority"], "task_timeout": s["task_timeout"],
                "machine": (active_detail["machine"] if active_detail else
                            (last_detail["machine"] if last_detail else None)),
                "worktree": bool(active_detail["worktree"]) if active_detail else False,
                "detached": bool(active_detail["detached"]) if active_detail else False,
                "herdr_target": active_detail["herdr_target"] if active_detail else None,
                "paused": bool(s["paused"]), "ended": bool(s["ended_at"]),
                "auto_pause_reason": auto_pause_reason if s["paused"] else None,
                "next_task_id": active["id"] if active else None,
                "next_status": active["status"] if active else None,
                "next_run_at": active_detail["scheduled_at"] if active_detail else None,
                "next_error": active_detail["error"] if active_detail else None,
                "next_started_at": active["started_at"] if active else None,
                "last_task_id": last["id"] if last else None,
                "last_status": last["status"] if last else None,
                "last_at": (last["completed_at"] or last["started_at"]) if last else None,
                "last_verdict": last["verdict"] if last else None,
                "failure_rate": round(failures / len(completed), 3) if completed else 0,
                "empty_rate": round(empties / len(completed), 3) if completed else 0,
                "avg_duration_seconds": round(sum(durations) / len(durations)) if durations else None,
                "broken": not active and not s["ended_at"] and not s["paused"],
            })
    out.sort(key=lambda x: (x["ended"], x["broken"], x["paused"], x["next_run_at"] or ""))
    return out


def get_series(series_id: int) -> Optional[dict]:
    return next((s for s in list_series() if s["id"] == series_id), None)


SERIES_EDITABLE_FIELDS = ("title", "base_recurrence", "temporary_recurrence",
                          "temporary_until", "temporary_empty_limit", "provider",
                          "model", "effort", "priority", "task_timeout")


def update_series(series_id: int, fields: dict) -> bool:
    fields = {k: v for k, v in fields.items() if k in SERIES_EDITABLE_FIELDS}
    if not fields:
        return False
    if "temporary_until" in fields and isinstance(fields["temporary_until"], datetime):
        fields["temporary_until"] = _to_utc_iso(fields["temporary_until"])
    # Only an explicit clear ends a temporary cadence. Partial PATCHes such as
    # a priority change must leave the active boost and its empty-run limit intact.
    if ("temporary_recurrence" in fields
            and fields["temporary_recurrence"] is None):
        fields.setdefault("temporary_until", None)
        fields.setdefault("temporary_empty_limit", None)
        fields["temporary_empty_count"] = 0
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _connect(immediate=True) as conn:
        current = conn.execute(
            "SELECT provider FROM task_series WHERE id = ? AND ended_at IS NULL",
            (series_id,),
        ).fetchone()
        if not current:
            return False
        provider_changed = (
            "provider" in fields and fields["provider"] != current["provider"]
        )
        cur = conn.execute(f"UPDATE task_series SET {sets} WHERE id = ? AND ended_at IS NULL",
                           (*fields.values(), series_id))
        if not cur.rowcount:
            return False
        task_fields = {}
        if "base_recurrence" in fields:
            task_fields["recurrence"] = fields["base_recurrence"]
        for name in ("provider", "model", "effort", "priority", "task_timeout"):
            if name in fields:
                task_fields[name] = fields[name]
        task_sets = [f"{k} = ?" for k in task_fields]
        task_values = list(task_fields.values())
        if "priority" in fields:
            # An explicit series edit supersedes a temporary route-derived
            # promotion and restarts any admission-wait ordering for the
            # edited occurrence.
            task_sets.extend([
                "pipeline_priority_restore = NULL",
                "budget_wait_started_at = CASE "
                "WHEN budget_wait_scope IS NOT NULL THEN ? ELSE NULL END",
            ])
            task_values.append(_now())
        if provider_changed:
            # Resume ids and retry budgets belong to the old provider. Carrying
            # them across a switch can ask a new CLI to resume an incompatible
            # session or fail it immediately on the old provider's last retry.
            task_sets.extend([
                "session_id = NULL", "retry_count = 0", "error = NULL",
                "exit_code = NULL",
                "status = CASE WHEN status = 'rate_limited' THEN 'pending' ELSE status END",
                "next_run_at = CASE WHEN status = 'rate_limited' THEN NULL ELSE next_run_at END",
            ])
        if task_sets:
            conn.execute(
                f"UPDATE tasks SET {', '.join(task_sets)} WHERE series_id = ? "
                "AND status IN ('pending', 'rate_limited')",
                (*task_values, series_id),
            )
        if any(name in fields for name in (
                "base_recurrence", "temporary_recurrence", "temporary_until")):
            # A shorter interval must affect the occurrence that is already in
            # the queue. Previously only future occurrences used it, so a user
            # selecting 30m could still wait four hours. Never postpone an
            # earlier pending run and never override retry backoff.
            updated = conn.execute(
                "SELECT * FROM task_series WHERE id = ?", (series_id,)
            ).fetchone()
            recurrence = _effective_series_recurrence(dict(updated)) if updated else None
            candidate = parse_recurrence(recurrence) if recurrence else None
            if candidate:
                candidate_iso = _to_utc_iso(candidate)
                conn.execute(
                    """UPDATE tasks SET scheduled_at = ?
                       WHERE series_id = ? AND status = 'pending'
                         AND scheduled_at > ?""",
                    (candidate_iso, series_id, candidate_iso),
                )
        return True


def apply_pipeline_series_cadence(
        series_id: int, *, idle_recurrence: str,
        busy_recurrence: Optional[str] = None, boost: bool = False,
        empty_runs_before_idle: int = 0,
        publication_guard: Optional[dict] = None) -> Optional[dict]:
    """Idempotently reconcile one profile-owned adaptive cadence.

    ``idle_recurrence`` is the durable safety interval.  ``busy_recurrence``
    uses the existing temporary-boost fields, so completion and crash recovery
    keep one source of truth.  When ``empty_runs_before_idle`` is non-zero, a
    quiet snapshot does not switch cadence by itself: consecutive ``ПУСТО``
    verdicts retire the boost in :func:`prepare_series_recurrence`.

    A profile that opts into this API owns these recurrence fields.  The write
    is one transaction and never postpones an occurrence already scheduled
    earlier than the new effective cadence.
    """
    if (not idle_recurrence or parse_recurrence(idle_recurrence) is None
            or (busy_recurrence is not None
                and parse_recurrence(busy_recurrence) is None)):
        raise ValueError("invalid adaptive pipeline recurrence")
    if (isinstance(empty_runs_before_idle, bool)
            or not isinstance(empty_runs_before_idle, int)
            or not 0 <= empty_runs_before_idle <= 20):
        raise ValueError("empty_runs_before_idle must be from 0 to 20")

    if publication_guard is not None:
        required = {
            "epoch_key", "epoch", "epoch_default", "profile_key",
            "profile_hash", "revision_key", "revision",
        }
        if (not isinstance(publication_guard, dict)
                or set(publication_guard) != required
                or any(not isinstance(publication_guard[name], str)
                       for name in required)):
            raise ValueError("invalid adaptive cadence publication guard")

    with _connect(immediate=True) as conn:
        if publication_guard is not None:
            def setting(name: str, default: Optional[str] = None):
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?",
                    (publication_guard[name],),
                ).fetchone()
                return row["value"] if row else default

            if (setting("epoch_key", publication_guard["epoch_default"])
                    != publication_guard["epoch"]
                    or setting("profile_key")
                    != publication_guard["profile_hash"]
                    or setting("revision_key")
                    != publication_guard["revision"]):
                return None
        row = conn.execute(
            "SELECT * FROM task_series WHERE id = ?", (series_id,)
        ).fetchone()
        if not row or row["ended_at"]:
            return None
        before = dict(row)
        desired = dict(before)
        desired["base_recurrence"] = idle_recurrence

        if busy_recurrence is not None:
            keep_until_empty = (
                not boost and empty_runs_before_idle > 0
                and before.get("temporary_recurrence") == busy_recurrence
            )
            if boost:
                desired["temporary_recurrence"] = busy_recurrence
                desired["temporary_until"] = None
                desired["temporary_empty_limit"] = (
                    empty_runs_before_idle or None)
                # A live busy observation breaks an empty-run streak even if
                # the effective interval itself was already boosted.
                desired["temporary_empty_count"] = 0
            elif not keep_until_empty:
                desired["temporary_recurrence"] = None
                desired["temporary_until"] = None
                desired["temporary_empty_limit"] = None
                desired["temporary_empty_count"] = 0
        else:
            # An adaptive policy owns the whole cadence. Event-only queues do
            # not inherit an unrelated/manual temporary boost forever.
            desired["temporary_recurrence"] = None
            desired["temporary_until"] = None
            desired["temporary_empty_limit"] = None
            desired["temporary_empty_count"] = 0

        changed_fields = [
            name for name in (
                "base_recurrence", "temporary_recurrence", "temporary_until",
                "temporary_empty_limit", "temporary_empty_count",
            )
            if desired.get(name) != before.get(name)
        ]
        if changed_fields:
            now = _now()
            assignments = ", ".join(f"{name} = ?" for name in changed_fields)
            conn.execute(
                f"UPDATE task_series SET {assignments}, updated_at = ? WHERE id = ?",
                (*[desired.get(name) for name in changed_fields], now, series_id),
            )
            if "base_recurrence" in changed_fields:
                conn.execute(
                    """UPDATE tasks SET recurrence = ? WHERE series_id = ?
                       AND status IN ('pending', 'rate_limited')""",
                    (idle_recurrence, series_id),
                )

            effective = _effective_series_recurrence(desired)
            candidate = parse_recurrence(effective)
            if candidate:
                candidate_iso = _to_utc_iso(candidate)
                conn.execute(
                    """UPDATE tasks SET scheduled_at = ?
                       WHERE series_id = ? AND status = 'pending'
                         AND scheduled_at > ?""",
                    (candidate_iso, series_id, candidate_iso),
                )

        return {
            "changed": bool(changed_fields),
            "effective_recurrence": _effective_series_recurrence(desired),
            "base_recurrence": desired["base_recurrence"],
            "temporary_recurrence": desired.get("temporary_recurrence"),
            "temporary_empty_limit": desired.get("temporary_empty_limit"),
            "temporary_empty_count": desired.get("temporary_empty_count") or 0,
        }


def _pipeline_series_wake_intent_key(series_id: int) -> str:
    if isinstance(series_id, bool) or not isinstance(series_id, int) or series_id <= 0:
        raise ValueError("pipeline series id must be a positive integer")
    return f"pipeline_series_wake_intent:v1:{series_id}"


_PIPELINE_BLOCKER_VERDICTS = {
    "НУЖЕН ЧЕЛОВЕК": "human",
    "HUMAN": "human",
    "HUMAN_REQUIRED": "human",
    "NEEDS_HUMAN": "human",
    "НЕ СМОГ": "unable",
    "UNABLE": "unable",
}
_PIPELINE_BLOCKER_LINE_RE = re.compile(
    r"^[ \t>*#-]*ИТОГ:\s*(?P<verdict>"
    + "|".join(re.escape(value) for value in _PIPELINE_BLOCKER_VERDICTS)
    + r")\b(?P<reason>.*)$",
    re.IGNORECASE,
)
_PIPELINE_VOLATILE_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_PIPELINE_VOLATILE_HEX_RE = re.compile(
    r"\b(?=[0-9a-f]{7,64}\b)(?=[0-9a-f]*[a-f])[0-9a-f]+\b",
    re.IGNORECASE,
)
_PIPELINE_VOLATILE_TIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
    re.IGNORECASE,
)
_PIPELINE_VOLATILE_TASK_RE = re.compile(
    r"\b(?:(?:scheduler\s+)?task\s*#?\s*\d+"
    r"|задач[аи]\s+(?:promptpilot|планировщика)\s*#?\s*\d+)\b",
    re.IGNORECASE,
)
_PIPELINE_GENERIC_BLOCKER_REASONS = {
    "blocked",
    "failed",
    "unable",
    "не получилось",
    "не смог",
    "нужен человек",
    "нужно решение",
    "подробности выше",
    "см. выше",
    "см. отчет выше",
    "см. отчёт выше",
    "требуется решение",
}
_PIPELINE_REPEAT_BLOCKER_PAUSE_PREFIX = "pipeline_repeat_blocker_pause:v1:"


def _pipeline_blocker_details(
        verdict: Optional[str], result: Optional[str]) -> Optional[dict]:
    """Stable identity and readable reason of one explicit blocker.

    Provider metadata, commit OIDs and the scheduler's own task id change on
    every run and therefore cannot participate in repeat detection. Issue and
    PR numbers deliberately remain: two blocked targets are two different
    reasons. A bare HUMAN/UNABLE without a reason is not safe to correlate.
    """
    kind = _PIPELINE_BLOCKER_VERDICTS.get(str(verdict or "").strip().upper())
    if kind is None:
        return None

    response = re.split(
        r"(?m)^\s*--- Meta ---\s*$", str(result or ""), maxsplit=1,
    )[0]
    lines = response.splitlines()
    selected = None
    for index, line in enumerate(lines):
        match = _PIPELINE_BLOCKER_LINE_RE.match(line)
        if not match:
            continue
        line_kind = _PIPELINE_BLOCKER_VERDICTS.get(
            match.group("verdict").strip().upper())
        if line_kind != kind:
            continue
        parts = [match.group("reason")]
        # Narrow terminals may wrap the final reason. Only indented lines are
        # presentation continuations; stop before worktree summaries or prose.
        for continuation in lines[index + 1:index + 9]:
            if not continuation.strip():
                break
            if not continuation[:1].isspace():
                break
            parts.append(continuation.strip())
        selected = " ".join(parts)
    if selected is None:
        return None

    display_reason = unicodedata.normalize("NFKC", selected)
    display_reason = re.sub(
        r"^\s*(?:[—–:-]\s*|\(\s*)+", "", display_reason)
    display_reason = re.sub(r"\s+", " ", display_reason).strip(
        " \t\r\n()[]{}.,;:—–-")
    normalized = display_reason.casefold()
    normalized = _PIPELINE_VOLATILE_UUID_RE.sub("<uuid>", normalized)
    normalized = _PIPELINE_VOLATILE_HEX_RE.sub("<oid>", normalized)
    normalized = _PIPELINE_VOLATILE_TIME_RE.sub("<time>", normalized)
    normalized = _PIPELINE_VOLATILE_TASK_RE.sub("task <id>", normalized)
    normalized = re.sub(r"\bpp[/\\]t\d+\b", "pp/t<id>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" \t\r\n()[]{}.,;:—–-")
    if (not normalized
            or normalized in _PIPELINE_GENERIC_BLOCKER_REASONS
            or not any(character.isalnum() for character in normalized)):
        return None
    return {
        "fingerprint": hashlib.sha256(
            f"{kind}\0{normalized}".encode("utf-8")).hexdigest(),
        "reason": display_reason[:1000],
    }


def _pipeline_blocker_fingerprint(
        verdict: Optional[str], result: Optional[str]) -> Optional[str]:
    details = _pipeline_blocker_details(verdict, result)
    return details["fingerprint"] if details else None


def _pipeline_repeat_blocker_pause_key(series_id: int) -> str:
    if isinstance(series_id, bool) or not isinstance(series_id, int) or series_id <= 0:
        raise ValueError("pipeline series id must be a positive integer")
    return f"{_PIPELINE_REPEAT_BLOCKER_PAUSE_PREFIX}{series_id}"


def _continue_pipeline_recurrence() -> dict:
    return {
        "suppress_recurrence": False,
        "newly_paused": False,
        "repeated": False,
    }


def _pause_pipeline_series_on_repeated_blocker(
        conn, series_id: int, current_task_id: int) -> dict:
    """Transactional implementation shared by recurrence and repair paths."""
    series = conn.execute(
        "SELECT paused, ended_at FROM task_series WHERE id = ?",
        (series_id,),
    ).fetchone()
    if not series or series["ended_at"]:
        return _continue_pipeline_recurrence()

    current = conn.execute(
        """SELECT id, status, verdict, result FROM tasks
           WHERE id = ? AND series_id = ?
             AND id = (SELECT MAX(id) FROM tasks WHERE series_id = ?)""",
        (current_task_id, series_id, series_id),
    ).fetchone()
    if not current or current["status"] != "completed":
        return _continue_pipeline_recurrence()
    current_details = _pipeline_blocker_details(
        current["verdict"], current["result"])
    if current_details is None:
        return _continue_pipeline_recurrence()

    previous = conn.execute(
        """SELECT id, status, verdict, result FROM tasks
           WHERE series_id = ? AND id < ?
           ORDER BY id DESC LIMIT 1""",
        (series_id, current_task_id),
    ).fetchone()
    if (not previous or previous["status"] != "completed"
            or _pipeline_blocker_fingerprint(
                previous["verdict"], previous["result"]
            ) != current_details["fingerprint"]):
        return _continue_pipeline_recurrence()

    now = _now()
    changed = conn.execute(
        """UPDATE task_series SET paused = 1, updated_at = ?
           WHERE id = ? AND paused = 0 AND ended_at IS NULL""",
        (now, series_id),
    ).rowcount > 0
    conn.execute(
        "DELETE FROM settings WHERE key = ?",
        (_pipeline_series_wake_intent_key(series_id),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (
            _pipeline_repeat_blocker_pause_key(series_id),
            json.dumps({
                "kind": "repeated_blocker",
                "reason": current_details["reason"],
                "fingerprint": current_details["fingerprint"],
                "previous_task_id": int(previous["id"]),
                "current_task_id": current_task_id,
            }, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return {
        "suppress_recurrence": True,
        "newly_paused": changed,
        "repeated": True,
        "reason": current_details["reason"],
        "current_task_id": current_task_id,
        "previous_task_id": int(previous["id"]),
    }


def pause_pipeline_series_on_repeated_blocker(
        series_id: int, current_task_id: int) -> dict:
    """Pause after two consecutive occurrences report the same blocker.

    The caller applies this only to a configured project-pipeline series. The
    comparison and pause share one write transaction, so no successor can be
    inserted between the decision and the pause made by this worker.
    """
    with _connect(immediate=True) as conn:
        return _pause_pipeline_series_on_repeated_blocker(
            conn, series_id, current_task_id)


def _recreate_series_occurrence(conn, series_id: int, series,
                                scheduled_at: datetime) -> bool:
    latest_row = conn.execute(
        "SELECT * FROM tasks WHERE series_id = ? ORDER BY id DESC LIMIT 1",
        (series_id,),
    ).fetchone()
    if not latest_row:
        return False
    latest = _row_to_task(latest_row)
    _insert_task(conn, TaskCreate(
        prompt=series["prompt"],
        working_dir=series["working_dir"],
        provider=series["provider"],
        priority=series["priority"],
        scheduled_at=scheduled_at,
        max_retries=latest.max_retries,
        skip_permissions=latest.skip_permissions,
        model=series["model"],
        effort=series["effort"],
        tg_chat_id=latest.tg_chat_id,
        recurrence=series["base_recurrence"],
        task_timeout=series["task_timeout"],
        detached=latest.detached,
        keep_pane=latest.keep_pane,
        herdr_target=latest.herdr_target,
        machine=latest.machine,
        worktree=latest.worktree,
        series_id=series_id,
    ))
    return True


def request_pipeline_series_wake(series_id: int) -> dict:
    """Move a pending stage now or remember the wake across its running task."""
    key = _pipeline_series_wake_intent_key(series_id)
    with _connect(immediate=True) as conn:
        series = conn.execute(
            "SELECT * FROM task_series WHERE id = ?",
            (series_id,),
        ).fetchone()
        if not series or series["paused"] or series["ended_at"]:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return {"accepted": False, "state": "inactive"}
        now = _now()
        moved = conn.execute(
            """UPDATE tasks SET scheduled_at = ?, next_run_at = NULL
               WHERE series_id = ? AND status = 'pending'
                 AND (next_run_at IS NULL OR next_run_at <= ?)""",
            (now, series_id, now),
        )
        if moved.rowcount:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return {"accepted": True, "state": "scheduled"}
        if conn.execute(
                """SELECT 1 FROM tasks
                   WHERE series_id = ? AND status = 'pending'
                     AND next_run_at > ?""",
                (series_id, now),
        ).fetchone():
            # GitHub reset/budget deadlines are stronger than an automatic
            # successor wake. Preserve the signal for the next occurrence.
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                (key,),
            )
            return {"accepted": True, "state": "latched_deferred"}
        if conn.execute(
                "SELECT 1 FROM tasks WHERE series_id = ? AND status = 'rate_limited'",
                (series_id,),
        ).fetchone():
            # A provider/account backoff is stronger than an automatic wake.
            # Do not shorten it, but remember that another occurrence became
            # useful while this one is waiting. Once the deferred attempt
            # finishes, its successor will be moved to now.
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                (key,),
            )
            return {"accepted": True, "state": "latched_rate_limited"}
        if conn.execute(
                "SELECT 1 FROM tasks WHERE series_id = ? AND status = 'running'",
                (series_id,),
        ).fetchone():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                (key,),
            )
            return {"accepted": True, "state": "latched"}
        latest = conn.execute(
            """SELECT id, status FROM tasks WHERE series_id = ?
               ORDER BY id DESC LIMIT 1""",
            (series_id,),
        ).fetchone()
        if latest and latest["status"] == "completed":
            repeat_guard = _pause_pipeline_series_on_repeated_blocker(
                conn, series_id, int(latest["id"]))
            if repeat_guard["suppress_recurrence"]:
                return {"accepted": False, "state": "repeat_blocker_paused"}
        if _recreate_series_occurrence(
                conn, series_id, series, datetime.now(timezone.utc)):
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return {"accepted": True, "state": "recreated"}
        return {"accepted": False, "state": "missing"}


def repair_active_series_occurrences(
        *, repeat_guard_series_ids=()) -> list[int]:
    """Recreate active series lost after a terminal commit and hard crash.

    A worker can die after marking an occurrence completed/failed but before
    its ``finally`` schedules the successor.  Repair only that narrow terminal
    state: a cancelled latest occurrence remains an explicit human stop.
    ``repeat_guard_series_ids`` is an explicit project-profile allowlist;
    generic recurring schedules retain their historical repair behaviour.
    """
    guarded = {
        value for value in repeat_guard_series_ids
        if type(value) is int and value > 0
    }
    repaired = []
    with _connect(immediate=True) as conn:
        series_rows = conn.execute(
            """SELECT * FROM task_series
               WHERE paused = 0 AND ended_at IS NULL ORDER BY id"""
        ).fetchall()
        for series in series_rows:
            series_id = int(series["id"])
            if conn.execute(
                    """SELECT 1 FROM tasks WHERE series_id = ?
                       AND status IN ('pending', 'running', 'rate_limited')
                       LIMIT 1""",
                    (series_id,),
            ).fetchone() is not None:
                continue
            latest = conn.execute(
                """SELECT id, status, completed_at FROM tasks WHERE series_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (series_id,),
            ).fetchone()
            if (latest is None
                    or latest["status"] not in ("completed", "failed")):
                continue
            if (series_id in guarded and latest["status"] == "completed"
                    and _pause_pipeline_series_on_repeated_blocker(
                        conn, series_id, int(latest["id"]))[
                            "suppress_recurrence"]):
                continue
            wake_key = _pipeline_series_wake_intent_key(series_id)
            has_wake = conn.execute(
                "SELECT 1 FROM settings WHERE key = ?", (wake_key,)
            ).fetchone() is not None
            anchor = _parse_dt(latest["completed_at"]) \
                or datetime.now(timezone.utc)
            recurrence = _effective_series_recurrence(dict(series), anchor)
            scheduled_at = (datetime.now(timezone.utc) if has_wake
                            else parse_recurrence(recurrence, now=anchor))
            if (scheduled_at is not None and _recreate_series_occurrence(
                    conn, series_id, series, scheduled_at)):
                # The immediate repaired occurrence itself satisfies a wake
                # latched before the crash. Keeping the bit would wake an
                # unnecessary second successor after this one completes.
                if has_wake:
                    conn.execute(
                        "DELETE FROM settings WHERE key = ?", (wake_key,))
                repaired.append(series_id)
    return repaired


def consume_pipeline_series_wake(series_id: int) -> bool:
    """Apply one durable wake after the running occurrence creates its next row."""
    key = _pipeline_series_wake_intent_key(series_id)
    with _connect(immediate=True) as conn:
        if conn.execute(
                "SELECT 1 FROM settings WHERE key = ?", (key,)
        ).fetchone() is None:
            return False
        series = conn.execute(
            "SELECT paused, ended_at FROM task_series WHERE id = ?",
            (series_id,),
        ).fetchone()
        if not series or series["paused"] or series["ended_at"]:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return False
        now = _now()
        moved = conn.execute(
            """UPDATE tasks SET scheduled_at = ?, next_run_at = NULL
               WHERE series_id = ? AND status = 'pending'
                 AND (next_run_at IS NULL OR next_run_at <= ?)""",
            (now, series_id, now),
        )
        if not moved.rowcount:
            return False
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return True


def clear_pipeline_series_wake(series_id: int) -> bool:
    key = _pipeline_series_wake_intent_key(series_id)
    with _connect() as conn:
        deleted = conn.execute(
            "DELETE FROM settings WHERE key = ?", (key,)
        )
        return deleted.rowcount > 0


def series_action(series_id: int, action: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM task_series WHERE id = ?", (series_id,)).fetchone()
        if not row:
            return False
        now = _now()
        if action == "pause":
            conn.execute("UPDATE task_series SET paused = 1, updated_at = ? WHERE id = ?",
                         (now, series_id))
            conn.execute(
                """UPDATE tasks
                   SET budget_wait_scope = NULL,
                       budget_wait_started_at = NULL
                   WHERE series_id = ?
                     AND status IN ('pending', 'rate_limited')""",
                (series_id,),
            )
            conn.execute(
                "DELETE FROM settings WHERE key = ?",
                (_pipeline_series_wake_intent_key(series_id),),
            )
        elif action == "resume":
            resumed = conn.execute(
                """UPDATE task_series SET paused = 0, updated_at = ?
                   WHERE id = ? AND paused = 1 AND ended_at IS NULL""",
                (now, series_id),
            ).rowcount > 0
            if resumed:
                conn.execute(
                    "DELETE FROM settings WHERE key = ?",
                    (_pipeline_repeat_blocker_pause_key(series_id),),
                )
            if resumed and conn.execute(
                    """SELECT 1 FROM tasks WHERE series_id = ?
                       AND status IN ('pending', 'running', 'rate_limited')""",
                    (series_id,),
            ).fetchone() is None:
                # An automatic repeat-blocker pause intentionally has no
                # successor. Resume restores exactly one runnable occurrence.
                _recreate_series_occurrence(
                    conn, series_id, row, datetime.now(timezone.utc))
        elif action == "run_now":
            # A paused series is an explicit stop. Creating or moving a task
            # here would still leave it unclaimable and falsely report that
            # the run started; Resume is the only way out of this state.
            if row["paused"] or row["ended_at"]:
                return False
            cur = conn.execute(
                """UPDATE tasks
                   SET scheduled_at = ?, next_run_at = NULL,
                       budget_wait_scope = NULL,
                       budget_wait_started_at = NULL
                   WHERE series_id = ? AND status IN ('pending', 'rate_limited')""",
                (now, series_id),
            )
            if cur.rowcount > 0:
                conn.execute(
                    "DELETE FROM settings WHERE key = ?",
                    (_pipeline_series_wake_intent_key(series_id),),
                )
                return True
            if conn.execute(
                "SELECT 1 FROM tasks WHERE series_id = ? AND status = 'running'",
                (series_id,),
            ).fetchone():
                return False

            # Cancelling the only occurrence used to leave an active series
            # broken forever: there was no pending row for run_now to move and
            # resume only toggles the pause flag. Recreate exactly one fresh
            # occurrence from the durable series plus non-editable execution
            # settings of its latest run. The UPDATE above takes the SQLite
            # write lock first, so concurrent run_now calls cannot insert two.
            recreated = _recreate_series_occurrence(
                conn, series_id, row, datetime.now(timezone.utc))
            if recreated:
                conn.execute(
                    "DELETE FROM settings WHERE key = ?",
                    (_pipeline_series_wake_intent_key(series_id),),
                )
            return recreated
        elif action == "end":
            conn.execute("UPDATE task_series SET ended_at = ?, updated_at = ? WHERE id = ?",
                         (now, now, series_id))
            conn.execute("UPDATE tasks SET status = 'cancelled', completed_at = ?, "
                         "budget_wait_scope = NULL, "
                         "budget_wait_started_at = NULL "
                         "WHERE series_id = ? AND status IN ('pending', 'rate_limited')",
                         (now, series_id))
            conn.execute(
                "DELETE FROM settings WHERE key = ?",
                (_pipeline_series_wake_intent_key(series_id),),
            )
            conn.execute(
                "DELETE FROM settings WHERE key = ?",
                (_pipeline_repeat_blocker_pause_key(series_id),),
            )
        else:
            return False
        return True


def _pipeline_cache_guard_matches(conn, guard: dict) -> bool:
    """Validate one full-cache token inside an existing write transaction."""
    try:
        cache_key = str(guard["cache_key"])
        revision_key = str(guard["revision_key"])
        epoch_key = str(guard["epoch_key"])
        expected_epoch = int(guard["epoch"])
        expected_revision = int(guard["revision"])
        expected_hash = str(guard["profile_hash"])
        generated_at = float(guard["generated_at"])
        ttl_seconds = max(0, int(guard["ttl_seconds"]))
    except (KeyError, TypeError, ValueError):
        return False
    epoch = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (epoch_key,)
    ).fetchone()
    try:
        current_epoch = int(epoch["value"]) if epoch else 0
    except (TypeError, ValueError):
        return False
    if current_epoch != expected_epoch:
        return False
    revision = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (revision_key,)
    ).fetchone()
    try:
        current_revision = int(revision["value"]) if revision else 0
    except (TypeError, ValueError):
        return False
    if current_revision != expected_revision:
        return False
    cached = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (cache_key,)
    ).fetchone()
    if cached is None:
        return False
    try:
        payload = json.loads(cached["value"])
        payload_epoch = int(payload.get("epoch"))
        payload_revision = int(payload.get("revision"))
        payload_generated_at = float(payload.get("generated_at"))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if (payload.get("profile_hash") != expected_hash
            or payload_epoch != expected_epoch
            or payload_revision != expected_revision
            or payload_generated_at != generated_at):
        return False
    return time.time() - generated_at < ttl_seconds


def wake_series_once(series_id: Optional[int], latch_key: str,
                     fingerprint: Optional[str], *, cache_guard: dict) -> bool:
    """Move one pending series task to now once per diagnostic fingerprint.

    The global-pause, latch and task checks share one immediate transaction so
    the API sampler and worker completion hook cannot race each other or a
    pause transition. Manual ``run_now`` intentionally bypasses this latch.
    """
    with _connect(immediate=True) as conn:
        paused = conn.execute(
            "SELECT value FROM settings WHERE key = 'worker_paused'"
        ).fetchone()
        if paused and paused["value"] == "1":
            return False
        if not _pipeline_cache_guard_matches(conn, cache_guard):
            return False
        if fingerprint is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (latch_key,))
            return False
        previous = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (latch_key,),
        ).fetchone()
        if previous and previous["value"] == fingerprint:
            return False
        if series_id is None:
            return False
        series = conn.execute(
            "SELECT paused, ended_at FROM task_series WHERE id = ?", (series_id,),
        ).fetchone()
        if not series or series["paused"] or series["ended_at"]:
            return False
        now = _now()
        task = conn.execute(
            """UPDATE tasks SET scheduled_at = ?, next_run_at = NULL
               WHERE series_id = ? AND status IN ('pending', 'rate_limited')
                 AND (next_run_at IS NULL OR next_run_at <= ?)""",
            (now, series_id, now),
        )
        if not task.rowcount:
            deferred = conn.execute(
                """SELECT 1 FROM tasks
                   WHERE series_id = ?
                     AND status IN ('pending', 'rate_limited')
                     AND next_run_at > ?""",
                (series_id, now),
            ).fetchone()
            if deferred is None:
                return False
            # Accept this diagnostic snapshot exactly once, but retain both
            # the hard deadline and a durable request for the successor.
            wake_key = _pipeline_series_wake_intent_key(series_id)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                (wake_key,),
            )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (latch_key, fingerprint),
        )
        return True


def wake_series_group_once(series_ids: list[int], latch_key: str,
                           fingerprint: Optional[str], *, cache_guard: dict) -> list[int]:
    """Atomically apply one diagnostic wake to every configured replica."""
    normalized = []
    for value in series_ids:
        if type(value) is int and value > 0 and value not in normalized:
            normalized.append(value)
    with _connect(immediate=True) as conn:
        paused = conn.execute(
            "SELECT value FROM settings WHERE key = 'worker_paused'"
        ).fetchone()
        if paused and paused["value"] == "1":
            return []
        if not _pipeline_cache_guard_matches(conn, cache_guard):
            return []
        if fingerprint is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (latch_key,))
            return []
        previous = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (latch_key,),
        ).fetchone()
        if previous and previous["value"] == fingerprint:
            return []
        now = _now()
        woken = []
        for series_id in normalized:
            series = conn.execute(
                "SELECT * FROM task_series WHERE id = ?",
                (series_id,),
            ).fetchone()
            if not series or series["paused"] or series["ended_at"]:
                continue
            task = conn.execute(
                """UPDATE tasks SET scheduled_at = ?, next_run_at = NULL
                   WHERE series_id = ? AND status IN ('pending', 'rate_limited')
                     AND (next_run_at IS NULL OR next_run_at <= ?)""",
                (now, series_id, now),
            )
            if not task.rowcount:
                deferred = conn.execute(
                    """SELECT 1 FROM tasks
                       WHERE series_id = ?
                         AND status IN ('pending', 'rate_limited')
                         AND next_run_at > ?""",
                    (series_id, now),
                ).fetchone()
                running = conn.execute(
                    """SELECT 1 FROM tasks
                       WHERE series_id = ? AND status = 'running'""",
                    (series_id,),
                ).fetchone()
                if deferred is None and running is None:
                    # A recurring worker commits its terminal result before
                    # ``_recur_after_run`` creates the successor.  Do not let
                    # the shared diagnostic latch make this replica miss the
                    # wake permanently when the group wake lands in that
                    # narrow gap.  Cancellation is an intentional human stop,
                    # so only repair the same terminal states as
                    # ``repair_active_series_occurrences``.
                    latest = conn.execute(
                        """SELECT id, status FROM tasks WHERE series_id = ?
                           ORDER BY id DESC LIMIT 1""",
                        (series_id,),
                    ).fetchone()
                    if (latest is None
                            or latest["status"] not in ("completed", "failed")):
                        continue
                    if (latest["status"] == "completed"
                            and _pause_pipeline_series_on_repeated_blocker(
                                conn, series_id, int(latest["id"]))[
                                    "suppress_recurrence"]):
                        continue
                    if not _recreate_series_occurrence(
                            conn, series_id, series, datetime.now(timezone.utc)):
                        continue
                    woken.append(series_id)
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                    (_pipeline_series_wake_intent_key(series_id),),
                )
            woken.append(series_id)
        if woken:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (latch_key, fingerprint),
            )
        return woken


def _pipeline_stale_reselect_guard_key(series_id: int) -> str:
    return f"pipeline_stale_reselect_guard:v1:{int(series_id)}"


def prepare_series_recurrence(series_id: int, verdict: Optional[str]) -> Optional[dict]:
    """Update temporary-boost counters and return settings for the next run."""
    # BEGIN IMMEDIATE serializes the read/modify/write with a live cadence
    # reconciliation. A deferred transaction could read the old empty counter
    # and then overwrite a concurrent busy-observation reset.
    with _connect(immediate=True) as conn:
        row = conn.execute("SELECT * FROM task_series WHERE id = ?", (series_id,)).fetchone()
        if not row or row["ended_at"]:
            return None
        s = dict(row)
        stale_key = _pipeline_stale_reselect_guard_key(series_id)
        stale = (verdict or "").upper() == "УСТАРЕЛО"
        stale_reselect_immediate = False
        if stale:
            prior = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (stale_key,)
            ).fetchone()
            if prior is None:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, '1')",
                    (stale_key,),
                )
                stale_reselect_immediate = True
        else:
            conn.execute("DELETE FROM settings WHERE key = ?", (stale_key,))
        now = datetime.now(timezone.utc)
        until = _parse_dt(s["temporary_until"])
        expired = bool(until and until <= now)
        empty_count = s["temporary_empty_count"] or 0
        if s["temporary_recurrence"]:
            empty_count = empty_count + 1 if (verdict or "").upper() == "ПУСТО" else 0
        reached = bool(s["temporary_empty_limit"] and
                       empty_count >= s["temporary_empty_limit"])
        if expired or reached:
            conn.execute(
                """UPDATE task_series SET temporary_recurrence = NULL,
                   temporary_until = NULL, temporary_empty_limit = NULL,
                   temporary_empty_count = 0, updated_at = ? WHERE id = ?""",
                (_now(), series_id),
            )
            s.update(temporary_recurrence=None, temporary_until=None,
                     temporary_empty_limit=None, temporary_empty_count=0)
        else:
            conn.execute("UPDATE task_series SET temporary_empty_count = ?, updated_at = ? WHERE id = ?",
                         (empty_count, _now(), series_id))
            s["temporary_empty_count"] = empty_count
        s["effective_recurrence"] = _effective_series_recurrence(s, now)
        s["stale_reselect_immediate"] = stale_reselect_immediate
        return s


EDITABLE_FIELDS = ("provider", "model", "effort", "priority", "recurrence",
                   "scheduled_at", "working_dir")


def update_task_fields(task_id: int, fields: dict) -> bool:
    """Правка ещё не начавшейся задачи: провайдер, модель, эффорт, расписание.

    Только pending/rate_limited: у running менять провайдера поздно (процесс уже
    идёт), а у завершённой бессмысленно — следующее вхождение серии это
    отдельная строка, её и надо править.
    """
    fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not fields:
        return False
    explicit_reschedule = "scheduled_at" in fields
    if explicit_reschedule:
        fields["scheduled_at"] = _to_utc_iso(fields["scheduled_at"])
        # An explicit operator reschedule supersedes an automatic soft wait.
        # Without this, a task intentionally moved far into the future would
        # keep lower-priority work from reserving GitHub budget indefinitely.
        fields["budget_wait_scope"] = None
        fields["budget_wait_started_at"] = None
    if "priority" in fields:
        # Do not let a later route restoration undo the operator's explicit
        # priority choice for a temporarily promoted waiting occurrence. The
        # edit also starts a fresh fairness history for the new priority.
        fields["pipeline_priority_restore"] = None
    sets = [f"{k} = ?" for k in fields]
    values = list(fields.values())
    if "priority" in fields and not explicit_reschedule:
        sets.append(
            "budget_wait_started_at = CASE "
            "WHEN budget_wait_scope IS NOT NULL THEN ? ELSE NULL END")
        values.append(_now())
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            "WHERE id = ? AND status IN ('pending', 'rate_limited')",
            (*values, task_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int) -> bool:
    with _connect() as conn:
        # A running provider may still own a checkout and an exact pipeline
        # target. Deletion must follow cancel + confirmed terminal cleanup;
        # otherwise removing the row would also free the reservation while the
        # old agent can still mutate that PR.
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND status != 'running'",
            (task_id,),
        )
        if cur.rowcount:
            _release_task_pipeline_targets(conn, task_id)
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


def add_pipeline_snapshot(profile_id: str, repository: str, payload: dict,
                          captured_at: Optional[datetime] = None, *,
                          lease_guard: Optional[dict] = None) -> Optional[dict]:
    """Persist one deterministic observation of an external pipeline.

    Snapshots intentionally contain only public queue metadata (counts, issue/
    PR identifiers and timestamps), never prompts, credentials or agent output.
    """
    captured = _to_utc_iso(captured_at or datetime.now(timezone.utc))
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _connect(immediate=lease_guard is not None) as conn:
        if (lease_guard is not None
                and not _pipeline_scan_lease_guard_matches(conn, lease_guard)):
            return None
        cur = conn.execute(
            """INSERT INTO pipeline_snapshots
               (profile_id, repository, captured_at, payload_json)
               VALUES (?, ?, ?, ?)""",
            (profile_id, repository, captured, encoded),
        )
        row_id = cur.lastrowid
    return {"id": row_id, "profile_id": profile_id, "repository": repository,
            "captured_at": captured, "payload": payload}


def list_pipeline_snapshots(profile_id: str, since: Optional[datetime] = None,
                            limit: int = 1000) -> list[dict]:
    """Return profile snapshots oldest-first for trend calculations."""
    clauses = ["profile_id = ?"]
    params: list = [profile_id]
    if since is not None:
        clauses.append("captured_at >= ?")
        params.append(_to_utc_iso(since))
    params.append(max(1, min(int(limit), 10000)))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, profile_id, repository, captured_at, payload_json
                FROM pipeline_snapshots WHERE {' AND '.join(clauses)}
                ORDER BY captured_at ASC LIMIT ?""",
            params,
        ).fetchall()
    return [{"id": row["id"], "profile_id": row["profile_id"],
             "repository": row["repository"], "captured_at": row["captured_at"],
             "payload": json.loads(row["payload_json"])} for row in rows]


_PIPELINE_SNAPSHOT_LOOKUP_LIMIT = 64


def _first_valid_pipeline_snapshot(rows, predicate) -> Optional[tuple]:
    """Decode only a bounded SQL-prefiltered candidate set."""
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and predicate(payload):
            return row, payload
    return None


def latest_pipeline_snapshot(
        profile_id: str, profile_hash: Optional[str] = None,
        allow_legacy: bool = True) -> Optional[dict]:
    """Return the newest matching durable queue observation for one profile."""
    selected = None
    with _connect() as conn:
        select = """SELECT id, profile_id, repository, captured_at, payload_json
                    FROM pipeline_snapshots WHERE profile_id = ?"""
        order = " ORDER BY captured_at DESC, id DESC LIMIT ?"
        if profile_hash is None:
            rows = conn.execute(
                select + order,
                (profile_id, _PIPELINE_SNAPSHOT_LOOKUP_LIMIT),
            ).fetchall()
            selected = _first_valid_pipeline_snapshot(rows, lambda _payload: True)
        else:
            # add_pipeline_snapshot writes compact JSON, so SQL can discard an
            # arbitrary number of newer snapshots for other profile revisions
            # before Python decodes a small candidate set.  The bound protects
            # cache-only UI reads from corrupt/hostile rows containing a false
            # textual marker; parsed top-level equality remains authoritative.
            marker = '"profile_hash":' + json.dumps(
                profile_hash, ensure_ascii=False, separators=(",", ":"))
            rows = conn.execute(
                select + " AND instr(payload_json, ?) > 0" + order,
                (profile_id, marker, _PIPELINE_SNAPSHOT_LOOKUP_LIMIT),
            ).fetchall()
            selected = _first_valid_pipeline_snapshot(
                rows, lambda payload: payload.get("profile_hash") == profile_hash)

            if selected is None and allow_legacy:
                # Legacy snapshots predate profile_hash altogether. Query them
                # separately so thousands of known-mismatching hashed rows do
                # not re-enter the bounded Python validation path.
                rows = conn.execute(
                    select + " AND instr(payload_json, ?) = 0" + order,
                    (profile_id, '"profile_hash"',
                     _PIPELINE_SNAPSHOT_LOOKUP_LIMIT),
                ).fetchall()
                selected = _first_valid_pipeline_snapshot(
                    rows, lambda payload: "profile_hash" not in payload)
    if selected is None:
        return None
    row, payload = selected
    return {
        "id": row["id"], "profile_id": row["profile_id"],
        "repository": row["repository"], "captured_at": row["captured_at"],
        "payload": payload,
    }


def prune_pipeline_snapshots(before: datetime) -> int:
    """Bound local history; aggregated dashboard windows need no raw data forever."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM pipeline_snapshots WHERE captured_at < ?",
                           (_to_utc_iso(before),))
        return cur.rowcount


def pipeline_run_metrics(series_ids: list[int], since: datetime) -> dict:
    """Semantic outcomes of scheduled runs belonging to one pipeline profile."""
    ids = sorted({int(value) for value in series_ids if value is not None})
    empty = {"runs": 0, "ready": 0, "empty": 0, "human": 0,
             "no_change": 0, "stale": 0, "unable": 0, "failed": 0,
             "other": 0,
             "unresolved_unable": 0, "unresolved_failed": 0,
             "recovered_unable": 0, "recovered_failed": 0,
             "tokens_known_runs": 0, "input_tokens": 0,
             "output_tokens": 0, "total_tokens": 0}
    if not ids:
        return empty
    marks = ",".join("?" for _ in ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, series_id, status, verdict, result FROM tasks
                WHERE series_id IN ({marks}) AND completed_at >= ?""",
            (*ids, _to_utc_iso(since)),
        ).fetchall()
    rows = sorted(rows, key=lambda row: row["id"])
    result = dict(empty)
    unresolved = {series_id: {"unable": 0, "failed": 0} for series_id in ids}
    for row in rows:
        result["runs"] += 1
        verdict = (row["verdict"] or "").strip().upper()
        output = row["result"] or ""
        usage = re.search(r"(?mi)^Tokens:\s*(\d+)\s+in\s*/\s*(\d+)\s+out\s*$", output)
        if usage:
            input_tokens, output_tokens = map(int, usage.groups())
            result["tokens_known_runs"] += 1
            result["input_tokens"] += input_tokens
            result["output_tokens"] += output_tokens
            result["total_tokens"] += input_tokens + output_tokens
        reported_no_change = any(pattern in output.lower() for pattern in (
            "github не изменялся", "очередь оставлена без изменений",
            "ничего не влито", "изменений не выполнялось",
            "no github changes", "no changes were made", "nothing was merged",
        ))
        if row["status"] == "failed":
            result["failed"] += 1
            unresolved[int(row["series_id"])]["failed"] += 1
        elif verdict == "УЖЕ СДЕЛАНО" or (verdict == "ГОТОВО" and reported_no_change):
            result["no_change"] += 1
            unresolved[int(row["series_id"])] = {"unable": 0, "failed": 0}
        elif verdict == "ГОТОВО":
            result["ready"] += 1
            unresolved[int(row["series_id"])] = {"unable": 0, "failed": 0}
        elif verdict == "ПУСТО":
            result["empty"] += 1
            unresolved[int(row["series_id"])] = {"unable": 0, "failed": 0}
        elif verdict == "НУЖЕН ЧЕЛОВЕК":
            result["human"] += 1
            # The stage itself completed correctly even if the selected item
            # now waits for a person, so an older execution incident is over.
            unresolved[int(row["series_id"])] = {"unable": 0, "failed": 0}
        elif verdict == "УСТАРЕЛО":
            # An exact target changed before the first mutation. This is a
            # successful safety fence followed by re-election, not an error.
            result["stale"] += 1
            unresolved[int(row["series_id"])] = {"unable": 0, "failed": 0}
        elif verdict == "НЕ СМОГ":
            result["unable"] += 1
            unresolved[int(row["series_id"])]["unable"] += 1
        else:
            result["other"] += 1
    result["unresolved_unable"] = sum(item["unable"] for item in unresolved.values())
    result["unresolved_failed"] = sum(item["failed"] for item in unresolved.values())
    result["recovered_unable"] = result["unable"] - result["unresolved_unable"]
    result["recovered_failed"] = result["failed"] - result["unresolved_failed"]
    return result


def pipeline_series_activity(series_ids: list[int]) -> dict[int, dict]:
    """Return the latest terminal run per series for pipeline dashboards."""
    ids = sorted({int(value) for value in series_ids if value is not None})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, series_id, status, verdict, started_at, completed_at,
                       result
                FROM tasks WHERE series_id IN ({marks})
                  AND status IN ('completed', 'failed', 'cancelled')
                ORDER BY id DESC""",
            ids,
        ).fetchall()
    activity = {}
    for row in rows:
        series_id = int(row["series_id"])
        if series_id in activity:
            continue
        output = (row["result"] or "").split("\n--- Meta ---", 1)[0].strip()
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        outcome = next((line for line in reversed(lines)
                        if line.upper().startswith("ИТОГ:")), lines[-1] if lines else "")
        duration = None
        if row["started_at"] and row["completed_at"]:
            duration = round((_parse_dt(row["completed_at"]) -
                              _parse_dt(row["started_at"])).total_seconds())
        usage = re.search(r"(?mi)^Tokens:\s*(\d+)\s+in\s*/\s*(\d+)\s+out\s*$",
                          row["result"] or "")
        activity[series_id] = {
            "task_id": row["id"], "status": row["status"],
            "verdict": row["verdict"], "at": row["completed_at"],
            "duration_seconds": duration, "summary": outcome[:300],
            "input_tokens": int(usage.group(1)) if usage else None,
            "output_tokens": int(usage.group(2)) if usage else None,
        }
    return activity


def get_setting(key: str, default: str = None) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def get_settings_snapshot(keys: list[str]) -> dict[str, str]:
    """Read several settings at one SQLite snapshot/linearization point."""
    unique_keys = list(dict.fromkeys(str(key) for key in keys))
    if not unique_keys:
        return {}
    marks = ",".join("?" for _ in unique_keys)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({marks})",
            unique_keys,
        ).fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def set_setting(key: str, value: str):
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def has_setting_prefix(prefix: str) -> bool:
    """Return whether any setting key starts with the exact literal prefix."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM settings WHERE substr(key, 1, ?) = ? LIMIT 1",
            (len(prefix), prefix),
        ).fetchone()
    return row is not None


def increment_int_setting(key: str, default: int = 0) -> int:
    """Atomically increment an integer setting shared by all PP processes."""
    with _connect(immediate=True) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        try:
            current = int(row["value"]) if row else int(default)
        except (TypeError, ValueError):
            current = int(default)
        updated = current + 1
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(updated)),
        )
    return updated


def set_setting_if_newer_revision(
        key: str, value: str, *, revision_key: str, revision: int,
        guard_key: str, expected_guard: str,
        guard_default: Optional[str] = None,
        lease_guard: Optional[dict] = None,
        publication_revision_key: Optional[str] = None,
        companion_key: Optional[str] = None,
        companion_value: Optional[str] = None) -> bool:
    """Atomically publish a newer revision while a durable guard matches."""
    if (companion_key is None) != (companion_value is None):
        raise ValueError("companion_key and companion_value must be set together")
    with _connect(immediate=True) as conn:
        if (lease_guard is not None
                and not _pipeline_scan_lease_guard_matches(conn, lease_guard)):
            return False
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (guard_key,)
        ).fetchone()
        actual = row["value"] if row else guard_default
        if actual != expected_guard:
            return False
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (revision_key,)
        ).fetchone()
        try:
            published_revision = int(row["value"]) if row else 0
        except (TypeError, ValueError):
            published_revision = 0
        if published_revision >= int(revision):
            return False
        if publication_revision_key is not None:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (publication_revision_key,),
            ).fetchone()
            try:
                active_revision = int(row["value"]) if row else 0
            except (TypeError, ValueError):
                active_revision = 0
            if active_revision >= int(revision):
                return False
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (revision_key, str(int(revision))),
        )
        if companion_key is not None:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (companion_key, companion_value),
            )
        if publication_revision_key is not None:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (publication_revision_key, str(int(revision))),
            )
    return True


def delete_setting(key: str):
    with _connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def _valid_pipeline_scan_token(value) -> bool:
    """Return whether a lease token has the exact durable wire type."""
    return isinstance(value, str) and 1 <= len(value) <= 256


def _pipeline_scan_lease_key(scope: str) -> str:
    """Return a bounded settings key for one shared GitHub scan scope."""
    value = str(scope).strip()
    if not value:
        raise ValueError("pipeline scan lease scope must not be empty")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"pipeline_github_scan_lease:v1:{digest}"


_PIPELINE_GITHUB_BUDGET_RESERVATION_PREFIX = \
    "pipeline_github_budget_reservations:v1:"
_PIPELINE_GITHUB_BUDGET_REVISION_PREFIX = \
    "pipeline_github_budget_revision:v1:"
_PIPELINE_GITHUB_RATE_SNAPSHOT_PREFIX = "pipeline_github_rate_snapshot:v1:"
_PIPELINE_GITHUB_BUDGET_RESOURCES = ("core", "search", "graphql")


def _pipeline_github_budget_reservation_key(scope: str) -> str:
    value = str(scope).strip()
    if not value:
        raise ValueError("pipeline GitHub budget scope must not be empty")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{_PIPELINE_GITHUB_BUDGET_RESERVATION_PREFIX}{digest}"


def _pipeline_github_budget_revision_key(scope: str) -> str:
    value = str(scope).strip()
    if not value:
        raise ValueError("pipeline GitHub budget scope must not be empty")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{_PIPELINE_GITHUB_BUDGET_REVISION_PREFIX}{digest}"


def _pipeline_github_budget_revision(conn, scope: str) -> int:
    key = _pipeline_github_budget_revision_key(scope)
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return 0
    try:
        revision = int(row["value"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "pipeline GitHub budget revision is corrupt") from exc
    if revision < 0:
        raise ValueError("pipeline GitHub budget revision is corrupt")
    return revision


def _advance_pipeline_github_budget_revision(conn, scope: str) -> int:
    revision = _pipeline_github_budget_revision(conn, scope) + 1
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (_pipeline_github_budget_revision_key(scope), str(revision)),
    )
    return revision


def _pipeline_github_rate_snapshot_key(scope: str) -> str:
    value = str(scope).strip()
    if not value:
        raise ValueError("pipeline GitHub budget scope must not be empty")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{_PIPELINE_GITHUB_RATE_SNAPSHOT_PREFIX}{digest}"


def _pipeline_budget_vector(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if set(value) != set(_PIPELINE_GITHUB_BUDGET_RESOURCES):
        raise ValueError(
            f"{name} must contain exactly "
            + ", ".join(_PIPELINE_GITHUB_BUDGET_RESOURCES))
    result = {}
    for resource in _PIPELINE_GITHUB_BUDGET_RESOURCES:
        amount = value.get(resource)
        if (isinstance(amount, bool) or not isinstance(amount, int)
                or amount < 0):
            raise ValueError(f"{name}.{resource} must be a non-negative integer")
        result[resource] = amount
    return result


def _load_pipeline_budget_reservations(conn, key: str) -> list[dict]:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["value"])
        values = payload["reservations"]
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or not isinstance(values, list)):
            raise ValueError("invalid reservation ledger envelope")
        result = []
        seen_tokens = set()
        seen_attempts = set()
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("invalid reservation ledger item")
            token = item.get("token")
            task_id = item.get("task_id")
            started_at = item.get("task_started_at")
            if (not _valid_pipeline_scan_token(token)
                    or isinstance(task_id, bool) or not isinstance(task_id, int)
                    or task_id <= 0 or not isinstance(started_at, str)
                    or not started_at or not isinstance(item.get("profile_id"), str)
                    or not item.get("profile_id")
                    or not isinstance(item.get("queue_id"), str)
                    or not item.get("queue_id")
                    or not isinstance(item.get("route"), str)
                    or not item.get("route")
                    or isinstance(item.get("created_at"), bool)
                    or not isinstance(item.get("created_at"), (int, float))
                    or not math.isfinite(float(item["created_at"]))):
                raise ValueError("invalid reservation ledger item identity")
            cost = _pipeline_budget_vector(item.get("cost"), "reservation cost")
            attempt = (task_id, started_at)
            if token in seen_tokens or attempt in seen_attempts:
                raise ValueError("duplicate reservation ledger item")
            seen_tokens.add(token)
            seen_attempts.add(attempt)
            result.append({
                "token": token, "task_id": task_id,
                "task_started_at": started_at,
                "profile_id": item["profile_id"],
                "queue_id": item["queue_id"], "route": item["route"],
                "cost": cost, "created_at": float(item["created_at"]),
            })
        return result
    except (KeyError, OverflowError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        # Unlike a mutex lease, reservations represent quota promised to live
        # tasks. Silently discarding malformed state could admit two expensive
        # providers, so corruption is a fail-closed operator condition.
        raise ValueError("pipeline GitHub budget reservation ledger is corrupt") from exc


def _load_or_recover_pipeline_budget_reservations(
        conn, key: str, scope: str) -> tuple[list[dict], dict | None]:
    """Reclaim corrupt state only when no provider attempt can still own it."""
    try:
        return _load_pipeline_budget_reservations(conn, key), None
    except ValueError:
        running = conn.execute(
            "SELECT 1 FROM tasks WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running is not None:
            raise
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        _advance_pipeline_github_budget_revision(conn, scope)
        woken = _wake_pipeline_github_budget_waiters(conn, scope)
        return [], woken


def _active_pipeline_budget_reservations(conn, values: list[dict]) -> tuple[list[dict], bool]:
    active = []
    for item in values:
        row = conn.execute(
            "SELECT status, started_at FROM tasks WHERE id = ?",
            (item["task_id"],),
        ).fetchone()
        if (row is not None and row["status"] == "running"
                and row["started_at"] == item["task_started_at"]):
            active.append(item)
    return active, len(active) != len(values)


def _write_pipeline_budget_reservations(conn, key: str, values: list[dict]) -> None:
    if not values:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps(
            {"version": 1, "reservations": values},
            ensure_ascii=False, separators=(",", ":"))),
    )


def _pipeline_budget_starvation_timeout(value: int) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value <= 86400):
        raise ValueError(
            "pipeline budget starvation timeout must be 0..86400 seconds")
    return value


def _pipeline_github_budget_waiter_summary(
        conn, scope: str, *, starvation_timeout_seconds: int = 0,
        now: Optional[float] = None) -> dict:
    starvation_timeout_seconds = _pipeline_budget_starvation_timeout(
        starvation_timeout_seconds)
    row = conn.execute(
        """SELECT COUNT(*) AS count, MIN(priority) AS min_priority
           FROM tasks
           WHERE budget_wait_scope = ? AND (
                 status = 'running' OR (
                   status = 'pending'
                   AND (series_id IS NULL OR EXISTS (
                     SELECT 1 FROM task_series s WHERE s.id = tasks.series_id
                       AND s.paused = 0 AND s.ended_at IS NULL))))""",
        (scope,),
    ).fetchone()
    fairness_waiter = None
    if starvation_timeout_seconds:
        oldest = conn.execute(
            """SELECT id, priority, budget_wait_started_at
               FROM tasks
               WHERE budget_wait_scope = ?
                 AND budget_wait_started_at IS NOT NULL
                 AND (status = 'running' OR (
                   status = 'pending'
                   AND (series_id IS NULL OR EXISTS (
                     SELECT 1 FROM task_series s WHERE s.id = tasks.series_id
                       AND s.paused = 0 AND s.ended_at IS NULL))))
               ORDER BY budget_wait_started_at ASC, priority ASC, id ASC
               LIMIT 1""",
            (scope,),
        ).fetchone()
        if oldest is not None:
            try:
                wait_started = datetime.fromisoformat(
                    oldest["budget_wait_started_at"])
                if wait_started.tzinfo is None:
                    raise ValueError("missing timezone")
                wait_started = wait_started.astimezone(timezone.utc)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "pipeline budget waiter timestamp is invalid") from exc
            current = (time.time() if now is None else float(now))
            if not math.isfinite(current):
                raise ValueError("pipeline budget fairness time must be finite")
            eligible_at = wait_started.timestamp() + starvation_timeout_seconds
            if current >= eligible_at:
                fairness_waiter = {
                    "task_id": int(oldest["id"]),
                    "priority": int(oldest["priority"]),
                    "wait_started_at": wait_started.isoformat(),
                    "eligible_at": datetime.fromtimestamp(
                        eligible_at, timezone.utc).isoformat(),
                    "wait_seconds": max(
                        0, int(current - wait_started.timestamp())),
                }
    return {
        "count": int(row["count"] or 0) if row is not None else 0,
        "min_priority": row["min_priority"] if row is not None else None,
        "fairness_waiter": fairness_waiter,
    }


def _wake_pipeline_github_budget_waiters(conn, scope: str) -> dict:
    """Wake soft waits while preserving their priority handoff marker."""
    summary = _pipeline_github_budget_waiter_summary(conn, scope)
    cur = conn.execute(
        """UPDATE tasks
           SET scheduled_at = ?, next_run_at = NULL
           WHERE status = 'pending' AND budget_wait_scope = ?""",
        (_now(), scope),
    )
    return {
        "count": cur.rowcount,
        "min_priority": summary["min_priority"],
    }


def arm_pipeline_github_budget_waiter(
        scope: str, *, task_id: int, task_started_at,
        expected_revision: int | None = None,
        now: Optional[float] = None) -> dict:
    """Persist a priority handoff before releasing the admission scan lease.

    New reservations are serialized by that lease, while reservation release
    is intentionally independent. Recording the exact running attempt before
    the lease is released closes both release-before-defer and
    release-before-lower-admission windows. The marker survives wake and claim;
    it is cleared only by successful reservation, terminal settlement, a
    different defer reason, or an explicit operator reschedule.
    """
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
        raise ValueError("pipeline GitHub budget scope must contain 1..256 characters")
    if (expected_revision is not None
            and (isinstance(expected_revision, bool)
                 or not isinstance(expected_revision, int)
                 or expected_revision < 0)):
        raise ValueError(
            "expected pipeline budget revision must be a non-negative integer")
    attempt = _attempt_iso(task_started_at)
    if attempt is None:
        raise ValueError("running task attempt is required for budget waiter")
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise ValueError("pipeline budget waiter time must be finite")
    wait_started_at = datetime.fromtimestamp(current, timezone.utc).isoformat()
    with _connect(immediate=True) as conn:
        revision = _pipeline_github_budget_revision(conn, scope)
        cur = conn.execute(
            """UPDATE tasks
               SET budget_wait_started_at = CASE
                     WHEN budget_wait_scope = ?
                       AND budget_wait_started_at IS NOT NULL
                     THEN budget_wait_started_at
                     ELSE ?
                   END,
                   budget_wait_scope = ?
               WHERE id = ? AND status = 'running' AND started_at = ?""",
            (scope, wait_started_at, scope, task_id, attempt),
        )
        return {
            "armed": cur.rowcount > 0,
            "revision": revision,
            "revision_changed": (
                expected_revision is not None
                and revision != expected_revision),
        }


def clear_pipeline_github_budget_waiter(
        *, task_id: int, task_started_at) -> bool:
    """Clear one successful attempt's handoff without touching a successor."""
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise ValueError("running task id is required for budget waiter clear")
    attempt = _attempt_iso(task_started_at)
    if attempt is None:
        raise ValueError("running task attempt is required for budget waiter clear")
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE tasks
               SET budget_wait_scope = NULL, budget_wait_started_at = NULL
               WHERE id = ? AND status = 'running' AND started_at = ?""",
            (task_id, attempt),
        )
        return cur.rowcount > 0


def pipeline_github_budget_reservations(
        scope: str, *, starvation_timeout_seconds: int = 0,
        now: Optional[float] = None) -> dict:
    """Return live, task-fenced reservations and prune completed attempts."""
    key = _pipeline_github_budget_reservation_key(scope)
    with _connect(immediate=True) as conn:
        values, recovered = _load_or_recover_pipeline_budget_reservations(
            conn, key, scope)
        active, changed = _active_pipeline_budget_reservations(conn, values)
        revision = _pipeline_github_budget_revision(conn, scope)
        woken = recovered or {"count": 0, "min_priority": None}
        if changed:
            _write_pipeline_budget_reservations(conn, key, active)
            revision = _advance_pipeline_github_budget_revision(conn, scope)
            stale_woken = _wake_pipeline_github_budget_waiters(conn, scope)
            priorities = [
                value for value in (
                    woken.get("min_priority"),
                    stale_woken.get("min_priority"))
                if type(value) is int
            ]
            woken = {
                "count": int(woken.get("count") or 0)
                         + int(stale_woken.get("count") or 0),
                "min_priority": min(priorities) if priorities else None,
            }
        totals = {resource: 0 for resource in _PIPELINE_GITHUB_BUDGET_RESOURCES}
        for item in active:
            for resource in totals:
                totals[resource] += item["cost"][resource]
        waiting = _pipeline_github_budget_waiter_summary(
            conn, scope,
            starvation_timeout_seconds=starvation_timeout_seconds,
            now=now)
        fairness_waiter = waiting.pop("fairness_waiter", None)
        result = {"count": len(active), "totals": totals, "items": active,
                  "revision": revision, "woken_waiters": woken,
                  "waiting_waiters": waiting}
        if fairness_waiter is not None:
            result["fairness_waiter"] = fairness_waiter
        return result


def record_pipeline_github_rate_snapshot(
        scope: str, limits: dict, *, observed_at: Optional[float] = None,
        scan_lease_guard: Optional[dict] = None) -> bool:
    """Persist a quota-free UI snapshot while the caller owns scan ordering."""
    key = _pipeline_github_rate_snapshot_key(scope)
    sanitized = {}
    for resource in _PIPELINE_GITHUB_BUDGET_RESOURCES:
        item = limits.get(resource) if isinstance(limits, dict) else None
        if not isinstance(item, dict):
            raise ValueError(f"invalid GitHub {resource} rate limit")
        values = {}
        for name in ("limit", "used", "remaining", "reset"):
            value = item.get(name)
            if type(value) is not int or value < 0:
                raise ValueError(f"invalid GitHub {resource}.{name}")
            values[name] = value
        reset_at = item.get("reset_at")
        if reset_at is not None and not isinstance(reset_at, str):
            raise ValueError(f"invalid GitHub {resource}.reset_at")
        values["reset_at"] = reset_at
        sanitized[resource] = values
    timestamp = time.time() if observed_at is None else float(observed_at)
    if not math.isfinite(timestamp):
        raise ValueError("GitHub rate snapshot time must be finite")
    payload = {"version": 1, "observed_at": timestamp, "limits": sanitized}
    with _connect(immediate=True) as conn:
        if (scan_lease_guard is not None
                and not _pipeline_scan_lease_guard_matches(
                    conn, scan_lease_guard)):
            return False
        existing = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if existing is not None:
            try:
                previous = json.loads(existing["value"])
                previous_at = float(previous["observed_at"])
            except (KeyError, OverflowError, TypeError, ValueError,
                    json.JSONDecodeError):
                previous_at = -1
            if previous_at > timestamp:
                return True
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(payload, ensure_ascii=False,
                             separators=(",", ":"))),
        )
        return True


def get_pipeline_github_rate_snapshot(scope: str) -> Optional[dict]:
    """Return the last locally observed GitHub limits without network I/O."""
    key = _pipeline_github_rate_snapshot_key(scope)
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["value"])
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or not math.isfinite(float(payload["observed_at"]))):
            raise ValueError("invalid GitHub rate snapshot")
        # Reuse the strict writer validation without touching SQLite.
        limits = payload["limits"]
        for resource in _PIPELINE_GITHUB_BUDGET_RESOURCES:
            item = limits[resource]
            if (not isinstance(item, dict)
                    or any(type(item.get(name)) is not int
                           or item[name] < 0
                           for name in ("limit", "used", "remaining", "reset"))
                    or (item.get("reset_at") is not None
                        and not isinstance(item.get("reset_at"), str))):
                raise ValueError("invalid GitHub rate snapshot")
        if set(limits) != set(_PIPELINE_GITHUB_BUDGET_RESOURCES):
            raise ValueError("invalid GitHub rate snapshot")
        return {"observed_at": float(payload["observed_at"]),
                "limits": limits}
    except (KeyError, OverflowError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        raise ValueError("pipeline GitHub rate snapshot is corrupt") from exc


def reserve_pipeline_github_budget(
        scope: str, *, token: str, task_id: int, task_started_at: str,
        profile_id: str, queue_id: str, route: str, cost: dict,
        limits: dict, minimum_remaining: dict,
        starvation_timeout_seconds: int = 0,
        now: Optional[float] = None,
        scan_lease_guard: Optional[dict] = None,
        expected_revision: Optional[int] = None) -> dict:
    """Atomically reserve quota for one exact running task attempt."""
    key = _pipeline_github_budget_reservation_key(scope)
    if not _valid_pipeline_scan_token(token):
        raise ValueError("pipeline budget token must contain 1..256 characters")
    if isinstance(task_started_at, datetime):
        task_started_at = _to_utc_iso(task_started_at)
    if (isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0
            or not isinstance(task_started_at, str) or not task_started_at
            or not isinstance(profile_id, str) or not profile_id
            or not isinstance(queue_id, str) or not queue_id
            or not isinstance(route, str) or not route):
        raise ValueError("invalid pipeline budget reservation identity")
    if (expected_revision is not None
            and (isinstance(expected_revision, bool)
                 or not isinstance(expected_revision, int)
                 or expected_revision < 0)):
        raise ValueError(
            "expected pipeline budget revision must be a non-negative integer")
    requested = _pipeline_budget_vector(cost, "cost")
    floor = _pipeline_budget_vector(minimum_remaining, "minimum_remaining")
    starvation_timeout_seconds = _pipeline_budget_starvation_timeout(
        starvation_timeout_seconds)
    reported = {}
    resets = {}
    for resource in _PIPELINE_GITHUB_BUDGET_RESOURCES:
        item = limits.get(resource) if isinstance(limits, dict) else None
        try:
            raw_remaining = item["remaining"]
            raw_reset = item["reset"]
            if (isinstance(raw_remaining, bool) or isinstance(raw_reset, bool)
                    or isinstance(raw_remaining, float)
                    and not raw_remaining.is_integer()
                    or isinstance(raw_reset, float) and not raw_reset.is_integer()):
                raise ValueError("non-integral GitHub rate limit")
            remaining = int(raw_remaining)
            reset = int(raw_reset)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid GitHub {resource} rate limit") from exc
        if remaining < 0 or reset <= 0:
            raise ValueError(f"invalid GitHub {resource} rate limit")
        reported[resource] = remaining
        resets[resource] = reset
    created_at = time.time() if now is None else float(now)
    if not math.isfinite(created_at):
        raise ValueError("pipeline budget reservation time must be finite")

    with _connect(immediate=True) as conn:
        if (scan_lease_guard is not None
                and not _pipeline_scan_lease_guard_matches(
                    conn, scan_lease_guard)):
            return {"allowed": False, "state": "scan_lease_lost",
                    "reason": "GitHub scan lease changed before budget reservation"}
        task = conn.execute(
            "SELECT status, started_at, priority FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()
        if (task is None or task["status"] != "running"
                or task["started_at"] != task_started_at):
            return {"allowed": False, "state": "task_fence_lost",
                    "reason": "running task attempt changed before budget reservation"}
        values, recovered = _load_or_recover_pipeline_budget_reservations(
            conn, key, scope)
        active, changed = _active_pipeline_budget_reservations(conn, values)
        revision = _pipeline_github_budget_revision(conn, scope)
        woken = recovered or {"count": 0, "min_priority": None}
        if changed:
            _write_pipeline_budget_reservations(conn, key, active)
            revision = _advance_pipeline_github_budget_revision(conn, scope)
            stale_woken = _wake_pipeline_github_budget_waiters(conn, scope)
            priorities = [
                value for value in (
                    woken.get("min_priority"),
                    stale_woken.get("min_priority"))
                if type(value) is int
            ]
            woken = {
                "count": int(woken.get("count") or 0)
                         + int(stale_woken.get("count") or 0),
                "min_priority": min(priorities) if priorities else None,
            }
        own = [item for item in active if item["token"] == token]
        if own:
            item = own[0]
            if (item["task_id"] != task_id
                    or item["task_started_at"] != task_started_at
                    or item["cost"] != requested or item["route"] != route):
                raise ValueError("pipeline budget token was reused inconsistently")
            conn.execute(
                """UPDATE tasks
                   SET budget_wait_scope = NULL,
                       budget_wait_started_at = NULL
                   WHERE id = ? AND status = 'running' AND started_at = ?
                     AND budget_wait_scope = ?""",
                (task_id, task_started_at, scope),
            )
            return {"allowed": True, "state": "reserved",
                    "reported_remaining": reported,
                    "reserved_other": {resource: sum(
                        value["cost"][resource] for value in active
                        if value["token"] != token) for resource in reported},
                    "requested_cost": requested,
                    "minimum_remaining": floor,
                    "effective_after": {resource: reported[resource] - sum(
                        value["cost"][resource] for value in active)
                        for resource in reported},
                    "blocked_resources": [],
                    "active_reservations": len(active),
                    "revision": revision}
        if any(item["task_id"] == task_id
               and item["task_started_at"] == task_started_at for item in active):
            raise ValueError("running task attempt already owns another reservation")

        reserved = {resource: sum(
            item["cost"][resource] for item in active) for resource in reported}
        after = {resource: reported[resource] - reserved[resource] - requested[resource]
                 for resource in reported}
        if expected_revision is not None and revision != expected_revision:
            return {
                "allowed": False, "state": "ledger_changed",
                "reason": (
                    "GitHub budget reservations changed between admission "
                    "and final reservation"),
                "blocked_resources": [],
                "reported_remaining": reported, "reserved_other": reserved,
                "requested_cost": requested, "minimum_remaining": floor,
                "effective_after": after, "active_reservations": len(active),
                "revision": revision,
            }

        waiting = _pipeline_github_budget_waiter_summary(
            conn, scope,
            starvation_timeout_seconds=starvation_timeout_seconds,
            now=created_at)
        fairness_waiter = waiting.get("fairness_waiter")
        if (isinstance(fairness_waiter, dict)
                and fairness_waiter.get("task_id") != task_id):
            return {
                "allowed": False, "state": "fairness_waiter",
                "reason": (
                    "the oldest GitHub budget waiter owns the admission baton"),
                "fairness_waiter": fairness_waiter,
                "blocked_resources": [],
                "reported_remaining": reported, "reserved_other": reserved,
                "requested_cost": requested, "minimum_remaining": floor,
                "effective_after": after, "active_reservations": len(active),
                "revision": revision,
            }
        waiting_priority = waiting.get("min_priority")
        if (fairness_waiter is None
                and type(waiting_priority) is int
                and waiting_priority < int(task["priority"])):
            return {
                "allowed": False, "state": "priority_waiter",
                "reason": (
                    "a higher-priority task is waiting for this GitHub "
                    "budget reservation"),
                "blocked_resources": [],
                "reported_remaining": reported, "reserved_other": reserved,
                "requested_cost": requested, "minimum_remaining": floor,
                "effective_after": after, "active_reservations": len(active),
                "revision": revision,
            }

        # Crash recovery may discover and prune a stale owner while a lower-
        # priority task is trying to reserve. Yield once to a waiter that the
        # same transaction just woke, otherwise the lower-priority task can
        # immediately consume the released budget and recreate the inversion.
        woken_priority = woken.get("min_priority")
        if ((changed or recovered is not None)
                and type(woken_priority) is int
                and woken_priority < int(task["priority"])):
            return {
                "allowed": False, "state": "priority_waiter",
                "reason": (
                    "a higher-priority GitHub budget waiter was woken after "
                    "stale reservation recovery"),
                "blocked_resources": [],
                "reported_remaining": reported,
                "reserved_other": {resource: sum(
                    item["cost"][resource] for item in active)
                    for resource in reported},
                "requested_cost": requested, "minimum_remaining": floor,
                "effective_after": {}, "active_reservations": len(active),
                "revision": revision,
            }
        blocked = [{
            "resource": resource,
            "reported_remaining": reported[resource],
            "reserved_other": reserved[resource],
            "requested_cost": requested[resource],
            "minimum_remaining": floor[resource],
            "effective_after": after[resource],
            "reset": resets[resource],
            "blocked_by": (
                "live" if reported[resource] - requested[resource]
                < floor[resource] else "reservation"),
        } for resource in reported if after[resource] < floor[resource]]
        if blocked:
            state = ("low" if any(item["blocked_by"] == "live"
                                  for item in blocked)
                     else "budget_in_flight")
            if state == "low":
                # A waiter that cannot fit under its unchanged quota floor
                # must relinquish the baton atomically with this decision. A
                # worker crash before defer must not freeze new reservations
                # until GitHub resets.
                conn.execute(
                    """UPDATE tasks
                       SET budget_wait_scope = NULL,
                           budget_wait_started_at = NULL
                       WHERE id = ? AND status = 'running'
                         AND started_at = ? AND budget_wait_scope = ?""",
                    (task_id, task_started_at, scope),
                )
            return {"allowed": False, "state": state,
                    "blocked_resources": blocked,
                    "reported_remaining": reported, "reserved_other": reserved,
                    "requested_cost": requested, "minimum_remaining": floor,
                    "effective_after": after, "active_reservations": len(active),
                    "revision": revision}

        active.append({
            "token": token, "task_id": task_id,
            "task_started_at": task_started_at, "profile_id": profile_id,
            "queue_id": queue_id, "route": route, "cost": requested,
            "created_at": created_at,
        })
        _write_pipeline_budget_reservations(conn, key, active)
        revision = _advance_pipeline_github_budget_revision(conn, scope)
        conn.execute(
            """UPDATE tasks
               SET budget_wait_scope = NULL,
                   budget_wait_started_at = NULL
               WHERE id = ? AND status = 'running' AND started_at = ?
                 AND budget_wait_scope = ?""",
            (task_id, task_started_at, scope),
        )
        return {"allowed": True, "state": "reserved",
                "reported_remaining": reported, "reserved_other": reserved,
                "requested_cost": requested, "minimum_remaining": floor,
                "effective_after": after,
                "active_reservations": len(active),
                "revision": revision}


def release_pipeline_github_budget(
        scope: str, *, token: str, task_id: int, task_started_at: str) -> bool:
    """Release only the exact reservation owned by this task attempt."""
    key = _pipeline_github_budget_reservation_key(scope)
    if not _valid_pipeline_scan_token(token):
        return False
    if isinstance(task_started_at, datetime):
        task_started_at = _to_utc_iso(task_started_at)
    with _connect(immediate=True) as conn:
        values, _recovered = _load_or_recover_pipeline_budget_reservations(
            conn, key, scope)
        kept = [item for item in values if not (
            item["token"] == token and item["task_id"] == task_id
            and item["task_started_at"] == task_started_at)]
        removed = len(kept) != len(values)
        active, changed = _active_pipeline_budget_reservations(conn, kept)
        if not removed and not changed:
            return False
        _write_pipeline_budget_reservations(conn, key, active)
        _advance_pipeline_github_budget_revision(conn, scope)
        _wake_pipeline_github_budget_waiters(conn, scope)
        return removed


_PIPELINE_REFRESH_STATUS_PREFIX = "pipeline_github_refresh_status:v1:"
_PIPELINE_REFRESH_REVISION_KEY = "pipeline_github_refresh_revision:v1"


def _pipeline_refresh_status_key(profile_id: str) -> str:
    """Return a bounded key for a profile's transient refresh status."""
    value = str(profile_id).strip()
    if not value:
        raise ValueError("pipeline profile id must not be empty")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{_PIPELINE_REFRESH_STATUS_PREFIX}{digest}"


def _pipeline_refresh_current_revision(conn) -> int:
    """Read the durable global event clock and recover it from status rows."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (_PIPELINE_REFRESH_REVISION_KEY,),
    ).fetchone()
    try:
        counter = int(row["value"]) if row is not None else 0
        if counter < 0:
            raise ValueError("negative refresh revision")
    except (TypeError, ValueError):
        counter = 0
    rows = conn.execute(
        "SELECT value FROM settings WHERE substr(key, 1, ?) = ?",
        (len(_PIPELINE_REFRESH_STATUS_PREFIX),
         _PIPELINE_REFRESH_STATUS_PREFIX),
    ).fetchall()
    for status_row in rows:
        try:
            payload = json.loads(status_row["value"])
            revision = payload["revision"]
            if (not isinstance(payload, dict)
                    or payload.get("version") != 1
                    or isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision <= 0):
                continue
            counter = max(counter, revision)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return counter


def _next_pipeline_refresh_revision(conn) -> int:
    """Allocate one globally monotonic status revision in this transaction."""
    revision = _pipeline_refresh_current_revision(conn) + 1
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (_PIPELINE_REFRESH_REVISION_KEY, str(revision)),
    )
    return revision


def _write_pipeline_refresh_status(
        conn, key: str, repository: str, status: Optional[dict],
        revision: int) -> bool:
    """Publish one already-allocated event if it is newer for this profile."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    existing_revision = 0
    if row is not None:
        try:
            existing = json.loads(row["value"])
            existing_revision = existing["revision"]
            if (not isinstance(existing, dict)
                    or existing.get("version") != 1
                    or isinstance(existing_revision, bool)
                    or not isinstance(existing_revision, int)
                    or existing_revision <= 0):
                raise ValueError("invalid refresh status payload")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            existing_revision = 0
    if existing_revision >= revision:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, json.dumps({
            "version": 1,
            "repository": str(repository),
            "revision": revision,
            "status": status,
        }, ensure_ascii=False, separators=(",", ":"))),
    )
    return True


def _pipeline_scan_lease_guard_matches(conn, guard: dict) -> bool:
    """Fence a write against the exact live owner inside its transaction."""
    try:
        key = _pipeline_scan_lease_key(str(guard["scope"]))
        token = guard["token"]
    except (KeyError, TypeError, ValueError):
        return False
    if not _valid_pipeline_scan_token(token):
        return False
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return False
    try:
        payload = json.loads(row["value"])
        if not isinstance(payload, dict):
            raise ValueError("invalid lease payload")
        expires_at = float(payload["expires_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("version") == 1
        and _valid_pipeline_scan_token(payload.get("token"))
        and payload.get("token") == token
        and math.isfinite(expires_at)
        and expires_at > time.time()
    )


def acquire_pipeline_scan_lease(
        scope: str, token: str, ttl_seconds: float, *,
        now: Optional[float] = None) -> dict:
    """Atomically acquire a renewable cross-process GitHub scan lease.

    The lease lives in the existing SQLite settings table, so the API server,
    worker, bot and any additional PromptPilot processes sharing ``DB_PATH``
    serialize expensive GitHub observations. A crashed owner is recoverable
    after ``expires_at``; atomically committed malformed state is reclaimable.
    """
    key = _pipeline_scan_lease_key(scope)
    owner = token
    ttl = float(ttl_seconds)
    current = time.time() if now is None else float(now)
    if not _valid_pipeline_scan_token(owner):
        raise ValueError("pipeline scan lease token must contain 1..256 characters")
    if not math.isfinite(current):
        raise ValueError("pipeline scan lease time must be finite")
    if not math.isfinite(ttl) or not 0 < ttl <= 86400:
        raise ValueError("pipeline scan lease ttl must be between 0 and 86400 seconds")

    with _connect(immediate=True) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            try:
                existing = json.loads(row["value"])
                if existing.get("version") != 1:
                    raise ValueError("unsupported lease payload")
                existing_token = existing["token"]
                existing_expires = float(existing["expires_at"])
                if (not _valid_pipeline_scan_token(existing_token)
                        or not math.isfinite(existing_expires)
                        or not existing_expires > 0
                        or existing_expires > current + 86400):
                    raise ValueError("invalid lease payload")
            except (AttributeError, KeyError, TypeError, ValueError,
                    json.JSONDecodeError):
                # SQLite commits the value atomically, so malformed JSON cannot
                # be a partially written live lease. Reclaim it under this same
                # BEGIN IMMEDIATE transaction instead of deadlocking forever.
                existing_token = ""
                existing_expires = 0
            if existing_token != owner and existing_expires > current:
                status_revision = _next_pipeline_refresh_revision(conn)
                return {
                    "acquired": False, "expires_at": existing_expires,
                    "state": "busy", "status_revision": status_revision,
                }

        expires_at = current + ttl
        status_revision = _next_pipeline_refresh_revision(conn)
        payload = json.dumps(
            {"version": 1, "token": owner, "expires_at": expires_at},
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, payload),
        )
    return {
        "acquired": True, "expires_at": expires_at, "state": "owned",
        "status_revision": status_revision,
    }


def renew_pipeline_scan_lease(
        scope: str, token: str, ttl_seconds: float, *,
        now: Optional[float] = None) -> Optional[float]:
    """Extend a scan lease iff ``token`` still owns it."""
    key = _pipeline_scan_lease_key(scope)
    owner = token
    ttl = float(ttl_seconds)
    current = time.time() if now is None else float(now)
    if (not _valid_pipeline_scan_token(owner) or not math.isfinite(current)
            or not math.isfinite(ttl)
            or not 0 < ttl <= 86400):
        return None
    with _connect(immediate=True) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["value"])
            if not isinstance(payload, dict):
                raise ValueError("invalid lease payload")
            existing_expires = float(payload["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (payload.get("version") != 1
                or not _valid_pipeline_scan_token(payload.get("token"))
                or payload.get("token") != owner
                or not math.isfinite(existing_expires)
                or existing_expires <= current):
            return None
        expires_at = current + ttl
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (json.dumps(
                {"version": 1, "token": owner, "expires_at": expires_at},
                separators=(",", ":")), key),
        )
    return expires_at


def release_pipeline_scan_lease(
        scope: str, token: str, *,
        refresh_status: Optional[dict] = None) -> bool:
    """Release a lease and optionally publish final refresh status atomically."""
    key = _pipeline_scan_lease_key(scope)
    owner = token
    if not _valid_pipeline_scan_token(owner):
        return False
    status_key = None
    status_repository = None
    final_status = None
    if refresh_status is not None:
        if not isinstance(refresh_status, dict):
            return False
        try:
            status_key = _pipeline_refresh_status_key(
                refresh_status["profile_id"])
            status_repository = str(refresh_status["repository"])
            final_status = refresh_status["status"]
        except (KeyError, TypeError, ValueError):
            return False
        if final_status is not None and not isinstance(final_status, dict):
            return False
    with _connect(immediate=True) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return False
        try:
            payload = json.loads(row["value"])
            if not isinstance(payload, dict):
                raise ValueError("invalid lease payload")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if (payload.get("version") != 1
                or not _valid_pipeline_scan_token(payload.get("token"))
                or payload.get("token") != owner):
            return False
        if status_key is not None:
            # Final status and lease deletion share one SQLite linearization
            # point. Its revision is newer than every busy observation that
            # completed while this owner held the lease; a successor can only
            # allocate a later revision after this transaction commits.
            revision = _next_pipeline_refresh_revision(conn)
            _write_pipeline_refresh_status(
                conn, status_key, status_repository, final_status, revision)
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    return True


def publish_pipeline_refresh_status(
        profile_id: str, repository: str, status: Optional[dict], *,
        revision: Optional[int] = None,
        lease_guard: Optional[dict] = None) -> bool:
    """Publish a blocked event or clear tombstone on the global SQLite clock."""
    key = _pipeline_refresh_status_key(profile_id)
    if status is not None and not isinstance(status, dict):
        raise ValueError("pipeline refresh status must be an object or null")
    if (revision is not None
            and (isinstance(revision, bool) or not isinstance(revision, int)
                 or revision <= 0)):
        raise ValueError("pipeline refresh status revision must be positive")
    with _connect(immediate=True) as conn:
        if (lease_guard is not None
                and not _pipeline_scan_lease_guard_matches(conn, lease_guard)):
            return False
        if revision is None:
            event_revision = _next_pipeline_refresh_revision(conn)
        else:
            # Caller revisions come only from acquire_pipeline_scan_lease().
            # Reject a fabricated/unallocated future value that could poison
            # the durable ordering. An allocated observation that is no longer
            # the global tip is already superseded, even if its per-profile
            # row has not yet been written by the newer contender/owner.
            current_revision = _pipeline_refresh_current_revision(conn)
            if revision > current_revision:
                raise ValueError("pipeline refresh status revision was not allocated")
            if revision < current_revision:
                return False
            event_revision = revision
        return _write_pipeline_refresh_status(
            conn, key, repository, status, event_revision)


def get_pipeline_refresh_status(profile_id: str) -> Optional[dict]:
    """Read one validated status event, including a clear tombstone."""
    raw = get_setting(_pipeline_refresh_status_key(profile_id))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        revision = payload["revision"]
        status = payload["status"]
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or isinstance(revision, bool) or not isinstance(revision, int)
                or revision <= 0
                or status is not None and not isinstance(status, dict)):
            raise ValueError("invalid refresh status payload")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload


def touch_worker_heartbeat(pid: int, now: Optional[datetime] = None):
    """Publish a portable worker liveness signal for the server and dashboards."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {"pid": int(pid), "heartbeat_at": stamp.isoformat(), "stopped": False}
    set_setting("worker_runtime", json.dumps(payload, separators=(",", ":")))


def mark_worker_stopped(pid: int, now: Optional[datetime] = None):
    """Make a graceful worker stop visible immediately instead of after TTL."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {"pid": int(pid), "heartbeat_at": stamp.isoformat(), "stopped": True}
    set_setting("worker_runtime", json.dumps(payload, separators=(",", ":")))


def worker_runtime_status(now: Optional[datetime] = None,
                          stale_after_seconds: int = 30) -> dict:
    """Return heartbeat age without relying on platform-specific process APIs."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw = get_setting("worker_runtime")
    if not raw:
        return {
            "state": "offline", "heartbeat_at": None, "age_seconds": None,
            "pid": None, "paused": is_paused(),
        }
    try:
        payload = json.loads(raw)
        heartbeat = _parse_dt(payload.get("heartbeat_at"))
        age = max(0, round((current - heartbeat).total_seconds())) if heartbeat else None
        online = not payload.get("stopped") and age is not None and age <= stale_after_seconds
        return {
            "state": "online" if online else "offline",
            "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
            "age_seconds": age, "pid": payload.get("pid"),
            "paused": is_paused(),
        }
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "state": "offline", "heartbeat_at": None, "age_seconds": None,
            "pid": None, "paused": is_paused(),
        }


def is_paused() -> bool:
    return get_setting("worker_paused", "0") == "1"


def parse_recurrence(recurrence: str, *,
                     now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse recurrence string and return next run datetime (UTC).

    Supported formats:
      "30m"          — every 30 minutes
      "6h"           — every 6 hours
      "daily@09:00"  — every day at 09:00 UTC
    """
    if not recurrence:
        return None
    s = recurrence.strip().lower()
    now = now or datetime.now(timezone.utc)
    # Nh or Nm
    m = re.fullmatch(r"(\d+)([mh])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(minutes=n) if unit == "m" else timedelta(hours=n)
        return now + delta
    # daily@HH:MM
    m = re.fullmatch(r"daily@(\d{1,2}):(\d{2})", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
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


def recover_running_attempt(task_id: int, started_at) -> bool:
    """Settle one exact orphan after its provider was proven stopped.

    A cancellation requested before the crash remains cancellation; every
    other orphan is requeued. The status change, flag consumption and exact
    reservation release share one transaction.
    """
    attempt = _attempt_iso(started_at)
    if attempt is None:
        return False
    with _connect(immediate=True) as conn:
        cancelled = conn.execute(
            "SELECT 1 FROM settings WHERE key = ?",
            (f"cancel_task:{task_id}",),
        ).fetchone() is not None
        if cancelled:
            cur = conn.execute(
                """UPDATE tasks
                   SET status = 'cancelled', completed_at = ?,
                       error = COALESCE(error, ?), note = NULL,
                       budget_wait_scope = NULL,
                       budget_wait_started_at = NULL
                   WHERE id = ? AND status = 'running' AND started_at = ?""",
                (_now(), "Отменена пользователем до перезапуска worker",
                 task_id, attempt),
            )
        else:
            cur = conn.execute(
                """UPDATE tasks SET status = 'pending', started_at = NULL
                   WHERE id = ? AND status = 'running' AND started_at = ?""",
                (task_id, attempt),
            )
        if cur.rowcount:
            _drop_cancel_flag(conn, task_id)
            _release_task_pipeline_targets(conn, task_id, attempt)
        return cur.rowcount > 0


def reset_task(task_id: int) -> bool:
    """Reset a single stuck 'running' task back to 'pending'."""
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE tasks SET status = 'pending', started_at = NULL
               WHERE id = ? AND status = 'running'
                 AND NOT EXISTS (
                     SELECT 1 FROM pipeline_target_reservations AS r
                     WHERE r.task_id = tasks.id
                 )""",
            (task_id,),
        )
        return cur.rowcount > 0


def task_has_live_pipeline_target_reservation(task_id: int) -> bool:
    """Whether reset must wait for an owned provider to stop safely."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM pipeline_target_reservations
               WHERE task_id = ? LIMIT 1""",
            (task_id,),
        ).fetchone()
        return row is not None


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


def _row_to_workflow_plan(row: sqlite3.Row) -> WorkflowPlanInDB:
    data = dict(row)
    data["output"] = _json_load(data.pop("output_json"), None)
    for field in ("created_at", "updated_at", "approved_at"):
        data[field] = _parse_dt(data[field])
    return WorkflowPlanInDB(**data)


def _row_to_workflow_stage(row: sqlite3.Row) -> WorkflowStageInDB:
    raw = dict(row)
    spec = _json_load(raw.pop("spec_json"))
    summary = _json_load(raw.pop("summary_json"), None)
    spec.update({
        "id": raw["id"],
        "workflow_id": raw["workflow_id"],
        "position": raw["position"],
        "code": raw["code"],
        "title": raw["title"],
        "objective": raw["objective"],
        "stage_type": raw["stage_type"],
        "status": raw["status"],
        "created_at": _parse_dt(raw["created_at"]),
        "started_at": _parse_dt(raw["started_at"]),
        "completed_at": _parse_dt(raw["completed_at"]),
        "summary": summary,
    })
    return WorkflowStageInDB(**spec)


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


def get_workflow_plan(workflow_id: str, *, conn=None) -> Optional[WorkflowPlanInDB]:
    def _query(c):
        row = c.execute(
            "SELECT * FROM workflow_plans WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        return _row_to_workflow_plan(row) if row else None

    if conn is not None:
        return _query(conn)
    with _connect() as c:
        return _query(c)


def list_workflow_stages(workflow_id: str, *, conn=None) -> list[WorkflowStageInDB]:
    def _query(c):
        rows = c.execute(
            """SELECT * FROM workflow_stages WHERE workflow_id = ?
               ORDER BY position""",
            (workflow_id,),
        ).fetchall()
        return [_row_to_workflow_stage(row) for row in rows]

    if conn is not None:
        return _query(conn)
    with _connect() as c:
        return _query(c)


def get_workflow_stage(stage_id: str, *, conn=None) -> Optional[WorkflowStageInDB]:
    def _query(c):
        row = c.execute(
            "SELECT * FROM workflow_stages WHERE id = ?", (stage_id,)
        ).fetchone()
        return _row_to_workflow_stage(row) if row else None

    if conn is not None:
        return _query(conn)
    with _connect() as c:
        return _query(c)


def _replace_workflow_stages(conn: sqlite3.Connection, workflow_id: str,
                             stages: list[WorkflowStageSpec]) -> list[WorkflowStageInDB]:
    conn.execute("DELETE FROM workflow_stages WHERE workflow_id = ?", (workflow_id,))
    now = _now()
    for position, stage in enumerate(stages, start=1):
        stage_id = _new_id("stage")
        spec = stage.model_dump(
            mode="json", exclude={"code", "title", "objective", "stage_type"}
        )
        conn.execute(
            """INSERT INTO workflow_stages
               (id, workflow_id, position, code, title, objective, stage_type,
                status, spec_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (stage_id, workflow_id, position, stage.code, stage.title,
             stage.objective, stage.stage_type.value, _json_dump(spec), now),
        )
    rows = conn.execute(
        "SELECT * FROM workflow_stages WHERE workflow_id = ? ORDER BY position",
        (workflow_id,),
    ).fetchall()
    return [_row_to_workflow_stage(row) for row in rows]


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
        metadata_changes = set(changes) - {"config_json"}
        if current["status"] != "draft" and metadata_changes:
            raise WorkflowConflictError(
                "workflow metadata except config can only be edited while status is draft"
            )
        if current["status"] in {"completed", "failed", "cancelled"}:
            raise WorkflowConflictError(
                "terminal workflow configuration cannot be edited"
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
