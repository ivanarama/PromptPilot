# -*- coding: utf-8 -*-
"""Ядро: провайдеры-по-расписанию, память миссии, tail-пейджинг событий."""
from datetime import datetime, timezone

from promptpilot import db, workflows
from promptpilot.models import WorkflowCreate, WorkflowEventCreate


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
