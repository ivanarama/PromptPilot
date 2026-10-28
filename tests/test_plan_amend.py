# -*- coding: utf-8 -*-
"""F5: динамическая перепланировка будущих карточек живого воркфлоу (opt-in).

planning.allow_editing_approved_plan=false (дефолт) — поведение прежнее.
=true — amend_plan меняет хвост плана: замороженный префикс (завершённые
+ текущий этап) неприкосновенен, финал обязан остаться integration,
каждая правка — событие plan.amended с причиной."""
import json
import asyncio

import httpx

import pytest

from promptpilot import db as pdb
from promptpilot import workflows as W
from promptpilot.models import (
    WorkflowCreate, WorkflowPlanReplace, WorkflowStageSpec,
)


def _mk_spec(code, deps=None, itype="implementation"):
    return WorkflowStageSpec(
        code=code, title=code, objective="objective " + code,
        stage_type=itype, dependencies=deps or [],
    )


def _mk_wf(tmp_path, monkeypatch, isolated_db, allow_amend=True):
    cfg = {
        "planning": {
            "enabled": True, "require_approval": True,
            "allow_editing_approved_plan": allow_amend,
            "max_stages": 30,
        },
    }
    wf = pdb.create_workflow(WorkflowCreate(
        slug="amend-test", objective="test goal",
        repository_path=str(tmp_path), candidate_branch="b", config=cfg,
    ))
    stages = [_mk_spec("S01"), _mk_spec("S02", ["S01"]),
              _mk_spec("S03", ["S02"], itype="integration")]
    # заменить план до утверждения: перевести в awaiting_plan_approval
    with pdb._connect(immediate=True) as conn:
        conn.execute(
            "INSERT INTO workflow_plans (workflow_id, status, created_at, updated_at) "
            "VALUES (?, 'awaiting_approval', ?, ?) "
            "ON CONFLICT(workflow_id) DO UPDATE SET status='awaiting_approval'",
            (wf.id, pdb._now(), pdb._now()))
        conn.execute(
            "UPDATE workflows SET status='awaiting_plan_approval' WHERE id=?",
            (wf.id,))
    row = pdb.get_workflow(wf.id)
    W.replace_plan(wf.id, WorkflowPlanReplace(
        expected_version=row.state_version, stages=stages))
    return wf.id


def _approve_and_freeze(wf_id, frozen_count):
    """Утвердить план; первые frozen_count-1 этапов -> completed,
    этап frozen_count -> executing (текущий)."""
    with pdb._connect(immediate=True) as conn:
        conn.execute(
            "UPDATE workflow_plans SET status='awaiting_approval' WHERE workflow_id=?",
            (wf_id,))
        conn.execute(
            "UPDATE workflows SET status='awaiting_plan_approval' WHERE id=?",
            (wf_id,))
    row = pdb.get_workflow(wf_id)
    W.approve_plan(wf_id, type("A", (), {"expected_version": row.state_version, "base_sha": None})())
    with pdb._connect(immediate=True) as conn:
        stages = conn.execute(
            "SELECT id, position FROM workflow_stages WHERE workflow_id=? ORDER BY position",
            (wf_id,)).fetchall()
        for s in stages[:frozen_count - 1]:
            conn.execute(
                "UPDATE workflow_stages SET status='completed' WHERE id=?",
                (s["id"],))
        cur = stages[frozen_count - 1]
        conn.execute(
            "UPDATE workflow_stages SET status='executing' WHERE id=?",
            (cur["id"],))
        conn.execute(
            "UPDATE workflows SET current_stage_id=? WHERE id=?",
            (cur["id"], wf_id))


def _amend(wf_id, stages, reason="test"):
    row = pdb.get_workflow(wf_id)
    return W.amend_plan(wf_id, type("R", (), {
        "expected_version": row.state_version,
        "stages": stages, "reason": reason})())


def test_amend_disabled_by_default(tmp_path, monkeypatch, isolated_db):
    wf_id = _mk_wf(tmp_path, monkeypatch, isolated_db, allow_amend=False)
    _approve_and_freeze(wf_id, 1)
    new = [_mk_spec("S01"), _mk_spec("S02", ["S01"]),
           _mk_spec("S04", ["S02"], itype="integration")]
    with pytest.raises(pdb.WorkflowConflictError):
        _amend(wf_id, new)


def test_amend_changes_only_tail(tmp_path, monkeypatch, isolated_db):
    wf_id = _mk_wf(tmp_path, monkeypatch, isolated_db)
    _approve_and_freeze(wf_id, 2)  # S01 completed, S02 executing
    new = [
        _mk_spec("S01"), _mk_spec("S02", ["S01"]),
        _mk_spec("S02A", ["S02"]), _mk_spec("S02B", ["S02A"]),
        _mk_spec("S03", ["S02B"], itype="integration"),
    ]
    stages = _amend(wf_id, new, "split S03")
    codes = [s.code for s in stages]
    assert codes == ["S01", "S02", "S02A", "S02B", "S03"]
    statuses = {s.code: s.status.value for s in stages}
    assert statuses["S01"] == "completed"
    assert statuses["S02"] == "executing"
    assert statuses["S02A"] == "pending"
    # событие plan.amended записано с причиной
    events = pdb.list_workflow_events(wf_id)
    amended = [e for e in events if e.event_type == "plan.amended"]
    assert amended and "split S03" in json.dumps(
        amended[-1].payload, ensure_ascii=False)


def test_amend_rejects_frozen_prefix_change(tmp_path, monkeypatch, isolated_db):
    wf_id = _mk_wf(tmp_path, monkeypatch, isolated_db)
    _approve_and_freeze(wf_id, 2)
    bad = [
        _mk_spec("S01"), _mk_spec("SXX", ["S01"]),  # переименован замороженный S02
        _mk_spec("S03", ["SXX"], itype="integration"),
    ]
    with pytest.raises(pdb.WorkflowConflictError, match="заморожен"):
        _amend(wf_id, bad)


def test_amend_requires_integration_last(tmp_path, monkeypatch, isolated_db):
    wf_id = _mk_wf(tmp_path, monkeypatch, isolated_db)
    _approve_and_freeze(wf_id, 1)
    bad = [_mk_spec("S01"), _mk_spec("S02", ["S01"])]  # финал не integration
    with pytest.raises(pdb.WorkflowConflictError, match="integration"):
        _amend(wf_id, bad)


def _http(wid, body):
    from promptpilot.api import app

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                    base_url="http://127.0.0.1:8420") as c:
            return await c.post(f"/api/workflows/{wid}/plan/amend", json=body)
    return asyncio.run(request())


def _snapshot():
    with pdb._connect() as conn:
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("workflows", "workflow_plans", "workflow_stages", "workflow_rounds", "workflow_events")}


@pytest.fixture
def approved(tmp_path, monkeypatch, isolated_db):
    wid = _mk_wf(tmp_path, monkeypatch, isolated_db)
    _approve_and_freeze(wid, 2)
    stages = [_mk_spec("S01"), _mk_spec("S02", ["S01"]), _mk_spec("S04", ["S02"], "integration")]
    return wid, {"expected_version": pdb.get_workflow(wid).state_version,
                 "stages": [s.model_dump(mode="json") for s in stages], "reason": "tail replacement"}


@pytest.mark.parametrize("field,value", [
    ("title", "changed"), ("objective", "changed"), ("acceptance_gates", ["false"]),
    ("allowed_paths", ["secret"]), ("executor_prompt", "changed"),
    ("reviewer_prompt", "changed"), ("dependencies", []), ("max_revision_rounds", 8),
])
def test_http_frozen_spec_changes_are_rejected_atomically(approved, field, value):
    wid, body = approved
    body["stages"][1][field] = value
    before = _snapshot()
    response = _http(wid, body)
    assert response.status_code == 409, response.text
    assert _snapshot() == before


@pytest.mark.parametrize("condition", ["missing_current", "pending_with_history", "started_tail", "not_approved", "limit", "cas"])
def test_http_unsafe_state_is_unchanged(approved, condition):
    wid, body = approved
    with pdb._connect() as conn:
        if condition == "missing_current":
            conn.execute("UPDATE workflows SET current_stage_id=NULL WHERE id=?", (wid,))
        elif condition == "pending_with_history":
            tail = conn.execute("SELECT id FROM workflow_stages WHERE workflow_id=? AND code='S03'", (wid,)).fetchone()[0]
            conn.execute("INSERT INTO workflow_rounds(id,workflow_id,round_no,stage_id,status,started_at) VALUES('tail-history',?,999,?,'completed',?)", (wid, tail, pdb._now()))
        elif condition == "started_tail":
            conn.execute("UPDATE workflow_stages SET started_at=? WHERE workflow_id=? AND code='S03'", (pdb._now(), wid))
        elif condition == "not_approved":
            conn.execute("UPDATE workflow_plans SET status='awaiting_approval' WHERE workflow_id=?", (wid,))
        elif condition == "limit":
            config = pdb.get_workflow(wid).config
            config["planning"]["max_stages"] = 2
            conn.execute("UPDATE workflows SET config_json=? WHERE id=?", (json.dumps(config), wid))
        else:
            body["expected_version"] -= 1
    before = _snapshot()
    response = _http(wid, body)
    assert response.status_code == 409, response.text
    assert _snapshot() == before


@pytest.mark.parametrize("condition", ["duplicate", "future_dependency", "no_integration", "blank_reason"])
def test_http_reuses_whole_plan_validation(approved, condition):
    wid, body = approved
    if condition == "duplicate":
        body["stages"][-1]["code"] = "S01"
    elif condition == "future_dependency":
        body["stages"][0]["dependencies"] = ["S04"]
    elif condition == "no_integration":
        body["stages"][-1]["stage_type"] = "implementation"
    else:
        body["reason"] = "   "
    before = _snapshot()
    assert _http(wid, body).status_code == 422
    assert _snapshot() == before


def test_http_failure_after_tail_rebuild_rolls_back_everything(approved, monkeypatch):
    wid, body = approved
    before = _snapshot()
    def fail(*a, **kw):
        raise RuntimeError("injected before transaction commit")
    monkeypatch.setattr(W, "_touch", fail)
    assert _http(wid, body).status_code == 500
    assert _snapshot() == before


def test_http_success_keeps_history_and_records_full_tail(approved):
    wid, body = approved
    before = _snapshot()
    response = _http(wid, body)
    assert response.status_code == 200, response.text
    after = _snapshot()
    assert after["workflow_stages"][:2] == before["workflow_stages"][:2]
    assert after["workflow_rounds"] == before["workflow_rounds"]
    event = pdb.list_workflow_events(wid)[-1]
    assert event.event_type == "plan.amended"
    assert event.payload["old_tail"][0]["code"] == "S03"
    assert event.payload["new_tail"][0] == body["stages"][-1]
    assert pdb.get_workflow(wid).state_version == body["expected_version"] + 1
