"""Окна диспатча ревьюера + атомарное создание миссии с планом."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from promptpilot import db, workflows
from promptpilot.api import app
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowPlanApproval,
    WorkflowPlanReplace,
    WorkflowStartRequest,
)

REVIEWER_PASS = (
    "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО — аудит завершён"
)
MON_9_17 = [{"from": "09:00", "to": "17:00", "tz_offset_hours": 0,
             "days": ["mon"]}]


# ── Окна: полный недельный цикл ────────────────────────────────────────────

def reviewer(window):
    return SimpleNamespace(dispatch_window=window)


def test_friday_evening_monday_window_forbids():
    """Обязательный сценарий: пт 18:00 UTC + окно mon 09:00–17:00 — запрет
    до ближайшего понедельника (прежний поиск только сегодня/завтра
    разворачивал запрет в «разрешено»)."""
    friday_evening = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
    inside, until = workflows._dispatch_window_allows(
        reviewer(MON_9_17), friday_evening)
    assert inside is False
    assert until == "09:00 UTC"


def test_monday_morning_inside():
    assert workflows._dispatch_window_allows(
        reviewer(MON_9_17),
        datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc))[0] is True


def test_overnight_window_spans_midnight():
    window = [{"from": "22:00", "to": "06:00"}]
    assert workflows._dispatch_window_allows(
        reviewer(window),
        datetime(2026, 10, 23, 0, 30, tzinfo=timezone.utc))[0] is True
    inside, until = workflows._dispatch_window_allows(
        reviewer(window), datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc))
    assert inside is False and until == "22:00 UTC"


def test_tz_offset_shifts_window():
    window = [{"from": "22:00", "to": "06:00", "tz_offset_hours": 3}]
    assert workflows._dispatch_window_allows(
        reviewer(window),
        datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc))[0] is True  # 23:00 МСК
    assert workflows._dispatch_window_allows(
        reviewer(window),
        datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc))[0] is False


def test_unparsable_legacy_window_fails_closed():
    assert workflows._dispatch_window_allows(
        reviewer([{"from": "не-время"}]))[0] is False


# ── Окна: интеграция с advance_workflow ────────────────────────────────────

def _config(window=None):
    roles = {"executor": {"provider": None}, "reviewer": {"provider": None}}
    if window is not None:
        roles["reviewer"] = {**roles["reviewer"], "dispatch_window": window}
    return {
        "automation": {"enabled": True},
        "roles": roles,
        "gate": {"enabled": False},
        "limits": {"max_rounds": 10},
    }


def _finish(isolated_db, result):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)


def test_outside_window_defers_review_dispatch(isolated_db, monkeypatch):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="win-defer",
        objective="окно",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/win",
        config=_config([{"from": "22:00", "to": "06:00"}]),
    ))
    fixed = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(workflows, "_utcnow", lambda: fixed)
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    _finish(isolated_db, "executor report")

    result = workflows.advance_workflow(workflow.id)
    assert result.status.value == "reviewing", "миссия ждёт окна, не падает"
    types = [e.event_type for e in
             isolated_db.list_workflow_events(workflow.id, limit=100)]
    assert "review.deferred" in types
    assert "reviewer.dispatched" not in types


def test_inside_window_dispatches_reviewer(isolated_db, monkeypatch):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="win-inside",
        objective="окно",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/win2",
        config=_config([{"from": "22:00", "to": "06:00"}]),
    ))
    fixed = datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(workflows, "_utcnow", lambda: fixed)
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    _finish(isolated_db, "executor report")
    workflows.advance_workflow(workflow.id)
    types = [e.event_type for e in
             isolated_db.list_workflow_events(workflow.id, limit=100)]
    assert "reviewer.dispatched" in types
    assert "review.deferred" not in types


# ── Атомарное создание с планом ────────────────────────────────────────────

def request(method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8420",
        ) as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def payload(plan, slug="atomic-plan"):
    return {"slug": slug, "objective": "o",
            "repository_path": "/tmp/x", "candidate_branch": "b",
            "config": {}, "plan": plan}


BAD_PLAN = [{"code": "S1", "title": "t", "objective": "o",
             "stage_type": "implementation"}]
GOOD_PLAN = [{"code": "S1", "title": "t", "objective": "o",
              "stage_type": "integration"}]


def test_invalid_plan_leaves_no_draft_and_slug_free(isolated_db):
    first = request("POST", "/api/workflows", json=payload(BAD_PLAN))
    assert first.status_code == 422
    assert [w.slug for w in db.list_workflows(limit=10)] == []
    second = request("POST", "/api/workflows", json=payload(GOOD_PLAN))
    assert second.status_code == 201
    assert second.json()["status"] == "awaiting_plan_approval"


def test_late_provide_failure_compensates(isolated_db, monkeypatch):
    from promptpilot import workflows as wf

    def boom(*a, **k):
        raise RuntimeError("поздний сбой укладки")

    original = wf.provide_plan
    monkeypatch.setattr(wf, "provide_plan", boom)
    response = request("POST", "/api/workflows", json=payload(GOOD_PLAN))
    assert response.status_code == 500
    slugs = [w.slug for w in db.list_workflows(limit=10)]
    assert "atomic-plan" not in slugs
    states = {w.slug: w.status.value for w in db.list_workflows(limit=10)}
    assert any(s.startswith("aborted-atomic-plan-") and v == "cancelled"
               for s, v in states.items()), states
    monkeypatch.setattr(wf, "provide_plan", original)
    retry = request("POST", "/api/workflows", json=payload(GOOD_PLAN))
    assert retry.status_code == 201, retry.text


def test_provide_plan_still_requires_explicit_approve(isolated_db):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="provide", objective="o",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="b", config=_config(),
    ))
    workflows.provide_plan(workflow.id, WorkflowPlanReplace(
        expected_version=0, stages=GOOD_PLAN))
    parked = workflows.advance_workflow(workflow.id)
    assert parked.status.value == "awaiting_plan_approval"
    approved = workflows.approve_plan(workflow.id, WorkflowPlanApproval(
        expected_version=parked.state_version))
    assert approved.status.value == "queued"
    workflows.advance_workflow(workflow.id)
    assert isolated_db.get_workflow(workflow.id).status.value == "executing"


def test_replace_plan_in_planning_cancels_hanging_planner(isolated_db):
    from promptpilot.models import WorkflowPlanDispatch
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="replace-hang", objective="o",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="b", config=_config(),
    ))
    workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(
        expected_version=0))
    current = isolated_db.get_workflow(workflow.id)
    assert current.status.value == "planning"
    hanging = isolated_db.list_tasks(status=TaskStatus.PENDING)
    assert len(hanging) == 1

    workflows.replace_plan(workflow.id, WorkflowPlanReplace(
        expected_version=current.state_version, stages=GOOD_PLAN))
    after = isolated_db.get_workflow(workflow.id)
    assert after.status.value == "awaiting_plan_approval"
    types = [e.event_type for e in
             isolated_db.list_workflow_events(workflow.id, limit=100)]
    assert "plan.replaced_hanging_planner" in types
    assert isolated_db.get_task(hanging[0].id).status.value == "cancelled"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []


TWO_STAGES = [{"code": "S1", "title": "first", "objective": "first"},
              {"code": "FINAL", "title": "last", "objective": "last", "stage_type": "integration"}]


@pytest.mark.parametrize("state", ["draft", "planning", "planning-running", "awaiting_plan_approval"])
def test_oversized_plan_never_mutates_plan_or_cancels_planner(isolated_db, state):
    from promptpilot.models import WorkflowPlanDispatch
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="limit-" + state, objective="x", repository_path=str(isolated_db.DB_DIR),
        candidate_branch="main", config={"planning": {"max_stages": 1}}))
    if state.startswith("planning"):
        workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(expected_version=0))
        if state == "planning-running":
            running = isolated_db.get_next_runnable()
            assert running is not None
    elif state == "awaiting_plan_approval":
        workflows.provide_plan(workflow.id, WorkflowPlanReplace(expected_version=0, stages=GOOD_PLAN))
    before = isolated_db.get_workflow(workflow.id)
    old_stages = [s.model_dump() for s in isolated_db.list_workflow_stages(workflow.id)]
    old_tasks = [t.model_dump() for t in isolated_db.list_tasks()]
    old_plan = isolated_db.get_workflow_plan(workflow.id)
    replacement = WorkflowPlanReplace(expected_version=before.state_version, stages=TWO_STAGES)
    with pytest.raises(db.WorkflowConflictError, match="max_stages"):
        (workflows.provide_plan if state == "draft" else workflows.replace_plan)(workflow.id, replacement)
    assert isolated_db.get_workflow(workflow.id).model_dump() == before.model_dump()
    assert [s.model_dump() for s in isolated_db.list_workflow_stages(workflow.id)] == old_stages
    assert [t.model_dump() for t in isolated_db.list_tasks()] == old_tasks
    assert isolated_db.get_workflow_plan(workflow.id) == old_plan
    if state == "planning-running":
        assert isolated_db.get_setting(f"cancel_task:{running.id}") is None


def test_http_oversized_plan_leaves_slug_available(isolated_db):
    data = payload(TWO_STAGES, slug="too-many")
    data["config"] = {"planning": {"max_stages": 1}}
    response = request("POST", "/api/workflows", json=data)
    assert response.status_code == 422
    assert isolated_db.list_workflows() == []
    data["plan"] = GOOD_PLAN
    assert request("POST", "/api/workflows", json=data).status_code == 201


@pytest.mark.parametrize("bad", [{"from": "aa:bb"}, {"from": "25:00", "to": "26:00"}, {"to": "12:60"}, {"days": ["bad"]}, {"tz_offset_hours": 30}])
def test_bad_dispatch_windows_are_rejected_by_http_without_breaking_advance(isolated_db, bad):
    data = payload(None, slug="bad-window")
    data["config"] = _config([{"from": "22:00", "to": "06:00", **bad}])
    assert request("POST", "/api/workflows", json=data).status_code == 422
    assert isolated_db.list_workflows() == []


def test_terminal_config_patch_needs_explicit_flag(isolated_db):
    from promptpilot.models import WorkflowUpdate
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="term", objective="o",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="b", config=_config(),
    ))
    with isolated_db._connect(immediate=True) as conn:
        conn.execute("UPDATE workflows SET status='completed' WHERE id=?",
                     (workflow.id,))
    with isolated_db._connect(immediate=True) as conn:
        version = conn.execute(
            "SELECT state_version FROM workflows WHERE id=?",
            (workflow.id,)).fetchone()[0]
    try:
        isolated_db.update_workflow(workflow.id, WorkflowUpdate(
            config={"limits": {"max_rounds": 5}},
            expected_version=version))
        raised = False
    except db.WorkflowConflictError:
        raised = True
    assert raised
    updated = isolated_db.update_workflow(workflow.id, WorkflowUpdate(
        config={"limits": {"max_rounds": 5}},
        expected_version=version,
        allow_terminal_config=True,
    ))
    assert updated.config["limits"]["max_rounds"] == 5
