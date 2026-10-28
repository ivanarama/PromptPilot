"""Composition regression: approved-plan amendment + optional stage memory."""
import asyncio

import httpx
import pytest

from promptpilot import db
from promptpilot.api import app
from test_plan_amend import _approve_and_freeze, _mk_spec, _mk_wf


def request(method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.fixture
def memory_plan(tmp_path, monkeypatch, isolated_db):
    with db._connect() as conn:
        if not any(c["name"] == "scope_stage" for c in conn.execute("PRAGMA table_info(workflow_memory)")):
            pytest.skip("stage-scoped memory is an optional independently integrated feature")
    wid = _mk_wf(tmp_path, monkeypatch, isolated_db)
    _approve_and_freeze(wid, 2)
    stages = [_mk_spec("S01"), _mk_spec("S02", ["S01"]), _mk_spec("S04", ["S02"], "integration")]
    body = {"expected_version": db.get_workflow(wid).state_version,
            "stages": [s.model_dump(mode="json") for s in stages], "reason": "tail replacement"}
    tail = db.list_workflow_stages(wid)[-1]
    response = request("PUT", f"/api/workflows/{wid}/memory", json={
        "name": "future.md", "content": "Keep this context", "scope_stage": tail.code})
    assert response.status_code == 200
    return wid, body, tail


def test_retained_stage_memory_stays_attached(memory_plan):
    wid, body, tail = memory_plan
    body["stages"][-1]["code"] = tail.code
    body["stages"][-1]["objective"] = "Revised pending work"
    response = request("POST", f"/api/workflows/{wid}/plan/amend", json=body)
    assert response.status_code == 200
    assert response.json()[-1]["id"] == tail.id
    notes = request("GET", f"/api/workflows/{wid}/memory").json()
    assert notes[0]["scope_stage"] == tail.id
    assert notes[0]["content"] == "Keep this context"


def test_removing_stage_with_notes_is_atomic_until_explicit_note_removal(memory_plan):
    wid, body, tail = memory_plan
    before = [stage.model_dump(mode="json") for stage in db.list_workflow_stages(wid)]
    response = request("POST", f"/api/workflows/{wid}/plan/amend", json=body)
    assert response.status_code == 409 and "scoped memory" in response.text
    assert [stage.model_dump(mode="json") for stage in db.list_workflow_stages(wid)] == before
    assert db.get_workflow(wid).state_version == body["expected_version"]
    assert request("DELETE", f"/api/workflows/{wid}/memory/future.md").status_code == 200
    response = request("POST", f"/api/workflows/{wid}/plan/amend", json=body)
    assert response.status_code == 200
