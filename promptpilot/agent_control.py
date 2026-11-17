"""Owner-configured agent access to ordinary, approval-gated workflows.

Reading this registry never creates a database or migrates its schema. Creating
an agent task uses the native workflow functions and the same SQLite transaction
as its idempotency receipt. This module neither starts nor approves a workflow.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config
from .models import WorkflowCreate, WorkflowPlanReplace, WorkflowStageSpec, normalize_workflow_config


class AgentControlError(ValueError):
    code = "invalid_request"


class AgentForbidden(AgentControlError):
    code = "forbidden"


class AgentConflict(AgentControlError):
    code = "conflict"


class AgentUnavailable(AgentControlError):
    code = "unavailable"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _finite_text(value):
    if not value.strip() or "\0" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError("text must be nonblank and valid Unicode without NUL")
    return value


def _repo_url(value):
    if value is None or not value.strip():
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+/?", parsed.path)):
        raise ValueError("repository_url must name one HTTPS GitHub repository")
    return "https://github.com" + parsed.path.rstrip("/").removesuffix(".git").lower()


class ProjectCreate(_StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=160)
    repository_url: str | None = Field(default=None, max_length=2048)
    repository_path: str = Field(min_length=1, max_length=4096)
    workflow_config: dict = Field(default_factory=dict, validate_default=True)
    default_executor: str = Field(default="zcode", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    reviewer: str = Field(default="codex", min_length=1, max_length=100)
    deliverables: list[str] = Field(default_factory=lambda: ["RESULT.md"], max_length=100)
    allowed_paths: list[str] = Field(default_factory=list, max_length=100)
    readable_paths: list[str] = Field(default_factory=lambda: ["README.md", "AGENTS.md"], max_length=100)
    branch_prefix: str = Field(default="agent/", min_length=1, max_length=80)
    enabled: bool = True

    _name = field_validator("name", "default_executor", "reviewer")(_finite_text)
    _repo = field_validator("repository_url")(_repo_url)

    @field_validator("repository_path")
    @classmethod
    def absolute_repository(cls, value):
        _finite_text(value)
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("repository_path must be an owner-configured absolute path")
        return str(path.resolve())

    @field_validator("deliverables", "allowed_paths")
    @classmethod
    def finite_items(cls, value):
        for item in value:
            _finite_text(item)
            if len(item) > 2048:
                raise ValueError("project item is too long")
        return value

    @field_validator("deliverables")
    @classmethod
    def default_deliverable(cls, value):
        return value or ["RESULT.md"]

    @field_validator("readable_paths")
    @classmethod
    def relative_readable_paths(cls, value):
        for item in value:
            path = item.removesuffix("/")
            if (not path or len(item) > 1024 or "\\" in item or ":" in item or "\0" in item
                    or any(part in {"", ".", ".."} for part in path.split("/"))):
                raise ValueError("readable_paths must be exact relative POSIX files or directory prefixes")
        return value

    @field_validator("branch_prefix")
    @classmethod
    def fixed_branch_prefix(cls, value):
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
                or ".." in value or "//" in value or any(part.endswith(".lock") for part in value.split("/"))):
            raise ValueError("branch_prefix must be a finite Git branch prefix")
        return value

    @field_validator("workflow_config")
    @classmethod
    def owner_workflow_config(cls, value):
        normalized = normalize_workflow_config(value)
        if normalized.get("workorder_guard") is not None:
            raise ValueError("website workorder authority cannot be delegated through agent projects")
        # Every submitted ready plan still needs the native owner's approval.
        normalized["planning"]["require_approval"] = True
        if "enabled" not in (value.get("automation") or {}):
            normalized["automation"]["enabled"] = True
        return normalized


class ConnectionCreate(_StrictModel):
    name: str = Field(min_length=1, max_length=160)
    project_ids: list[str] = Field(min_length=1, max_length=100)

    _name = field_validator("name")(_finite_text)

    @field_validator("project_ids")
    @classmethod
    def distinct_ids(cls, value):
        if len(set(value)) != len(value) or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", v) for v in value):
            raise ValueError("project_ids must be distinct configured project IDs")
        return value


class PlanStage(_StrictModel):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=20000)

    _text = field_validator("title", "objective")(_finite_text)


class TaskCreate(_StrictModel):
    project_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    objective: str = Field(min_length=1, max_length=20000)
    issue_url: str | None = Field(default=None, max_length=2048)
    plan: list[PlanStage] | None = Field(default=None, min_length=1, max_length=50)

    _objective = field_validator("objective")(_finite_text)


def _wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _require_admin_boundary():
    if not config.API_TOKEN:
        raise AgentForbidden("Configure PP_API_TOKEN administrator authentication before enabling agent connections")


def _schema(conn):
    # Do not use executescript: it implicitly commits an existing transaction.
    for statement in (
        "CREATE TABLE IF NOT EXISTS agent_projects (id TEXT PRIMARY KEY, config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS agent_connections (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, project_ids_json TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)",
        "CREATE TABLE IF NOT EXISTS agent_task_requests (connection_id TEXT NOT NULL REFERENCES agent_connections(id), request_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES agent_projects(id), workflow_id TEXT NOT NULL UNIQUE REFERENCES workflows(id), payload_sha256 TEXT NOT NULL, issue_url TEXT, created_at TEXT NOT NULL, PRIMARY KEY(connection_id,request_id))",
        "CREATE TABLE IF NOT EXISTS agent_issue_claims (project_id TEXT NOT NULL REFERENCES agent_projects(id), issue_url TEXT NOT NULL, workflow_id TEXT NOT NULL UNIQUE REFERENCES workflows(id), PRIMARY KEY(project_id,issue_url))",
    ):
        conn.execute(statement)


@contextmanager
def _read():
    path = Path(config.DB_PATH)
    if not path.is_file():
        yield None
        return
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


@contextmanager
def _write():
    # Normal application startup owns the native DB schema. Never bootstrap it
    # from a registry read or silently create a second, empty application DB.
    if not Path(config.DB_PATH).is_file():
        raise AgentUnavailable("PromptPilot database is not initialized")
    conn = sqlite3.connect(str(config.DB_PATH), timeout=3, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise AgentUnavailable("Database operation unavailable; reconcile creation with the same request_id") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _has_registry(conn):
    return conn is not None and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_projects'").fetchone() is not None


def _project(conn, project_id):
    row = conn.execute("SELECT config_json FROM agent_projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise AgentForbidden("Project access is not available")
    value = json.loads(row["config_json"])
    if not value["enabled"]:
        raise AgentForbidden("Project access is not available")
    return value


def _scope(conn, connection_id):
    row = conn.execute("SELECT * FROM agent_connections WHERE id=? AND revoked_at IS NULL", (connection_id,)).fetchone()
    if not row:
        raise AgentForbidden("Connection access is not available")
    return json.loads(row["project_ids_json"])


def _authorized(conn, connection_id, project_id):
    if project_id not in _scope(conn, connection_id):
        raise AgentForbidden("Project access is not available")
    return _project(conn, project_id)


def list_projects():
    """Owner-only registry view; callers of MCP get the separate safe view."""
    with _read() as conn:
        if not _has_registry(conn):
            return []
        return [json.loads(row[0]) for row in conn.execute("SELECT config_json FROM agent_projects ORDER BY id")]


def upsert_project(data: ProjectCreate):
    value = data.model_dump(mode="json")
    value["workflow_config"]["roles"]["reviewer"]["provider"] = value["reviewer"]
    value["workflow_config"]["external_executor"] = {"client_id": value["default_executor"], "label": value["default_executor"], "advance_after_submit": True}
    value["workflow_config"]["stage"]["execution_mode"] = "external"
    # The owner sees and persists this effective policy before issuing access.
    # A missing custom check selects the built-in receipt/artifact check; it
    # does not disable deterministic verification or claim code-quality proof.
    if value["workflow_config"]["gate"]["enabled"] and not value["workflow_config"]["gate"]["commands"]:
        from .native_external_gate import default_command
        value["workflow_config"]["gate"]["commands"] = [default_command()]
    value["workflow_config"].setdefault("external_result_policy", {
        "artifacts": value["deliverables"],
        "text_artifacts": ["RESULT.md"] if "RESULT.md" in value["deliverables"] else [],
    })
    now = _now()
    with _write() as conn:
        conn.execute("INSERT INTO agent_projects(id,config_json,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at", (data.id, _wire(value), now, now))
    return value


def delete_project(project_id):
    with _write() as conn:
        row = conn.execute("SELECT config_json FROM agent_projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return False
        value = json.loads(row[0])
        value["enabled"] = False
        conn.execute("UPDATE agent_projects SET config_json=?,updated_at=? WHERE id=?", (_wire(value), _now(), project_id))
    return True


def _connection_public(row):
    return {"id": row["id"], "name": row["name"], "project_ids": json.loads(row["project_ids_json"]), "created_at": row["created_at"], "revoked_at": row["revoked_at"]}


def issue_connection(data: ConnectionCreate):
    _require_admin_boundary()
    token = "pp_agent_" + secrets.token_urlsafe(32)
    identity = "agent_" + uuid.uuid4().hex
    with _write() as conn:
        for project_id in data.project_ids:
            _project(conn, project_id)
        conn.execute("INSERT INTO agent_connections(id,name,token_hash,project_ids_json,created_at) VALUES(?,?,?,?,?)", (identity, data.name, hashlib.sha256(token.encode()).hexdigest(), _wire(data.project_ids), _now()))
        public = _connection_public(conn.execute("SELECT * FROM agent_connections WHERE id=?", (identity,)).fetchone())
    return {"connection": public, "token": token}


def list_connections():
    with _read() as conn:
        if not _has_registry(conn):
            return []
        return [_connection_public(row) for row in conn.execute("SELECT * FROM agent_connections ORDER BY created_at,id")]


def revoke_connection(connection_id):
    with _write() as conn:
        changed = conn.execute("UPDATE agent_connections SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (_now(), connection_id)).rowcount
    return bool(changed)


def authenticate(token):
    _require_admin_boundary()
    if not isinstance(token, str) or len(token) > 256 or not token.startswith("pp_agent_"):
        raise AgentForbidden("A valid agent connection bearer token is required")
    with _read() as conn:
        if _has_registry(conn):
            row = conn.execute("SELECT id FROM agent_connections WHERE token_hash=? AND revoked_at IS NULL", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row:
                return row[0]
    raise AgentForbidden("A valid agent connection bearer token is required")


def authorized_project(connection_id, project_id):
    with _read() as conn:
        if not _has_registry(conn):
            raise AgentForbidden("Project access is not available")
        return _authorized(conn, connection_id, project_id)


def agent_projects(connection_id):
    with _read() as conn:
        if not _has_registry(conn):
            raise AgentForbidden("Connection access is not available")
        ids = _scope(conn, connection_id)
        result = []
        for row in conn.execute("SELECT config_json FROM agent_projects ORDER BY id"):
            value = json.loads(row[0])
            if value["id"] in ids and value["enabled"]:
                result.append({k: value[k] for k in ("id", "name", "repository_url", "default_executor", "reviewer", "deliverables", "readable_paths")})
        return result


def _issue(value, project):
    if value is None:
        return None
    if not project["repository_url"]:
        raise AgentControlError("issue_url requires an owner-configured GitHub repository")
    parsed = urlsplit(value)
    expected = urlsplit(project["repository_url"])
    match = re.fullmatch(re.escape(expected.path) + r"/issues/([1-9][0-9]{0,11})/?", parsed.path.lower())
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.query or parsed.fragment or not match:
        raise AgentControlError("issue_url must name an issue in the configured repository")
    return project["repository_url"] + "/issues/" + match[1]


def _workflow_config(project, workflow_id):
    value = json.loads(_wire(project["workflow_config"]))
    value["roles"]["reviewer"]["provider"] = project["reviewer"]
    value["planning"]["require_approval"] = True
    value["external_executor"] = {"client_id": project["default_executor"], "label": project["default_executor"], "advance_after_submit": True}
    value["stage"]["execution_mode"] = "external"
    # Substitute only an owner-defined, literal opaque workflow ID placeholder.
    for i, command in enumerate(value["gate"]["commands"]):
        if isinstance(command, str):
            value["gate"]["commands"][i] = command.replace("{workflow_id}", workflow_id)
        else:
            command["argv"] = [part.replace("{workflow_id}", workflow_id) for part in command["argv"]]
    return value


def _task_public(row):
    # An explicit allowlist: never serialize native configuration, gate argv,
    # local checkout paths, provider credentials, or raw runtime messages.
    return {"task_id": row["id"], "project_id": row["project_id"], "slug": row["slug"], "objective": row["objective"], "issue_url": row["issue_url"], "status": row["status"], "state_version": row["state_version"], "created_at": row["created_at"], "updated_at": row["updated_at"], "workflow_path": "/api/workflows/" + row["id"], "dashboard_path": "/#workflow/" + row["id"], "approval_required": row["status"] in {"draft", "awaiting_plan_approval"}, "started_by_this_request": False}


def _task_row(conn, workflow_id):
    return conn.execute("SELECT w.*,r.project_id,r.issue_url FROM workflows w JOIN agent_task_requests r ON r.workflow_id=w.id WHERE w.id=?", (workflow_id,)).fetchone()


def create_task(connection_id, data: TaskCreate):
    _require_admin_boundary()
    # Imports are the normal application's native modules; registry reads above
    # intentionally do not import db and trigger its startup initialization.
    from . import db, workflows

    with _write() as conn:
        _require_admin_boundary()
        project = _authorized(conn, connection_id, data.project_id)
        issue_url = _issue(data.issue_url, project)
        payload = data.model_dump(mode="json")
        payload["issue_url"] = issue_url
        fingerprint = hashlib.sha256(_wire(payload).encode()).hexdigest()
        existing = conn.execute("SELECT * FROM agent_task_requests WHERE connection_id=? AND request_id=?", (connection_id, data.request_id)).fetchone()
        if existing:
            if existing["payload_sha256"] != fingerprint:
                raise AgentConflict("request_id already belongs to different task input")
            return {**_task_public(_task_row(conn, existing["workflow_id"])), "replayed": True}
        if issue_url:
            claim = conn.execute("SELECT c.workflow_id,w.status FROM agent_issue_claims c LEFT JOIN workflows w ON w.id=c.workflow_id WHERE c.project_id=? AND c.issue_url=?", (data.project_id, issue_url)).fetchone()
            if claim:
                if claim["status"] not in {"completed", "failed", "cancelled"}:
                    raise AgentConflict("This project issue already has an active agent task")
                conn.execute("DELETE FROM agent_issue_claims WHERE project_id=? AND issue_url=?", (data.project_id, issue_url))
        workflow_id = "wf_" + uuid.uuid4().hex
        slug = "agent-" + hashlib.sha256((connection_id + ":" + data.request_id).encode()).hexdigest()[:40]
        objective = data.objective + ("\n\nReference issue (context only): " + issue_url if issue_url else "")
        native = WorkflowCreate(slug=slug, objective=objective, repository_path=project["repository_path"], candidate_branch=project["branch_prefix"] + slug, config=_workflow_config(project, workflow_id))
        workflow = db.create_workflow(native, workflow_id=workflow_id, conn=conn)
        proposed = data.plan or [PlanStage(title=next(line.strip() for line in data.objective.splitlines() if line.strip())[:200], objective=objective)]
        specs = [WorkflowStageSpec(code=f"stage-{i+1:02}", title=stage.title, objective=stage.objective + ("\n\nReference issue (context only): " + issue_url if issue_url and issue_url not in stage.objective else ""), stage_type="integration" if i == len(proposed)-1 else "implementation", execution_mode="external", dependencies=[f"stage-{i:02}"] if i else [], allowed_paths=project["allowed_paths"], deliverables=project["deliverables"]) for i, stage in enumerate(proposed)]
        workflows.provide_plan(workflow.id, WorkflowPlanReplace(expected_version=workflow.state_version, stages=specs), conn=conn)
        conn.execute("INSERT INTO agent_task_requests(connection_id,request_id,project_id,workflow_id,payload_sha256,issue_url,created_at) VALUES(?,?,?,?,?,?,?)", (connection_id, data.request_id, data.project_id, workflow.id, fingerprint, issue_url, _now()))
        if issue_url:
            conn.execute("INSERT INTO agent_issue_claims(project_id,issue_url,workflow_id) VALUES(?,?,?)", (data.project_id, issue_url, workflow.id))
        result = {**_task_public(_task_row(conn, workflow.id)), "replayed": False}
    return result


def get_task(connection_id, task_id):
    with _read() as conn:
        if not _has_registry(conn):
            raise AgentForbidden("Task access is not available")
        row = _task_row(conn, task_id)
        if row is None:
            raise AgentForbidden("Task access is not available")
        _authorized(conn, connection_id, row["project_id"])
        return _task_public(row)


def list_tasks(connection_id, project_id=None, limit=25, offset=0):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
        raise AgentControlError("Invalid task pagination")
    with _read() as conn:
        if not _has_registry(conn):
            raise AgentForbidden("Connection access is not available")
        ids = _scope(conn, connection_id)
        if project_id is not None:
            _authorized(conn, connection_id, project_id)
            ids = [project_id]
        else:
            ids = [row[0] for row in conn.execute("SELECT id FROM agent_projects WHERE id IN (" + ",".join("?" for _ in ids) + ") AND json_extract(config_json,'$.enabled')=1", ids)] if ids else []
        if not ids:
            return []
        rows = conn.execute("SELECT w.*,r.project_id,r.issue_url FROM workflows w JOIN agent_task_requests r ON r.workflow_id=w.id WHERE r.project_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY w.created_at DESC,w.id DESC LIMIT ? OFFSET ?", [*ids, limit, offset]).fetchall()
        return [_task_public(row) for row in rows]
