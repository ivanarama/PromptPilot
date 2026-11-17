"""Real temporary SQLite and native workflow intake; no provider/network runs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
import time

import pytest
from pydantic import ValidationError

from promptpilot import agent_control as agents, config, workflows
from promptpilot.models import WorkflowPlanApproval


@pytest.fixture
def registry(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", isolated_db.DB_PATH)
    monkeypatch.setattr(config, "API_TOKEN", "owner-administrator-only")
    root = tmp_path / "repository"
    root.mkdir()
    return isolated_db, root


def project(root, identity="demo", **changes):
    values = {"id": identity, "name": "Example project", "repository_url": "https://github.com/example/demo",
              "repository_path": str(root), "deliverables": ["RESULT.md"], "allowed_paths": ["docs/"],
              "workflow_config": {"gate": {"enabled": False}, "automation": {"enabled": True},
                                  "roles": {"executor": {"provider": "must-never-run"}}}}
    values.update(changes)
    return agents.upsert_project(agents.ProjectCreate(**values))


def connection(project_ids=("demo",)):
    result = agents.issue_connection(agents.ConnectionCreate(name="An MCP client", project_ids=list(project_ids)))
    return result["connection"]["id"], result["token"]


def request(key="stable-1", **changes):
    values = {"project_id": "demo", "request_id": key, "objective": "Prepare a documentation proposal"}
    values.update(changes)
    return agents.TaskCreate(**values)


def test_read_methods_never_create_registry_or_database(registry, monkeypatch, tmp_path):
    db, _ = registry
    with db._connect() as conn:
        before = list(conn.iterdump())
    assert agents.list_projects() == agents.list_connections() == []
    with db._connect() as conn:
        assert list(conn.iterdump()) == before
    missing = tmp_path / "not-created.db"
    monkeypatch.setattr(config, "DB_PATH", missing)
    assert agents.list_projects() == agents.list_connections() == []
    assert not missing.exists()


def test_omitted_owner_configuration_gets_visible_safe_defaults(registry):
    _, root = registry
    value = agents.ProjectCreate(id="local", name="Local Git", repository_path=str(root))
    saved = agents.upsert_project(value)
    assert saved["workflow_config"]["planning"]["require_approval"] is True
    assert saved["workflow_config"]["gate"]["commands"]
    assert saved["workflow_config"]["external_executor"]["client_id"] == "zcode"
    assert saved["workflow_config"]["roles"]["reviewer"]["provider"] == "codex"
    assert saved["workflow_config"]["automation"]["enabled"] is True
    assert "{workflow_id}" in saved["workflow_config"]["gate"]["commands"][0]
    assert saved["deliverables"] == ["RESULT.md"]
    assert saved["repository_url"] is None
    identity, _ = connection(("local",))
    result = agents.create_task(identity, request(project_id="local"))
    assert result["status"] == "awaiting_plan_approval"
    assert result["dashboard_path"] == "/#workflow/" + result["task_id"]
    with pytest.raises(agents.AgentControlError):
        agents.create_task(identity, request("with-issue", project_id="local", issue_url="https://github.com/example/demo/issues/1"))


def test_tokens_once_hash_only_and_administrator_boundary(registry, monkeypatch):
    db, root = registry
    project(root)
    identity, token = connection()
    assert agents.authenticate(token) == identity
    assert token not in json.dumps(agents.list_connections())
    with db._connect() as conn:
        stored = dict(conn.execute("SELECT * FROM agent_connections").fetchone())
    assert "token" not in stored and token not in json.dumps(stored)
    assert len(stored["token_hash"]) == 64
    monkeypatch.setattr(config, "API_TOKEN", "")
    with pytest.raises(agents.AgentForbidden):
        connection()
    with pytest.raises(agents.AgentForbidden):
        agents.authenticate(token)
    with pytest.raises(agents.AgentForbidden):
        agents.create_task(identity, request())


@pytest.mark.parametrize("plan", [None, [{"title": "Study", "objective": "Read the context"}, {"title": "Propose", "objective": "Write the result"}]])
def test_actual_native_approval_external_handoff_never_cli_provider(registry, monkeypatch, plan):
    db, root = registry
    project(root)
    identity, _ = connection()
    result = agents.create_task(identity, request(plan=plan, issue_url="https://github.com/example/demo/issues/42"))
    workflow = db.get_workflow(result["task_id"])
    assert workflow.status.value == "awaiting_plan_approval" and result["approval_required"]
    assert "https://github.com/example/demo/issues/42" in workflow.objective
    assert db.list_tasks() == []
    assert workflow.config["roles"]["executor"]["provider"] == "must-never-run"
    assert workflow.config["external_executor"]["client_id"] == "zcode"
    stages = db.list_workflow_stages(workflow.id)
    assert len(stages) == (len(plan) if plan else 1)
    assert all(stage.execution_mode.value == "external" and stage.allowed_paths == ["docs/"] for stage in stages)
    assert all("https://github.com/example/demo/issues/42" in stage.objective for stage in stages)
    assert stages[-1].stage_type.value == "integration"
    assert not any(event.event_type == "plan.approved" for event in db.list_workflow_events(workflow.id))
    monkeypatch.setattr(db, "create_task", lambda *a, **kw: pytest.fail("External executor was dispatched as a CLI provider"))
    workflows.approve_plan(workflow.id, WorkflowPlanApproval(expected_version=workflow.state_version))
    waiting = workflows.advance_workflow(workflow.id)
    assert waiting.status.value == "awaiting_external"
    assignment = workflows.external_assignment(workflow.id)
    assert assignment and "https://github.com/example/demo/issues/42" in assignment.assignment
    # Exercise the real existing executor MCP router with this native assignment.
    import asyncio
    import httpx
    from fastapi import FastAPI
    from promptpilot import external_inbox
    app = FastAPI()
    app.include_router(external_inbox.router)
    async def claim():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            initialized = await client.post("/api/external-clients/zcode/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            response = await client.post("/api/external-clients/zcode/mcp", headers={"Mcp-Session-Id": initialized.headers["Mcp-Session-Id"]}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "pilot_next_task", "arguments": {}}})
            return response.json()["result"]
    claimed = asyncio.run(claim())
    assert not claimed["isError"]
    assert claimed["structuredContent"]["workflow_id"] == workflow.id


def test_idempotency_shared_project_read_scope_and_terminal_issue_policy(registry):
    db, root = registry
    project(root)
    project(root, "private")
    first, _ = connection()
    second, _ = connection()
    outsider, _ = connection(("private",))
    data = request(issue_url="https://github.com/example/demo/issues/11")
    created = agents.create_task(first, data)
    assert agents.create_task(first, data)["task_id"] == created["task_id"]
    assert agents.create_task(first, data)["replayed"]
    with pytest.raises(agents.AgentConflict):
        agents.create_task(first, request(objective="Different task", issue_url=data.issue_url))
    assert agents.get_task(second, created["task_id"])["task_id"] == created["task_id"]
    assert agents.list_tasks(second)[0]["task_id"] == created["task_id"]
    with pytest.raises(agents.AgentForbidden):
        agents.get_task(outsider, created["task_id"])
    with pytest.raises(agents.AgentConflict):
        agents.create_task(second, request("another", issue_url=data.issue_url))
    with db._connect() as conn:
        conn.execute("UPDATE workflows SET status='cancelled' WHERE id=?", (created["task_id"],))
    later = agents.create_task(second, request("another", issue_url=data.issue_url))
    assert later["task_id"] != created["task_id"]
    assert agents.revoke_connection(second)
    with pytest.raises(agents.AgentForbidden):
        agents.get_task(second, later["task_id"])


def test_native_failure_rolls_back_workflow_plan_and_idempotency(registry, monkeypatch):
    db, root = registry
    project(root)
    identity, _ = connection()
    def fail(*a, **kw):
        raise ValueError("Injected plan failure after native workflow creation")
    monkeypatch.setattr(workflows, "provide_plan", fail)
    with pytest.raises(ValueError):
        agents.create_task(identity, request())
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM agent_task_requests").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM workflow_events").fetchone()[0] == 0


def test_two_clients_race_same_key_and_same_project_issue(registry):
    db, root = registry
    project(root)
    identity, _ = connection()
    data = request()
    barrier = threading.Barrier(2)
    def create():
        barrier.wait(timeout=3)
        return agents.create_task(identity, data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert len({r["task_id"] for r in results}) == 1
    assert sorted(r["replayed"] for r in results) == [False, True]
    second, _ = connection()
    barrier = threading.Barrier(2)
    def competing(who):
        barrier.wait(timeout=3)
        try:
            return agents.create_task(who, request("issue", issue_url="https://github.com/example/demo/issues/99"))
        except agents.AgentConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing, (identity, second)))
    assert sum(r is not None for r in results) == 1
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 2


def test_revoked_while_waiting_for_writer_is_rechecked_before_native_creation(registry):
    db, root = registry
    project(root)
    identity, token = connection()
    assert agents.authenticate(token) == identity
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db._connect(immediate=True) as conn:
            conn.execute("UPDATE agent_connections SET revoked_at='owner-revoked' WHERE id=?", (identity,))
            future = pool.submit(agents.create_task, identity, request())
            time.sleep(0.05)
        with pytest.raises(agents.AgentForbidden):
            future.result(timeout=4)
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0


def test_lost_commit_ack_reconciles_same_key_without_second_native_creation(registry, monkeypatch):
    db, root = registry
    project(root)
    identity, _ = connection()
    original_write = agents._write
    @contextmanager
    def lost_ack():
        with original_write() as conn:
            yield conn
        raise agents.AgentUnavailable("Injected lost acknowledgement after commit")
    monkeypatch.setattr(agents, "_write", lost_ack)
    with pytest.raises(agents.AgentUnavailable):
        agents.create_task(identity, request())
    monkeypatch.setattr(agents, "_write", original_write)
    reconciled = agents.create_task(identity, request())
    assert reconciled["replayed"]
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [("repository_path", "/other"), ("workflow_config", {}), ("provider", "unsafe"), ("approval", True), ("plan", [{"title": "Stage", "objective": "Work", "allowed_paths": ["../"]}])])
def test_caller_cannot_override_owner_authority(field, value):
    with pytest.raises(ValidationError):
        request(**{field: value})


def test_owner_placeholder_substitution_only_and_no_secret_configuration_in_task_views(registry):
    db, root = registry
    project(root, workflow_config={"gate": {"commands": ["trusted-check {workflow_id}"]}, "roles": {"executor": {"provider": "owner-only"}}})
    identity, _ = connection()
    result = agents.create_task(identity, request(objective="Keep literal {workflow_id} in untrusted task text"))
    workflow = db.get_workflow(result["task_id"])
    assert workflow.config["gate"]["commands"] == ["trusted-check " + workflow.id]
    assert "{workflow_id}" in workflow.objective
    assert "repository_path" not in result and "config" not in result and "owner-only" not in json.dumps(result)
