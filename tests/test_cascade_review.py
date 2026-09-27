from pathlib import Path

from promptpilot import workflows
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowHumanInput,
    WorkflowStartRequest,
)
from tools.addons.cascade_review import controller as cascade_module
from tools.addons.cascade_review.controller import CascadeController


def _chain_config():
    return {
        "automation": {
            "enabled": True,
            "auto_dispatch_executor": True,
            "auto_gate": True,
            "auto_dispatch_reviewer": False,
            "auto_apply_review": False,
            "auto_resume_revision": False,
        },
        "roles": {
            "executor": {"provider": "mmx-m3"},
            "reviewer": {"provider": "goose-zai"},
        },
        "review_chain": {
            "enabled": True,
            "base_executor_provider": "mmx-m3",
            "base_reviewer_provider": "goose-zai",
            "steps": [
                {"provider": "goose-zai", "fixer": "self", "max_rounds": 1},
                {"provider": "qwen-review", "fixer": "goose-zai", "max_rounds": 2},
            ],
        },
        # The core's single-reviewer budget is intentionally lower than the
        # configured slot budget.  The additive controller must own the
        # cascade retry count so the second qwen-review revision still runs.
        "stage": {"max_revision_rounds": 1},
        "gate": {"commands": []},
    }


def _finish_next(db, result: str):
    task = db.list_tasks(status=TaskStatus.PENDING)[0]
    workflows.sync_task(task.id)
    db.mark_completed(task.id, result, exit_code=0)
    db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def test_cascade_routes_slots_and_configured_fixer(isolated_db, tmp_path: Path):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="cascade-routing",
        objective="Проверить каскад ревью",
        repository_path=str(tmp_path),
        candidate_branch="workflow/reader",
        config=_chain_config(),
    ))
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    assert workflows.advance_workflow(workflow.id).status.value == "executing"

    controller = CascadeController(
        state_path=tmp_path / "cascade-state.json",
        log_path=tmp_path / "cascade.log",
    )

    executor = _finish_next(isolated_db, "implementation round 1")
    assert executor.provider == "mmx-m3"
    assert isolated_db.get_workflow(workflow.id).status.value == "reviewing"

    first_review = controller.process_workflow(workflow.id)
    assert first_review.status.value == "reviewing"
    first_reviewer = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert first_reviewer.provider == "goose-zai"

    _finish_next(
        isolated_db,
        "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО",
    )
    second_review = controller.process_workflow(workflow.id)
    assert second_review.status.value == "reviewing"
    second_reviewer = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert second_reviewer.provider == "qwen-review"

    _finish_next(
        isolated_db,
        'AUDIT_FINDINGS_JSON: [{"fingerprint":"f2","severity":"high",'
        '"category":"runtime","title":"Broken","status":"open",'
        '"payload":{"path":"x"}}]\nAUDIT_VERDICT: REVISION_REQUIRED\n'
        "ИТОГ: ГОТОВО",
    )
    # Reproduce a restart after core automation already recorded the review
    # and hit its single-reviewer stage limit.  The controller must recover the
    # existing decision without writing the same finding event twice.
    recorded_workflow = isolated_db.get_workflow(workflow.id)
    recorded_decision = workflows.parse_reviewer_report(
        'AUDIT_FINDINGS_JSON: [{"fingerprint":"f2","severity":"high",'
        '"category":"runtime","title":"Broken","status":"open",'
        '"payload":{"path":"x"}}]\nAUDIT_VERDICT: REVISION_REQUIRED\n'
    )
    assert recorded_decision is not None
    recorded_workflow = workflows.record_review(
        workflow.id,
        recorded_decision.model_copy(update={
            "expected_version": recorded_workflow.state_version,
        }),
    )
    limited = workflows.human_input(
        workflow.id,
        WorkflowHumanInput(
            expected_version=recorded_workflow.state_version,
            text="core single-reviewer limit",
            resume=True,
        ),
    )
    assert limited.status.value == "awaiting_human"

    revision = controller.process_workflow(workflow.id)
    assert revision.status.value == "executing"
    fixer = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert fixer.provider == "goose-zai"

    _finish_next(isolated_db, "fixed by the configured reviewer provider")
    controller.process_workflow(workflow.id)
    retry_reviewer = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert retry_reviewer.provider == "qwen-review"

    _finish_next(
        isolated_db,
        'AUDIT_FINDINGS_JSON: [{"fingerprint":"f2","severity":"high",'
        '"category":"runtime","title":"Still broken","status":"open",'
        '"payload":{"verified":false}}]\nAUDIT_VERDICT: REVISION_REQUIRED\n'
        "ИТОГ: ГОТОВО",
    )
    revision_again = controller.process_workflow(workflow.id)
    assert revision_again.status.value == "executing"
    fixer_again = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert fixer_again.provider == "goose-zai"

    _finish_next(isolated_db, "fixed by the configured reviewer provider again")
    controller.process_workflow(workflow.id)
    retry_reviewer_again = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert retry_reviewer_again.provider == "qwen-review"

    completed = _finish_next(
        isolated_db,
        'AUDIT_FINDINGS_JSON: [{"fingerprint":"f2","severity":"high",'
        '"category":"runtime","title":"Broken","status":"resolved",'
        '"payload":{"verified":true}}]\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО',
    )
    assert completed.provider == "qwen-review"
    controller.process_workflow(workflow.id)
    assert isolated_db.get_workflow(workflow.id).status.value == "completed"

    events = [
        event.event_type
        for event in isolated_db.list_workflow_events(workflow.id, limit=1000)
    ]
    assert "cascade.slot_passed" in events
    assert "cascade.revision_requested" in events
    assert "review.passed" in events
    assert all(task.provider in {"mmx-m3", "goose-zai", "qwen-review"}
               for task in isolated_db.list_tasks())


def test_windowed_slot_waits_without_manual_intervention(
    isolated_db, tmp_path: Path, monkeypatch
):
    config = _chain_config()
    config["review_chain"]["steps"] = [
        {
            "provider": "goose-zai",
            "fixer": "self",
            "max_rounds": 1,
            "window": {"from": "22:00", "to": "04:00", "tz_offset_hours": 3},
        }
    ]
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="cascade-window-wait",
        objective="Проверить ожидание окна",
        repository_path=str(tmp_path),
        candidate_branch="workflow/reader",
        config=config,
    ))
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    _finish_next(isolated_db, "implementation")

    controller = CascadeController(
        state_path=tmp_path / "cascade-window-state.json",
        log_path=tmp_path / "cascade-window.log",
    )
    monkeypatch.setattr(cascade_module, "in_window", lambda _slot: False)

    waiting = controller.process_workflow(workflow.id)

    assert waiting.status.value == "reviewing"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []
