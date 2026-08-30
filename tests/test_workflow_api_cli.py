import asyncio
import io
from pathlib import Path

import httpx
from click.testing import CliRunner

from promptpilot.api import HERDR_UI_KEYS, app
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
    assert "/api/workflows/validate-setup" in paths
    assert "/api/workflows/{workflow_id}/events" in paths
    assert "/api/workflows/{workflow_id}/rounds/{round_id}/runs" in paths
    assert "/api/workflows/{workflow_id}/advance" in paths
    assert "/api/workflows/{workflow_id}/report" in paths
    assert "/api/workflows/{workflow_id}/plan" in paths
    assert "/api/workflows/{workflow_id}/stages" in paths
    assert "/api/workflows/{workflow_id}/plan/dispatch" in paths
    assert "/api/workflows/{workflow_id}/plan/approve" in paths
    assert "WorkflowEventInDB" in schema["components"]["schemas"]


def test_codex_provider_exposes_effort_without_claude_skill_toggle(isolated_db):
    codex = request("GET", "/api/providers").json()["codex"]

    assert codex["supports_effort"] is True
    assert codex["supports_skills"] is False


def test_workflow_setup_preflight_accepts_repo_provider_and_gate(isolated_db, monkeypatch):
    monkeypatch.setattr("promptpilot.api.load_providers", lambda: {"test-provider": {"cmd": "test"}})
    monkeypatch.setattr("promptpilot.api.provider_available", lambda info: True)
    response = request(
        "POST", "/api/workflows/validate-setup", json={
            "repository_path": str(Path(__file__).parents[1]),
            "candidate_branch": "feature/workflow-wizard",
            "providers": ["test-provider"],
            "gate_commands": ["python --version"],
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["ready"] is True
    assert {item["code"] for item in result["checks"]} >= {
        "repository", "branch", "provider", "gate",
    }


def test_workflow_setup_preflight_rejects_invalid_inputs(isolated_db, tmp_path):
    response = request(
        "POST", "/api/workflows/validate-setup", json={
            "repository_path": str(tmp_path / "missing"),
            "candidate_branch": "bad branch",
            "providers": ["missing-provider"],
            "gate_commands": ["if ("],
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["ready"] is False
    assert sum(item["status"] == "error" for item in result["checks"]) >= 4


def test_active_workflow_allows_config_but_not_metadata_edits(isolated_db):
    created = request("POST", "/api/workflows", json=payload("active-config")).json()
    started = request(
        "POST", f"/api/workflows/{created['id']}/start",
        json={"expected_version": created["state_version"]},
    ).json()
    assert started["status"] == "queued"

    updated = request(
        "PATCH", f"/api/workflows/{created['id']}",
        json={
            "expected_version": started["state_version"],
            "config": {"limits": {"max_rounds": 9}},
        },
    )
    assert updated.status_code == 200
    assert updated.json()["config"]["limits"]["max_rounds"] == 9

    rejected = request(
        "PATCH", f"/api/workflows/{created['id']}",
        json={
            "expected_version": updated.json()["state_version"],
            "objective": "scope mutation while active",
        },
    )
    assert rejected.status_code == 409


def test_web_ui_exposes_workflow_and_live_agent_controls():
    html = (Path(__file__).parents[1] / "promptpilot" / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="workflowModal"' in html
    assert 'onclick="openWorkflows()"' in html
    assert "function wfDispatch(role)" in html
    assert "function wfStopTask(taskId)" in html
    assert "function wfRefreshScreen" in html
    assert "function wfSendKey" in html
    assert "function workflowStatusInfo" in html
    assert "function workflowOwnerInfo" in html
    assert "Мяч сейчас:" in html
    assert "function workflowStopReason" in html
    assert "function autoRefreshWorkflowState" in html
    assert "function captureWorkflowOpenDetails" in html
    assert "function restoreWorkflowOpenDetails" in html
    assert 'data-wf-detail="audit-report"' in html
    assert 'data-wf-detail="technical-log"' in html
    assert "AUDIT_VERDICT:" in html
    assert "Показать отчёт независимого аудитора" in html
    assert "Причина остановки:" in html
    assert "review.awaiting_decision" in html
    assert "История завершённых раундов" in html
    assert "Технический журнал" in html
    assert "Настройки автономного workflow" in html
    assert "Новый автономный workflow" in html
    assert "1. Задача" in html
    assert "2. Команда" in html
    assert "3. Правила" in html
    assert "4. Проверка" in html
    assert "function wfWizardRunPreflight" in html
    assert "function wfWizardApplyPreset" in html
    assert "function wfWizardSaveTemplate" in html
    assert "workflows/validate-setup" in html
    assert "Создать и сформировать план" in html
    assert "skip permissions" in html
    assert "function preferredTaskProvider" in html
    assert "promptpilot.last-provider" in html
    assert 'id="providerModeHint"' in html
    assert "Живая сессия уже имеет свой каталог" in html
    assert "function wfSaveSettings" in html
    assert "function wfPlannerDispatch" in html
    assert "function wfSavePlan" in html
    assert "function wfApprovePlan" in html
    assert "План этапов" in html
    assert "function wfDownloadReport" in html
    assert "агент ещё работает" in html
    assert "orphanPane" in html
    assert 'id="wfAgentScreen"' in html
    assert "a.agent || 'agent'" in html
    assert HERDR_UI_KEYS == ("enter", "1", "2", "3", "4", "esc")


def test_workflow_api_validates_slug_and_duplicate(isolated_db):
    assert request("POST", "/api/workflows", json=payload("bad slug")).status_code == 422
    assert request("POST", "/api/workflows", json=payload()).status_code == 201
    assert request("POST", "/api/workflows", json=payload()).status_code == 409
    assert request("GET", "/api/workflows/does-not-exist").status_code == 404


def test_w3_plan_api_dispatch_edit_and_approve(isolated_db):
    body = payload("api-stage-plan")
    body["config"] = {
        "planning": {"enabled": True, "require_approval": True},
        "automation": {"enabled": False},
    }
    created = request("POST", "/api/workflows", json=body).json()
    dispatched = request(
        "POST", f"/api/workflows/{created['id']}/plan/dispatch",
        json={"expected_version": created["state_version"]},
    )
    assert dispatched.status_code == 200
    task = isolated_db.get_next_runnable()
    isolated_db.mark_completed(
        task.id,
        "WORKFLOW_PLAN_JSON_BEGIN\n"
        '{"stages":[{"code":"FINAL","title":"Контракт","objective":"Сделать контракт","stage_type":"integration"}]}'
        "\nWORKFLOW_PLAN_JSON_END\nИТОГ: ГОТОВО — план",
        exit_code=0,
    )
    isolated_db.set_verdict(task.id, "ГОТОВО")
    synced = request("POST", f"/api/workflows/{created['id']}/sync")
    assert synced.status_code == 200
    assert synced.json()["workflow"]["status"] == "awaiting_plan_approval"
    plan = request("GET", f"/api/workflows/{created['id']}/plan").json()
    stages = request("GET", f"/api/workflows/{created['id']}/stages").json()
    assert plan["status"] == "awaiting_approval"
    assert [stage["code"] for stage in stages] == ["FINAL"]

    current = request("GET", f"/api/workflows/{created['id']}").json()
    edited = request(
        "PUT", f"/api/workflows/{created['id']}/plan",
        json={
            "expected_version": current["state_version"],
            "stages": [{
                "code": "FINAL", "title": "Контракт API", "stage_type": "integration",
                "objective": "Проверить редактирование", "acceptance_gates": ["pytest -q"],
            }],
        },
    )
    assert edited.status_code == 200
    assert edited.json()[0]["title"] == "Контракт API"

    current = request("GET", f"/api/workflows/{created['id']}").json()
    approved = request(
        "POST", f"/api/workflows/{created['id']}/plan/approve",
        json={"expected_version": current["state_version"]},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"
    assert approved.json()["current_stage_id"]


def test_w1_api_manual_start_dispatch_sync_and_gate(isolated_db):
    created = request("POST", "/api/workflows", json=payload("api-w1")).json()
    started = request(
        "POST",
        f"/api/workflows/{created['id']}/start",
        json={"expected_version": 0, "base_sha": "abc123"},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "queued"

    dispatched = request(
        "POST",
        f"/api/workflows/{created['id']}/dispatch",
        json={
            "expected_version": started.json()["state_version"],
            "role": "executor",
            "prompt": "Implement the next round",
        },
    )
    assert dispatched.status_code == 200
    task_id = dispatched.json()["task"]["id"]

    task = isolated_db.get_next_runnable()
    assert task.id == task_id
    isolated_db.mark_completed(task.id, "executor complete", exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    synced = request("POST", f"/api/workflows/{created['id']}/sync")
    assert synced.status_code == 200
    assert synced.json()["workflow"]["status"] == "gating"

    current = synced.json()["workflow"]
    gate = request(
        "POST",
        f"/api/workflows/{created['id']}/gate",
        json={
            "expected_version": current["state_version"],
            "verdict": "PASS",
            "gate_id": "test-gate",
        },
    )
    assert gate.status_code == 200
    assert gate.json()["status"] == "reviewing"


def test_w1_dispatch_preserves_herdr_target_and_effort(isolated_db):
    created = request("POST", "/api/workflows", json=payload("api-herdr")).json()
    started = request(
        "POST",
        f"/api/workflows/{created['id']}/start",
        json={"expected_version": 0},
    ).json()

    dispatched = request(
        "POST",
        f"/api/workflows/{created['id']}/dispatch",
        json={
            "expected_version": started["state_version"],
            "role": "executor",
            "prompt": "Continue in the existing agy session",
            "provider": "herdr-session",
            "herdr_target": "wB:p1",
            "effort": "high",
            "keep_pane": True,
        },
    )

    assert dispatched.status_code == 200
    task = dispatched.json()["task"]
    assert task["provider"] == "herdr-session"
    assert task["herdr_target"] == "wB:p1"
    assert task["effort"] == "high"
    assert task["keep_pane"] is True
    events = request(
        "GET", f"/api/workflows/{created['id']}/events"
    ).json()
    assert events[-2]["payload"]["herdr_target"] == "wB:p1"


def test_w1_api_history_import_is_idempotent(isolated_db):
    created = request("POST", "/api/workflows", json=payload("api-history")).json()
    history = {
        "expected_version": 0,
        "idempotency_key": "legacy-v1",
        "source": "git+audit",
        "rounds": [{
            "round_no": 3,
            "status": "revision_required",
            "facts": [{
                "claim": "Runtime gate passed",
                "status": "VERIFIED",
                "source": "audit-log",
            }],
        }],
    }
    first = request(
        "POST", f"/api/workflows/{created['id']}/history/import", json=history
    )
    second = request(
        "POST", f"/api/workflows/{created['id']}/history/import", json=history
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["state_version"] == second.json()["state_version"] == 1
    events = request(
        "GET", f"/api/workflows/{created['id']}/events"
    ).json()
    assert [event["event_type"] for event in events].count("history.fact_imported") == 1


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


def test_cli_exposes_w1_manual_pilot_commands(isolated_db):
    runner = CliRunner()
    help_result = runner.invoke(cli, ["workflow", "--help"])

    assert help_result.exit_code == 0
    for command in (
        "create", "start", "dispatch", "gate", "review", "input", "cancel",
        "sync", "import-history",
    ):
        assert command in help_result.output


def test_cli_create_and_import_history(isolated_db, tmp_path):
    workflow_file = tmp_path / "workflow.json"
    workflow_file.write_text(
        __import__("json").dumps(payload("cli-import"), ensure_ascii=False),
        encoding="utf-8",
    )
    history_file = tmp_path / "history.json"
    history_file.write_text(
        __import__("json").dumps({
            "idempotency_key": "pre-pilot-v1",
            "source": "manual+git",
            "rounds": [{
                "round_no": 1,
                "status": "completed",
                "facts": [{
                    "claim": "Baseline hash was recorded",
                    "status": "VERIFIED",
                    "source": "manifest.json",
                }],
            }],
        }),
        encoding="utf-8",
    )
    runner = CliRunner()

    created = runner.invoke(cli, ["workflow", "create", str(workflow_file)])
    imported = runner.invoke(
        cli, ["workflow", "import-history", "cli-import", str(history_file)]
    )

    assert created.exit_code == 0
    assert "Created cli-import" in created.output
    assert imported.exit_code == 0
    assert "Imported history through round 1" in imported.output


def test_windows_utf8_stream_setup_handles_non_cp1251_text(monkeypatch):
    from promptpilot import cli as cli_module

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1251")
    monkeypatch.setattr(cli_module.os, "name", "nt")
    monkeypatch.setattr(cli_module.sys, "stdout", stream)
    monkeypatch.setattr(cli_module.sys, "stderr", stream)

    cli_module._ensure_windows_utf8_streams()
    stream.write("УТ10 → БП3")
    stream.flush()

    assert stream.encoding.lower().replace("-", "") == "utf8"
    assert raw.getvalue().decode("utf-8") == "УТ10 → БП3"
