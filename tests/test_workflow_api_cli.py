import asyncio

import httpx
from click.testing import CliRunner

from promptpilot.api import app
from promptpilot.cli import cli
from promptpilot.models import WorkflowCreate


def payload(slug="api-workflow"):
    return {
        "slug": slug,
        "objective": "Проверить W0",
        "repository_path": r"D:\Projects\PromptPilot",
        "candidate_branch": "main",
        "config": {"max_rounds": 6},
    }


def request(method, path, **kwargs):
    """Exercise the real ASGI app without Starlette's deprecated TestClient."""
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_run())


def test_workflow_api_create_read_update_and_history(isolated_db):
    response = request("POST", "/api/workflows", json=payload())
    assert response.status_code == 201
    created = response.json()

    assert request("GET", "/api/workflows").json()[0]["id"] == created["id"]
    assert request("GET", f"/api/workflows/{created['id']}").status_code == 200

    updated = request(
        "PATCH",
        f"/api/workflows/{created['id']}",
        json={"objective": "Уточнённая цель", "expected_version": 0},
    )
    assert updated.status_code == 200
    assert updated.json()["state_version"] == 1
    assert updated.json()["objective"] == "Уточнённая цель"

    stale = request(
        "PATCH",
        f"/api/workflows/{created['id']}",
        json={"objective": "Потерянное обновление", "expected_version": 0},
    )
    assert stale.status_code == 409

    events = request("GET", f"/api/workflows/{created['id']}/events").json()
    assert [event["event_type"] for event in events] == [
        "workflow.created",
        "workflow.updated",
    ]
    assert request("GET", f"/api/workflows/{created['id']}/rounds").json() == []
    assert request("GET", f"/api/workflows/{created['id']}/findings").json() == []
    assert request("GET", f"/api/workflows/{created['id']}/artifacts").json() == []


def test_openapi_contains_read_models_and_workflow_routes(isolated_db):
    schema = app.openapi()
    paths = schema["paths"]

    assert "/api/workflows" in paths
    assert "/api/workflows/{workflow_id}/events" in paths
    assert "/api/workflows/{workflow_id}/rounds/{round_id}/runs" in paths
    assert "WorkflowEventInDB" in schema["components"]["schemas"]


def test_workflow_api_validates_slug_and_duplicate(isolated_db):
    assert request("POST", "/api/workflows", json=payload("bad slug")).status_code == 422
    assert request("POST", "/api/workflows", json=payload()).status_code == 201
    assert request("POST", "/api/workflows", json=payload()).status_code == 409
    assert request("GET", "/api/workflows/does-not-exist").status_code == 404


def test_workflow_cli_read_only_commands(isolated_db):
    workflow = isolated_db.create_workflow(
        WorkflowCreate(**payload("cli-workflow")),
        workflow_id="wf_cli",
    )
    runner = CliRunner()

    listed = runner.invoke(cli, ["workflow", "list"])
    assert listed.exit_code == 0
    assert "cli-workflow" in listed.output

    shown = runner.invoke(cli, ["workflow", "show", workflow.slug, "--json"])
    assert shown.exit_code == 0
    assert '"id": "wf_cli"' in shown.output

    events = runner.invoke(cli, ["workflow", "events", workflow.id])
    assert events.exit_code == 0
    assert "workflow.created" in events.output

    findings = runner.invoke(cli, ["workflow", "findings", workflow.id])
    assert findings.exit_code == 0
    assert "Findings not found" in findings.output


def test_cli_has_no_w0_mutation_subcommands(isolated_db):
    runner = CliRunner()
    help_result = runner.invoke(cli, ["workflow", "--help"])

    assert help_result.exit_code == 0
    assert " start" not in help_result.output
    assert " cancel" not in help_result.output
    assert " delete" not in help_result.output
