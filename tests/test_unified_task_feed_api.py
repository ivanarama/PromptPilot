"""Unified API integration: real local DB, loopback peers and native inbox.

No production peer, worker, client GUI or external provider is contacted.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from promptpilot import api, db, task_feed, workflows
from promptpilot.models import TaskCreate, WorkflowCreate, WorkflowRoundCreate, WorkflowRunCreate
from test_external_inbox import ASGIClient, data, result, session, tool, waiting
from test_task_sources import entry, item, peer, registry


@pytest.fixture
def client(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("PP_TASK_SOURCES_FILE", str(tmp_path / "not-registered.json"))
    monkeypatch.setattr(api, "API_TOKEN", "")
    return ASGIClient(api.app)


def feed(client, **params):
    response = client.get("/api/task-feed", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def grouped_workflow(tmp_path):
    wf = db.create_workflow(WorkflowCreate(slug="grouped-business", objective="One approved goal",
        repository_path=str(tmp_path), candidate_branch="draft/grouped"))
    children = []
    for number in (1, 2):
        rnd = db.create_workflow_round(WorkflowRoundCreate(workflow_id=wf.id, round_no=number))
        for role in ("executor", "reviewer"):
            for attempt in (1, 2):
                child = db.create_task(TaskCreate(prompt=f"Internal {role} revision {number}/{attempt}"))
                db.create_workflow_run(WorkflowRunCreate(workflow_id=wf.id, round_id=rnd.id,
                    role=role, attempt_no=attempt, task_id=child.id, input_sha256="fixture-input"))
                children.append(child)
    return wf, children


def test_grouping_counts_filters_and_pagination_happen_after_peer_merge(client, tmp_path, monkeypatch):
    wf, children = grouped_workflow(tmp_path)
    outside = db.create_task(TaskCreate(prompt="Outside independent task"))
    failed = db.create_task(TaskCreate(prompt="Outside failed task"))
    with db._connect(immediate=True) as conn:
        conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (failed.id,))
    def remote(status):
        def respond(path):
            assert urlsplit(path).path == "/api/task-feed"
            assert "local_only=true" in path
            return 200, {"items": [item(wf.id, status=status)], "total": 1}, {}
        return respond
    with peer(remote("active")) as (first, first_calls), peer(remote("completed")) as (second, second_calls):
        monkeypatch.setenv("PP_TASK_SOURCES_FILE", str(registry(tmp_path, entry(first, "peer_a"), entry(second, "peer_b"))))
        pages = [feed(client, limit=2, offset=n) for n in (0, 2, 4)]
        assert all(page["total"] == 5 for page in pages)
        rows = [row for page in pages for row in page["items"]]
        assert len(rows) == 5 and len({row["id"] for row in rows}) == 5
        assert {row["id"] for row in rows} == {
            "local:workflow:" + wf.id, "peer_a:workflow:" + wf.id, "peer_b:workflow:" + wf.id,
            f"local:task:{outside.id}", f"local:task:{failed.id}"}
        assert all(f"local:task:{child.id}" not in {r["id"] for r in rows} for child in children)
        expected = {state: 0 for state in task_feed.STATUSES}
        expected.update(queued=2, active=1, completed=1, needs_attention=1)
        assert all(page["counts"] == expected for page in pages)
        only_peer = feed(client, source_id="peer_a")
        assert only_peer["total"] == 1 and only_peer["items"][0]["id"] == "peer_a:workflow:" + wf.id
        assert only_peer["counts"]["active"] == 1 and sum(only_peer["counts"].values()) == 1
        only_workflows = feed(client, kind="workflow")
        assert only_workflows["total"] == 3 and sum(only_workflows["counts"].values()) == 3
        only_completed = feed(client, status="completed")
        assert only_completed["total"] == 1 and only_completed["counts"] == expected
        searched = feed(client, q="Outside")
        assert searched["total"] == 2 and sum(searched["counts"].values()) == 2
        assert client.get("/api/task-feed", params={"source_id": "unknown"}).status_code == 404
    assert first_calls and second_calls
    assert all(call["method"] == "GET" for call in first_calls + second_calls)


def test_local_only_skips_remote_getter_and_preserves_grouping(client, tmp_path, monkeypatch):
    wf, _ = grouped_workflow(tmp_path)
    monkeypatch.setattr(api.task_sources, "list_source_items", lambda: pytest.fail("local_only reached federation"))
    page = feed(client, local_only=True)
    assert page["total"] == 1 and page["items"][0]["object_id"] == wf.id
    assert [source["id"] for source in page["sources"]] == ["local"]
    assert page["errors"] == []


def test_unavailable_registered_source_remains_visible_with_local_items(client, tmp_path, monkeypatch):
    standalone = db.create_task(TaskCreate(prompt="Local work survives peer failure"))
    with peer(lambda path: (503, {"error": "PRIVATE-AUTH-DETAIL"}, {})) as (url, calls):
        monkeypatch.setenv("PP_TASK_SOURCES_FILE", str(registry(tmp_path, entry(url, "offline_peer"))))
        page = feed(client)
    assert page["total"] == 1 and page["items"][0]["object_id"] == standalone.id
    state = next(source for source in page["sources"] if source["id"] == "offline_peer")
    assert state["status"] == "unavailable" and state["readonly"]
    assert state["error"]["code"] and state["error"]["message"]
    assert "PRIVATE-AUTH-DETAIL" not in json.dumps(page) and url not in json.dumps(page)
    assert len(calls) == 1


def test_exact_current_claim_promotes_only_its_one_workflow_card(client, tmp_path):
    first = waiting(tmp_path, slug="first-external")
    second = waiting(tmp_path, slug="second-external")
    token = session(client)
    claimed = data(tool(client, token))
    page = feed(client, local_only=True)
    rows = {row["object_id"]: row for row in page["items"]}
    assert page["total"] == 2 and set(rows) == {first.id, second.id}
    assert sum(row["status"] == "active" for row in rows.values()) == 1
    assert sum(row["status"] == "blocked" for row in rows.values()) == 1
    active = rows[claimed["workflow_id"]]
    assert active["external_execution"]["state"] == "claimed"
    assert active["external_execution"]["last_seen_at"] and not active["external_execution"]["execution_observed"]
    assert active["workflow"]["state_version"] == claimed["expected_version"]
    assert db.list_tasks() == []


@pytest.mark.parametrize("retarget", [False, True])
def test_stale_claim_never_replaces_current_configured_target(client, tmp_path, retarget):
    wf = waiting(tmp_path)
    claimed = data(tool(client, session(client)))
    with db._connect(immediate=True) as conn:
        cfg = db.get_workflow(wf.id).config
        if retarget:
            cfg["external_executor"]["client_id"] = "replacement"
        conn.execute("UPDATE workflows SET config_json=?,state_version=state_version+1 WHERE id=?",
                     (json.dumps(cfg), wf.id))
    row = feed(client, local_only=True)["items"][0]
    assert row["status"] == "blocked"
    assert row["external_execution"]["state"] == "awaiting_connection"
    assert row["external_execution"]["client_id"] == ("replacement" if retarget else "glm")
    assert row["workflow"]["state_version"] != claimed["expected_version"]


def test_unknown_submission_of_still_current_assignment_needs_attention(client, tmp_path, monkeypatch):
    wf = waiting(tmp_path)
    token = session(client)
    claimed = data(tool(client, token))
    def unknown(*args):
        raise TimeoutError("unknown native outcome")
    monkeypatch.setattr(workflows, "submit_external_result", unknown)
    assert tool(client, token, "pilot_submit_result", result(claimed))["isError"]
    row = feed(client, local_only=True)["items"][0]
    assert row["object_id"] == wf.id and row["status"] == "needs_attention"
    assert row["external_execution"]["state"] == "unknown"
    assert db.get_workflow(wf.id).status.value == "awaiting_external"


def test_detail_report_files_and_internal_history_belong_to_the_same_card(client, tmp_path):
    wf = waiting(tmp_path)
    token = session(client)
    claimed = data(tool(client, token))
    ack = data(tool(client, token, "pilot_submit_result", result(claimed)))
    page = feed(client, local_only=True)
    assert page["total"] == 1 and page["items"][0]["id"] == "local:workflow:" + wf.id
    response = client.get(f"/api/task-feed/local/workflow/{wf.id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["item"]["id"] == page["items"][0]["id"]
    report = detail["external_result"]
    assert report["run_id"] == claimed["run_id"] and report["receipt_id"] == ack["receipt_id"]
    assert report["unreviewed"] and "UNREVIEWED EXTERNAL RESULT" in report["text"]
    assert len(detail["history"]["runs"]) == 1 and detail["history"]["runs"][0]["id"] == claimed["run_id"]
    assert report["artifacts"][0]["path"] == "notes/proposal.md"
    assert client.get(report["artifacts"][0]["download_url"]).content == b"Unreviewed proposal & exact labels."
    serialized = json.dumps(detail)
    assert token not in serialized and claimed["claim_token"] not in serialized
    assert "input_json" not in serialized and "config_json" not in serialized
    assert db.list_tasks() == []


@pytest.mark.parametrize("object_id", ["0", "-1", "9223372036854775808", "9" * 5000, "１２"])
@pytest.mark.parametrize("source_id", ["local", "registered_peer"])
def test_invalid_local_and_remote_task_ids_are_404_before_lookup(client, monkeypatch, object_id, source_id):
    monkeypatch.setattr(api.task_sources, "get_source_detail", lambda *a, **k: pytest.fail("invalid remote ID reached lookup"))
    assert client.get(f"/api/task-feed/{source_id}/task/{object_id}").status_code == 404


def test_asset_serves_captured_javascript_after_runtime_source_disappears(client, tmp_path, monkeypatch):
    expected = api._TASK_FEED_JS
    monkeypatch.setattr(api, "STATIC_DIR", tmp_path / "absent-frozen-extraction")
    original = Path.read_text
    def no_script_read(path, *args, **kwargs):
        if path.name == "task-feed.js":
            pytest.fail("asset attempted late source read")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", no_script_read)
    response = client.get("/assets/task-feed.js")
    assert response.status_code == 200 and response.text == expected and len(response.content) > 100
    assert "javascript" in response.headers["content-type"]