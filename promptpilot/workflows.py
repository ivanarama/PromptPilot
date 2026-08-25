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
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
    WorkflowPlanInDB,
    WorkflowPlanReplace,
    WorkflowReviewDecision,
    WorkflowRole,
    WorkflowRoundInDB,
    WorkflowRoundStatus,
    WorkflowRunInDB,
    WorkflowStageInDB,
    WorkflowStageSpec,
    WorkflowStageStatus,
    WorkflowStartRequest,
    WorkflowStatus,
    WorkflowTaskDispatch,
    WorkflowGateDecision,
    WorkflowConfig,
)


ALLOWED_TRANSITIONS = {
    WorkflowStatus.DRAFT: {
        WorkflowStatus.PLANNING,
        WorkflowStatus.QUEUED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.PLANNING: {
        WorkflowStatus.AWAITING_PLAN_APPROVAL,
        WorkflowStatus.AWAITING_HUMAN,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.AWAITING_PLAN_APPROVAL: {
        WorkflowStatus.PLANNING,
        WorkflowStatus.QUEUED,
        WorkflowStatus.CANCELLED,
    },
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
        WorkflowStatus.PLANNING,
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

DEFAULT_PLANNER_PROMPT = """Ты — ведущий инженер-планировщик PromptPilot.
Изучи репозиторий и разложи общую задачу на короткие последовательные этапы,
которые сможет надёжно выполнить более слабый исполнитель.

Цель: {{objective}}
Репозиторий: {{repository_path}}
Ветка кандидата: {{candidate_branch}}

Требования к плану:
- каждый этап даёт один проверяемый инженерный результат;
- зависимости могут ссылаться только на предыдущие этапы;
- укажи разрешённые пути, deliverables и deterministic acceptance gates;
- ограничивай scope этапа, не объединяй независимые функциональные области;
- последним добавь integration-этап, проверяющий результат целиком;
- не изменяй файлы репозитория: твой результат — только план.

Верни план между маркерами строго как JSON-объект {"stages": [...]}.
Для каждого этапа обязательны code, title, objective; доступны stage_type
(implementation/integration), dependencies, allowed_paths, deliverables,
acceptance_gates, executor_prompt, reviewer_prompt, max_revision_rounds.
"""

PLAN_OUTPUT_CONTRACT = """
<promptpilot-plan-contract version="stage-plan-v1">
WORKFLOW_PLAN_JSON_BEGIN
{"stages":[{"code":"S1","title":"...","objective":"...","stage_type":"implementation","dependencies":[],"allowed_paths":[],"deliverables":[],"acceptance_gates":[]},{"code":"FINAL","title":"Интеграционный аудит","objective":"Проверить результат целиком","stage_type":"integration","dependencies":["S1"],"allowed_paths":[],"deliverables":[],"acceptance_gates":[]}]}
WORKFLOW_PLAN_JSON_END
</promptpilot-plan-contract>
"""

DEFAULT_EXECUTOR_PROMPT = """Ты — исполнитель в автономном инженерном workflow PromptPilot.

Цель: {{objective}}
Текущий этап: {{stage_code}} {{stage_title}}
Цель этапа: {{stage_goal}}
Разрешённые пути: {{allowed_paths}}
Ожидаемые результаты: {{deliverables}}
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
                  historical: bool = False, stage_id: str = None) -> sqlite3.Row:
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
               (id, workflow_id, round_no, stage_id, status, base_sha, candidate_sha,
                audit_sha, started_at, completed_at, summary_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (round_id, workflow_id, round_no, stage_id, status.value, base_sha,
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
            "stage_id": stage_id,
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
           WHERE workflow_id = ? AND event_type IN ('workflow.started', 'plan.approved')
           ORDER BY seq LIMIT 1""",
        (workflow_id,),
    ).fetchone()
    if not row:
        return None
    payload = db._json_load(row["payload_json"])
    return payload.get("first_automated_round") or payload.get("first_round")


def _check_round_budget(conn: sqlite3.Connection, workflow: sqlite3.Row,
                        next_round: int) -> bool:
    first = _first_automated_round(conn, workflow["id"])
    if first is None:
        return True
    used_after_create = next_round - first + 1
    return used_after_create <= _max_automated_rounds(workflow)


def _render_planner_prompt(workflow: WorkflowInDB, custom_prompt: str = "") -> str:
    rendered = _config_for(workflow).planning.prompt_template or DEFAULT_PLANNER_PROMPT
    values = {
        "objective": workflow.objective,
        "repository_path": workflow.repository_path,
        "candidate_branch": workflow.candidate_branch,
    }
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    if custom_prompt.strip():
        rendered += "\n\nДополнительные указания пользователя:\n" + custom_prompt.strip()
    return rendered.strip() + "\n\n" + PLAN_OUTPUT_CONTRACT.strip()


def parse_workflow_plan(report: str, max_stages: int = 20) -> list[WorkflowStageSpec] | None:
    match = re.search(
        r"WORKFLOW_PLAN_JSON_BEGIN\s*(\{.*?\})\s*WORKFLOW_PLAN_JSON_END",
        report or "",
        flags=re.DOTALL,
    )
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
        replacement = WorkflowPlanReplace(
            expected_version=0, stages=payload.get("stages") or []
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if len(replacement.stages) > max_stages:
        return None
    return replacement.stages


def dispatch_planner(workflow_id: str,
                     dispatch: WorkflowPlanDispatch) -> WorkflowPlanInDB:
    with db._connect(immediate=True) as conn:
        workflow_row = _workflow_row(conn, workflow_id)
        _require_version(workflow_row, dispatch.expected_version)
        state = WorkflowStatus(workflow_row["status"])
        if state not in {
            WorkflowStatus.DRAFT,
            WorkflowStatus.AWAITING_PLAN_APPROVAL,
            WorkflowStatus.AWAITING_HUMAN,
        }:
            raise db.WorkflowConflictError(
                f"cannot dispatch planner while workflow is {state.value}"
            )
        if state is WorkflowStatus.AWAITING_HUMAN and workflow_row["current_round"]:
            raise db.WorkflowConflictError(
                "planner can only be retried before stage execution starts"
            )
        workflow = db._row_to_workflow(workflow_row)
        config = _config_for(workflow)
        role = config.roles.planner
        provider = dispatch.provider if dispatch.provider is not None else role.provider
        herdr_target = dispatch.herdr_target or role.herdr_target
        if provider == "herdr-session" and not herdr_target:
            raise db.WorkflowConflictError("planner using herdr-session needs herdr_target")
        prompt = _render_planner_prompt(workflow, dispatch.prompt)
        task = db._insert_task(conn, TaskCreate(
            prompt=prompt.rstrip() + WORKFLOW_VERDICT_INSTRUCTION,
            working_dir=workflow.repository_path,
            provider=provider,
            priority=dispatch.priority if dispatch.priority != 5 else role.priority,
            max_retries=(
                dispatch.max_retries if dispatch.max_retries != 5 else role.max_retries
            ),
            skip_permissions=False,
            model=dispatch.model or role.model,
            effort=dispatch.effort or role.effort,
            task_timeout=(
                dispatch.task_timeout
                if dispatch.task_timeout is not None else role.task_timeout
            ),
            keep_pane=dispatch.keep_pane,
            herdr_target=herdr_target,
            machine=dispatch.machine or role.machine,
        ))
        input_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        now = db._now()
        conn.execute(
            """INSERT INTO workflow_plans
               (workflow_id, status, planner_task_id, input_sha256,
                created_at, updated_at)
               VALUES (?, 'planning', ?, ?, ?, ?)
               ON CONFLICT(workflow_id) DO UPDATE SET status='planning',
                 planner_task_id=excluded.planner_task_id,
                 input_sha256=excluded.input_sha256,
                 output_sha256=NULL, output_json=NULL,
                 updated_at=excluded.updated_at, approved_at=NULL""",
            (workflow_id, task.id, input_sha, now, now),
        )
        workflow_row = _transition(
            conn, workflow_row, WorkflowStatus.PLANNING,
            "planner.dispatched",
            {"task_id": task.id, "provider": provider, "input_sha256": input_sha},
        )
        row = conn.execute(
            "SELECT * FROM workflow_plans WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        return db._row_to_workflow_plan(row)


def sync_planner_task(task_id: int) -> Optional[WorkflowPlanInDB]:
    auto_approve: tuple[str, int] | None = None
    with db._connect(immediate=True) as conn:
        plan = conn.execute(
            "SELECT * FROM workflow_plans WHERE planner_task_id = ?", (task_id,)
        ).fetchone()
        if not plan:
            return None
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise db.WorkflowConflictError(f"planner task {task_id} is missing")
        workflow = _workflow_row(conn, plan["workflow_id"])
        if task["status"] in {"pending", "rate_limited", "running"}:
            return db._row_to_workflow_plan(plan)
        output = {
            "task_status": task["status"], "result": task["result"],
            "error": task["error"], "exit_code": task["exit_code"],
            "verdict": task["verdict"], "model_used": task["model_used"],
        }
        output_json = db._json_dump(output)
        output_sha = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
        if plan["output_sha256"] == output_sha:
            return db._row_to_workflow_plan(plan)
        config = _config_for(db._row_to_workflow(workflow))
        stages = (
            parse_workflow_plan(task["result"] or "", config.planning.max_stages)
            if task["status"] == "completed" and task["verdict"] in SUCCESS_VERDICTS
            else None
        )
        if stages:
            db._replace_workflow_stages(conn, workflow["id"], stages)
            conn.execute(
                """UPDATE workflow_plans SET status='awaiting_approval',
                   output_sha256=?, output_json=?, updated_at=?
                   WHERE workflow_id=?""",
                (output_sha, output_json, db._now(), workflow["id"]),
            )
            workflow = _transition(
                conn, workflow, WorkflowStatus.AWAITING_PLAN_APPROVAL,
                "planner.completed",
                {"task_id": task_id, "stage_count": len(stages),
                 "stage_codes": [stage.code for stage in stages],
                 "output_sha256": output_sha},
            )
            # LLM-authored shell commands are never executed without a human
            # seeing the plan, even when command-free plans may auto-approve.
            has_generated_commands = any(stage.acceptance_gates for stage in stages)
            if not config.planning.require_approval and not has_generated_commands:
                auto_approve = (workflow["id"], workflow["state_version"])
        else:
            conn.execute(
                """UPDATE workflow_plans SET status='failed', output_sha256=?,
                   output_json=?, updated_at=? WHERE workflow_id=?""",
                (output_sha, output_json, db._now(), workflow["id"]),
            )
            _transition(
                conn, workflow, WorkflowStatus.AWAITING_HUMAN,
                "planner.invalid_output",
                {"task_id": task_id, "output_sha256": output_sha,
                 "reason": "planner did not return a valid stage-plan-v1 contract"},
            )
        row = conn.execute(
            "SELECT * FROM workflow_plans WHERE workflow_id = ?",
            (plan["workflow_id"],),
        ).fetchone()
        result = db._row_to_workflow_plan(row)
    if auto_approve:
        approve_plan(
            auto_approve[0],
            WorkflowPlanApproval(expected_version=auto_approve[1]),
        )
        return db.get_workflow_plan(auto_approve[0])
    return result


def replace_plan(workflow_id: str,
                 replacement: WorkflowPlanReplace) -> list[WorkflowStageInDB]:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, replacement.expected_version)
        if WorkflowStatus(workflow["status"]) is not WorkflowStatus.AWAITING_PLAN_APPROVAL:
            raise db.WorkflowConflictError("plan can only be edited before approval")
        limit = _config_for(db._row_to_workflow(workflow)).planning.max_stages
        if len(replacement.stages) > limit:
            raise db.WorkflowConflictError(f"plan exceeds max_stages={limit}")
        stages = db._replace_workflow_stages(conn, workflow_id, replacement.stages)
        conn.execute(
            "UPDATE workflow_plans SET updated_at=? WHERE workflow_id=?",
            (db._now(), workflow_id),
        )
        _touch(
            conn, workflow, "plan.edited",
            {"stage_count": len(stages), "stage_codes": [stage.code for stage in stages]},
        )
        return stages


def approve_plan(workflow_id: str,
                 approval: WorkflowPlanApproval) -> WorkflowInDB:
    with db._connect(immediate=True) as conn:
        workflow = _workflow_row(conn, workflow_id)
        _require_version(workflow, approval.expected_version)
        if WorkflowStatus(workflow["status"]) is not WorkflowStatus.AWAITING_PLAN_APPROVAL:
            raise db.WorkflowConflictError("workflow plan is not awaiting approval")
        stages = conn.execute(
            "SELECT * FROM workflow_stages WHERE workflow_id=? ORDER BY position",
            (workflow_id,),
        ).fetchall()
        if not stages:
            raise db.WorkflowConflictError("workflow plan has no stages")
        now = db._now()
        conn.execute(
            "UPDATE workflow_stages SET status='pending' WHERE workflow_id=?",
            (workflow_id,),
        )
        first = stages[0]
        conn.execute(
            """UPDATE workflow_stages SET status='executing', started_at=?
               WHERE id=?""",
            (now, first["id"]),
        )
        conn.execute(
            """UPDATE workflow_plans SET status='approved', approved_at=?,
               updated_at=? WHERE workflow_id=?""",
            (now, now, workflow_id),
        )
        conn.execute(
            "UPDATE workflows SET current_stage_id=? WHERE id=?",
            (first["id"], workflow_id),
        )
        round_no = workflow["current_round"] + 1
        round_row = _insert_round(
            conn, workflow_id, round_no, base_sha=approval.base_sha,
            stage_id=first["id"],
        )
        workflow = _workflow_row(conn, workflow_id)
        workflow = _transition(
            conn, workflow, WorkflowStatus.QUEUED, "plan.approved",
            {"stage_count": len(stages), "stage_id": first["id"],
             "stage_code": first["code"], "first_round": round_no},
            round_id=round_row["id"],
        )
        return db._row_to_workflow(workflow)


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
            current_stage = None
            if workflow["current_stage_id"]:
                current_stage = conn.execute(
                    "SELECT * FROM workflow_stages WHERE id=?",
                    (workflow["current_stage_id"],),
                ).fetchone()
            if current_stage:
                now = db._now()
                conn.execute(
                    """UPDATE workflow_stages SET status='completed',
                       completed_at=?, summary_json=? WHERE id=?""",
                    (now, db._json_dump(common), current_stage["id"]),
                )
                db._append_workflow_event(conn, WorkflowEventCreate(
                    workflow_id=workflow_id,
                    round_id=round_row["id"],
                    event_type="stage.completed",
                    idempotency_key=f"stage.completed:{current_stage['id']}",
                    payload={"stage_id": current_stage["id"],
                             "stage_code": current_stage["code"], **common},
                ))
                next_stage = conn.execute(
                    """SELECT * FROM workflow_stages
                       WHERE workflow_id=? AND position>? AND status='pending'
                       ORDER BY position LIMIT 1""",
                    (workflow_id, current_stage["position"]),
                ).fetchone()
                if next_stage:
                    conn.execute(
                        """UPDATE workflow_stages SET status='executing',
                           started_at=? WHERE id=?""",
                        (now, next_stage["id"]),
                    )
                    conn.execute(
                        "UPDATE workflows SET current_stage_id=? WHERE id=?",
                        (next_stage["id"], workflow_id),
                    )
                    new_round = _insert_round(
                        conn, workflow_id, workflow["current_round"] + 1,
                        base_sha=round_row["candidate_sha"],
                        stage_id=next_stage["id"],
                    )
                    workflow = _workflow_row(conn, workflow_id)
                    workflow = _transition(
                        conn, workflow, WorkflowStatus.QUEUED,
                        "stage.advanced",
                        {"completed_stage_id": current_stage["id"],
                         "completed_stage_code": current_stage["code"],
                         "next_stage_id": next_stage["id"],
                         "next_stage_code": next_stage["code"]},
                        round_id=new_round["id"],
                    )
                else:
                    workflow = _transition(
                        conn, workflow, WorkflowStatus.COMPLETED,
                        "review.passed",
                        {**common, "final_stage_id": current_stage["id"],
                         "final_stage_code": current_stage["code"]},
                        round_id=round_row["id"],
                    )
            else:
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
        round_row = (
            _current_round_row(conn, workflow)
            if workflow["current_round"] else None
        )
        if not action.resume:
            workflow = _touch(
                conn, workflow, "human.input",
                {"text": action.text, "resume": False},
                round_id=round_row["id"] if round_row else None,
            )
            return db._row_to_workflow(workflow)
        if round_row is None:
            raise db.WorkflowConflictError(
                "planning failure must be retried with planner dispatch"
            )

        if state is WorkflowStatus.REVISION_REQUIRED:
            next_round = workflow["current_round"] + 1
            current_stage = None
            if workflow["current_stage_id"]:
                current_stage = conn.execute(
                    "SELECT * FROM workflow_stages WHERE id=?",
                    (workflow["current_stage_id"],),
                ).fetchone()
            if current_stage:
                spec = db._json_load(current_stage["spec_json"])
                configured_limit = (
                    spec.get("max_revision_rounds")
                    or _config_for(db._row_to_workflow(workflow)).planning.max_revisions_per_stage
                )
                revision_count = conn.execute(
                    """SELECT COUNT(*) FROM workflow_rounds
                       WHERE stage_id=? AND status='revision_required'""",
                    (current_stage["id"],),
                ).fetchone()[0]
                if revision_count >= int(configured_limit):
                    workflow = _transition(
                        conn, workflow, WorkflowStatus.AWAITING_HUMAN,
                        "limit.stage_revisions",
                        {"text": action.text, "stage_id": current_stage["id"],
                         "stage_code": current_stage["code"],
                         "max_revision_rounds": int(configured_limit)},
                        round_id=round_row["id"],
                    )
                    return db._row_to_workflow(workflow)
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
                stage_id=workflow["current_stage_id"],
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
        planner_task = conn.execute(
            """SELECT t.id, t.status FROM workflow_plans p
               JOIN tasks t ON t.id=p.planner_task_id
               WHERE p.workflow_id=? AND t.status IN
               ('pending','rate_limited','running')""",
            (workflow_id,),
        ).fetchone()
        if planner_task:
            if planner_task["status"] == "running":
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                    (f"cancel_task:{planner_task['id']}",),
                )
            else:
                conn.execute(
                    """UPDATE tasks SET status='cancelled', completed_at=?
                       WHERE id=?""",
                    (db._now(), planner_task["id"]),
                )
            conn.execute(
                "UPDATE workflow_plans SET status='cancelled', updated_at=? WHERE workflow_id=?",
                (db._now(), workflow_id),
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
            {"linked_tasks": [item["id"] for item in linked]
             + ([planner_task["id"]] if planner_task else [])},
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
    stage_row = (
        db.get_workflow_stage(workflow.current_stage_id)
        if workflow.current_stage_id else None
    )
    stage = (
        stage_row.model_dump(mode="json")
        if stage_row else (workflow.config.get("stage") or {})
    )
    values = {
        "objective": workflow.objective,
        "repository_path": workflow.repository_path,
        "candidate_branch": workflow.candidate_branch,
        "round_no": str(round_no),
        "stage_code": str(stage.get("code") or ""),
        "stage_title": str(stage.get("title") or ""),
        "stage_goal": str(stage.get("objective") or stage.get("goal") or workflow.objective),
        "allowed_paths": "\n".join(stage.get("allowed_paths") or []) or "(не ограничены планом)",
        "deliverables": "\n".join(stage.get("deliverables") or []) or "(см. цель этапа)",
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
    if stage_row:
        override = (
            stage_row.executor_prompt
            if role is WorkflowRole.EXECUTOR else stage_row.reviewer_prompt
        )
        if override:
            rendered = override
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
    stage = (
        db.get_workflow_stage(workflow.current_stage_id)
        if workflow.current_stage_id else None
    )
    commands = list(gate.commands)
    if stage:
        commands.extend(
            command for command in stage.acceptance_gates
            if command not in commands
        )
    evidence: list[str] = []
    if not gate.enabled or not commands:
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
    for index, command in enumerate(commands, start=1):
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
        summary=f"Пройдено команд gate: {len(commands)}",
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
            if workflow.status is WorkflowStatus.AWAITING_PLAN_APPROVAL:
                if config.planning.require_approval:
                    return workflow
                stages = db.list_workflow_stages(workflow.id)
                if any(stage.acceptance_gates for stage in stages):
                    return workflow
                workflow = approve_plan(
                    workflow.id,
                    WorkflowPlanApproval(expected_version=workflow.state_version),
                )
                continue

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
        planner = conn.execute(
            "SELECT workflow_id FROM workflow_plans WHERE planner_task_id = ?",
            (task_id,),
        ).fetchone()
    if planner:
        sync_planner_task(task_id)
        return advance_workflow(planner["workflow_id"])
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
        if workflow_id:
            planner_rows = conn.execute(
                """SELECT planner_task_id AS task_id, workflow_id
                   FROM workflow_plans WHERE workflow_id=? AND status='planning'
                   AND planner_task_id IS NOT NULL""",
                (workflow_id,),
            ).fetchall()
        else:
            planner_rows = conn.execute(
                """SELECT planner_task_id AS task_id, workflow_id
                   FROM workflow_plans WHERE status='planning'
                   AND planner_task_id IS NOT NULL"""
            ).fetchall()
    count = 0
    workflow_ids: set[str] = {row["id"] for row in recoverable}
    for row in rows:
        if sync_task(row["task_id"]):
            count += 1
            workflow_ids.add(row["workflow_id"])
    for row in planner_rows:
        if sync_planner_task(row["task_id"]):
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
    plan = db.get_workflow_plan(workflow_id)
    stages = db.list_workflow_stages(workflow_id)
    task_ids = [run.task_id for run in runs if run.task_id]
    task_ids.extend(
        event.payload.get("task_id") for event in events
        if event.event_type == "planner.dispatched" and event.payload.get("task_id")
    )
    tasks = [db.get_task(task_id) for task_id in dict.fromkeys(task_ids)]
    tasks = [task for task in tasks if task]

    provider_counts = Counter(task.provider or "default" for task in tasks)
    model_counts = Counter(
        task.model_used or task.model or "unknown" for task in tasks
    )
    finding_statuses = Counter(item.status.value for item in findings)
    finding_severities = Counter(item.severity.value for item in findings)
    event_counts = Counter(item.event_type for item in events)
    stage_statuses = Counter(item.status.value for item in stages)
    runtime_seconds = sum(
        max(0.0, (task.completed_at - task.started_at).total_seconds())
        for task in tasks if task.started_at and task.completed_at
    )
    metrics = {
        "rounds_total": len(rounds),
        "rounds_imported": sum(bool(item.summary and item.summary.get("historical")) for item in rounds),
        "rounds_promptpilot": sum(not bool(item.summary and item.summary.get("historical")) for item in rounds),
        "executor_attempts": sum(run.role is WorkflowRole.EXECUTOR for run in runs),
        "reviewer_attempts": sum(run.role is WorkflowRole.REVIEWER for run in runs),
        "planner_attempts": event_counts["planner.dispatched"],
        "stages_total": len(stages),
        "stages_by_status": dict(stage_statuses),
        "gate_passes": event_counts["gate.passed"],
        "gate_failures": event_counts["gate.failed"],
        "review_revisions": event_counts["review.revision_required"],
        "review_passes": (
            event_counts["stage.completed"] if stages else event_counts["review.passed"]
        ),
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
        "schema_version": 2,
        "generated_at": db._now(),
        "workflow": workflow.model_dump(mode="json"),
        "metrics": metrics,
        "plan": plan.model_dump(mode="json") if plan else None,
        "stages": [item.model_dump(mode="json") for item in stages],
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
    lines.extend(["", "## Stage plan", ""])
    if report["stages"]:
        for stage in report["stages"]:
            lines.append(
                f"- {stage['position']}. `{stage['code']}` — {stage['title']} "
                f"[{stage['stage_type']}/{stage['status']}]: {stage['objective']}"
            )
    else:
        lines.append("- No stage plan recorded.")
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
