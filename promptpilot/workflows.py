"""W1 workflow state machine and manual executor/reviewer pilot.

The module deliberately owns lifecycle decisions while :mod:`promptpilot.db`
remains the persistence layer. Every state change and materialized projection
update is committed in the same SQLite transaction as its append-only event.
"""

import hashlib
import json
import sqlite3
from typing import Optional

from . import db
from .models import (
    FindingStatus,
    GateVerdict,
    ReviewVerdict,
    TaskCreate,
    WorkflowDispatchResult,
    WorkflowEventCreate,
    WorkflowFindingInDB,
    WorkflowHistoryImport,
    WorkflowHumanInput,
    WorkflowInDB,
    WorkflowReviewDecision,
    WorkflowRole,
    WorkflowRoundInDB,
    WorkflowRoundStatus,
    WorkflowRunInDB,
    WorkflowStartRequest,
    WorkflowStatus,
    WorkflowTaskDispatch,
    WorkflowGateDecision,
)


ALLOWED_TRANSITIONS = {
    WorkflowStatus.DRAFT: {WorkflowStatus.QUEUED, WorkflowStatus.CANCELLED},
    WorkflowStatus.QUEUED: {
        WorkflowStatus.EXECUTING,
        WorkflowStatus.AWAITING_HUMAN,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.EXECUTING: {
        WorkflowStatus.GATING,
        WorkflowStatus.REVISION_REQUIRED,
        WorkflowStatus.AWAITING_HUMAN,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.GATING: {
        WorkflowStatus.REVIEWING,
        WorkflowStatus.REVISION_REQUIRED,
        WorkflowStatus.AWAITING_HUMAN,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.REVIEWING: {
        WorkflowStatus.AWAITING_HUMAN,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.REVISION_REQUIRED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.REVISION_REQUIRED: {
        WorkflowStatus.QUEUED,
        WorkflowStatus.AWAITING_HUMAN,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.AWAITING_HUMAN: {
        WorkflowStatus.QUEUED,
        WorkflowStatus.REVIEWING,
        WorkflowStatus.REVISION_REQUIRED,
        WorkflowStatus.COMPLETED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.COMPLETED: set(),
    WorkflowStatus.FAILED: set(),
    WorkflowStatus.CANCELLED: set(),
}


TERMINAL_STATES = {
    WorkflowStatus.COMPLETED,
    WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED,
}


WORKFLOW_VERDICT_INSTRUCTION = """

<promptpilot-workflow-contract version="w1-verdict-v1">
Не завершай ответ уточняющим вопросом, если можешь продолжить самостоятельно.
Последней строкой ответа обязательно напиши ровно один вариант:
ИТОГ: ГОТОВО — задача выполнена, изменения и проверки перечислены выше
ИТОГ: УЖЕ СДЕЛАНО — изменений не потребовалось, причина доказана выше
ИТОГ: НУЖЕН ЧЕЛОВЕК — требуется конкретное решение или доступ
ИТОГ: НЕ СМОГ — задача не выполнена, блокер доказан выше
</promptpilot-workflow-contract>
"""

SUCCESS_VERDICTS = {"ГОТОВО", "УЖЕ СДЕЛАНО"}


def _workflow_row(conn: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
    ).fetchone()
    if not row:
        raise db.WorkflowNotFoundError(workflow_id)
    return row


def _current_round_row(conn: sqlite3.Connection,
                       workflow: sqlite3.Row) -> sqlite3.Row:
    row = conn.execute(
        """SELECT * FROM workflow_rounds
           WHERE workflow_id = ? AND round_no = ?""",
        (workflow["id"], workflow["current_round"]),
    ).fetchone()
    if not row:
        raise db.WorkflowConflictError(
            f"workflow {workflow['id']} has no current round"
        )
    return row


def _require_version(workflow: sqlite3.Row, expected_version: int):
    if workflow["state_version"] != expected_version:
        raise db.WorkflowConflictError(
            f"workflow version is {workflow['state_version']}, "
            f"expected {expected_version}"
        )


def _event_key(prefix: str, workflow_id: str, version: int) -> str:
    return f"{prefix}:{workflow_id}:v{version}"


def _transition(conn: sqlite3.Connection, workflow: sqlite3.Row,
                target: WorkflowStatus, event_type: str, payload: dict,
                round_id: str = None) -> sqlite3.Row:
    current = WorkflowStatus(workflow["status"])
    if target not in ALLOWED_TRANSITIONS[current]:
        raise db.WorkflowConflictError(
            f"invalid workflow transition: {current.value} -> {target.value}"
        )
    version = workflow["state_version"] + 1
    now = db._now()
    completed_at = now if target in TERMINAL_STATES else None
    cur = conn.execute(
        """UPDATE workflows
           SET status = ?, state_version = ?, updated_at = ?, completed_at = ?
           WHERE id = ? AND state_version = ?""",
        (target.value, version, now, completed_at, workflow["id"],
         workflow["state_version"]),
    )
    if cur.rowcount != 1:
        raise db.WorkflowConflictError("workflow changed concurrently")
    db._append_workflow_event(conn, WorkflowEventCreate(
        workflow_id=workflow["id"],
        round_id=round_id,
        event_type=event_type,
        idempotency_key=_event_key(event_type, workflow["id"], version),
        payload={
            "from": current.value,
            "to": target.value,
            "version": version,
            **payload,
        },
    ))
    return _workflow_row(conn, workflow["id"])


def _touch(conn: sqlite3.Connection, workflow: sqlite3.Row,
           event_type: str, payload: dict, round_id: str = None,
           run_id: str = None) -> sqlite3.Row:
    """Record an aggregate-changing action without changing lifecycle state."""
    version = workflow["state_version"] + 1
    cur = conn.execute(
        """UPDATE workflows SET state_version = ?, updated_at = ?
           WHERE id = ? AND state_version = ?""",
        (version, db._now(), workflow["id"], workflow["state_version"]),
    )
    if cur.rowcount != 1:
        raise db.WorkflowConflictError("workflow changed concurrently")
    db._append_workflow_event(conn, WorkflowEventCreate(
        workflow_id=workflow["id"],
        round_id=round_id,
        run_id=run_id,
        event_type=event_type,
        idempotency_key=_event_key(event_type, workflow["id"], version),
        payload={"version": version, **payload},
    ))
    return _workflow_row(conn, workflow["id"])


def _insert_round(conn: sqlite3.Connection, workflow_id: str, round_no: int,
                  base_sha: str = None, status: WorkflowRoundStatus =
                  WorkflowRoundStatus.PENDING, candidate_sha: str = None,
                  audit_sha: str = None, summary: dict = None,
                  historical: bool = False) -> sqlite3.Row:
    round_id = db._new_id("round")
    now = db._now()
    completed_at = now if status in {
        WorkflowRoundStatus.COMPLETED,
        WorkflowRoundStatus.FAILED,
        WorkflowRoundStatus.CANCELLED,
        WorkflowRoundStatus.REVISION_REQUIRED,
    } else None
    try:
        conn.execute(
            """INSERT INTO workflow_rounds
               (id, workflow_id, round_no, status, base_sha, candidate_sha,
                audit_sha, started_at, completed_at, summary_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (round_id, workflow_id, round_no, status.value, base_sha,
             candidate_sha, audit_sha, now, completed_at,
             db._json_dump(summary) if summary is not None else None),
        )
    except sqlite3.IntegrityError as exc:
        raise db.WorkflowConflictError(
            f"round {round_no} already exists for {workflow_id}"
        ) from exc
    conn.execute(
        """UPDATE workflows SET current_round = MAX(current_round, ?),
           updated_at = ? WHERE id = ?""",
        (round_no, now, workflow_id),
    )
    db._append_workflow_event(conn, WorkflowEventCreate(
        workflow_id=workflow_id,
        round_id=round_id,
        event_type="round.imported" if historical else "round.created",
        idempotency_key=(
            f"round.imported:{workflow_id}:{round_no}"
            if historical else f"round.created:{round_id}"
        ),
        payload={
            "round_no": round_no,
            "base_sha": base_sha,
            "candidate_sha": candidate_sha,
            "audit_sha": audit_sha,
            "status": status.value,
            "historical": historical,
        },
    ))
    return conn.execute(
        "SELECT * FROM workflow_rounds WHERE id = ?", (round_id,)
    ).fetchone()


def _max_automated_rounds(workflow: sqlite3.Row) -> int:
    config = db._json_load(workflow["config_json"])
    raw = (config.get("limits") or {}).get("max_rounds", 6)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 6
    return max(1, value)


def _first_automated_round(conn: sqlite3.Connection,
                           workflow_id: str) -> Optional[int]:
    row = conn.execute(
        """SELECT payload_json FROM workflow_events
           WHERE workflow_id = ? AND event_type = 'workflow.started'
           ORDER BY seq LIMIT 1""",
        (workflow_id,),
    ).fetchone()
    if not row:
        return None
    return db._json_load(row["payload_json"]).get("first_automated_round")


def _check_round_budget(conn: sqlite3.Connection, workflow: sqlite3.Row,
                        next_round: int) -> bool:
    first = _first_automated_round(conn, workflow["id"])
    if first is None:
        return True
    used_after_create = next_round - first + 1
    return used_after_create <= _max_automated_rounds(workflow)


def start_workflow(workflow_id: str,
                   request: WorkflowStartRequest) -> WorkflowInDB:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, request.expected_version)
        if WorkflowStatus(workflow["status"]) is not WorkflowStatus.DRAFT:
            raise db.WorkflowConflictError("only a draft workflow can be started")
        round_no = workflow["current_round"] + 1
        round_row = _insert_round(
            conn, workflow_id, round_no, base_sha=request.base_sha
        )
        workflow = _workflow_row(conn, workflow_id)
        workflow = _transition(
            conn,
            workflow,
            WorkflowStatus.QUEUED,
            "workflow.started",
            {
                "first_automated_round": round_no,
                "base_sha": request.base_sha,
            },
            round_id=round_row["id"],
        )
        return db._row_to_workflow(workflow)


def _input_sha(dispatch: WorkflowTaskDispatch, working_dir: str) -> str:
    payload = dispatch.model_dump(mode="json")
    payload["working_dir"] = working_dir
    payload["output_contract"] = "w1-verdict-v1"
    return hashlib.sha256(db._json_dump(payload).encode("utf-8")).hexdigest()


def dispatch_task(workflow_id: str,
                  dispatch: WorkflowTaskDispatch) -> WorkflowDispatchResult:
    if dispatch.role not in {WorkflowRole.EXECUTOR, WorkflowRole.REVIEWER}:
        raise db.WorkflowConflictError("W1 can dispatch only executor or reviewer")
    if dispatch.role is WorkflowRole.REVIEWER and dispatch.skip_permissions:
        raise db.WorkflowConflictError(
            "reviewer cannot use skip_permissions in the W1 manual pilot"
        )

    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, dispatch.expected_version)
        state = WorkflowStatus(workflow["status"])
        required = (
            WorkflowStatus.QUEUED
            if dispatch.role is WorkflowRole.EXECUTOR
            else WorkflowStatus.REVIEWING
        )
        if state is not required:
            raise db.WorkflowConflictError(
                f"cannot dispatch {dispatch.role.value} while workflow is {state.value}"
            )
        round_row = _current_round_row(conn, workflow)
        working_dir = dispatch.working_dir or workflow["repository_path"]
        task = db._insert_task(conn, TaskCreate(
            prompt=dispatch.prompt.rstrip() + WORKFLOW_VERDICT_INSTRUCTION,
            working_dir=working_dir,
            provider=dispatch.provider,
            priority=dispatch.priority,
            max_retries=dispatch.max_retries,
            skip_permissions=dispatch.skip_permissions,
            model=dispatch.model,
            effort=dispatch.effort,
            task_timeout=dispatch.task_timeout,
            worktree=dispatch.worktree,
            keep_pane=dispatch.keep_pane,
            herdr_target=dispatch.herdr_target,
            machine=dispatch.machine,
        ))
        attempt_no = conn.execute(
            """SELECT COALESCE(MAX(attempt_no), 0) + 1
               FROM workflow_runs WHERE round_id = ? AND role = ?""",
            (round_row["id"], dispatch.role.value),
        ).fetchone()[0]
        run_id = db._new_id("run")
        input_sha = _input_sha(dispatch, working_dir)
        conn.execute(
            """INSERT INTO workflow_runs
               (id, workflow_id, round_id, role, attempt_no, task_id, status,
                input_sha256)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (run_id, workflow_id, round_row["id"], dispatch.role.value,
             attempt_no, task.id, input_sha),
        )
        db._append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=workflow_id,
            round_id=round_row["id"],
            run_id=run_id,
            event_type="run.created",
            idempotency_key=f"run.created:{run_id}",
            payload={
                "role": dispatch.role.value,
                "attempt_no": attempt_no,
                "task_id": task.id,
                "input_sha256": input_sha,
                "provider": dispatch.provider,
                "herdr_target": dispatch.herdr_target,
                "machine": dispatch.machine,
            },
        ))
        if dispatch.role is WorkflowRole.EXECUTOR:
            conn.execute(
                "UPDATE workflow_rounds SET status = 'executing' WHERE id = ?",
                (round_row["id"],),
            )
            workflow = _transition(
                conn, workflow, WorkflowStatus.EXECUTING,
                "executor.dispatched",
                {"task_id": task.id, "run_id": run_id},
                round_id=round_row["id"],
            )
        else:
            workflow = _touch(
                conn, workflow, "reviewer.dispatched",
                {"task_id": task.id, "run_id": run_id},
                round_id=round_row["id"], run_id=run_id,
            )
        run_row = conn.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        round_row = conn.execute(
            "SELECT * FROM workflow_rounds WHERE id = ?", (round_row["id"],)
        ).fetchone()
        return WorkflowDispatchResult(
            workflow=db._row_to_workflow(workflow),
            round=db._row_to_workflow_round(round_row),
            run=db._row_to_workflow_run(run_row),
            task=task,
        )


def record_gate(workflow_id: str,
                decision: WorkflowGateDecision) -> WorkflowInDB:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, decision.expected_version)
        if WorkflowStatus(workflow["status"]) is not WorkflowStatus.GATING:
            raise db.WorkflowConflictError("workflow is not waiting for gates")
        round_row = _current_round_row(conn, workflow)
        common = {
            "gate_id": decision.gate_id,
            "summary": decision.summary,
            "evidence": decision.evidence,
            "verdict": decision.verdict.value,
        }
        if decision.verdict is GateVerdict.PASS:
            round_status = WorkflowRoundStatus.REVIEWING
            target = WorkflowStatus.REVIEWING
            event_type = "gate.passed"
        elif decision.verdict is GateVerdict.FAIL:
            round_status = WorkflowRoundStatus.REVISION_REQUIRED
            target = WorkflowStatus.REVISION_REQUIRED
            event_type = "gate.failed"
        else:
            round_status = WorkflowRoundStatus.FAILED
            target = WorkflowStatus.AWAITING_HUMAN
            event_type = "gate.human_required"
        conn.execute(
            "UPDATE workflow_rounds SET status = ? WHERE id = ?",
            (round_status.value, round_row["id"]),
        )
        workflow = _transition(
            conn, workflow, target, event_type, common,
            round_id=round_row["id"],
        )
        return db._row_to_workflow(workflow)


def _upsert_review_finding(conn: sqlite3.Connection, workflow_id: str,
                           round_no: int, finding) -> WorkflowFindingInDB:
    current = conn.execute(
        """SELECT * FROM workflow_findings
           WHERE workflow_id = ? AND fingerprint = ?""",
        (workflow_id, finding.fingerprint),
    ).fetchone()
    payload_json = db._json_dump(finding.payload)
    if current:
        reopened = (
            current["status"] in {
                FindingStatus.RESOLVED.value,
                FindingStatus.ACCEPTED_RISK.value,
            }
            and finding.status in {FindingStatus.OPEN, FindingStatus.REOPENED}
        )
        reopen_count = current["reopen_count"] + int(reopened)
        conn.execute(
            """UPDATE workflow_findings SET severity = ?, category = ?,
               title = ?, status = ?, last_seen_round = ?, reopen_count = ?,
               payload_json = ? WHERE id = ?""",
            (finding.severity.value, finding.category, finding.title,
             finding.status.value, round_no, reopen_count, payload_json,
             current["id"]),
        )
        finding_id = current["id"]
        event_type = "finding.reopened" if reopened else "finding.updated"
    else:
        finding_id = db._new_id("finding")
        conn.execute(
            """INSERT INTO workflow_findings
               (id, workflow_id, fingerprint, severity, category, title,
                status, first_seen_round, last_seen_round, reopen_count,
                payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (finding_id, workflow_id, finding.fingerprint,
             finding.severity.value, finding.category, finding.title,
             finding.status.value, round_no, round_no, payload_json),
        )
        event_type = "finding.created"
    digest_payload = {
        "round_no": round_no,
        "fingerprint": finding.fingerprint,
        "severity": finding.severity.value,
        "status": finding.status.value,
        "payload": finding.payload,
    }
    digest = hashlib.sha256(
        db._json_dump(digest_payload).encode("utf-8")
    ).hexdigest()
    db._append_workflow_event(conn, WorkflowEventCreate(
        workflow_id=workflow_id,
        event_type=event_type,
        idempotency_key=f"review.finding:{workflow_id}:{digest}",
        payload={"finding_id": finding_id, **digest_payload},
    ))
    row = conn.execute(
        "SELECT * FROM workflow_findings WHERE id = ?", (finding_id,)
    ).fetchone()
    return db._row_to_workflow_finding(row)


def record_review(workflow_id: str,
                  decision: WorkflowReviewDecision) -> WorkflowInDB:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, decision.expected_version)
        if WorkflowStatus(workflow["status"]) is not WorkflowStatus.AWAITING_HUMAN:
            raise db.WorkflowConflictError(
                "workflow is not waiting for a manual review verdict"
            )
        round_row = _current_round_row(conn, workflow)
        reviewer = conn.execute(
            """SELECT * FROM workflow_runs WHERE round_id = ?
               AND role = 'reviewer' AND status = 'completed'
               ORDER BY attempt_no DESC LIMIT 1""",
            (round_row["id"],),
        ).fetchone()
        if not reviewer:
            raise db.WorkflowConflictError(
                "current round has no completed reviewer run"
            )
        gate = conn.execute(
            """SELECT 1 FROM workflow_events WHERE workflow_id = ?
               AND round_id = ? AND event_type = 'gate.passed'""",
            (workflow_id, round_row["id"]),
        ).fetchone()
        if not gate:
            raise db.WorkflowConflictError("current round has no passed gate")

        for finding in decision.findings:
            _upsert_review_finding(
                conn, workflow_id, round_row["round_no"], finding
            )

        common = {
            "verdict": decision.verdict.value,
            "summary": decision.summary,
            "finding_count": len(decision.findings),
        }
        if decision.verdict is ReviewVerdict.PASS:
            blockers = conn.execute(
                """SELECT COUNT(*) FROM workflow_findings
                   WHERE workflow_id = ? AND severity IN ('blocker', 'high')
                   AND status IN ('open', 'reopened')""",
                (workflow_id,),
            ).fetchone()[0]
            if blockers:
                raise db.WorkflowConflictError(
                    f"review cannot pass with {blockers} open blocker/high findings"
                )
            conn.execute(
                """UPDATE workflow_rounds SET status = 'completed',
                   completed_at = ?, summary_json = ? WHERE id = ?""",
                (db._now(), db._json_dump(common), round_row["id"]),
            )
            workflow = _transition(
                conn, workflow, WorkflowStatus.COMPLETED, "review.passed",
                common, round_id=round_row["id"],
            )
        elif decision.verdict is ReviewVerdict.REVISION_REQUIRED:
            conn.execute(
                """UPDATE workflow_rounds SET status = 'revision_required',
                   completed_at = ?, summary_json = ? WHERE id = ?""",
                (db._now(), db._json_dump(common), round_row["id"]),
            )
            workflow = _transition(
                conn, workflow, WorkflowStatus.REVISION_REQUIRED,
                "review.revision_required", common,
                round_id=round_row["id"],
            )
        else:
            workflow = _touch(
                conn, workflow, "review.human_required", common,
                round_id=round_row["id"], run_id=reviewer["id"],
            )
        return db._row_to_workflow(workflow)


def human_input(workflow_id: str,
                action: WorkflowHumanInput) -> WorkflowInDB:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, action.expected_version)
        state = WorkflowStatus(workflow["status"])
        if state not in {
            WorkflowStatus.AWAITING_HUMAN,
            WorkflowStatus.REVISION_REQUIRED,
        }:
            raise db.WorkflowConflictError(
                f"workflow is not waiting for human input: {state.value}"
            )
        round_row = _current_round_row(conn, workflow)
        if not action.resume:
            workflow = _touch(
                conn, workflow, "human.input",
                {"text": action.text, "resume": False},
                round_id=round_row["id"],
            )
            return db._row_to_workflow(workflow)

        if state is WorkflowStatus.REVISION_REQUIRED:
            next_round = workflow["current_round"] + 1
            if not _check_round_budget(conn, workflow, next_round):
                workflow = _transition(
                    conn, workflow, WorkflowStatus.AWAITING_HUMAN,
                    "limit.max_rounds",
                    {
                        "text": action.text,
                        "max_rounds": _max_automated_rounds(workflow),
                        "refused_round": next_round,
                    },
                    round_id=round_row["id"],
                )
                return db._row_to_workflow(workflow)
            new_round = _insert_round(
                conn, workflow_id, next_round,
                base_sha=round_row["candidate_sha"],
            )
            workflow = _workflow_row(conn, workflow_id)
            workflow = _transition(
                conn, workflow, WorkflowStatus.QUEUED, "human.resumed",
                {
                    "text": action.text,
                    "new_round": next_round,
                    "previous_round": round_row["round_no"],
                },
                round_id=new_round["id"],
            )
        else:
            conn.execute(
                """UPDATE workflow_rounds SET status = 'pending',
                   completed_at = NULL WHERE id = ?""",
                (round_row["id"],),
            )
            workflow = _transition(
                conn, workflow, WorkflowStatus.QUEUED, "human.resumed",
                {"text": action.text, "same_round": round_row["round_no"]},
                round_id=round_row["id"],
            )
        return db._row_to_workflow(workflow)


def cancel_workflow(workflow_id: str,
                    expected_version: int) -> WorkflowInDB:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, expected_version)
        state = WorkflowStatus(workflow["status"])
        if state in TERMINAL_STATES:
            raise db.WorkflowConflictError(
                f"workflow is already terminal: {state.value}"
            )
        linked = conn.execute(
            """SELECT t.id, t.status, r.id AS run_id FROM workflow_runs r
               JOIN tasks t ON t.id = r.task_id
               WHERE r.workflow_id = ? AND t.status IN
               ('pending', 'rate_limited', 'running')""",
            (workflow_id,),
        ).fetchall()
        for item in linked:
            if item["status"] == "running":
                conn.execute(
                    """INSERT OR REPLACE INTO settings (key, value)
                       VALUES (?, '1')""",
                    (f"cancel_task:{item['id']}",),
                )
            else:
                conn.execute(
                    """UPDATE tasks SET status = 'cancelled', completed_at = ?
                       WHERE id = ?""",
                    (db._now(), item["id"]),
                )
            conn.execute(
                "UPDATE workflow_runs SET status = 'cancelled' WHERE id = ?",
                (item["run_id"],),
            )
        round_row = None
        if workflow["current_round"]:
            round_row = _current_round_row(conn, workflow)
            conn.execute(
                """UPDATE workflow_rounds SET status = 'cancelled',
                   completed_at = ? WHERE id = ?""",
                (db._now(), round_row["id"]),
            )
        workflow = _transition(
            conn, workflow, WorkflowStatus.CANCELLED, "workflow.cancelled",
            {"linked_tasks": [item["id"] for item in linked]},
            round_id=round_row["id"] if round_row else None,
        )
        return db._row_to_workflow(workflow)


def import_history(workflow_id: str,
                   history: WorkflowHistoryImport) -> WorkflowInDB:
    """Import pre-orchestrator evidence while preserving provenance labels."""
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        payload = history.model_dump(mode="json", exclude={"expected_version"})
        key = f"history.imported:{workflow_id}:{history.idempotency_key}"
        existing = conn.execute(
            "SELECT * FROM workflow_events WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing:
            if existing["payload_json"] != db._json_dump(payload):
                raise db.WorkflowConflictError(
                    "history idempotency key was reused with different content"
                )
            return db._row_to_workflow(workflow)
        _require_version(workflow, history.expected_version)
        if WorkflowStatus(workflow["status"]) is not WorkflowStatus.DRAFT:
            raise db.WorkflowConflictError(
                "history can only be imported into a draft workflow"
            )

        seen = set()
        for historical_round in history.rounds:
            if historical_round.status not in {
                WorkflowRoundStatus.COMPLETED,
                WorkflowRoundStatus.REVISION_REQUIRED,
                WorkflowRoundStatus.FAILED,
                WorkflowRoundStatus.CANCELLED,
            }:
                raise db.WorkflowConflictError(
                    "historical rounds must have a terminal status"
                )
            if historical_round.round_no in seen:
                raise db.WorkflowConflictError(
                    f"duplicate historical round {historical_round.round_no}"
                )
            seen.add(historical_round.round_no)
            summary = {
                "historical": True,
                "source": history.source,
                "summary": historical_round.summary,
                "metrics": historical_round.metrics,
            }
            round_row = _insert_round(
                conn,
                workflow_id,
                historical_round.round_no,
                base_sha=historical_round.base_sha,
                status=historical_round.status,
                candidate_sha=historical_round.candidate_sha,
                audit_sha=historical_round.audit_sha,
                summary=summary,
                historical=True,
            )
            for index, fact in enumerate(historical_round.facts):
                db._append_workflow_event(conn, WorkflowEventCreate(
                    workflow_id=workflow_id,
                    round_id=round_row["id"],
                    event_type="history.fact_imported",
                    idempotency_key=(
                        f"history.fact:{workflow_id}:{history.idempotency_key}:"
                        f"{historical_round.round_no}:{index}"
                    ),
                    payload={
                        "round_no": historical_round.round_no,
                        **fact.model_dump(mode="json"),
                    },
                ))

        workflow = _workflow_row(conn, workflow_id)
        workflow = _touch(
            conn,
            workflow,
            "history.imported",
            payload,
        )
        # _touch creates a version-derived key; append the caller's stable key
        # as an alias event so a replay after other metadata edits is still
        # recognized by the explicit idempotency check above.
        db._append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=workflow_id,
            event_type="history.import_marker",
            idempotency_key=key,
            payload=payload,
        ))
        return db._row_to_workflow(workflow)


def sync_task(task_id: int) -> Optional[WorkflowRunInDB]:
    """Reconcile one linked queue task into its workflow projection.

    The operation is safe to repeat after crashes. Final task status, run
    projection and lifecycle transition are committed atomically.
    """
    with db._connect(immediate=True) as conn:
        run = conn.execute(
            "SELECT * FROM workflow_runs WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not run:
            return None
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task_row:
            raise db.WorkflowConflictError(
                f"workflow run {run['id']} references missing task {task_id}"
            )
        workflow = _workflow_row(conn, run["workflow_id"])
        task_status = task_row["status"]

        if task_status in {"pending", "rate_limited"}:
            if run["status"] != "pending":
                conn.execute(
                    """UPDATE workflow_runs SET status = 'pending',
                       started_at = NULL WHERE id = ?""",
                    (run["id"],),
                )
            db._append_workflow_event(conn, WorkflowEventCreate(
                workflow_id=run["workflow_id"],
                round_id=run["round_id"],
                run_id=run["id"],
                event_type="run.queued" if task_status == "pending" else "run.requeued",
                idempotency_key=(
                    f"run.{task_status}:{run['id']}:retry{task_row['retry_count']}"
                ),
                payload={"task_id": task_id, "task_status": task_status,
                         "retry_count": task_row["retry_count"]},
            ))
        elif task_status == "running":
            conn.execute(
                """UPDATE workflow_runs SET status = 'running', started_at = ?
                   WHERE id = ?""",
                (task_row["started_at"] or db._now(), run["id"]),
            )
            db._append_workflow_event(conn, WorkflowEventCreate(
                workflow_id=run["workflow_id"],
                round_id=run["round_id"],
                run_id=run["id"],
                event_type="run.started",
                idempotency_key=(
                    f"run.started:{run['id']}:retry{task_row['retry_count']}"
                ),
                payload={"task_id": task_id,
                         "retry_count": task_row["retry_count"]},
            ))
        elif task_status in {"completed", "failed", "cancelled"}:
            final_status = task_status
            output = {
                "task_status": task_status,
                "result": task_row["result"],
                "error": task_row["error"],
                "exit_code": task_row["exit_code"],
                "model_used": task_row["model_used"],
                "session_id": task_row["session_id"],
                "verdict": task_row["verdict"],
            }
            output_json = db._json_dump(output)
            output_sha = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
            # A terminal run whose durable output is already projected is a
            # complete no-op. Without this guard, worker startup replayed an
            # old ``invalid_output`` transition after a human had resumed the
            # same round, racing ``queued`` back to ``awaiting_human``.
            if (
                run["status"] == final_status
                and run["output_sha256"] == output_sha
            ):
                return db._row_to_workflow_run(run)
            conn.execute(
                """UPDATE workflow_runs SET status = ?, output_json = ?,
                   output_sha256 = ?, completed_at = ? WHERE id = ?""",
                (final_status, output_json, output_sha,
                 task_row["completed_at"] or db._now(), run["id"]),
            )
            db._append_workflow_event(conn, WorkflowEventCreate(
                workflow_id=run["workflow_id"],
                round_id=run["round_id"],
                run_id=run["id"],
                event_type=f"run.{final_status}",
                idempotency_key=f"run.{final_status}:{run['id']}:task{task_id}",
                payload={"task_id": task_id, "output_sha256": output_sha,
                         "exit_code": task_row["exit_code"]},
            ))

            state = WorkflowStatus(workflow["status"])
            role = WorkflowRole(run["role"])
            if (
                final_status == "completed"
                and task_row["verdict"] not in SUCCESS_VERDICTS
            ):
                if (
                    state not in TERMINAL_STATES
                    and state is not WorkflowStatus.AWAITING_HUMAN
                ):
                    conn.execute(
                        """UPDATE workflow_rounds SET status = 'failed',
                           completed_at = ? WHERE id = ?""",
                        (db._now(), run["round_id"]),
                    )
                    workflow = _transition(
                        conn,
                        workflow,
                        WorkflowStatus.AWAITING_HUMAN,
                        f"{role.value}.invalid_output",
                        {
                            "task_id": task_id,
                            "run_id": run["id"],
                            "verdict": task_row["verdict"],
                            "reason": "missing successful workflow verdict",
                        },
                        round_id=run["round_id"],
                    )
                refreshed = conn.execute(
                    "SELECT * FROM workflow_runs WHERE id = ?", (run["id"],)
                ).fetchone()
                return db._row_to_workflow_run(refreshed)
            if final_status == "completed" and role is WorkflowRole.EXECUTOR:
                if state is WorkflowStatus.EXECUTING:
                    conn.execute(
                        "UPDATE workflow_rounds SET status = 'gating' WHERE id = ?",
                        (run["round_id"],),
                    )
                    workflow = _transition(
                        conn, workflow, WorkflowStatus.GATING,
                        "executor.completed",
                        {"task_id": task_id, "run_id": run["id"],
                         "output_sha256": output_sha},
                        round_id=run["round_id"],
                    )
            elif final_status == "completed" and role is WorkflowRole.REVIEWER:
                if state is WorkflowStatus.REVIEWING:
                    workflow = _transition(
                        conn, workflow, WorkflowStatus.AWAITING_HUMAN,
                        "review.awaiting_decision",
                        {"task_id": task_id, "run_id": run["id"],
                         "output_sha256": output_sha},
                        round_id=run["round_id"],
                    )
            elif state not in TERMINAL_STATES and state is not WorkflowStatus.AWAITING_HUMAN:
                conn.execute(
                    """UPDATE workflow_rounds SET status = 'failed',
                       completed_at = ? WHERE id = ?""",
                    (db._now(), run["round_id"]),
                )
                workflow = _transition(
                    conn, workflow, WorkflowStatus.AWAITING_HUMAN,
                    f"{role.value}.{final_status}",
                    {"task_id": task_id, "run_id": run["id"],
                     "error": task_row["error"]},
                    round_id=run["round_id"],
                )

        refreshed = conn.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run["id"],)
        ).fetchone()
        return db._row_to_workflow_run(refreshed)


def sync_all_tasks(workflow_id: str = None) -> int:
    with db._connect() as conn:
        if workflow_id:
            rows = conn.execute(
                """SELECT task_id FROM workflow_runs
                   WHERE workflow_id = ? AND task_id IS NOT NULL""",
                (workflow_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT task_id FROM workflow_runs WHERE task_id IS NOT NULL"
            ).fetchall()
    count = 0
    for row in rows:
        if sync_task(row["task_id"]):
            count += 1
    return count
