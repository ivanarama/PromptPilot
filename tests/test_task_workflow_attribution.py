"""Task cards must describe the task's own run, including historical stages."""
import asyncio
import sqlite3

import httpx

from promptpilot import workflows
from promptpilot.api import app
from promptpilot.models import (
    TaskCreate, WorkflowCreate, WorkflowPlanApproval, WorkflowPlanDispatch,
    WorkflowPlanReplace, WorkflowStageSpec, WorkflowRoundCreate, WorkflowRunCreate,
)


def get(path):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://127.0.0.1:8420") as client:
            return await client.get(path)
    return asyncio.run(send())


def finish(db, result):
    task = db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    db.mark_completed(task.id, result, verdict="ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def test_task_api_keeps_historical_stage_and_uses_actual_planner_link(isolated_db, tmp_path):
    config = {"planning": {"enabled": True}, "automation": {"enabled": True},
              "gate": {"enabled": False}, "limits": {"max_rounds": 6}}
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="task-history", objective="two stages", repository_path=str(tmp_path),
        candidate_branch="main", config=config,
    ))
    stages = workflows.provide_plan(workflow.id, WorkflowPlanReplace(expected_version=0, stages=[
        WorkflowStageSpec(code="FIRST", title="First <stage>", objective="first"),
        WorkflowStageSpec(code="FINAL", title="Final stage", objective="final",
                          stage_type="integration", dependencies=["FIRST"]),
    ]))
    workflows.approve_plan(workflow.id, WorkflowPlanApproval(
        expected_version=isolated_db.get_workflow(workflow.id).state_version))
    workflows.advance_workflow(workflow.id)
    first_executor = finish(isolated_db, "first executor complete")
    first_reviewer = finish(isolated_db, "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS")
    current = isolated_db.get_workflow(workflow.id)
    assert current.current_stage_id == stages[1].id
    current_executor = next(task for task in isolated_db.list_tasks() if task.status.value == "pending")
    unrelated = isolated_db.create_task(TaskCreate(
        prompt="reviewer FIRST FINAL", working_dir=str(tmp_path)))
    planner_workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="task-planner", objective="planning", repository_path=str(tmp_path),
        candidate_branch="main", config=config,
    ))
    planner = workflows.dispatch_planner(planner_workflow.id, WorkflowPlanDispatch(expected_version=0))
    all_tasks = {item["id"]: item for item in get("/api/tasks?limit=100").json()}
    for task, role, stage, round_no in (
        (first_executor, "executor", stages[0], 1),
        (first_reviewer, "reviewer", stages[0], 1),
        (current_executor, "executor", stages[1], 2),
    ):
        item = all_tasks[task.id]
        assert (item["workflow_id"], item["role"]) == (workflow.id, role)
        assert item["workflow_stage_id"] == stage.id
        assert item["workflow_stage_code"] == stage.code
        assert item["workflow_stage_title"] == stage.title
        assert item["workflow_round_no"] == round_no
        single = get(f"/api/tasks/{task.id}").json()
        for field in ("workflow_id", "role", "workflow_stage_id", "workflow_stage_code",
                      "workflow_stage_title", "workflow_round_no"):
            assert single[field] == item[field]
    assert all_tasks[unrelated.id]["workflow_id"] is None
    assert all_tasks[unrelated.id]["role"] is None
    assert all_tasks[unrelated.id]["workflow_stage_id"] is None
    planning = all_tasks[planner.planner_task_id]
    assert (planning["workflow_id"], planning["role"]) == (planner_workflow.id, "planner")
    assert planning["workflow_stage_id"] is planning["workflow_round_no"] is None


def test_single_task_read_does_not_scan_unrelated_history_or_rank_the_queue(
        isolated_db, tmp_path, monkeypatch):
    from promptpilot import worker
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="old-history", objective="history", repository_path=str(tmp_path), candidate_branch="main"))
    round_data = isolated_db.create_workflow_round(WorkflowRoundCreate(
        workflow_id=workflow.id, round_no=1))
    # Deleted tasks may legitimately leave old workflow_runs records behind.
    with isolated_db._connect() as connection:
        connection.executemany(
            """INSERT INTO workflow_runs(id,workflow_id,round_id,role,attempt_no,task_id,input_sha256)
               VALUES (?,?,?,'executor',?,?,'old')""",
            ((f"old-run-{i}", workflow.id, round_data.id, i, 10000+i) for i in range(5000)),
        )
    task = isolated_db.create_task(TaskCreate(prompt="point read"))
    original_connect = sqlite3.connect
    work = [0]

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)

        def budget():
            work[0] += 1000
            return int(work[0] > 5000)

        connection.set_progress_handler(budget, 1000)
        return connection

    def no_queue_scan():
        raise AssertionError("point task reads must not rank the entire runnable queue")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(worker, "admission_snapshot", no_queue_scan)
    response = get(f"/api/tasks/{task.id}")
    assert response.status_code == 200
    assert response.json()["workflow_id"] is None


def test_ambiguous_task_run_links_remain_unattributed(isolated_db, tmp_path):
    task = isolated_db.create_task(TaskCreate(prompt="ambiguous stored link"))
    for number, role in enumerate(("executor", "reviewer")):
        workflow = isolated_db.create_workflow(WorkflowCreate(
            slug=f"ambiguous-{number}", objective="link", repository_path=str(tmp_path),
            candidate_branch="main"))
        round_data = isolated_db.create_workflow_round(WorkflowRoundCreate(
            workflow_id=workflow.id, round_no=1))
        isolated_db.create_workflow_run(WorkflowRunCreate(
            workflow_id=workflow.id, round_id=round_data.id, role=role,
            input_sha256="fixture", task_id=task.id))
    for item in (get(f"/api/tasks/{task.id}").json(), get("/api/tasks").json()[0]):
        assert item["workflow_id"] is item["role"] is item["workflow_stage_id"] is None
