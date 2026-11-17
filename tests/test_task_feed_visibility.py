"""Hiding a dashboard card preserves execution history and peer exports."""
import sqlite3

import pytest

from promptpilot import api, db, task_feed, task_feed_visibility
from promptpilot.models import TaskCreate
from test_external_inbox import ASGIClient


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", "")
    monkeypatch.setattr(api.task_sources, "list_source_items", lambda: {"items": [], "sources": [], "errors": []})
    return ASGIClient(api.app)


def visibility(client, source, kind, item, hidden):
    return client.request("PUT", f"/api/task-feed/{source}/{kind}/{item}/visibility", json={"hidden": hidden})


def test_local_hide_survives_refresh_and_restore_without_changing_task(client):
    old = db.create_task(TaskCreate(prompt="Old card"))
    other = db.create_task(TaskCreate(prompt="Current card"))
    before = db.get_task(old.id).model_dump()
    assert visibility(client, "local", "task", old.id, True).status_code == 200
    for _ in range(2):
        listing = client.get("/api/task-feed").json()
        assert [r["object_id"] for r in listing["items"]] == [other.id]
        assert listing["total"] == 1 and sum(listing["counts"].values()) == 1
    assert db.get_task(old.id).model_dump() == before
    all_items = client.get("/api/task-feed?include_hidden=true").json()
    assert all_items["total"] == 2
    assert next(i for i in all_items["items"] if i["object_id"] == old.id)["hidden"] is True
    assert client.get(f"/api/task-feed/local/task/{old.id}").status_code == 200
    assert visibility(client, "local", "task", old.id, False).status_code == 200
    assert client.get("/api/task-feed").json()["total"] == 2


@pytest.mark.parametrize("kind", ["task", "request", "workflow"])
def test_readonly_source_hide_is_local_and_namespaced(client, monkeypatch, kind):
    local = db.create_task(TaskCreate(prompt="Local"))
    item = task_feed.project_task({"id": local.id, "prompt": "Remote", "status": "pending"},
        source_id="peer", source_label="Peer", readonly=True)
    item.update(kind=kind, id=f"peer:{kind}:{local.id}")
    monkeypatch.setattr(api.task_sources, "list_source_items", lambda: {
        "items": [item], "sources": [{"id": "peer", "status": "online"}], "errors": []})
    assert visibility(client, "peer", kind, local.id, True).status_code == 200
    listing = client.get("/api/task-feed?limit=1").json()
    assert listing["total"] == 1 and listing["items"][0]["source_id"] == "local"
    assert client.get("/api/task-feed?source_id=peer").json()["total"] == 0
    assert client.get("/api/task-feed?include_hidden=true").json()["total"] == 2
    assert item.get("hidden") is None  # Source dictionaries are not mutated.


def test_hidden_local_item_remains_in_federation_export(client):
    item = db.create_task(TaskCreate(prompt="Exported card"))
    assert visibility(client, "local", "task", item.id, True).status_code == 200
    exported = client.get("/api/task-feed?local_only=true").json()
    assert exported["total"] == 1 and exported["items"][0]["object_id"] == item.id
    assert "hidden" not in exported["items"][0]


def test_visibility_requires_auth_and_strict_boolean(client, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", "owner-credential")
    assert visibility(client, "local", "task", "1", True).status_code == 401
    assert client.request("PUT", "/api/task-feed/local/task/1/visibility", json={"hidden": "false"},
        headers={"Authorization": "Bearer owner-credential"}).status_code == 422
    assert client.request("PUT", "/api/task-feed/local/task/1/visibility", json={"hidden": True, "cancel": True},
        headers={"Authorization": "Bearer owner-credential"}).status_code == 422


def test_visibility_read_does_not_create_database(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    with pytest.raises(task_feed.FeedUnavailable):
        task_feed_visibility.apply([])
    assert not path.exists()


def test_legacy_database_without_settings_is_readable(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks(id INTEGER)")
    monkeypatch.setattr(db, "DB_PATH", path)
    assert task_feed_visibility.apply([]) == []


def test_more_than_two_hundred_hidden_cards_are_all_preserved(client):
    for number in range(205):
        task_feed_visibility.set_hidden("peer", "task", str(number), True)
    rows = [dict(source_id="peer", kind="task", object_id=str(n)) for n in range(206)]
    assert task_feed_visibility.apply(rows) == [rows[-1]]
