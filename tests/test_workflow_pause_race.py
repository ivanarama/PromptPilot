"""A stale automatic reconciliation cannot undo a newer operator decision."""
import asyncio

import httpx
import pytest

from promptpilot import workflows
from promptpilot.api import app
from promptpilot.models import WorkflowCreate, WorkflowStartRequest


def request(method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def awaiting_review(db, tmp_path):
    item = db.create_workflow(WorkflowCreate(
        slug="pause-race", objective="goal", repository_path=str(tmp_path),
        candidate_branch="feature/test", config={"automation": {"enabled": True},
                                                   "gate": {"enabled": False}}))
    workflows.start_workflow(item.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(item.id)
    for output in ["executor complete", "review lacks its required contract"]:
        task = db.get_next_runnable()
        assert task is not None
        workflows.sync_task(task.id)
        db.mark_completed(task.id, output, exit_code=0)
        db.set_verdict(task.id, "ГОТОВО")
        workflows.sync_task(task.id)
        if output == "executor complete":
            workflows.advance_linked_task(task.id)
    result = db.get_workflow(item.id)
    assert result.status.value == "awaiting_human"
    return result


@pytest.mark.parametrize("fail", [False, True])
def test_operator_resume_during_reconciliation_is_not_paused_again(
        isolated_db, tmp_path, monkeypatch, fail):
    workflow = awaiting_review(isolated_db, tmp_path)
    resumed = []

    def parse_after_resume(_report):
        response = request("POST", f"/api/workflows/{workflow.id}/human-input", json={
            "expected_version": workflow.state_version, "text": "Retry with a new executor",
            "resume": True})
        assert response.status_code == 200
        resumed.append(response.json())
        if fail:
            raise RuntimeError("old reconciliation failed after resume")
        return None

    monkeypatch.setattr(workflows, "parse_reviewer_report", parse_after_resume)
    response = request("POST", f"/api/workflows/{workflow.id}/advance")
    assert response.status_code == 200
    assert len(resumed) == 1
    assert response.json()["status"] == "queued"
    current = isolated_db.get_workflow(workflow.id)
    assert current.state_version == resumed[0]["state_version"]
    assert current.status.value == "queued"
    events = isolated_db.list_workflow_events(workflow.id, limit=1000)
    assert events[-1].event_type == "human.resumed"
    assert not any(event.event_type == "automation.paused" for event in events)


def test_unchanged_invalid_report_is_paused_once_without_touching_every_poll(
        isolated_db, tmp_path):
    workflow = awaiting_review(isolated_db, tmp_path)
    first = request("POST", f"/api/workflows/{workflow.id}/advance")
    assert first.status_code == 200
    assert first.json()["status"] == "awaiting_human"
    second = request("POST", f"/api/workflows/{workflow.id}/advance")
    assert second.status_code == 200
    assert second.json()["state_version"] == first.json()["state_version"]
    events = isolated_db.list_workflow_events(workflow.id, limit=1000)
    assert sum(event.event_type == "automation.paused" for event in events) == 1


def test_a_new_invalid_reviewer_after_resume_still_records_its_own_pause(
        isolated_db, tmp_path):
    workflow = awaiting_review(isolated_db, tmp_path)
    first = request("POST", f"/api/workflows/{workflow.id}/advance").json()
    resumed = request("POST", f"/api/workflows/{workflow.id}/human-input", json={
        "expected_version": first["state_version"], "text": "Retry the review", "resume": True})
    assert resumed.status_code == 200
    for output in ["new executor report", "new invalid reviewer report"]:
        response = request("POST", f"/api/workflows/{workflow.id}/advance")
        assert response.status_code == 200
        task = isolated_db.get_next_runnable()
        assert task is not None
        workflows.sync_task(task.id)
        isolated_db.mark_completed(task.id, output, exit_code=0)
        isolated_db.set_verdict(task.id, "ГОТОВО")
        workflows.sync_task(task.id)
    second = request("POST", f"/api/workflows/{workflow.id}/advance")
    assert second.status_code == 200
    assert second.json()["status"] == "awaiting_human"
    assert second.json()["state_version"] > first["state_version"]
    events = isolated_db.list_workflow_events(workflow.id, limit=1000)
    assert sum(event.event_type == "automation.paused" for event in events) == 2


def test_zero_action_budget_uses_an_observed_version(isolated_db, tmp_path):
    workflow = awaiting_review(isolated_db, tmp_path)
    stopped = workflows.advance_workflow(workflow.id, max_actions=0)
    assert stopped.status.value == "awaiting_human"
    latest = isolated_db.list_workflow_events(workflow.id, limit=1000)[-1]
    assert latest.event_type == "automation.paused"
    assert "0" in latest.payload["reason"]
