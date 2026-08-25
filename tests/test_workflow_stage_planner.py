import json

from promptpilot import workflows
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
)


def config(max_revisions=3, max_rounds=10, require_approval=True):
    return {
        "planning": {
            "enabled": True,
            "require_approval": require_approval,
            "max_stages": 10,
            "max_revisions_per_stage": max_revisions,
        },
        "automation": {"enabled": True},
        "roles": {
            "planner": {"provider": None},
            "executor": {"provider": None},
            "reviewer": {"provider": None},
        },
        "gate": {"commands": []},
        "limits": {"max_rounds": max_rounds},
    }


def create(isolated_db, slug="planned", **kwargs):
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="Сделать сложную задачу маленькими этапами",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/planned",
        config=config(**kwargs),
    ))


def plan_result(stages):
    return (
        "WORKFLOW_PLAN_JSON_BEGIN\n"
        + json.dumps({"stages": stages}, ensure_ascii=False)
        + "\nWORKFLOW_PLAN_JSON_END\n"
        + "ИТОГ: ГОТОВО — план сформирован"
    )


def finish_task(isolated_db, result):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def reviewer_pass():
    return (
        "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\n"
        "ИТОГ: ГОТОВО — этап принят"
    )


def reviewer_revision():
    return (
        'AUDIT_FINDINGS_JSON: [{"fingerprint":"needs-fix","severity":"medium",'
        '"category":"test","title":"Нужно исправление"}]\n'
        "AUDIT_VERDICT: REVISION_REQUIRED\n"
        "ИТОГ: ГОТОВО — нужны исправления"
    )


def test_planner_approval_and_two_stage_autonomous_lifecycle(isolated_db):
    workflow = create(isolated_db)
    plan = workflows.dispatch_planner(
        workflow.id,
        WorkflowPlanDispatch(expected_version=workflow.state_version),
    )
    assert plan.status == "planning"
    assert isolated_db.get_workflow(workflow.id).status.value == "planning"

    finish_task(isolated_db, plan_result([
        {
            "code": "S1", "title": "Контракт", "objective": "Зафиксировать контракт",
            "allowed_paths": ["promptpilot/models.py"],
            "deliverables": ["schema"], "acceptance_gates": [],
        },
        {
            "code": "FINAL", "title": "Интеграция",
            "objective": "Проверить результат целиком", "stage_type": "integration",
            "dependencies": ["S1"], "acceptance_gates": [],
        },
    ]))
    waiting = isolated_db.get_workflow(workflow.id)
    assert waiting.status.value == "awaiting_plan_approval"
    stages = isolated_db.list_workflow_stages(workflow.id)
    assert [stage.code for stage in stages] == ["S1", "FINAL"]
    assert all(stage.status.value == "draft" for stage in stages)

    approved = workflows.approve_plan(
        workflow.id,
        WorkflowPlanApproval(expected_version=waiting.state_version),
    )
    assert approved.status.value == "queued"
    assert approved.current_stage_id == stages[0].id
    workflows.advance_workflow(workflow.id)

    executor = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert "Текущий этап: S1 Контракт" in executor.prompt
    assert "promptpilot/models.py" in executor.prompt
    finish_task(isolated_db, "executor S1")
    finish_task(isolated_db, reviewer_pass())

    second = isolated_db.get_workflow(workflow.id)
    assert second.status.value == "executing"
    assert second.current_round == 2
    current_stage = isolated_db.get_workflow_stage(second.current_stage_id)
    assert current_stage.code == "FINAL"
    assert isolated_db.list_workflow_stages(workflow.id)[0].status.value == "completed"

    finish_task(isolated_db, "executor FINAL")
    finish_task(isolated_db, reviewer_pass())
    completed = isolated_db.get_workflow(workflow.id)
    assert completed.status.value == "completed"
    assert all(
        stage.status.value == "completed"
        for stage in isolated_db.list_workflow_stages(workflow.id)
    )
    assert [
        event.event_type
        for event in isolated_db.list_workflow_events(workflow.id, limit=1000)
    ].count("stage.completed") == 2
    report = workflows.workflow_report(workflow.id)
    assert report["schema_version"] == 2
    assert report["metrics"]["planner_attempts"] == 1
    assert report["metrics"]["stages_by_status"] == {"completed": 2}
    assert [item["code"] for item in report["stages"]] == ["S1", "FINAL"]
    assert "## Stage plan" in workflows.workflow_report_markdown(workflow.id)


def test_invalid_planner_output_stops_for_human(isolated_db):
    workflow = create(isolated_db, slug="invalid-plan")
    workflows.dispatch_planner(
        workflow.id,
        WorkflowPlanDispatch(expected_version=workflow.state_version),
    )
    finish_task(isolated_db, "План в свободном тексте")
    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    assert isolated_db.get_workflow_plan(workflow.id).status == "failed"
    assert isolated_db.list_workflow_stages(workflow.id) == []


def test_plan_parser_rejects_forward_dependency():
    report = plan_result([
        {"code": "S1", "title": "one", "objective": "one", "dependencies": ["S2"]},
        {"code": "S2", "title": "two", "objective": "two"},
    ])
    assert workflows.parse_workflow_plan(report) is None


def test_stage_revision_limit_stops_autonomous_ping_pong(isolated_db):
    workflow = create(isolated_db, slug="revision-limit", max_revisions=1)
    plan = workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([
        {"code": "FINAL", "title": "Один этап", "objective": "Завершить этап",
         "stage_type": "integration"},
    ]))
    waiting = isolated_db.get_workflow(workflow.id)
    approved = workflows.approve_plan(
        workflow.id, WorkflowPlanApproval(expected_version=waiting.state_version)
    )
    workflows.advance_workflow(approved.id)

    finish_task(isolated_db, "executor report")
    finish_task(isolated_db, reviewer_revision())

    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    assert stopped.current_round == 1
    assert isolated_db.list_workflow_stages(workflow.id)[0].status.value == "executing"
    events = isolated_db.list_workflow_events(workflow.id, limit=1000)
    assert events[-1].event_type == "limit.stage_revisions"
    assert events[-1].payload["max_revision_rounds"] == 1


def test_plan_requires_final_integration_stage():
    report = plan_result([
        {"code": "S1", "title": "only", "objective": "only"},
    ])
    assert workflows.parse_workflow_plan(report) is None


def test_command_free_plan_can_auto_approve_and_dispatch(isolated_db):
    workflow = create(
        isolated_db, slug="auto-plan", require_approval=False
    )
    plan = workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([{
        "code": "FINAL", "title": "Integration", "objective": "Check all",
        "stage_type": "integration", "acceptance_gates": [],
    }]))

    executing = isolated_db.get_workflow(workflow.id)
    assert executing.status.value == "executing"
    assert isolated_db.get_workflow_plan(workflow.id).status == "approved"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING)[0].id != plan.planner_task_id


def test_llm_gate_commands_force_human_plan_approval(isolated_db):
    workflow = create(
        isolated_db, slug="unsafe-auto-plan", require_approval=False
    )
    plan = workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([{
        "code": "FINAL", "title": "Integration", "objective": "Check all",
        "stage_type": "integration", "acceptance_gates": ["pytest -q"],
    }]))

    waiting = isolated_db.get_workflow(workflow.id)
    assert waiting.status.value == "awaiting_plan_approval"
    assert isolated_db.get_workflow_plan(workflow.id).status == "awaiting_approval"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []


def test_planned_workflow_honors_global_round_budget(isolated_db):
    workflow = create(isolated_db, slug="plan-round-budget", max_rounds=1)
    plan = workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([{
        "code": "FINAL", "title": "Integration", "objective": "Check all",
        "stage_type": "integration",
    }]))
    waiting = isolated_db.get_workflow(workflow.id)
    approved = workflows.approve_plan(
        workflow.id, WorkflowPlanApproval(expected_version=waiting.state_version)
    )
    workflows.advance_workflow(approved.id)
    finish_task(isolated_db, "executor report")
    finish_task(isolated_db, reviewer_revision())

    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    assert isolated_db.list_workflow_events(workflow.id, limit=1000)[-1].event_type == "limit.max_rounds"
