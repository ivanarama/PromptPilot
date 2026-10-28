"""Owner budgets override model stage pins; absence remains a durable distinction."""
import json

import pytest

from promptpilot import workflows
from promptpilot.models import WorkflowCreate, WorkflowPlanApproval, WorkflowPlanReplace, WorkflowStageSpec


def finish(db, text):
    task = db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    db.mark_completed(task.id, text, exit_code=0)
    db.set_verdict(task.id, "\u0413\u041e\u0422\u041e\u0412\u041e")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)


@pytest.mark.parametrize("owner,pin,limit", [(2, 1, 2), (1, 5, 1), (None, 1, 1), (None, None, 3)])
def test_planned_effective_budget_survives_normalization_and_reload(isolated_db, owner, pin, limit):
    config = {"planning": {"enabled": True}, "automation": {"enabled": True},
              "gate": {"enabled": False}, "limits": {"max_rounds": 12}}
    if owner is not None:
        config["planning"]["max_revisions_per_stage"] = owner
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="budget", objective="Task", repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/budget", config=config))
    with isolated_db._connect() as conn:
        stored = json.loads(conn.execute("SELECT config_json FROM workflows WHERE id=?", (workflow.id,)).fetchone()[0])
    assert ("max_revisions_per_stage" in stored["planning"]) is (owner is not None)
    assert ("max_revisions_per_stage" in isolated_db.get_workflow(workflow.id).config["planning"]) is (owner is not None)
    workflows.provide_plan(workflow.id, WorkflowPlanReplace(expected_version=workflow.state_version, stages=[
        WorkflowStageSpec(code="FINAL", title="Final", objective="Complete", stage_type="integration", max_revision_rounds=pin)]))
    workflow = isolated_db.get_workflow(workflow.id)
    workflows.approve_plan(workflow.id, WorkflowPlanApproval(expected_version=workflow.state_version))
    workflows.advance_workflow(workflow.id)
    revision = 'AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: REVISION_REQUIRED'
    for round_no in range(1, limit + 1):
        finish(isolated_db, "executor report")
        finish(isolated_db, revision)
        current = isolated_db.get_workflow(workflow.id)
        if round_no < limit:
            assert current.status.value == "executing"
            assert current.current_round == round_no + 1
    assert current.status.value == "awaiting_human"
    assert current.current_round == limit
    events = isolated_db.list_workflow_events(workflow.id)
    assert events[-1].event_type == "limit.stage_revisions"
    assert events[-1].payload["max_revision_rounds"] == limit


@pytest.mark.parametrize("owner,pin,limit", [(2, 1, 2), (1, 5, 1), (None, 1, 1)])
def test_planless_budget_uses_same_owner_precedence(isolated_db, owner, pin, limit):
    from promptpilot.models import WorkflowStartRequest
    config = {"stage": {"max_revision_rounds": pin}, "automation": {"enabled": True},
              "gate": {"enabled": False}, "limits": {"max_rounds": 10}}
    if owner is not None:
        config["planning"] = {"max_revisions_per_stage": owner}
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="planless", objective="Task", repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/budget", config=config))
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=workflow.state_version))
    workflows.advance_workflow(workflow.id)
    for round_no in range(1, limit + 1):
        finish(isolated_db, "Executor result")
        finish(isolated_db, "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: REVISION_REQUIRED")
        current = isolated_db.get_workflow(workflow.id)
        if round_no < limit:
            assert current.status.value == "executing"
    assert current.status.value == "awaiting_human" and current.current_round == limit


def test_owner_can_raise_budget_and_resume_a_stage_pinned_by_the_model(isolated_db):
    from promptpilot.models import WorkflowHumanInput, WorkflowUpdate
    config = {"planning": {"enabled": True, "max_revisions_per_stage": 1},
              "automation": {"enabled": True}, "gate": {"enabled": False}, "limits": {"max_rounds": 6}}
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="raise-budget", objective="Task", repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/budget", config=config))
    workflows.provide_plan(workflow.id, WorkflowPlanReplace(expected_version=workflow.state_version, stages=[
        WorkflowStageSpec(code="FINAL", title="Final", objective="Complete", stage_type="integration", max_revision_rounds=1)]))
    waiting = isolated_db.get_workflow(workflow.id)
    workflows.approve_plan(workflow.id, WorkflowPlanApproval(expected_version=waiting.state_version))
    workflows.advance_workflow(workflow.id)
    finish(isolated_db, "Executor result")
    finish(isolated_db, "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: REVISION_REQUIRED")
    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    changed_config = dict(stopped.config)
    changed_config["planning"] = dict(changed_config["planning"], max_revisions_per_stage=2)
    isolated_db.update_workflow(workflow.id, WorkflowUpdate(expected_version=stopped.state_version, config=changed_config))
    reloaded = isolated_db.get_workflow(workflow.id)
    resumed = workflows.human_input(workflow.id, WorkflowHumanInput(expected_version=reloaded.state_version, resume=True, text="Continue within raised owner budget"))
    assert resumed.status.value == "queued" and resumed.current_round == 2
