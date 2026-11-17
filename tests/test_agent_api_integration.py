"""Actual application middleware separates owner REST and scoped creator MCP."""
import asyncio
import json

import httpx
import pytest

from promptpilot import agent_control as control, api, config, db, task_feed


def request(method, path, token, **kwargs):
    async def send():
        headers = {"Authorization": "Bearer " + token, **kwargs.pop("headers", {})}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://localhost") as client:
            return await client.request(method, path, headers=headers, **kwargs)
    return asyncio.run(send())


@pytest.fixture
def access(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", isolated_db.DB_PATH)
    monkeypatch.setattr(config, "API_TOKEN", "owner-fixture")
    monkeypatch.setattr(api, "API_TOKEN", "owner-fixture")
    owner = "owner-fixture"
    project = request("POST", "/api/agent-control/projects", owner, json={
        "id": "example", "name": "Example project", "repository_path": str(tmp_path),
        "repository_url": "https://github.com/example/project", "default_executor": "zcode",
        "reviewer": "codex", "deliverables": ["RESULT.md"]})
    assert project.status_code == 200, project.text
    connection = request("POST", "/api/agent-control/connections", owner,
                         json={"name": "Any compatible client", "project_ids": ["example"]})
    assert connection.status_code == 201, connection.text
    return owner, connection.json()["token"], connection.json()["connection"]["id"]


def tool(token, name, arguments=None, **kwargs):
    return request("POST", "/api/agents/mcp", token,
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments or {}}}, **kwargs)


def test_scoped_creator_cannot_use_owner_rest_or_owner_token_as_mcp(access):
    owner, token, _ = access
    result = tool(token, "pilot_list_projects")
    assert result.status_code == 200 and not result.json()["result"]["isError"]
    text = result.json()["result"]["content"][0]["text"]
    assert "repository_path" not in text and "workflow_config" not in text
    for method, path, body in [("GET", "/api/workflows", None),
                                ("POST", "/api/worker/resume", {}),
                                ("POST", "/api/workflows", {}),
                                ("POST", "/api/agent-control/connections", {})]:
        assert request(method, path, token, json=body).status_code == 401
    assert tool(owner, "pilot_list_projects").status_code == 401


def test_create_via_real_middleware_has_approval_and_no_task_dispatch(access):
    owner, token, _ = access
    payload = {"project_id": "example", "request_id": "first-request",
               "objective": "Prepare a proposed document", "issue_url": "https://github.com/example/project/issues/1"}
    response = tool(token, "pilot_create_task", payload)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result["isError"], result
    created = json.loads(result["content"][0]["text"])
    assert created["status"] == "awaiting_plan_approval"
    assert created["approval_required"] is True
    workflow = request("GET", "/api/workflows/" + created["task_id"], owner).json()
    # API details may wrap the workflow; inspect the canonical row as well.
    assert workflow
    native = db.get_workflow(created["task_id"])
    assert native.config["planning"]["require_approval"] is True
    assert native.config["automation"]["enabled"] is True
    assert native.config["gate"]["enabled"] is True and native.config["gate"]["commands"]
    assert native.config["external_executor"]["client_id"] == "zcode"
    assert payload["issue_url"] in native.objective
    assert native.config["agent_task_title"] == payload["objective"]
    assert task_feed.project_workflow(native.model_dump(mode="json"))["title"] == payload["objective"]
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    again = tool(token, "pilot_create_task", payload).json()["result"]
    assert json.loads(again["content"][0]["text"])["task_id"] == created["task_id"]
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 1


def test_connection_revocation_and_secret_validation_redaction(access):
    owner, token, connection_id = access
    listed = request("GET", "/api/agent-control/connections", owner)
    assert token not in listed.text and "token_hash" not in listed.text
    secret = "invalid-owner-config-secret"
    invalid = request("POST", "/api/agent-control/projects", owner,
                      json={"id": "invalid", "workflow_config": {"secret": secret}})
    assert invalid.status_code == 422 and secret not in invalid.text
    revoked = request("DELETE", "/api/agent-control/connections/" + connection_id, owner)
    assert revoked.status_code == 200
    assert tool(token, "pilot_list_projects").status_code == 401


def test_mcp_origin_checked_for_post_and_get(access):
    _, token, _ = access
    headers = {"Origin": "https://untrusted.example"}
    assert tool(token, "pilot_list_projects", headers=headers).status_code == 403
    assert request("GET", "/api/agents/mcp", token, headers=headers).status_code == 403
    assert request("GET", "/api/agents/mcp", token).status_code == 405


def test_no_mcp_connections_without_owner_authentication(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", isolated_db.DB_PATH)
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(api, "API_TOKEN", "")
    control.upsert_project(control.ProjectCreate(id="local", name="Local", repository_path=str(tmp_path)))
    result = request("POST", "/api/agent-control/connections", "",
                     json={"name": "client", "project_ids": ["local"]})
    assert result.status_code == 403
    assert control.list_connections() == []
