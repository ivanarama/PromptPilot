"""Actual ASGI requests against scoped MCP and the ordinary admin boundary."""
import asyncio
import json

from fastapi import FastAPI
import httpx
import pytest

from promptpilot import agent_control as agents, agent_mcp, config


class Client:
    def __init__(self, app, token=None):
        self.app, self.token = app, token

    def request(self, method, path=agent_mcp.MCP_PATH, **kwargs):
        headers = {"MCP-Protocol-Version": "2025-11-25"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        headers.update(kwargs.pop("headers", {}))
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://localhost") as client:
                return await client.request(method, path, headers=headers, **kwargs)
        return asyncio.run(send())

    def rpc(self, method, params=None, identity=1, **kwargs):
        value = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if identity is not None:
            value["id"] = identity
        return self.request("POST", json=value, **kwargs)

    def tool(self, name, arguments=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}).json()


@pytest.fixture
def clients(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", isolated_db.DB_PATH)
    monkeypatch.setattr(config, "API_TOKEN", "owner-not-agent")
    monkeypatch.setattr(config, "ALLOWED_HOSTS", [])
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", [])
    root = tmp_path / "repository"
    root.mkdir()
    for identity in ("shared", "private"):
        agents.upsert_project(agents.ProjectCreate(id=identity, name=identity, repository_path=str(root), workflow_config={"gate": {"enabled": False}}))
    def token(ids):
        return agents.issue_connection(agents.ConnectionCreate(name="MCP client", project_ids=ids))["token"]
    app = FastAPI()
    app.include_router(agent_mcp.router)
    return isolated_db, Client(app, token(["shared"])), Client(app, token(["shared"])), Client(app, token(["private"]))


def content(response):
    result = response["result"]
    assert not result["isError"], response
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("version", agent_mcp.PROTOCOLS)
def test_negotiation_notifications_list_and_versioned_tool_result(clients, version):
    _, first, _, _ = clients
    initialized = first.rpc("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "Any compatible client", "version": "1"}})
    assert initialized.json()["result"]["protocolVersion"] == version
    assert "Mcp-Session-Id" not in initialized.headers
    notification = first.rpc("notifications/initialized", identity=None)
    assert notification.status_code == 202 and not notification.content
    listed = first.rpc("tools/list", {"_meta": {"progressToken": "not-authority"}}).json()
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {"pilot_create_task", "pilot_get_task", "pilot_list_tasks", "pilot_list_projects", "pilot_read_project_file"}
    result = first.rpc("tools/call", {"name": "pilot_list_projects", "arguments": {}, "_meta": {}}, headers={"MCP-Protocol-Version": version}).json()["result"]
    assert ("structuredContent" in result) is (version != "2025-03-26")


def test_real_two_client_create_lost_response_reconcile_shared_read_and_isolation(clients):
    db, first, second, outsider = clients
    args = {"project_id": "shared", "request_id": "stable-call", "objective": "Prepare a proposal"}
    initial = content(first.tool("pilot_create_task", args))
    repeated = content(first.tool("pilot_create_task", args))
    assert initial["task_id"] == repeated["task_id"] and repeated["replayed"]
    assert initial["status"] == "awaiting_plan_approval"
    assert content(second.tool("pilot_get_task", {"task_id": initial["task_id"]}))["task_id"] == initial["task_id"]
    assert content(second.tool("pilot_list_tasks"))["tasks"][0]["task_id"] == initial["task_id"]
    assert outsider.tool("pilot_get_task", {"task_id": initial["task_id"]})["result"]["isError"]
    changed = first.tool("pilot_create_task", {**args, "objective": "Changed input"})
    assert changed["result"]["isError"] and "conflict" in changed["result"]["content"][0]["text"]
    assert db.list_tasks() == []
    workflow = db.get_workflow(initial["task_id"])
    assert workflow.status.value == "awaiting_plan_approval"
    assert "repository_path" not in initial and "config" not in initial


def test_authentication_and_revocation_are_immediate(clients):
    _, first, _, _ = clients
    assert Client(first.app).rpc("tools/list").status_code == 401
    assert Client(first.app, "owner-not-agent").rpc("tools/list").status_code == 401
    assert Client(first.app, "pp_agent_bad").rpc("tools/list").status_code == 401
    identity = agents.authenticate(first.token)
    agents.revoke_connection(identity)
    assert first.rpc("tools/list").status_code == 401


@pytest.mark.parametrize("origin,host", [("https://evil.example", "localhost"), ("http://evil.example", "evil.example"), ("null", "localhost"), ("http://localhost:9999", "localhost"), ("http://user:password@localhost", "localhost")])
def test_origins_reject_dns_rebinding_even_with_a_valid_token(clients, origin, host):
    _, first, _, _ = clients
    response = first.rpc("tools/list", headers={"Origin": origin, "Host": host})
    assert response.status_code == 403
    assert first.request("GET", headers={"Origin": origin, "Host": host}).status_code == 403


def test_trusted_origin_and_native_no_origin(clients, monkeypatch):
    _, first, _, _ = clients
    assert first.rpc("tools/list", headers={"Origin": "http://localhost"}).status_code == 200
    assert first.rpc("tools/list", headers={"Host": "remote.example"}).status_code == 200
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://owner.example"])
    assert first.rpc("tools/list", headers={"Origin": "https://owner.example"}).status_code == 200


@pytest.mark.parametrize("raw", [
    b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/call"}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1e999}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"\\ud800"}}',
    b'[' * 65 + b'0' + b']' * 65,
    b'\xff',
    b'[]',
])
def test_strict_json_and_protocol_input_has_no_mutations(clients, raw):
    db, first, _, _ = clients
    with db._connect() as conn:
        before = list(conn.iterdump())
    response = first.request("POST", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    with db._connect() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("identity", [True, None, 1.25])
def test_invalid_ids_or_mutating_notification_never_create(clients, identity):
    db, first, _, _ = clients
    params = {"name": "pilot_create_task", "arguments": {"project_id": "shared", "request_id": "invisible", "objective": "No invisible creation"}}
    if identity is None:
        response = first.rpc("tools/call", params, identity=None)
    else:
        response = first.rpc("tools/call", params, identity=identity)
    assert response.status_code == 400
    assert db.list_workflows() == []


def test_protocol_media_body_bounds_and_unknown_tools(clients):
    db, first, _, _ = clients
    assert first.request("GET").status_code == 405
    assert first.rpc("ping", headers={"MCP-Protocol-Version": "2099-01-01"}).status_code == 400
    assert first.request("POST", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert first.request("POST", content=b" " * (agent_mcp.MAX_BODY + 1), headers={"Content-Type": "application/json"}).status_code == 413
    for params in ({"name": {}}, {"name": []}, {"name": "not-a-tool"}, {"name": "pilot_list_projects", "arguments": {"path": "/secret"}}, {"name": "pilot_create_task", "arguments": {"project_id": "shared", "request_id": "bad", "objective": "Proposal", "approve": True}}):
        assert first.rpc("tools/call", params).json()["error"]["code"] == -32602
    assert db.list_workflows() == []


def test_full_application_creator_key_cannot_use_admin_approval_route(clients, monkeypatch):
    _, first, _, _ = clients
    from promptpilot import api
    monkeypatch.setattr(api, "API_TOKEN", "owner-not-agent")
    scoped = Client(api.app, first.token)
    response = scoped.request("POST", "/api/workflows/not-owned/plan/approve", json={"expected_version": 0})
    assert response.status_code == 401


def test_context_tool_uses_only_owner_granted_committed_file(clients, monkeypatch):
    _, first, _, _ = clients
    from promptpilot import agent_context
    observed = []
    def fixed(connection_id, project_id, path, offset=0, length=16384):
        project = agents.authorized_project(connection_id, project_id)
        observed.append(project["id"])
        if path != "README.md":
            raise agent_context.ContextError("File is outside the project's context policy")
        return {"path": path, "text": "A committed fixture", "eof": True}
    monkeypatch.setattr(agent_context, "read_project_file", fixed)
    assert content(first.tool("pilot_read_project_file", {"project_id": "shared", "path": "README.md"}))["text"] == "A committed fixture"
    assert first.tool("pilot_read_project_file", {"project_id": "private", "path": "README.md"})["result"]["isError"]
    assert first.tool("pilot_read_project_file", {"project_id": "shared", "path": "../secret"})["result"]["isError"]
    assert observed == ["shared", "shared"]
