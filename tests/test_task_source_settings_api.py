"""Actual authenticated API and real temporary registry/credentials/HTTP peers."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from promptpilot import api
from test_external_inbox import ASGIClient
from test_task_sources import entry, peer, registry


@pytest.fixture
def owner(isolated_db, tmp_path, monkeypatch):
    path = tmp_path / "task-sources.json"
    monkeypatch.setenv("PP_TASK_SOURCES_FILE", str(path))
    monkeypatch.setattr(api, "API_TOKEN", "")
    return ASGIClient(api.app), path


def put(client, source_id, revision, **values):
    return client.request("PUT", "/api/task-sources/" + source_id, json={
        "id": source_id, "label": "Studio Windows", "base_url": "https://worker.example",
        "type": "promptpilot", "auth_mode": "none", "revision": revision, **values,
    })


def test_default_install_is_empty_editable_and_read_only(owner):
    client, path = owner
    before = set(path.parent.iterdir())
    response = client.get("/api/task-sources")
    assert response.status_code == 200
    assert response.json() == {"revision": "missing", "editable": True, "sources": [], "errors": []}
    assert set(path.parent.iterdir()) == before


def test_create_probe_edit_rotate_disconnect_through_actual_api(owner):
    client, path = owner
    secret = "source-key-never-returned-0123456789"
    second_secret = "replacement-key-never-returned-9876543210"
    def respond(route):
        assert urlsplit(route).path == "/api/task-feed"
        return 200, {"items": [], "total": 0}, {}
    with peer(respond) as (url, calls):
        draft = {"id": "studio", "label": "Studio Windows", "base_url": url,
                 "auth_mode": "token", "token": secret}
        probe = client.post("/api/task-sources/probe", json={"draft": draft})
        assert probe.status_code == 200 and probe.json()["capabilities"] == {"tasks": True, "workflows": True}
        assert not path.exists() and not path.with_name(path.name + ".credentials").exists()
        created = put(client, "studio", "missing", base_url=url, auth_mode="token", token=secret)
        assert created.status_code == 200, created.text
        snapshot = created.json()
        assert snapshot["editable"] and snapshot["sources"][0]["has_token"]
        assert snapshot["sources"][0]["type"] == "promptpilot"
        stored = json.loads(path.read_text(encoding="utf-8"))["sources"][0]
        key_file = Path(stored["token_file"])
        assert key_file.read_text() == secret and secret not in path.read_text()
        assert secret not in created.text and "token_file" not in created.text and "token_env" not in created.text
        checked = client.post("/api/task-sources/probe", json={"source_id": "studio"})
        assert checked.status_code == 200 and checked.json()["api"] == "task-feed"
        assert all(call["authorization"] == "Bearer " + secret and call["method"] == "GET" for call in calls)
        renamed = put(client, "studio", snapshot["revision"], label="Remote studio", base_url=url, auth_mode="preserve")
        assert renamed.status_code == 200, renamed.text
        assert json.loads(path.read_text())["sources"][0]["token_file"] == str(key_file)
        rotated = put(client, "studio", renamed.json()["revision"], base_url=url, auth_mode="token", token=second_secret)
        assert rotated.status_code == 200, rotated.text
        assert second_secret not in rotated.text and key_file.read_text() == secret
        new_key = Path(json.loads(path.read_text())["sources"][0]["token_file"])
        assert new_key != key_file and new_key.read_text() == second_secret
        stale = client.request("DELETE", "/api/task-sources/studio", json={"revision": snapshot["revision"]})
        assert stale.status_code == 409 and len(json.loads(path.read_text())["sources"]) == 1
        deleted = client.request("DELETE", "/api/task-sources/studio", json={"revision": rotated.json()["revision"]})
        assert deleted.status_code == 200 and deleted.json()["sources"] == []
        assert key_file.read_text() == secret and new_key.read_text() == second_secret


def test_changed_address_never_receives_preserved_credential(owner):
    client, path = owner
    secret = "private-existing-peer-credential"
    with peer(lambda _: (200, {"items": [], "total": 0}, {})) as (first, _), \
            peer(lambda _: (200, {"items": [], "total": 0}, {})) as (second, calls):
        original = put(client, "studio", "missing", base_url=first, auth_mode="token", token=secret)
        assert original.status_code == 200, original.text
        raw = path.read_bytes()
        result = put(client, "studio", original.json()["revision"], base_url=second, auth_mode="preserve")
        assert result.status_code == 400 and path.read_bytes() == raw
        probe = client.post("/api/task-sources/probe", json={"draft": {
            "id": "studio", "label": "Changed address", "base_url": second, "auth_mode": "preserve"}})
        assert probe.status_code == 400 and calls == []
        assert secret not in result.text + probe.text


def test_legacy_peer_has_explicit_capability_reason_in_probe_and_task_list(owner):
    client, _path = owner
    def respond(route):
        if urlsplit(route).path == "/api/task-feed":
            return 404, {}, {}
        assert urlsplit(route).path == "/api/workflows"
        return 200, [], {}
    with peer(respond) as (url, calls):
        saved = put(client, "remote", "missing", base_url=url)
        assert saved.status_code == 200, saved.text
        result = client.post("/api/task-sources/probe", json={"source_id": "remote"})
        assert result.status_code == 200
        assert result.json()["status"] == "partial" and result.json()["api"] == "workflows"
        assert result.json()["capabilities"] == {"tasks": False, "workflows": True}
        assert "Старый API" in result.json()["message"] and "Workflow" in result.json()["message"]
        feed = client.get("/api/task-feed")
        source = next(row for row in feed.json()["sources"] if row["id"] == "remote")
        assert source["status"] == "partial" and "Старый API" in source["message"]
        assert all(call["method"] == "GET" for call in calls)


def test_environment_binding_is_preserved_without_exposing_its_name(owner, monkeypatch):
    client, path = owner
    monkeypatch.setenv("FIXTURE_PRIVATE_SOURCE_KEY", "never-return-env-key")
    registry(path.parent, entry("https://peer.example", "environment-peer", token_env="FIXTURE_PRIVATE_SOURCE_KEY"))
    before = client.get("/api/task-sources")
    assert before.status_code == 200 and before.json()["sources"][0]["auth_kind"] == "env"
    assert "FIXTURE_PRIVATE_SOURCE_KEY" not in before.text and "never-return-env-key" not in before.text
    edited = put(client, "environment-peer", before.json()["revision"], base_url="https://peer.example", auth_mode="preserve")
    assert edited.status_code == 200, edited.text
    assert json.loads(path.read_text())["sources"][0]["token_env"] == "FIXTURE_PRIVATE_SOURCE_KEY"


def test_malformed_registry_cannot_be_silently_replaced(owner):
    client, path = owner
    raw = b'{"version":1,"sources":[{"id":"invalid","unknown":"keep-original"}]}'
    path.write_bytes(raw)
    current = client.get("/api/task-sources")
    assert current.status_code == 200 and not current.json()["editable"]
    result = put(client, "new", hashlib.sha256(raw).hexdigest())
    assert result.status_code == 409 and path.read_bytes() == raw


@pytest.mark.parametrize("route,body", [
    ("/api/task-sources/studio", {"id": "studio", "label": "Studio", "base_url": "https://peer.example", "revision": False}),
    ("/api/task-sources/studio", {"label": [], "base_url": "https://peer.example"}),
    ("/api/task-sources/probe", {"draft": {"id": "studio", "label": [], "base_url": "https://peer.example"}}),
])
def test_invalid_typed_request_never_echoes_write_only_secret(owner, route, body):
    client, path = owner
    secret = "DO-NOT-ECHO-NEW-SOURCE-CREDENTIAL"
    if "draft" in body:
        body["draft"]["token"] = secret
    else:
        body["token"] = secret
    response = client.request("POST" if route.endswith("/probe") else "PUT", route, json=body)
    assert response.status_code == 422 and secret not in response.text
    assert not path.exists()


def test_existing_global_auth_and_browser_checks_cover_source_management(owner, monkeypatch):
    client, path = owner
    monkeypatch.setattr(api, "API_TOKEN", "local-owner-api-key")
    assert client.get("/api/task-sources").status_code == 401
    response = client.request("PUT", "/api/task-sources/studio", headers={
        "Authorization": "Bearer local-owner-api-key", "Sec-Fetch-Site": "cross-site"}, json={
            "id": "studio", "label": "Studio", "base_url": "https://peer.example", "revision": "missing"})
    assert response.status_code == 403 and not path.exists()
    assert client.get("/api/task-sources", headers={"Authorization": "Bearer local-owner-api-key"}).status_code == 200


def test_source_script_survives_removed_static_directory_and_other_validation_is_unchanged(owner, monkeypatch, tmp_path):
    client, _path = owner
    monkeypatch.setattr(api, "STATIC_DIR", tmp_path / "deleted-unpacked-static")
    result = client.get("/assets/task-source-settings.js")
    assert result.status_code == 200 and "function openTaskSourceSettings" in result.text
    assert result.headers["cache-control"] == "no-store"
    ordinary = client.post("/api/tasks", json={})
    assert ordinary.status_code == 422 and isinstance(ordinary.json()["detail"], list)
