"""Local serial gate admission and durable uncertainty, never a PID/TTL lease.

The aux file belongs to the trusted data directory and is never unlinked. Its
writer transaction excludes live coordinators without locking the primary DB.
The primary journal survives that lock's release, including process crashes.
"""

from contextlib import contextmanager
import hashlib
import json
import os
import secrets
import sqlite3

from . import db
from .models import WorkflowGateDecision, WorkflowStatus


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lock_path(workflow_id):
    # Never interpolate an API identifier or a repository path into a filename.
    return db.DB_DIR / "gate-locks" / (_sha(workflow_id) + ".sqlite3")


@contextmanager
def execution_lock(workflow_id):
    path = lock_path(workflow_id)
    conn = None
    try:
        try:
            with db._connect() as primary:
                has_history = primary.execute(
                    "SELECT 1 FROM workflow_gate_executions WHERE workflow_id=? LIMIT 1",
                    (workflow_id,),
                ).fetchone() is not None
            # Bootstrap once. Losing an established lock is an operational
            # error, not permission to create another inode beside an owner.
            if has_history and not path.is_file():
                raise db.WorkflowConflictError("established gate lock file is missing")
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
            acquired = True
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("PRAGMA schema_version").fetchone()
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                # Python 3.10 lacks sqlite_errorcode; recognize only SQLite's
                # specific contention messages, never arbitrary open/I/O errors.
                busy = ((code is not None and (code & 0xff) in (5, 6))
                        or (code is None and str(exc).lower() in {
                            "database is locked", "database table is locked"}))
                if not busy:
                    raise
                acquired = False
        except (OSError, sqlite3.Error) as exc:
            raise db.WorkflowConflictError(f"gate lock unavailable: {exc}") from exc
        yield acquired
    finally:
        # Also close when acquisition itself is interrupted before the yield.
        if conn is not None:
            conn.close()


def snapshot(conn, workflow):
    """Freeze the effective vector, shell argv and completed run provenance."""
    from . import workflows

    config = workflows._config_for(workflow)
    gate = config.gate.model_dump(mode="json")
    stage = conn.execute("SELECT spec_json FROM workflow_stages WHERE id=?",
                         (workflow.current_stage_id,)).fetchone()
    commands = list(dict.fromkeys([
        *gate["commands"],
        *((db._json_load(stage["spec_json"]).get("acceptance_gates") or []) if stage else []),
    ]))
    round_row = conn.execute(
        "SELECT id FROM workflow_rounds WHERE workflow_id=? AND round_no=?",
        (workflow.id, workflow.current_round),
    ).fetchone()
    executor = conn.execute(
        "SELECT id, input_sha256, output_sha256, input_json, output_json "
        "FROM workflow_runs WHERE round_id=? AND role='executor' AND status='completed' "
        "ORDER BY attempt_no DESC LIMIT 1", (round_row["id"] if round_row else None,),
    ).fetchone()
    return {
        "workflow_id": workflow.id,
        "round_id": round_row["id"] if round_row else None,
        "stage_id": workflow.current_stage_id,
        "executor": dict(executor) if executor else None,
        "repository_path": workflow.repository_path,
        "cwd": workflow.repository_path,
        "gate": gate,
        "commands": commands,
        "argv": [(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
                  if os.name == "nt" else ["/bin/sh", "-lc", command]) for command in commands],
    }


def _unknown(execution_id, reason):
    from . import workflows

    with db._connect(immediate=True) as conn:
        execution = conn.execute(
            "SELECT * FROM workflow_gate_executions WHERE execution_id=?", (execution_id,),
        ).fetchone()
        workflow = workflows._workflow_row(conn, execution["workflow_id"])
        if execution["state"] in {"STARTED", "SETTLED"}:
            nonce = secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE workflow_gate_executions SET state='UNKNOWN', recovery_nonce=? "
                "WHERE execution_id=?", (nonce, execution_id),
            )
            payload = {
                "execution_id": execution_id, "recovery_nonce": nonce, "reason": reason,
                "cleanup_confirmed": False,
                "recovery_required": "explicit engineering recovery; ordinary resume is blocked",
            }
            if WorkflowStatus(workflow["status"]) not in workflows.TERMINAL_STATES | {
                    WorkflowStatus.AWAITING_HUMAN}:
                workflow = workflows._transition(
                    conn, workflow, WorkflowStatus.AWAITING_HUMAN,
                    "gate.execution_unknown", payload, round_id=execution["round_id"],
                    idempotency_key=f"gate.execution_unknown:{execution_id}",
                )
            else:
                workflow = workflows._touch(
                    conn, workflow, "gate.execution_unknown", payload,
                    round_id=execution["round_id"],
                    idempotency_key=f"gate.execution_unknown:{execution_id}",
                )
        return db._row_to_workflow(workflow)


def _claim_gate(conn, workflow_id, observed_version):
    from . import workflows

    db.require_gate_idle(conn, workflow_id)
    row = workflows._workflow_row(conn, workflow_id)
    workflows._require_version(conn, row, observed_version)
    workflow = db._row_to_workflow(row)
    config = workflows._config_for(workflow)
    if (workflow.status is not WorkflowStatus.GATING
            or not config.automation.enabled or not config.automation.auto_gate):
        raise db.WorkflowConflictError("workflow is not ready for an automatic gate")
    scope = snapshot(conn, workflow)
    scope_json = _canonical(scope)
    execution_id, token = db._new_id("gate"), secrets.token_urlsafe(32)
    row = workflows._touch(conn, row, "gate.execution_started", {
        "execution_id": execution_id, "scope_sha256": _sha(scope_json),
    }, round_id=scope["round_id"])
    conn.execute(
        "INSERT INTO workflow_gate_executions "
        "(execution_id,workflow_id,token,observed_version,admitted_version,round_id,stage_id,"
        "scope_sha256,scope_json,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,'STARTED',?)",
        (execution_id, workflow_id, token, observed_version, row["state_version"],
         scope["round_id"], scope["stage_id"], _sha(scope_json), scope_json, db._now()),
    )
    return dict(conn.execute("SELECT * FROM workflow_gate_executions WHERE execution_id=?",
                             (execution_id,)).fetchone()), db._row_to_workflow(row), scope


def _settle_gate(execution, decision):
    decision_json = _canonical(decision.model_dump(mode="json"))
    with db._connect(immediate=True) as conn:
        updated = conn.execute(
            "UPDATE workflow_gate_executions SET state='SETTLED', decision_json=?, "
            "decision_sha256=?, cleanup_json=?, settled_at=? "
            "WHERE execution_id=? AND token=? AND state='STARTED'",
            (decision_json, _sha(decision_json), _canonical({
                "cleanup_confirmed": True,
                "basis": "all run_owned calls returned or completed timeout cleanup; "
                         "disabled/empty gates and pre-factory validation invoke no process factory",
            }), db._now(), execution["execution_id"], execution["token"]),
        )
        if updated.rowcount != 1:
            raise db.WorkflowConflictError("gate execution settlement ownership changed")


def _project_settled_gate(execution_id):
    from . import workflows

    mismatch = False
    with db._connect(immediate=True) as conn:
        execution = conn.execute("SELECT * FROM workflow_gate_executions WHERE execution_id=?",
                                 (execution_id,)).fetchone()
        workflow = db.get_workflow(execution["workflow_id"], conn=conn)
        if execution["state"] != "SETTLED":
            return workflow
        if (_sha(execution["scope_json"]) != execution["scope_sha256"]
                or _sha(execution["decision_json"]) != execution["decision_sha256"]
                or json.loads(execution["cleanup_json"]).get("cleanup_confirmed") is not True):
            mismatch = True
        elif (workflow.status is not WorkflowStatus.GATING
              or workflow.state_version != execution["admitted_version"]
              or _canonical(snapshot(conn, workflow)) != execution["scope_json"]):
            mismatch = True
        else:
            decision = WorkflowGateDecision.model_validate_json(execution["decision_json"])
            if decision.expected_version != execution["admitted_version"]:
                mismatch = True
            else:
                workflow = workflows._record_gate(
                    conn, workflow.id, decision,
                    execution_id=execution_id, token=execution["token"],
                )
                # The decision, round projection, event and APPLIED share one commit.
                updated = conn.execute(
                    "UPDATE workflow_gate_executions SET state='APPLIED', projected_version=? "
                    "WHERE execution_id=? AND token=? AND state='SETTLED'",
                    (workflow.state_version, execution_id, execution["token"]),
                )
                if updated.rowcount != 1:
                    raise db.WorkflowConflictError("gate projection ownership changed")
    if mismatch:
        return _unknown(execution_id, "settled decision cannot apply to the current version/scope")
    return workflow


def _reconcile_locked(workflow_id):
    with db._connect() as conn:
        active = db.active_gate_execution(conn, workflow_id)
    if active is None:
        return None
    if active["state"] == "SETTLED":
        return _project_settled_gate(active["execution_id"])
    return _unknown(active["execution_id"], "owner ended without durable settlement")


def reconcile_unresolved(workflow_id):
    with db._connect() as conn:
        active = db.active_gate_execution(conn, workflow_id)
    if active is None:
        return None
    with execution_lock(workflow_id) as acquired:
        if not acquired:
            return db.get_workflow(workflow_id)
        return _reconcile_locked(workflow_id)


def execute_automatic_gate(workflow_id, observed_version):
    from . import workflows

    with execution_lock(workflow_id) as acquired:
        if not acquired:
            return db.get_workflow(workflow_id)
        existing = _reconcile_locked(workflow_id)
        if existing is not None:
            return existing
        # Commit must return successfully before any process factory is called.
        with db._connect(immediate=True) as conn:
            current = db.get_workflow(workflow_id, conn=conn)
            if current is None:
                raise db.WorkflowNotFoundError(workflow_id)
            config = workflows._config_for(current)
            if (current.state_version != observed_version or current.status is not WorkflowStatus.GATING
                    or not config.automation.enabled or not config.automation.auto_gate):
                return current
            execution, workflow, scope = _claim_gate(conn, workflow_id, observed_version)
        try:
            decision = workflows._run_gate_commands(workflow, snapshot=scope)
        except BaseException as exc:
            result = _unknown(execution["execution_id"],
                              f"owned gate outcome/cleanup uncertain: {type(exc).__name__}: {exc}")
            if not isinstance(exc, Exception):
                raise
            return result
        # A failed/ambiguous settlement leaves STARTED, never an in-memory PASS.
        _settle_gate(execution, decision)
        return _project_settled_gate(execution["execution_id"])


def recover_unknown_gate(workflow_id, request):
    from . import workflows

    if not request.acknowledge_gate_effects_reviewed or not request.reason.strip():
        raise db.WorkflowConflictError("recovery requires explicit acknowledgement and reason")
    with execution_lock(workflow_id) as acquired:
        if not acquired:
            raise db.WorkflowConflictError("gate execution is busy")
        with db._connect(immediate=True) as conn:
            workflow = workflows._workflow_row(conn, workflow_id)
            workflows._require_version(conn, workflow, request.expected_version,
                                       allow_gate_execution=True)
            execution = db.active_gate_execution(conn, workflow_id)
            if (execution is None or execution["state"] != "UNKNOWN"
                    or execution["execution_id"] != request.execution_id
                    or execution["recovery_nonce"] != request.recovery_nonce):
                raise db.WorkflowConflictError("unknown execution identity/nonce changed or already consumed")
            if WorkflowStatus(workflow["status"]) in workflows.TERMINAL_STATES:
                raise db.WorkflowConflictError("terminal workflow cannot restart a gate")
            current_round = workflows._current_round_row(conn, workflow)
            if current_round["id"] != execution["round_id"]:
                raise db.WorkflowConflictError("gate recovery round changed")
            resolution = {
                "execution_id": request.execution_id, "recovery_nonce": request.recovery_nonce,
                "expected_version": request.expected_version, "reason": request.reason.strip(),
                "acknowledge_gate_effects_reviewed": True,
                "operator_acknowledgement": "operator states old processes have stopped and "
                                            "effects/checkout were reviewed; server did not verify cleanup",
                "cleanup_confirmed": False, "resolved_at": db._now(),
            }
            updated = conn.execute(
                "UPDATE workflow_gate_executions SET state='RESOLVED', resolution_json=? "
                "WHERE execution_id=? AND state='UNKNOWN' AND recovery_nonce=?",
                (_canonical(resolution), request.execution_id, request.recovery_nonce),
            )
            if updated.rowcount != 1:
                raise db.WorkflowConflictError("gate recovery nonce was already consumed")
            conn.execute("UPDATE workflow_rounds SET status='gating' WHERE id=?",
                         (current_round["id"],))
            workflow = workflows._transition(
                conn, workflow, WorkflowStatus.GATING, "gate.execution_resolved", resolution,
                round_id=current_round["id"],
            )
            return db._row_to_workflow(workflow)
