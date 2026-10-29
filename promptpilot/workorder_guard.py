"""Website authorization bound to immutable workflow and dispatch evidence."""

import contextlib
import copy
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request

from . import db
from .models import WorkorderGuardConfig


class WorkorderAuthorizationError(RuntimeError):
    """Execution was refused by the website authorization boundary."""


_TASK_FIELDS = ("provider", "working_dir", "model", "effort", "rights",
                "skip_permissions", "machine", "herdr_target", "worktree",
                "detached", "max_retries", "task_timeout", "recurrence", "series_id")
_PROVIDER_FIELDS = ("cmd", "args", "executor", "kind", "env", "prompt_stdin",
                    "rights", "effort", "guard", "session_target")


def provider_digest(provider):
    policy = {key: provider[key] for key in _PROVIDER_FIELDS if key in provider}
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def workflow_origin(connection, workflow):
    """Current edits cannot remove a guard that existed at workflow birth."""
    event = connection.execute(
        "SELECT payload_json FROM workflow_events WHERE idempotency_key=?",
        ("workflow.created:" + workflow["id"],),
    ).fetchone()
    current = json.loads(workflow["config_json"])
    original = json.loads(event["payload_json"]) if event else {}
    original_guard = (original.get("config") or {}).get("workorder_guard")
    current_guard = current.get("workorder_guard")
    if original_guard is None and current_guard is None:
        return None
    if original_guard is None or original_guard != current_guard:
        raise WorkorderAuthorizationError("Workorder guard origin changed or is missing")
    try:
        guard = WorkorderGuardConfig.model_validate(original_guard).model_dump()
    except ValueError as exc:
        raise WorkorderAuthorizationError("Invalid workorder guard origin") from exc
    root = original.get("repository_path")
    if (not isinstance(root, str) or not os.path.isabs(root)
            or root != os.path.realpath(root)
            or workflow["repository_path"] != root):
        raise WorkorderAuthorizationError("Workorder repository origin changed")
    return {"guard": guard, "repository_path": root}


def prepare_dispatch(connection, workflow, dispatch):
    """Bind effective local policy in the existing append-only run event."""
    origin = workflow_origin(connection, workflow)
    if origin is None:
        return dispatch, None
    from .config import DEFAULT_CLI, load_providers, rights_args, RightsUnsupported
    provider = dispatch.provider or DEFAULT_CLI
    selected = load_providers().get(provider, {})
    rights = dispatch.rights or ("read" if dispatch.role.value == "reviewer" else "write")
    if (dispatch.execution_mode.value != "automatic" or selected.get("executor")
            or dispatch.worktree or dispatch.skip_permissions or dispatch.machine
            or dispatch.herdr_target or dispatch.max_retries != 0
            or not dispatch.task_timeout or dispatch.task_timeout > 3600
            or rights == "full" or dispatch.role.value == "reviewer" and rights == "write"):
        raise WorkorderAuthorizationError("Workorders require bounded local execution with narrow rights")
    root = origin["repository_path"]
    if dispatch.working_dir and os.path.realpath(dispatch.working_dir) != root:
        raise WorkorderAuthorizationError("Workorder working directory is pinned")
    try:
        rights_args(selected, rights)
        digest = provider_digest(selected)
    except (RightsUnsupported, TypeError, ValueError) as exc:
        raise WorkorderAuthorizationError("Workorder provider cannot prove the requested narrow rights") from exc
    dispatch = dispatch.model_copy(update={"provider": provider, "rights": rights, "working_dir": root})
    task = {key: getattr(dispatch, key, False if key == "detached" else None) for key in _TASK_FIELDS}
    return dispatch, {"version": 1, "role": dispatch.role.value, "task": task, "provider_sha256": digest}


def _context(task_id):
    try:
        with db._connect() as connection:
            rows = connection.execute(
                "SELECT w.*, r.id AS guarded_run_id, r.role AS guarded_role, "
                "r.input_sha256 AS guarded_input_sha FROM workflows w "
                "JOIN workflow_runs r ON r.workflow_id=w.id WHERE r.task_id=?", (task_id,),
            ).fetchall()
            origins = [(row, workflow_origin(connection, row)) for row in rows]
            guarded = [(row, origin) for row, origin in origins if origin is not None]
            if not guarded:
                task = connection.execute("SELECT series_id FROM tasks WHERE id=?", (task_id,)).fetchone()
                if task and task["series_id"] is not None:
                    _, forbidden_series = db.workorder_origins(connection, series_id=task["series_id"])
                    if task["series_id"] in forbidden_series:
                        raise WorkorderAuthorizationError("Recurring workorder descendants are unsupported")
                return None
            if len(rows) != 1:
                raise WorkorderAuthorizationError("Ambiguous workorder task origin")
            row, origin = guarded[0]
            event = connection.execute(
                "SELECT payload_json FROM workflow_events WHERE idempotency_key=?",
                ("run.created:" + row["guarded_run_id"],),
            ).fetchone()
            payload = json.loads(event["payload_json"]) if event else {}
            binding = payload.get("workorder_binding")
            if (payload.get("task_id") != task_id
                    or payload.get("role") != row["guarded_role"]
                    or payload.get("input_sha256") != row["guarded_input_sha"]
                    or not isinstance(binding, dict)
                    or set(binding) != {"version", "role", "task", "provider_sha256"}
                    or type(binding.get("version")) is not int or binding["version"] != 1
                    or binding.get("role") != row["guarded_role"]
                    or not isinstance(binding.get("task"), dict)
                    or set(binding["task"]) != set(_TASK_FIELDS)
                    or not isinstance(binding.get("provider_sha256"), str)
                    or re.fullmatch("[0-9a-f]{64}", binding["provider_sha256"]) is None):
                raise WorkorderAuthorizationError("Missing or invalid immutable workorder dispatch binding")
            return {**origin, "binding": binding, "workflow_status": row["status"]}
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise WorkorderAuthorizationError("Workorder origin is unavailable") from exc


def configuration(task_id):
    context = _context(task_id)
    return context["guard"] if context else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def check_task(task_id, reserve=False, timeout=8):
    config = configuration(task_id)
    if config is None:
        return
    token = os.environ.get("WORKORDERS_WORKER_TOKEN", "")
    allowed_site = os.environ.get("WORKORDERS_SITE_URL", "").rstrip("/")
    if (not token or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token)
            or config["site_url"] != allowed_site):
        raise WorkorderAuthorizationError("Workorder guard is not configured")
    operation = "reserve" if reserve else "check"
    url = f"{allowed_site}/internal/workorders/v1/runs/{config['run_id']}/{operation}/"
    task = db.get_task(task_id)
    attempt = task.started_at.isoformat() if task and task.started_at else "pending"
    try:
        request = urllib.request.Request(url, method="POST", data=json.dumps({
            "stage_key": f"task:{task_id}:{attempt}"}).encode(), headers={
                "Authorization": f"Bearer {token}", "X-Worker-Key": config["worker_key"],
                "X-Lease-Token": config["lease_token"], "Content-Type": "application/json"})
        with _open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise WorkorderAuthorizationError("Workorder authorization redirect refused")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise WorkorderAuthorizationError("Workorder authorization response too large")
            result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise WorkorderAuthorizationError("Workorder authorization unavailable") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise WorkorderAuthorizationError("Workorder authorization unavailable") from exc
    if not isinstance(result, dict) or result.get("approved" if reserve else "continue") is not True:
        raise WorkorderAuthorizationError("Workorder authorization refused")


class ExecutionGuard:
    """Owning-thread supervision, with a latched refusal for this execution."""

    def __init__(self, task_id):
        self.task_id = task_id
        self.started_at = None
        self.failed = None
        self.next_check = 0.0

    def provider_snapshot(self, task, provider_cfg):
        self.validate_task(task, provider_cfg)
        snapshot = copy.deepcopy(provider_cfg)
        context = _context(self.task_id)
        if context is None or provider_digest(snapshot) != context["binding"]["provider_sha256"]:
            self.failed = WorkorderAuthorizationError("Workorder provider snapshot changed")
            raise self.failed
        return snapshot

    def validate_task(self, task, provider_cfg=None, cmd=None):
        if self.failed is not None:
            raise self.failed
        try:
            context = _context(self.task_id)
            if context is None:
                raise WorkorderAuthorizationError("Workorder origin disappeared")
            fresh = db.get_task(self.task_id)
            if (fresh is None or task is None or fresh.status.value != "running" or fresh.started_at is None
                    or task.id != self.task_id or task.started_at != fresh.started_at
                    or self.started_at is not None and self.started_at != fresh.started_at):
                raise WorkorderAuthorizationError("Workorder execution attempt changed")
            self.started_at = fresh.started_at
            bound = context["binding"]["task"]
            if (context["workflow_status"] != ("executing" if context["binding"]["role"] == "executor" else "reviewing")
                    or any(getattr(fresh, key, None) != bound[key] or getattr(task, key, None) != bound[key]
                           for key in _TASK_FIELDS)):
                raise WorkorderAuthorizationError("Workorder execution policy changed")
            from .config import load_providers, rights_args, RightsUnsupported
            current = load_providers().get(fresh.provider, {})
            if (current.get("executor") or fresh.rights not in {"none", "read", "write"}
                    or fresh.skip_permissions or fresh.machine or fresh.detached or fresh.worktree
                    or fresh.herdr_target or fresh.max_retries != 0 or not fresh.task_timeout
                    or fresh.task_timeout > 3600 or os.path.realpath(fresh.working_dir) != context["repository_path"]
                    or provider_digest(current) != context["binding"]["provider_sha256"]
                    or provider_cfg is not None and provider_digest(provider_cfg) != provider_digest(current)):
                raise WorkorderAuthorizationError("Workorder provider policy changed")
            try:
                rights_args(current, fresh.rights)
            except RightsUnsupported as exc:
                raise WorkorderAuthorizationError("Workorder narrow rights are unavailable") from exc
            if cmd is not None and any(arg.split("=", 1)[0] in {
                    "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
                    "--allow-dangerously-skip-permissions", "--dangerously-bypass-hook-trust", "--yolo"}
                                      for arg in cmd):
                raise WorkorderAuthorizationError("Workorder bypass argument refused")
        except WorkorderAuthorizationError as exc:
            self.failed = exc
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            self.failed = WorkorderAuthorizationError("Workorder execution policy is unavailable")
            raise self.failed from exc

    def __call__(self, task, *, force=False, reserve=False, provider_cfg=None, cmd=None, timeout=8):
        self.validate_task(task, provider_cfg, cmd)
        if not force and time.monotonic() < self.next_check:
            return
        try:
            check_task(self.task_id, reserve=reserve, timeout=timeout)
            self.validate_task(task, provider_cfg, cmd)
        except WorkorderAuthorizationError as exc:
            self.failed = exc
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            self.failed = WorkorderAuthorizationError("Workorder authorization is unavailable")
            raise self.failed from exc
        self.next_check = time.monotonic() + 10


@contextlib.contextmanager
def watch_task(task_id, stop_tree):
    """Reserve before execution; the owning worker polls the yielded guard.

    There is no daemon that can lose cleanup errors. The owning worker must
    confirm process stop before a terminal write, or retain its quarantine.
    """
    if configuration(task_id) is None:
        yield None
        return
    guard = ExecutionGuard(task_id)
    guard(db.get_task(task_id), force=True, reserve=True)
    yield guard
