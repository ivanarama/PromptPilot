import copy
import json

import pytest

from promptpilot import db, workorder_guard, workflows
from promptpilot.models import WorkflowCreate, WorkflowTaskDispatch, WorkflowStartRequest
from promptpilot.workorder_api import EnsureWorkflow, ensure_workflow
from fastapi import HTTPException


def request_data():
    return {"request_id": "run-example-1", "workflow": {
        "slug": "order-example-1", "objective": "Approved objective",
        "repository_path": "D:/sandbox/test", "candidate_branch": "master",
        "config": {"automation": {"enabled": False}}}}


def test_workorder_create_is_idempotent_and_compares_original_inputs(isolated_db):
    payload = request_data()
    first = ensure_workflow(EnsureWorkflow(**payload))
    second = ensure_workflow(EnsureWorkflow(**payload))
    assert first.id == second.id
    assert len(db.list_workflows()) == 1
    changed = copy.deepcopy(payload)
    changed["workflow"]["objective"] = "Another objective"
    with pytest.raises(HTTPException) as conflict:
        ensure_workflow(EnsureWorkflow(**changed))
    assert conflict.value.status_code == 409


def guarded_task():
    payload = request_data()["workflow"]
    payload["config"]["workorder_guard"] = {"site_url": "http://localhost:8000", "run_id": "run-id", "worker_key": "worker", "lease_token": "lease"}
    workflow = db.create_workflow(WorkflowCreate(**payload))
    workflow = workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=workflow.state_version))
    dispatch = workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(expected_version=workflow.state_version,
        role="executor", prompt="Approved", max_retries=0, task_timeout=60))
    return dispatch.task


def test_guard_fails_closed_without_secret(isolated_db, monkeypatch):
    task = guarded_task()
    monkeypatch.delenv("WORKORDERS_WORKER_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        workorder_guard.check_task(task.id, reserve=True)


def test_guard_reserves_task_identity_and_checks_exact_site(isolated_db, monkeypatch):
    task = guarded_task()
    calls = []
    monkeypatch.setenv("WORKORDERS_WORKER_TOKEN", "worker-token")
    monkeypatch.setenv("WORKORDERS_SITE_URL", "http://localhost:8000")

    class Response:
        def __init__(self, url):
            self.url = url

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, size):
            return b'{"approved":true}'

    def urlopen(request, timeout):
        calls.append(request)
        return Response(request.full_url)

    monkeypatch.setattr(workorder_guard, "_open", urlopen)
    workorder_guard.check_task(task.id, reserve=True)
    assert calls[0].full_url.endswith("/runs/run-id/reserve/")
    assert json.loads(calls[0].data)["stage_key"] == f"task:{task.id}:pending"
    monkeypatch.setenv("WORKORDERS_SITE_URL", "https://another-host")
    with pytest.raises(RuntimeError):
        workorder_guard.check_task(task.id)


def test_ordinary_task_has_no_external_guard(isolated_db):
    assert workorder_guard.configuration(99999) is None
    workorder_guard.check_task(99999)


def test_guarded_workflow_disallows_unbounded_dispatch(isolated_db):
    guarded_task()
    workflow = db.list_workflows()[0]
    with pytest.raises(db.WorkflowConflictError):
        workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(expected_version=workflow.state_version,
            role="executor", prompt="Unsafe", max_retries=5, skip_permissions=True))


def test_guard_does_not_swallow_worker_ownership_errors(isolated_db, monkeypatch):
    from contextlib import nullcontext
    from promptpilot import worker

    task = guarded_task()
    monkeypatch.setattr(workorder_guard, "watch_task", lambda *args: nullcontext())

    class OwnershipFailure(RuntimeError):
        pass

    def execute(*args):
        raise OwnershipFailure("process cleanup is not confirmed")

    monkeypatch.setattr(worker, "_execute_task_inner", execute)
    with pytest.raises(OwnershipFailure):
        worker.execute_task(task)


def test_network_failure_is_an_authorization_refusal(isolated_db, monkeypatch):
    task = guarded_task()
    monkeypatch.setenv("WORKORDERS_WORKER_TOKEN", "worker-token")
    monkeypatch.setenv("WORKORDERS_SITE_URL", "http://localhost:8000")

    def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(workorder_guard, "_open", unavailable)
    with pytest.raises(workorder_guard.WorkorderAuthorizationError):
        workorder_guard.check_task(task.id, reserve=True)
