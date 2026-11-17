"""Actual native receipts/round transitions projected without writes or claims."""
import asyncio
import json

from fastapi import FastAPI
import httpx
import pytest

from promptpilot import db, external_inbox, workflow_progress as progress, workflows
from promptpilot.models import (WorkflowCreate, WorkflowGateDecision, WorkflowHumanInput,
    WorkflowPlanApproval, WorkflowPlanReplace, WorkflowStageSpec, WorkflowTaskDispatch)


def tool(app, method, params, session=None):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post("/api/external-clients/test/mcp",
                headers={"Mcp-Session-Id": session} if session else {},
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            assert response.status_code == 200
            return response
    return asyncio.run(send())


@pytest.fixture
def waiting(isolated_db):
    workflow = db.create_workflow(WorkflowCreate(slug="progress", objective="Improve documentation",
        repository_path=str(isolated_db.DB_DIR), candidate_branch="main", config={
            "automation": {"enabled": False}, "gate": {"enabled": True, "commands": ["echo verify"]},
            "external_executor": {"client_id": "test", "label": "ZCode", "advance_after_submit": False},
            "roles": {"reviewer": {"provider": "codex"}},
            "external_result_policy": {"artifacts": ["changes.patch", "RESULT.md"]}}))
    workflows.provide_plan(workflow.id, WorkflowPlanReplace(expected_version=workflow.state_version,
        stages=[WorkflowStageSpec(code="docs", title="Documentation", objective="Propose changes",
            stage_type="integration", execution_mode="external", deliverables=["changes.patch", "RESULT.md"])]))
    workflow = db.get_workflow(workflow.id)
    workflow = workflows.approve_plan(workflow.id, WorkflowPlanApproval(expected_version=workflow.state_version))
    workflow = workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(expected_version=workflow.state_version,
        role="executor", execution_mode="external", prompt="Return the declared files.")).workflow
    app = FastAPI()
    app.include_router(external_inbox.router)
    session = tool(app, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "fixture", "version": "1"}}).headers["Mcp-Session-Id"]
    return workflow.id, app, session


def submit_wrong_paths(waiting, *, record=True):
    workflow_id, app, session = waiting
    claim = tool(app, "tools/call", {"name": "pilot_next_task", "arguments": {}}, session).json()["result"]["structuredContent"]
    submitted = tool(app, "tools/call", {"name": "pilot_submit_result", "arguments": {
        "job_id": claim["job_id"], "claim_token": claim["claim_token"], "outcome": "completed",
        "summary": "Proposed documentation changes.", "artifacts": [
            {"path": ".pp-run/changes.patch", "text": "proposed patch"},
            {"path": ".pp-run/RESULT.md", "text": "unreviewed report"}]}}, session).json()["result"]
    assert not submitted["isError"]
    workflow = db.get_workflow(workflow_id)
    if not record:
        return workflow
    return workflows.record_gate(workflow_id, WorkflowGateDecision(expected_version=workflow.state_version,
        verdict="FAIL", summary="Command exited with code 2", evidence=["opaque legacy stderr"]))


def next_round(workflow):
    workflow = workflows.human_input(workflow.id, WorkflowHumanInput(expected_version=workflow.state_version,
        resume=True, text="Continue correcting the same stage."))
    return workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(expected_version=workflow.state_version,
        role="executor", execution_mode="external", prompt="Correct the previous receipt.")).workflow


def test_progress_read_does_not_claim_or_change_database(waiting):
    workflow_id, _, _ = waiting
    with db._connect() as conn:
        before = list(conn.iterdump())
    value = progress.get_progress(workflow_id)
    assert value["current"]["round_no"] == 1
    assert value["current"]["stage_index"] == value["current"]["stage_total"] == 1
    assert value["current"]["stage_title"] == "Documentation"
    assert "заберёт" in value["current"]["headline"]
    assert [s["state"] for s in value["steps"]] == ["done", "current", "waiting", "waiting", "waiting"]
    with db._connect() as conn:
        assert list(conn.iterdump()) == before


def test_actual_claim_is_shown_as_pickup_not_observed_execution(waiting):
    workflow_id, app, session = waiting
    tool(app, "tools/call", {"name": "pilot_next_task", "arguments": {}}, session)
    value = progress.get_progress(workflow_id)
    assert "забрал" in value["current"]["headline"]
    assert "не доказывают работу модели" in value["current"]["detail"]


def test_uncertain_submission_never_looks_like_active_model_work(waiting):
    workflow_id, app, session = waiting
    tool(app, "tools/call", {"name": "pilot_next_task", "arguments": {}}, session)
    with db._connect(immediate=True) as conn:
        conn.execute("UPDATE external_inbox_claims_v1 SET state='unknown'")
    value = progress.get_progress(workflow_id)
    assert "сверить" in value["current"]["headline"]
    assert "забрал" not in value["current"]["headline"]


def test_manual_gate_is_presented_as_owner_action(waiting):
    workflow = submit_wrong_paths(waiting, record=False)
    value = progress.get_progress(workflow.id)
    assert value["current"]["status"] == "gating"
    assert value["current"]["owner"] == "Вы"
    assert "зафиксировать" in value["current"]["headline"]


def test_external_stage_without_mcp_binding_still_has_normal_submission_action(waiting):
    workflow_id = waiting[0]
    with db._connect(immediate=True) as conn:
        configuration = json.loads(conn.execute("SELECT config_json FROM workflows WHERE id=?", (workflow_id,)).fetchone()[0])
        configuration.pop("external_executor", None)
        conn.execute("UPDATE workflows SET config_json=? WHERE id=?", (json.dumps(configuration), workflow_id))
    value = progress.get_progress(workflow_id)
    assert value["current"]["headline"] == "Ждём результат внешнего этапа"
    assert "карточке" in value["current"]["next_action"]


def test_round_two_retains_return_reason_and_does_not_reuse_old_pickup(waiting):
    first = submit_wrong_paths(waiting)
    workflow = next_round(first)
    value = progress.get_progress(workflow.id)
    assert value["current"]["round_no"] == 2 and "заберёт доработку" in value["current"]["headline"]
    reason = value["last_return"]
    assert reason["round_no"] == 1 and reason["kind"] == "artifact_paths_mismatch"
    assert reason["basis"] == "current_artifact_policy"
    assert reason["expected_paths"] == ["RESULT.md", "changes.patch"]
    assert reason["received_paths"] == [".pp-run/RESULT.md", ".pp-run/changes.patch"]
    assert value["history"][1]["status"] == "revision_required"
    assert value["steps"][2]["state"] == "waiting"
    assert not any(key in json.dumps(value) for key in ["claim_token", "session_id", "assignment", "repository_path"])


@pytest.mark.parametrize("damage", ["artifact", "receipt", "policy"])
def test_legacy_comparison_requires_receipt_integrity_and_matching_stage_policy(waiting, damage):
    workflow = submit_wrong_paths(waiting)
    with db._connect(immediate=True) as conn:
        if damage == "artifact":
            conn.execute("UPDATE external_inbox_artifacts_v1 SET sha256=?", ("0" * 64,))
        elif damage == "receipt":
            conn.execute("UPDATE external_inbox_claims_v1 SET expected_output_sha=?", ("0" * 64,))
        else:
            row = conn.execute("SELECT config_json FROM workflows WHERE id=?", (workflow.id,)).fetchone()
            configuration = json.loads(row[0])
            configuration["external_result_policy"]["artifacts"] = ["different.md"]
            conn.execute("UPDATE workflows SET config_json=? WHERE id=?", (json.dumps(configuration), workflow.id))
    reason = progress.get_progress(workflow.id)["last_return"]
    assert reason["kind"] == "gate_failed" and "received_paths" not in reason


def test_recorded_pass_supersedes_prior_return_in_current_stage(waiting):
    workflow = next_round(submit_wrong_paths(waiting))
    app, session = waiting[1:]
    claim = tool(app, "tools/call", {"name": "pilot_next_task", "arguments": {}}, session).json()["result"]["structuredContent"]
    reply = tool(app, "tools/call", {"name": "pilot_submit_result", "arguments": {
        "job_id": claim["job_id"], "claim_token": claim["claim_token"], "outcome": "completed",
        "summary": "Correctly named receipt.", "artifacts": [{"path": "changes.patch", "text": "patch"},
            {"path": "RESULT.md", "text": "report"}]}}, session).json()["result"]
    assert not reply["isError"]
    workflow = db.get_workflow(workflow.id)
    workflows.record_gate(workflow.id, WorkflowGateDecision(expected_version=workflow.state_version,
        verdict="PASS", summary="Owner's configured check passed"))
    value = progress.get_progress(workflow.id)
    assert value["last_return"] is None
    assert value["current"]["owner"] == "codex" and value["current"]["status"] == "reviewing"
    assert [s["state"] for s in value["steps"]] == ["done", "done", "done", "current", "waiting"]


def test_progress_missing_database_never_creates_it(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "absent" / "db.sqlite3")
    with pytest.raises(progress.ProgressUnavailable):
        progress.get_progress("wf_" + "a" * 32)
    assert not (tmp_path / "absent").exists()


def test_diagnostic_parser_accepts_only_known_schema_code_and_relative_names():
    value = {"schema": "external-result-error-v1", "code": "artifact_paths_mismatch",
             "expected_paths": ["changes.patch"], "received_paths": [".pp-run/changes.patch"]}
    parsed = progress.external_diagnostic("unrelated private stderr\n" + json.dumps(value))
    assert parsed["kind"] == "artifact_paths_mismatch"
    assert "private" not in json.dumps(parsed)
    value["received_paths"] = ["../private"]
    assert progress.external_diagnostic(json.dumps(value)) is None
    assert progress.external_diagnostic('{"schema":"external-result-error-v1","code":"arbitrary"}') is None


def test_cancellation_before_first_round_does_not_imply_plan_was_approved(waiting):
    workflow = db.create_workflow(WorkflowCreate(slug="never-started", objective="A draft",
        repository_path=str(db.DB_DIR), candidate_branch="main"))
    workflows.cancel_workflow(workflow.id, expected_version=workflow.state_version)
    value = progress.get_progress(workflow.id)
    assert value["current"]["round_no"] == 0
    assert [s["state"] for s in value["steps"]] == ["blocked", "waiting", "waiting", "waiting", "waiting"]


