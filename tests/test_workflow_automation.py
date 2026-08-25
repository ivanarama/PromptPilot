from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from promptpilot import workflows
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowStartRequest,
)


def autonomous_config(*, max_rounds=3, commands=None):
    return {
        "schema_version": 1,
        "automation": {"enabled": True},
        "roles": {
            "executor": {"provider": None},
            "reviewer": {"provider": None},
        },
        "gate": {"commands": commands or []},
        "limits": {"max_rounds": max_rounds},
    }


def create(isolated_db, slug="auto", **config_kwargs):
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="Исправлять до независимого PASS",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/auto",
        config=autonomous_config(**config_kwargs),
    ))


def complete_next(isolated_db, result):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def test_autonomous_revision_round_then_pass(isolated_db):
    workflow = create(isolated_db)
    workflows.start_workflow(
        workflow.id, WorkflowStartRequest(expected_version=0)
    )

    started = workflows.advance_workflow(workflow.id)
    assert started.status.value == "executing"

    complete_next(isolated_db, "executor report round 1")
    reviewing = isolated_db.get_workflow(workflow.id)
    assert reviewing.status.value == "reviewing"
    reviewer_task = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert reviewer_task.prompt.find("AUDIT_VERDICT") >= 0

    complete_next(
        isolated_db,
        "AUDIT_FINDINGS_JSON: []\n"
        "AUDIT_VERDICT: REVISION_REQUIRED\n"
        "ИТОГ: ГОТОВО — аудит завершён",
    )
    revision_started = isolated_db.get_workflow(workflow.id)
    assert revision_started.status.value == "executing"
    assert revision_started.current_round == 2
    executor_retry = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert "AUDIT_VERDICT: REVISION_REQUIRED" in executor_retry.prompt

    complete_next(isolated_db, "executor report round 2")
    complete_next(
        isolated_db,
        "AUDIT_FINDINGS_JSON: []\n"
        "AUDIT_VERDICT: PASS\n"
        "ИТОГ: ГОТОВО — аудит завершён",
    )
    completed = isolated_db.get_workflow(workflow.id)
    assert completed.status.value == "completed"

    event_types = [
        event.event_type
        for event in isolated_db.list_workflow_events(workflow.id, limit=1000)
    ]
    assert event_types.count("gate.passed") == 2
    assert "review.revision_required" in event_types
    assert event_types[-1] == "review.passed"

    report = workflows.workflow_report(workflow.id)
    assert report["metrics"]["rounds_promptpilot"] == 2
    assert report["metrics"]["gate_passes"] == 2
    assert report["metrics"]["review_revisions"] == 1
    assert report["events"][-1]["event_type"] == "review.passed"
    markdown = workflows.workflow_report_markdown(workflow.id)
    assert "# Workflow report: auto" in markdown
    assert "## Provenance timeline" in markdown


def test_failed_automatic_gate_stops_at_round_budget(monkeypatch, isolated_db):
    workflow = create(
        isolated_db, slug="auto-gate-fail", max_rounds=1,
        commands=["pytest -q"],
    )
    workflows.start_workflow(
        workflow.id, WorkflowStartRequest(expected_version=0)
    )
    workflows.advance_workflow(workflow.id)
    monkeypatch.setattr(
        workflows.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="failed test", stderr=""
        ),
    )

    complete_next(isolated_db, "executor report")
    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    event_types = [
        event.event_type
        for event in isolated_db.list_workflow_events(workflow.id, limit=1000)
    ]
    assert "gate.failed" in event_types
    assert event_types[-1] == "limit.max_rounds"


def test_invalid_reviewer_contract_pauses_for_human(isolated_db):
    workflow = create(isolated_db, slug="bad-review")
    workflows.start_workflow(
        workflow.id, WorkflowStartRequest(expected_version=0)
    )
    workflows.advance_workflow(workflow.id)
    complete_next(isolated_db, "executor report")
    complete_next(isolated_db, "Свободный текст без машинного вердикта")

    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    assert isolated_db.list_workflow_events(workflow.id)[-1].event_type == (
        "automation.paused"
    )


def test_workflow_config_rejects_unrestricted_reviewer():
    with pytest.raises(ValidationError, match="reviewer cannot use skip_permissions"):
        WorkflowCreate(
            slug="unsafe-reviewer",
            objective="x",
            repository_path="x",
            candidate_branch="x",
            config={"roles": {"reviewer": {"skip_permissions": True}}},
        )


def test_reviewer_report_parser_accepts_structured_findings():
    parsed = workflows.parse_reviewer_report(
        'AUDIT_FINDINGS_JSON: [{"fingerprint":"f1","severity":"high",'
        '"category":"runtime","title":"Broken","status":"open",'
        '"payload":{"path":"x"}}]\nAUDIT_VERDICT: REVISION_REQUIRED'
    )
    assert parsed is not None
    assert parsed.verdict.value == "REVISION_REQUIRED"
    assert parsed.findings[0].severity.value == "high"
