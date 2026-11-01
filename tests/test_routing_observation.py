"""Public report paths consume produced health JSON; task failures use SQLite."""
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from promptpilot import pipeline_insights
from promptpilot.models import TaskCreate
from test_pipeline_report import _profile, _request


FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN = json.loads((FIXTURES / "routing-observation-origin.json").read_text(encoding="utf-8"))
RAW = (FIXTURES / "routing-observation-v1.json").read_bytes()
GOLDEN = json.loads(RAW)


def _publish_health(db, monkeypatch, health, *, stale=False):
    profile = _profile()
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    _cached, generation = pipeline_insights._cache_snapshot("example")
    epoch = pipeline_insights._cache_epoch()
    revision = db.increment_int_setting(pipeline_insights._refresh_revision_key("example"))
    now = datetime.now(timezone.utc) - timedelta(days=int(stale))
    assert pipeline_insights._publish_cache("example", profile, generation, epoch, revision, {
        "profile_id": "example", "title": profile["title"], "repository": profile["repository"],
        "queues": [{"id": queue["id"], "title": queue["title"], "backlog": None,
                    "membership_complete": False, "items": []} for queue in profile["queues"]],
        "history": {}, "health": {"state": "yellow", "label": "есть ожидания"},
        "diagnostics": copy.deepcopy(health), "generated_at": now.timestamp(),
    })


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda case: case["name"])
def test_public_api_and_real_ui_keep_go_produced_handoff(case, isolated_db, monkeypatch):
    assert hashlib.sha256(RAW).hexdigest() == ORIGIN["sha256"]
    assert GOLDEN["producer_head"] == ORIGIN["producer_head"] == "66239703ab6368e21e0065a971b871e55b54eec5"
    assert GOLDEN["producer_tree"] == ORIGIN["producer_tree"]
    assert GOLDEN["authority"] == "unverified-offline-input"
    _publish_health(isolated_db, monkeypatch, case["health"])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("observing cached health invoked GitHub or a provider")

    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", forbidden)
    response = _request("GET", "/api/pipeline-insights/example/report")
    assert response.status_code == 200
    report = response.json()
    routing = report["board"]["routing"]
    assert routing["available"] is True, (report["cache"], report["coverage"])
    assert routing["mutation_authorized"] is False
    assert routing["items"] == case["health"]["routing_observation"]["items"]
    if case["name"] in {"missing-claim", "changed-head-with-unsettled-pipeline"}:
        assert routing["complete"] is False
        assert all(group["count"] is None for group in routing["groups"]
                   if group["id"] != "tool_error")
        assert next(group for group in routing["groups"] if group["id"] == "unknown")["items"]
    state = _request("GET", "/api/pipeline-insights/example").json()
    assert state["routing"]["items"] == routing["items"]
    markdown = _request("GET", "/api/pipeline-insights/example/report?format=markdown")
    assert markdown.status_code == 200 and "Причины остановок" in markdown.text
    node = shutil.which("node")
    if node:
        result = subprocess.run([node, "tests/helpers/routing_observation.cjs"],
                                input=json.dumps(report, ensure_ascii=False),
                                text=True, encoding="utf-8", capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        assert "public API to real UI OK" in result.stdout


@pytest.mark.parametrize("stale,missing", [(True, False), (False, True)])
def test_public_api_cannot_turn_stale_or_old_checker_into_zero(stale, missing, isolated_db, monkeypatch):
    health = copy.deepcopy(GOLDEN["cases"][0]["health"])
    if missing:
        health.pop("routing_observation")
    _publish_health(isolated_db, monkeypatch, health, stale=stale)
    routing = _request("GET", "/api/pipeline-insights/example/report").json()["board"]["routing"]
    assert routing["available"] is False
    assert routing["complete"] is False
    assert routing["items"] == []
    assert all(group["count"] is None for group in routing["groups"] if group["id"] != "tool_error")


def test_public_report_separates_execution_error_from_model_decision(
        isolated_db, monkeypatch):
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": _profile()})
    failed = isolated_db.create_task(TaskCreate(prompt="Example - Review", recurrence="1h"))
    isolated_db.mark_failed(failed.id, "runner <unavailable>")
    human = isolated_db.create_task(TaskCreate(prompt="Example - Merge", recurrence="1h"))
    isolated_db.mark_completed(human.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК", verdict="НУЖЕН ЧЕЛОВЕК")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only report attempted a GitHub refresh")

    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    response = _request("GET", "/api/pipeline-insights/example/report")
    assert response.status_code == 200
    routing = response.json()["board"]["routing"]
    groups = {item["id"]: item for item in routing["groups"]}
    assert groups["tool_error"]["items"] == [{
        "task_id": failed.id, "series_id": failed.series_id,
        "category": "tool_error", "source": "task.status=failed",
        "recipient": "engineering", "summary": "runner <unavailable>",
        "transition_at": isolated_db.get_task(failed.id).completed_at.isoformat(),
    }]
    assert not groups["product"]["items"]
    assert groups["product"]["count"] is None
    assert routing["mutation_authorized"] is False


def test_public_report_keeps_old_failures_until_an_actual_completed_run(
        isolated_db, monkeypatch):
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": _profile()})
    task = isolated_db.create_task(TaskCreate(prompt="Example - Review", recurrence="1h"))
    isolated_db.mark_failed(task.id, "lost executor")
    # A human pause is not a completed run or proof that an executor stopped.
    isolated_db.series_action(task.series_id, "pause")
    response = _request("GET", "/api/pipeline-insights/example/report")
    group = next(item for item in response.json()["board"]["routing"]["groups"]
                 if item["id"] == "tool_error")
    assert group["items"][0]["task_id"] == task.id
    successor = isolated_db.create_task(TaskCreate(
        prompt="Example - Review", recurrence="1h", series_id=task.series_id))
    isolated_db.mark_completed(successor.id, "ИТОГ: ПУСТО", verdict="ПУСТО")
    response = _request("GET", "/api/pipeline-insights/example/report")
    group = next(item for item in response.json()["board"]["routing"]["groups"]
                 if item["id"] == "tool_error")
    assert group["items"] == []
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    assert isolated_db.pipeline_operational_errors([], since) == {"complete": True, "items": []}


def test_public_report_preserves_late_failure_after_newer_run_completed(isolated_db, monkeypatch):
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": _profile()})
    old = isolated_db.create_task(TaskCreate(prompt="Example - Review", recurrence="1h"))
    newer = isolated_db.create_task(TaskCreate(
        prompt="Example - Review", recurrence="1h", series_id=old.series_id))
    isolated_db.mark_completed(newer.id, "ИТОГ: ПУСТО", verdict="ПУСТО")
    isolated_db.mark_failed(old.id, "older executor failed later")
    response = _request("GET", "/api/pipeline-insights/example/report")
    group = next(item for item in response.json()["board"]["routing"]["groups"]
                 if item["id"] == "tool_error")
    assert group["items"][0]["task_id"] == old.id


@pytest.mark.parametrize("broken", [None, {"route": []}, {"evidence_status": []},
                                    {"categories": None}, {"blockers": {"injected": "value"}},
                                    {"blockers": [{"summary": "x", "evidence": {"options": "invalid"}}]}])
def test_public_report_rejects_malformed_rows_without_claiming_zero(broken, isolated_db, monkeypatch):
    health = copy.deepcopy(GOLDEN["cases"][0]["health"])
    original = health["routing_observation"]["items"][0]
    health["routing_observation"]["items"] = [None if broken is None else {**original, **broken}]
    _publish_health(isolated_db, monkeypatch, health)
    response = _request("GET", "/api/pipeline-insights/example/report")
    assert response.status_code == 200
    routing = response.json()["board"]["routing"]
    assert routing["complete"] is False
    assert routing["items"] == []
    assert all(group["count"] is None for group in routing["groups"] if group["id"] != "tool_error")
