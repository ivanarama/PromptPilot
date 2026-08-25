"""W1 workflow state machine and manual executor/reviewer pilot.

The module deliberately owns lifecycle decisions while :mod:`promptpilot.db`
remains the persistence layer. Every state change and materialized projection
update is committed in the same SQLite transaction as its append-only event.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from typing import Optional

from . import db
from .models import (
    FindingStatus,
    FindingSeverity,
    GateVerdict,
    ReviewVerdict,
    ReviewFindingInput,
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
    WorkflowConfig,
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

DEFAULT_EXECUTOR_PROMPT = """Ты — исполнитель в автономном инженерном workflow PromptPilot.

Цель: {{objective}}
Текущий этап: {{stage_code}} {{stage_title}}
Цель этапа: {{stage_goal}}
Репозиторий: {{repository_path}}
Ветка кандидата: {{candidate_branch}}
Раунд: {{round_no}}

Замечания предыдущего независимого аудита:
{{previous_review}}

Результаты предыдущих deterministic gate:
{{gate_evidence}}

Исправь замечания в заданном scope, проверь результат, сохрани доказательства и
сделай локальные коммиты. Не меняй критерии приёмки и не объявляй готовность без
проверяемых фактов. В итоговом ответе перечисли изменения, команды проверок,
commit SHA, незакрытые ограничения и пути к evidence.
"""

DEFAULT_REVIEWER_PROMPT = """Ты — независимый аудитор в автономном workflow PromptPilot.
Не исправляй код и не принимай заявления исполнителя на веру.

Цель: {{objective}}
Текущий этап: {{stage_code}} {{stage_title}}
Цель этапа: {{stage_goal}}
Репозиторий: {{repository_path}}
Ветка кандидата: {{candidate_branch}}
Раунд: {{round_no}}

Отчёт исполнителя:
{{executor_report}}

Deterministic gate:
{{gate_evidence}}

Проверь diff, историю Git, тесты и evidence. В конце отчёта обязательно выведи
две машинно-читаемые строки (каждая целиком на одной строке):
AUDIT_FINDINGS_JSON: []
AUDIT_VERDICT: PASS

Для замечаний верни JSON-массив объектов с полями fingerprint, severity
(blocker/high/medium/low/info), category, title, status (open/resolved/reopened/
accepted_risk), payload. AUDIT_VERDICT допускает только PASS,
REVISION_REQUIRED или HUMAN_REQUIRED.
"""


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


def _config_for(workflow: WorkflowInDB | sqlite3.Row) -> WorkflowConfig:
    raw = (
        db._json_load(workflow["config_json"])
        if isinstance(workflow, sqlite3.Row)
        else workflow.config
    )
    return WorkflowConfig.model_validate(raw or {})


def _latest_run_output(workflow_id: str, role: WorkflowRole,
                       *, before_round: int = None) -> str:
    with db._connect() as conn:
        params: list[object] = [workflow_id, role.value]
        round_filter = ""
        if before_round is not None:
            round_filter = " AND rd.round_no < ?"
            params.append(before_round)
        row = conn.execute(
            f"""SELECT r.output_json FROM workflow_runs r
                JOIN workflow_rounds rd ON rd.id = r.round_id
                WHERE r.workflow_id = ? AND r.role = ?
                  AND r.status = 'completed'{round_filter}
                ORDER BY rd.round_no DESC, r.attempt_no DESC LIMIT 1""",
            params,
        ).fetchone()
    if not row:
        return "(нет: это первый раунд)"
    output = db._json_load(row["output_json"])
    return output.get("result") or output.get("error") or "(пустой отчёт)"


def _latest_gate_evidence(workflow_id: str, *, before_round: int = None) -> str:
    with db._connect() as conn:
        params: list[object] = [workflow_id]
        round_filter = ""
        if before_round is not None:
            round_filter = " AND rd.round_no <= ?"
            params.append(before_round)
        row = conn.execute(
            f"""SELECT e.payload_json FROM workflow_events e
                LEFT JOIN workflow_rounds rd ON rd.id = e.round_id
                WHERE e.workflow_id = ? AND e.event_type IN
                  ('gate.passed', 'gate.failed'){round_filter}
                ORDER BY e.seq DESC LIMIT 1""",
            params,
        ).fetchone()
    if not row:
        return "(gate ещё не выполнялся)"
    payload = db._json_load(row["payload_json"])
    summary = payload.get("summary") or payload.get("verdict") or "gate"
    evidence = payload.get("evidence") or []
    return summary + ("\n" + "\n".join(evidence) if evidence else "")


def _render_role_prompt(workflow: WorkflowInDB, role: WorkflowRole,
                        template: str) -> str:
    round_no = workflow.current_round
    stage = workflow.config.get("stage") or {}
    values = {
        "objective": workflow.objective,
        "repository_path": workflow.repository_path,
        "candidate_branch": workflow.candidate_branch,
        "round_no": str(round_no),
        "stage_code": str(stage.get("code") or ""),
        "stage_title": str(stage.get("title") or ""),
        "stage_goal": str(stage.get("goal") or workflow.objective),
        "previous_review": _latest_run_output(
            workflow.id, WorkflowRole.REVIEWER, before_round=round_no
        ),
        "executor_report": _latest_run_output(
            workflow.id, WorkflowRole.EXECUTOR
        ),
        "gate_evidence": _latest_gate_evidence(
            workflow.id, before_round=round_no
        ),
    }
    rendered = template or (
        DEFAULT_EXECUTOR_PROMPT
        if role is WorkflowRole.EXECUTOR else DEFAULT_REVIEWER_PROMPT
    )
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    return rendered.strip()


def _dispatch_configured_role(workflow: WorkflowInDB,
                              role: WorkflowRole) -> WorkflowDispatchResult:
    config = _config_for(workflow)
    role_config = (
        config.roles.executor
        if role is WorkflowRole.EXECUTOR else config.roles.reviewer
    )
    if role_config.provider == "herdr-session" and not role_config.herdr_target:
        raise ValueError(
            f"autonomous {role.value} using herdr-session needs herdr_target"
        )
    return dispatch_task(workflow.id, WorkflowTaskDispatch(
        expected_version=workflow.state_version,
        role=role,
        prompt=_render_role_prompt(
            workflow, role, role_config.prompt_template
        ),
        provider=role_config.provider,
        priority=role_config.priority,
        max_retries=role_config.max_retries,
        skip_permissions=role_config.skip_permissions,
        model=role_config.model,
        effort=role_config.effort,
        working_dir=workflow.repository_path,
        worktree=role_config.worktree,
        keep_pane=role_config.keep_pane,
        herdr_target=role_config.herdr_target,
        machine=role_config.machine,
        task_timeout=role_config.task_timeout,
    ))


def _run_gate_commands(workflow: WorkflowInDB) -> WorkflowGateDecision:
    gate = _config_for(workflow).gate
    evidence: list[str] = []
    if not gate.enabled or not gate.commands:
        return WorkflowGateDecision(
            expected_version=workflow.state_version,
            verdict=GateVerdict.PASS,
            gate_id="automatic-gate",
            summary=(
                "Gate отключён настройками"
                if not gate.enabled else "Настроенных deterministic-команд нет"
            ),
            evidence=[],
        )
    for index, command in enumerate(gate.commands, start=1):
        argv = (
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if os.name == "nt" else ["/bin/sh", "-lc", command]
        )
        try:
            completed = subprocess.run(
                argv,
                cwd=workflow.repository_path,
                capture_output=True,
                text=True,
                timeout=gate.timeout_seconds,
                errors="replace",
            )
            output = ((completed.stdout or "") + (completed.stderr or ""))[-4000:]
            evidence.append(
                f"[{index}] exit={completed.returncode}: {command}\n{output}".rstrip()
            )
            if completed.returncode and gate.stop_on_failure:
                return WorkflowGateDecision(
                    expected_version=workflow.state_version,
                    verdict=GateVerdict.FAIL,
                    gate_id="automatic-gate",
                    summary=f"Команда gate #{index} завершилась с кодом {completed.returncode}",
                    evidence=evidence,
                )
        except subprocess.TimeoutExpired:
            evidence.append(
                f"[{index}] timeout={gate.timeout_seconds}s: {command}"
            )
            return WorkflowGateDecision(
                expected_version=workflow.state_version,
                verdict=GateVerdict.FAIL,
                gate_id="automatic-gate",
                summary=f"Команда gate #{index} превысила timeout",
                evidence=evidence,
            )
        except OSError as exc:
            evidence.append(f"[{index}] environment error: {exc}")
            return WorkflowGateDecision(
                expected_version=workflow.state_version,
                verdict=GateVerdict.HUMAN_REQUIRED,
                gate_id="automatic-gate",
                summary="Не удалось запустить deterministic gate",
                evidence=evidence,
            )
    return WorkflowGateDecision(
        expected_version=workflow.state_version,
        verdict=GateVerdict.PASS,
        gate_id="automatic-gate",
        summary=f"Пройдено команд gate: {len(gate.commands)}",
        evidence=evidence,
    )


def parse_reviewer_report(report: str) -> WorkflowReviewDecision | None:
    """Parse the strict reviewer tail while retaining a safe human fallback."""
    match = re.search(
        r"(?mi)^AUDIT_VERDICT:\s*(PASS|REVISION_REQUIRED|HUMAN_REQUIRED)\s*$",
        report or "",
    )
    if not match:
        return None
    findings: list[ReviewFindingInput] = []
    findings_match = re.search(
        r"(?mi)^AUDIT_FINDINGS_JSON:\s*(\[.*\])\s*$", report or ""
    )
    if findings_match:
        try:
            raw_findings = json.loads(findings_match.group(1))
            findings = [ReviewFindingInput.model_validate(item) for item in raw_findings]
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
    verdict = ReviewVerdict(match.group(1).upper())
    if verdict is ReviewVerdict.REVISION_REQUIRED and not findings:
        digest = hashlib.sha256((report or "").encode("utf-8")).hexdigest()[:16]
        findings = [ReviewFindingInput(
            fingerprint=f"review-report-{digest}",
            severity=FindingSeverity.MEDIUM,
            category="independent_review",
            title="Аудитор потребовал доработку; подробности в полном отчёте",
            payload={"report": report},
        )]
    return WorkflowReviewDecision(
        expected_version=0,
        verdict=verdict,
        summary=report or "",
        findings=findings,
    )


def _latest_completed_reviewer(workflow: WorkflowInDB) -> Optional[WorkflowRunInDB]:
    rounds = db.list_workflow_rounds(workflow.id)
    current = next(
        (item for item in rounds if item.round_no == workflow.current_round), None
    )
    if not current:
        return None
    reviewers = [
        item for item in db.list_workflow_runs(current.id)
        if item.role is WorkflowRole.REVIEWER and item.status.value == "completed"
    ]
    return reviewers[-1] if reviewers else None


def _pause_automation(workflow_id: str, reason: str) -> WorkflowInDB:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        state = WorkflowStatus(workflow["status"])
        round_row = _current_round_row(conn, workflow)
        if state is WorkflowStatus.AWAITING_HUMAN:
            workflow = _touch(
                conn, workflow, "automation.paused", {"reason": reason},
                round_id=round_row["id"],
            )
        elif state not in TERMINAL_STATES:
            workflow = _transition(
                conn, workflow, WorkflowStatus.AWAITING_HUMAN,
                "automation.paused", {"reason": reason},
                round_id=round_row["id"],
            )
        return db._row_to_workflow(workflow)


def advance_workflow(workflow_id: str, max_actions: int = 12) -> WorkflowInDB:
    """Advance all deterministic hand-offs until an agent or human must act."""
    for _ in range(max_actions):
        workflow = db.get_workflow(workflow_id)
        if not workflow:
            raise db.WorkflowNotFoundError(workflow_id)
        config = _config_for(workflow)
        if not config.automation.enabled or workflow.status in TERMINAL_STATES:
            return workflow
        try:
            if workflow.status is WorkflowStatus.QUEUED:
                if not config.automation.auto_dispatch_executor:
                    return workflow
                return _dispatch_configured_role(workflow, WorkflowRole.EXECUTOR).workflow

            if workflow.status is WorkflowStatus.GATING:
                if not config.automation.auto_gate:
                    return workflow
                decision = _run_gate_commands(workflow)
                workflow = record_gate(workflow.id, decision)
                if decision.verdict is GateVerdict.HUMAN_REQUIRED:
                    return workflow
                continue

            if workflow.status is WorkflowStatus.REVIEWING:
                if not config.automation.auto_dispatch_reviewer:
                    return workflow
                current_round = next(
                    item for item in db.list_workflow_rounds(workflow.id)
                    if item.round_no == workflow.current_round
                )
                reviewer_runs = [
                    item for item in db.list_workflow_runs(current_round.id)
                    if item.role is WorkflowRole.REVIEWER
                ]
                if reviewer_runs:
                    return workflow
                return _dispatch_configured_role(workflow, WorkflowRole.REVIEWER).workflow

            if workflow.status is WorkflowStatus.AWAITING_HUMAN:
                if not config.automation.auto_apply_review:
                    return workflow
                reviewer = _latest_completed_reviewer(workflow)
                if not reviewer:
                    return workflow
                report = (reviewer.output or {}).get("result") or ""
                decision = parse_reviewer_report(report)
                if not decision:
                    return _pause_automation(
                        workflow.id,
                        "Аудитор не вернул валидные AUDIT_VERDICT/AUDIT_FINDINGS_JSON",
                    )
                decision = decision.model_copy(update={
                    "expected_version": workflow.state_version
                })
                workflow = record_review(workflow.id, decision)
                if decision.verdict is ReviewVerdict.HUMAN_REQUIRED:
                    return workflow
                continue

            if workflow.status is WorkflowStatus.REVISION_REQUIRED:
                if not config.automation.auto_resume_revision:
                    return workflow
                workflow = human_input(workflow.id, WorkflowHumanInput(
                    expected_version=workflow.state_version,
                    text="Автоматический новый раунд по результатам gate/review",
                    resume=True,
                ))
                if workflow.status is WorkflowStatus.AWAITING_HUMAN:
                    return workflow
                continue

            return workflow
        except db.WorkflowConflictError as exc:
            # Retry only actual optimistic-version races. A semantic conflict
            # (for example PASS with an open blocker) requires human review.
            if "version is" in str(exc) or "changed concurrently" in str(exc):
                continue
            return _pause_automation(
                workflow.id, f"WorkflowConflictError: {exc}"
            )
        except Exception as exc:
            return _pause_automation(
                workflow.id, f"{type(exc).__name__}: {exc}"
            )
    return _pause_automation(
        workflow_id, f"Превышен лимит внутренних переходов: {max_actions}"
    )


def advance_linked_task(task_id: int) -> Optional[WorkflowInDB]:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT workflow_id FROM workflow_runs WHERE task_id = ?", (task_id,)
        ).fetchone()
    return advance_workflow(row["workflow_id"]) if row else None


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
                """SELECT task_id, workflow_id FROM workflow_runs
                   WHERE workflow_id = ? AND task_id IS NOT NULL""",
                (workflow_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT task_id, workflow_id FROM workflow_runs
                   WHERE task_id IS NOT NULL"""
            ).fetchall()
        recoverable = conn.execute(
            """SELECT id FROM workflows WHERE status NOT IN
               ('completed', 'failed', 'cancelled')"""
        ).fetchall() if workflow_id is None else []
    count = 0
    workflow_ids: set[str] = {row["id"] for row in recoverable}
    for row in rows:
        if sync_task(row["task_id"]):
            count += 1
            workflow_ids.add(row["workflow_id"])
    for linked_workflow_id in workflow_ids:
        advance_workflow(linked_workflow_id)
    return count


def workflow_report(workflow_id: str) -> dict:
    """Build a self-contained, provenance-rich report for analysis/articles."""
    workflow = db.get_workflow(workflow_id)
    if not workflow:
        raise db.WorkflowNotFoundError(workflow_id)
    rounds = db.list_workflow_rounds(workflow_id)
    runs = []
    for round_item in rounds:
        runs.extend(db.list_workflow_runs(round_item.id))
    findings = db.list_workflow_findings(workflow_id)
    artifacts = db.list_workflow_artifacts(workflow_id)
    events = db.list_workflow_events(workflow_id, limit=100000)
    tasks = [db.get_task(run.task_id) for run in runs if run.task_id]
    tasks = [task for task in tasks if task]

    provider_counts = Counter(task.provider or "default" for task in tasks)
    model_counts = Counter(
        task.model_used or task.model or "unknown" for task in tasks
    )
    finding_statuses = Counter(item.status.value for item in findings)
    finding_severities = Counter(item.severity.value for item in findings)
    event_counts = Counter(item.event_type for item in events)
    runtime_seconds = sum(
        max(0.0, (run.completed_at - run.started_at).total_seconds())
        for run in runs if run.started_at and run.completed_at
    )
    metrics = {
        "rounds_total": len(rounds),
        "rounds_imported": sum(bool(item.summary and item.summary.get("historical")) for item in rounds),
        "rounds_promptpilot": sum(not bool(item.summary and item.summary.get("historical")) for item in rounds),
        "executor_attempts": sum(run.role is WorkflowRole.EXECUTOR for run in runs),
        "reviewer_attempts": sum(run.role is WorkflowRole.REVIEWER for run in runs),
        "gate_passes": event_counts["gate.passed"],
        "gate_failures": event_counts["gate.failed"],
        "review_revisions": event_counts["review.revision_required"],
        "review_passes": event_counts["review.passed"],
        "human_pauses": event_counts["automation.paused"] + event_counts["review.human_required"],
        "agent_runtime_seconds": round(runtime_seconds, 3),
        "elapsed_seconds": round((workflow.updated_at - workflow.created_at).total_seconds(), 3),
        "providers": dict(provider_counts),
        "models": dict(model_counts),
        "findings_by_status": dict(finding_statuses),
        "findings_by_severity": dict(finding_severities),
        "artifacts": len(artifacts),
        "events": len(events),
    }
    return {
        "schema_version": 1,
        "generated_at": db._now(),
        "workflow": workflow.model_dump(mode="json"),
        "metrics": metrics,
        "rounds": [item.model_dump(mode="json") for item in rounds],
        "runs": [item.model_dump(mode="json") for item in runs],
        "findings": [item.model_dump(mode="json") for item in findings],
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "events": [item.model_dump(mode="json") for item in events],
    }


def workflow_report_markdown(workflow_id: str) -> str:
    report = workflow_report(workflow_id)
    workflow = report["workflow"]
    metrics = report["metrics"]
    lines = [
        f"# Workflow report: {workflow['slug']}",
        "",
        f"- Objective: {workflow['objective']}",
        f"- Status: {workflow['status']}",
        f"- Repository: `{workflow['repository_path']}`",
        f"- Candidate branch: `{workflow['candidate_branch']}`",
        f"- Created: {workflow['created_at']}",
        f"- Updated: {workflow['updated_at']}",
        "",
        "## Metrics",
        "",
    ]
    for name, value in metrics.items():
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
        lines.append(f"- {name}: {rendered}")
    lines.extend(["", "## Rounds", ""])
    for round_item in report["rounds"]:
        lines.append(
            f"- Round {round_item['round_no']}: {round_item['status']} "
            f"({round_item['started_at']} → {round_item.get('completed_at') or 'active'})"
        )
    lines.extend(["", "## Findings", ""])
    if report["findings"]:
        for finding in report["findings"]:
            lines.append(
                f"- [{finding['severity']}/{finding['status']}] "
                f"{finding['title']} (`{finding['fingerprint']}`)"
            )
    else:
        lines.append("- No structured findings recorded.")
    lines.extend(["", "## Provenance timeline", ""])
    for event in report["events"]:
        lines.append(
            f"- #{event['seq']} {event['created_at']} — `{event['event_type']}`"
        )
    lines.append("")
    return "\n".join(str(line) for line in lines)
