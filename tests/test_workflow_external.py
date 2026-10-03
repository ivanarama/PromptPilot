"""External stages of a workflow (issue #122).

Acceptance: «automatic stage → external stage → automatic stage» runs the
whole cycle — including sending the external result back for revision —
without touching the database by hand and without losing history. Waiting
for the outside is a planned state, not a HUMAN_REQUIRED stop, and holds no
queue task.
"""

import asyncio
import json

import httpx
import pytest
from click.testing import CliRunner

from promptpilot import api, workflows
from promptpilot.cli import cli
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowExternalResult,
    WorkflowPlanApproval,
    WorkflowPlanReplace,
    WorkflowStageSpec,
)


def config(auto_dispatch_executor=True):
    return {
        "planning": {"enabled": True, "require_approval": True, "max_stages": 10,
                     "max_revisions_per_stage": 3},
        "automation": {"enabled": True, "auto_dispatch_executor": auto_dispatch_executor},
        "roles": {"planner": {"provider": None}, "executor": {"provider": None},
                  "reviewer": {"provider": None}},
        "gate": {"commands": []},
        "limits": {"max_rounds": 10},
    }


def finish_task(isolated_db, result):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


PASS = "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО — этап принят"
REVISION = (
    'AUDIT_FINDINGS_JSON: [{"fingerprint":"sources","severity":"medium",'
    '"category":"research","title":"Нет источников у вывода 2"}]\n'
    "AUDIT_VERDICT: REVISION_REQUIRED\nИТОГ: ГОТОВО — нужны исправления"
)
RESOLVED = (
    'AUDIT_FINDINGS_JSON: [{"fingerprint":"sources","severity":"medium",'
    '"category":"research","title":"Нет источников у вывода 2","status":"resolved"}]\n'
    "AUDIT_VERDICT: PASS\nИТОГ: ГОТОВО — этап принят"
)


def planned(isolated_db, stages, **kwargs):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="external", objective="Выбрать библиотеку и встроить её",
        repository_path=str(isolated_db.DB_DIR), candidate_branch="feature/lib",
        config=config(**kwargs)))
    workflows.dispatch_planner(workflow.id, workflows.WorkflowPlanDispatch(
        expected_version=workflow.state_version))
    planner = isolated_db.get_next_runnable()
    isolated_db.mark_completed(planner.id, "план", exit_code=0)
    isolated_db.set_verdict(planner.id, "ГОТОВО")
    workflow = isolated_db.get_workflow(workflow.id)
    with isolated_db._connect() as conn:  # the planner's text does not matter here
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (
            "WORKFLOW_PLAN_JSON_BEGIN\n" + json.dumps({"stages": stages}, ensure_ascii=False)
            + "\nWORKFLOW_PLAN_JSON_END\nИТОГ: ГОТОВО", planner.id))
    workflows.sync_planner_task(planner.id)
    waiting = isolated_db.get_workflow(workflow.id)
    return workflows.approve_plan(workflow.id, WorkflowPlanApproval(
        expected_version=waiting.state_version))


THREE_STAGES = [
    {"code": "S1", "title": "Каркас", "objective": "Подготовить модуль"},
    {"code": "S2", "title": "Исследование", "objective": "Сравнить три библиотеки",
     "execution_mode": "external", "dependencies": ["S1"],
     "deliverables": ["таблица сравнения", "рекомендация"]},
    {"code": "FINAL", "title": "Интеграция", "objective": "Встроить выбранную библиотеку",
     "stage_type": "integration", "dependencies": ["S2"]},
]


def submit(workflow_id, text, **kwargs):
    workflow = workflows.db.get_workflow(workflow_id)
    submitted = workflows.submit_external_result(workflow_id, WorkflowExternalResult(
        expected_version=workflow.state_version, result=text, **kwargs))
    return workflows.advance_workflow(submitted.id)


def test_auto_external_auto_with_a_revision_of_the_external_result(isolated_db):
    workflow = planned(isolated_db, THREE_STAGES)
    workflows.advance_workflow(workflow.id)
    finish_task(isolated_db, "S1: модуль готов, commit abc123")
    finish_task(isolated_db, PASS)

    waiting = isolated_db.get_workflow(workflow.id)
    assert waiting.status.value == "awaiting_external"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []  # nothing queued
    first = workflows.external_assignment(workflow.id)
    assert first.stage_code == "S2" and first.status.value == "awaiting_external"
    assert "Сравнить три библиотеки" in first.assignment
    assert "таблица сравнения" in first.assignment
    assert "S1: модуль готов" in first.assignment  # results of earlier stages
    assert "ИТОГ:" not in first.assignment  # no agent contract for a person
    assert workflows.advance_workflow(workflow.id).status.value == "awaiting_external"

    reviewing = submit(workflow.id, "Вывод 1: A быстрее. Вывод 2: B надёжнее.",
                       performer="Мария (внешний сервис)", comment="черновик")
    assert reviewing.status.value == "reviewing"
    reviewer = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert "Вывод 2: B надёжнее" in reviewer.prompt
    assert "Мария (внешний сервис)" in reviewer.prompt and "внешний-исполнитель" in reviewer.prompt
    finish_task(isolated_db, REVISION)

    again = isolated_db.get_workflow(workflow.id)
    assert again.status.value == "awaiting_external"  # back out for revision
    second = workflows.external_assignment(workflow.id)
    assert second.round_no > first.round_no
    assert "Нет источников у вывода 2" in second.assignment  # the remarks to fix
    assert "первая попытка этапа" in first.assignment  # S1's PASS is not S2's remarks

    submit(workflow.id, "Вывод 2: B надёжнее — см. отчёт X и бенчмарк Y.", performer="Мария")
    finish_task(isolated_db, RESOLVED)

    final = isolated_db.get_workflow(workflow.id)
    assert final.status.value == "executing"  # FINAL is automatic again
    executor = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert "Встроить выбранную библиотеку" in executor.prompt
    finish_task(isolated_db, "FINAL: встроено")
    finish_task(isolated_db, PASS)

    done = isolated_db.get_workflow(workflow.id)
    assert done.status.value == "completed"
    events = [event.event_type for event in isolated_db.list_workflow_events(workflow.id, limit=1000)]
    assert events.count("external.requested") == 2 and events.count("external.submitted") == 2
    report = workflows.workflow_report(workflow.id)
    external_runs = [run for run in report["runs"]
                     if (run.get("input") or {}).get("execution_mode") == "external"]
    assert [run["output"]["external"]["performer"] for run in external_runs] == ["Мария (внешний сервис)", "Мария"]
    assert report["metrics"]["external_attempts"] == 2
    assert all(run["task_id"] is None for run in external_runs)


def test_manual_mode_hands_the_stage_out_on_request(isolated_db):
    workflow = planned(isolated_db, [
        {"code": "S1", "title": "Исследование", "objective": "Найти причину",
         "execution_mode": "external"},
        {"code": "FINAL", "title": "Итог", "objective": "Свести", "stage_type": "integration",
         "dependencies": ["S1"]},
    ], auto_dispatch_executor=False)

    handed = workflows.advance_workflow(workflow.id)  # handing out starts no agent

    assert handed.status.value == "awaiting_external"
    assert workflows.external_assignment(workflow.id).stage_code == "S1"


def test_result_is_refused_when_nothing_waits(isolated_db):
    workflow = planned(isolated_db, THREE_STAGES)
    workflows.advance_workflow(workflow.id)

    with pytest.raises(isolated_db.WorkflowConflictError, match="not waiting"):
        submit(workflow.id, "рано")
    finish_task(isolated_db, "S1")
    finish_task(isolated_db, PASS)
    stale = isolated_db.get_workflow(workflow.id).state_version - 1
    with pytest.raises(isolated_db.WorkflowConflictError, match="version"):
        workflows.submit_external_result(workflow.id, WorkflowExternalResult(
            expected_version=stale, result="x"))


def test_cancel_closes_the_open_assignment(isolated_db):
    workflow = planned(isolated_db, [dict(THREE_STAGES[1], dependencies=[]), THREE_STAGES[2]])
    waiting = workflows.advance_workflow(workflow.id)

    workflows.cancel_workflow(workflow.id, waiting.state_version)

    assert workflows.external_assignment(workflow.id).status.value == "cancelled"


def test_review_stays_independent_and_stages_default_to_automatic(isolated_db):
    stage = WorkflowStageSpec(code="S1", title="t", objective="o")
    assert stage.execution_mode.value == "automatic"
    workflow = planned(isolated_db, THREE_STAGES)
    workflows.advance_workflow(workflow.id)
    finish_task(isolated_db, "S1")
    finish_task(isolated_db, PASS)
    submit(workflow.id, "результат")
    current = isolated_db.get_workflow(workflow.id)

    with pytest.raises(isolated_db.WorkflowConflictError, match="review stays independent"):
        workflows.dispatch_task(workflow.id, workflows.WorkflowTaskDispatch(
            expected_version=current.state_version, role="reviewer", prompt="x",
            execution_mode="external"))


def test_plan_editing_keeps_the_mode(isolated_db):
    workflow = planned(isolated_db, THREE_STAGES)
    stages = isolated_db.list_workflow_stages(workflow.id)

    assert [stage.execution_mode.value for stage in stages] == ["automatic", "external", "automatic"]
    assert WorkflowPlanReplace(expected_version=0, stages=[
        WorkflowStageSpec(**{k: v for k, v in s.model_dump().items() if k in WorkflowStageSpec.model_fields})
        for s in stages]).stages[1].execution_mode.value == "external"


# --- API and CLI ----------------------------------------------------------------------

def call(method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def test_api_hands_out_and_takes_in(isolated_db):
    workflow = planned(isolated_db, [dict(THREE_STAGES[1], dependencies=[]), THREE_STAGES[2]])
    workflows.advance_workflow(workflow.id)
    current = isolated_db.get_workflow(workflow.id)

    assignment = call("GET", f"/api/workflows/{workflow.id}/external")
    submitted = call("POST", f"/api/workflows/{workflow.id}/external-result",
                     json={"expected_version": current.state_version, "result": "готово",
                           "performer": "API"})
    missing = call("GET", "/api/workflows/nope/external")

    assert assignment.status_code == 200 and "Сравнить три библиотеки" in assignment.json()["assignment"]
    assert submitted.status_code == 200 and submitted.json()["status"] == "reviewing"
    assert missing.status_code == 404


def test_cli_prints_the_assignment_and_submits(isolated_db, tmp_path):
    workflow = planned(isolated_db, [dict(THREE_STAGES[1], dependencies=[]), THREE_STAGES[2]])
    workflows.advance_workflow(workflow.id)
    result_file = tmp_path / "result.md"
    result_file.write_text("Сравнение готово", encoding="utf-8")
    runner = CliRunner()

    shown = runner.invoke(cli, ["workflow", "external", workflow.id])
    submitted = runner.invoke(cli, ["workflow", "submit", workflow.id, "-f", str(result_file),
                                    "--performer", "cli"])

    assert shown.exit_code == 0 and "Сравнить три библиотеки" in shown.output
    assert submitted.exit_code == 0 and "reviewing" in submitted.output
