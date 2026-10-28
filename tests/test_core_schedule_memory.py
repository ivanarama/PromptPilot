# -*- coding: utf-8 -*-
"""Ядро: провайдеры-по-расписанию, память миссии, tail-пейджинг событий."""
from datetime import datetime, timezone
import asyncio
import httpx
import pytest

from promptpilot import api, db, workflows
from promptpilot.models import WorkflowCreate, WorkflowEventCreate, WorkflowPlanDispatch, WorkflowStartRequest, TaskStatus


def _wf(db, tmp_path, config=None):
    return db.create_workflow(WorkflowCreate(
        slug="core-test", objective="цель", repository_path=str(tmp_path),
        candidate_branch="b", config=config or {}))


def test_resolve_role_provider_windows(isolated_db, tmp_path):
    cfg = {"roles": {"executor": {
        "provider": "main-prov",
        "provider_windows": [
            {"provider": "cheap-prov",
             "window": {"from": "13:00", "to": "04:00",
                        "tz_offset_hours": 3, "days": "all"}},
        ]}}}
    workflow = _wf(isolated_db, tmp_path, cfg)
    # 15:00 МСК (12:00 UTC) — внутри окна
    inside = workflows.resolve_role_provider(
        workflow, "executor", datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))
    # 10:00 МСК (07:00 UTC) — вне
    outside = workflows.resolve_role_provider(
        workflow, "executor", datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc))
    assert inside == "cheap-prov" and outside == "main-prov"


def test_resolve_role_provider_no_windows_fallback(isolated_db, tmp_path):
    workflow = _wf(isolated_db, tmp_path, {"roles": {"executor": {"provider": "p1"}}})
    assert workflows.resolve_role_provider(workflow, "executor") == "p1"


@pytest.mark.parametrize("role", ["executor", "reviewer", "planner"])
def test_actual_dispatch_selects_each_windows_own_timezone(isolated_db, tmp_path, monkeypatch, role):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(workflows, "datetime", Clock)
    windows = [
        {"provider": "inactive", "window": {"from": "00:00", "to": "01:00", "tz_offset_hours": 0}},
        {"provider": "selected", "window": {"from": "14:00", "to": "16:00", "tz_offset_hours": 3}},
    ]
    # Dispatch scheduling is independent of deterministic gate execution.
    config = {"automation": {"enabled": True}, "gate": {"enabled": False},
              "roles": {role: {"provider": "fallback", "provider_windows": windows}}}
    workflow = _wf(isolated_db, tmp_path, config)
    if role == "planner":
        workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(expected_version=0))
    else:
        workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
        workflows.advance_workflow(workflow.id)
        if role == "reviewer":
            executor = isolated_db.get_next_runnable()
            workflows.sync_task(executor.id)
            isolated_db.mark_completed(executor.id, "done", exit_code=0)
            isolated_db.set_verdict(executor.id, "ГОТОВО")
            workflows.sync_task(executor.id)
            workflows.advance_linked_task(executor.id)
    tasks = isolated_db.list_tasks(status=TaskStatus.PENDING)
    assert len(tasks) == 1 and tasks[0].provider == "selected"


@pytest.mark.parametrize("bad", [
    {"from": "aa:bb"}, {"from": "25:00"}, {"to": "26:00"},
    {"from": "12:60"}, {"days": ["bogus"]}, {"days": []},
    {"tz_offset_hours": "3"}, {"tz_offset_hours": True}, {"tz_offset_hours": 24},
])
def test_invalid_windows_are_rejected_by_http_before_persistence(isolated_db, tmp_path, bad):
    window = {"from": "01:00", "to": "02:00", **bad}
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://127.0.0.1:8420") as client:
            return await client.post("/api/workflows", json={"slug": "invalid-window", "objective": "x", "repository_path": str(tmp_path), "candidate_branch": "main", "config": {"roles": {"executor": {"provider_windows": [{"provider": "p", "window": window}]}}}})
    assert asyncio.run(request()).status_code == 422
    assert isolated_db.list_workflows() == []


def test_overnight_window_uses_start_weekday_with_its_own_offset(isolated_db, tmp_path):
    config = {"roles": {"executor": {"provider": "fallback", "provider_windows": [
        {"provider": "friday", "window": {"from": "22:00", "to": "04:00", "tz_offset_hours": 3, "days": ["fri"]}},
    ]}}}
    workflow = _wf(isolated_db, tmp_path, config)
    # Saturday 02:00 at UTC+3 belongs to the Friday night window.
    assert workflows.resolve_role_provider(workflow, "executor", datetime(2026, 10, 9, 23, tzinfo=timezone.utc)) == "friday"


def test_memory_save_list_delete_roundtrip(isolated_db, tmp_path):
    workflow = _wf(isolated_db, tmp_path)
    ok, _ = db.save_memory(workflow.id, "memory.md", "# память")
    assert ok
    names = [n["name"] for n in db.list_memory(workflow.id)]
    assert names == ["memory.md"]
    ok2, msg = db.save_memory(workflow.id, "big.md", "x" * (17 * 1024))
    assert not ok2 and "больше" in msg
    assert db.delete_memory(workflow.id, "memory.md") is True


def test_events_tail_returns_last_in_order(isolated_db, tmp_path):
    workflow = _wf(isolated_db, tmp_path)
    for i in range(5):
        db.append_workflow_event(WorkflowEventCreate(
            workflow_id=workflow.id, event_type="e",
            idempotency_key=f"k{i}", payload={"i": i}))
    tail = db.list_workflow_events(workflow.id, limit=3, tail=True)
    assert [e.payload["i"] for e in tail] == [2, 3, 4]
    first_e_seq = db.list_workflow_events(workflow.id, limit=100)[1].seq
    head = db.list_workflow_events(workflow.id, after_seq=first_e_seq - 1,
                                   limit=2)
    assert [e.payload["i"] for e in head] == [0, 1]
