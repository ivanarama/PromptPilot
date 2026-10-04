# -*- coding: utf-8 -*-
"""F5: динамическая перепланировка будущих карточек живого воркфлоу (opt-in).

planning.allow_editing_approved_plan=false (дефолт) — поведение прежнее.
=true — amend_plan меняет хвост плана: замороженный префикс (завершённые
+ текущий этап) неприкосновенен, финал обязан остаться integration,
каждая правка — событие plan.amended с причиной."""
import json

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
