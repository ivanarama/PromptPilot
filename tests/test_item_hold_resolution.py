"""Explicit one-item hold control through public API and real SQLite commits."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
import sqlite3
import threading

import httpx
import pytest

from promptpilot import api, pipeline_insights
from promptpilot.models import TaskCreate


def request(method, path, body=None):
    async def send():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
                base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, json=body)
    return asyncio.run(send())


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def database_snapshot(database):
    with closing(sqlite3.connect(database.DB_PATH)) as c:
        return {name: c.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
                for name in ("tasks", "task_series", "settings", "pipeline_target_reservations")}


@pytest.fixture
def held(isolated_db, monkeypatch):
    # These control endpoints must never scan GitHub, launch a provider or wake work.
    def forbidden(*args, **kwargs):
        raise AssertionError("hold control must not run pipeline admission")
    monkeypatch.setattr(pipeline_insights, "read_cached", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", forbidden)
    monkeypatch.setattr(pipeline_insights, "_tool_preflight", forbidden)
    target = isolated_db.create_task(TaskCreate(prompt="Generic MERGE", recurrence="30m"))
    other = isolated_db.create_task(TaskCreate(prompt="Other active queue", recurrence="1h", priority=1))
    running = isolated_db.get_next_runnable()
    assert running.id == other.id
    key = f"pipeline_item_holds:v1:{target.series_id}"
    mode = f"pipeline_item_hold_mode:v1:{target.series_id}"
    records = {
        "101": {"head": "a" * 40, "stage": "merge", "task_id": 123,
                "at": 1234.5, "reason": "API budget refusal — operator decision required"},
        "202": {"head": "b" * 40, "stage": "merge", "task_id": 456,
                "at": 5678, "reason": "Другой hold", "opaque": {"keep": [True, None, 7]}},
    }
    raw = json.dumps(records, ensure_ascii=False, indent=2)
    with isolated_db._connect() as c:
        c.executemany("INSERT INTO settings(key,value) VALUES(?,?)", [
            (key, raw), (mode, "1"), (f"pipeline_item_holds:v1:{other.series_id}", '{"other":"preserve"}'),
            (f"pipeline_series_wake:v1:{target.series_id}", "1"),
            ("pipeline_github_budget_reservations:v1:fixture", '{"opaque":"preserve"}'),
        ])
        c.execute("""UPDATE tasks SET scheduled_at=?,next_run_at=?,retry_count=3,
                     budget_wait_scope='preserve',budget_wait_started_at='preserve-time',
                     pipeline_priority_restore=7,rights='read' WHERE id=?""",
                  ("2100-01-01T00:00:00+00:00", "2100-01-02T00:00:00+00:00", target.id))
        c.execute("""INSERT INTO pipeline_target_reservations
                     (repository,stage,number,head,task_id,token,reserved_at,expires_at,
                      task_started_at,ownership_kind)
                     VALUES('owner/repo','review',202,?,?,?,'reserved','expires',?,'headless')""",
                  ("b" * 40, running.id, "other-token", running.started_at.isoformat()))
    return {"db": isolated_db, "task": target, "other": running, "key": key,
            "mode": mode, "raw": raw, "records": records,
            "url": f"/api/schedule/{target.series_id}/holds/101/resolve",
            "body": {"expected_head": "a" * 40, "expected_task_id": 123,
                     "expected_raw_holds_sha256": digest(raw), "request_nonce": "operator-retry-1"}}


def test_read_holds_returns_exact_raw_and_stable_record_hashes_without_writes(held, monkeypatch):
    db = held["db"]
    before = database_snapshot(db)
    statements = []
    original = db._connect
    @contextmanager
    def traced(*args, **kwargs):
        with original(*args, **kwargs) as c:
            c.set_trace_callback(statements.append)
            yield c
    monkeypatch.setattr(db, "_connect", traced)
    response = request("GET", f'/api/schedule/{held["task"].series_id}/holds')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["raw_holds"] == held["raw"]
    assert data["raw_holds_sha256"] == digest(held["raw"])
    assert data["hold_mode"] == "1"
    assert [item["number"] for item in data["holds"]] == [101, 202]
    assert data["holds"][0]["record"] == held["records"]["101"]
    assert data["holds"][0]["record_sha256"] == db._item_hold_record_digest(held["records"]["101"])
    assert database_snapshot(db) == before
    assert not any(sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE", "REPLACE"))
                   for sql in statements)


def test_resolution_only_removes_matching_entry_and_preserves_every_other_state(held):
    db = held["db"]
    before = database_snapshot(db)
    response = request("POST", held["url"], held["body"])
    assert response.status_code == 200, response.text
    receipt = response.json()
    after = database_snapshot(db)
    for table in ("tasks", "task_series", "pipeline_target_reservations"):
        assert after[table] == before[table]
    expected = dict(before["settings"])
    expected[held["key"]] = db.get_setting(held["key"])
    receipt_key = db._item_resolution_nonce_key(held["body"]["request_nonce"])
    expected[receipt_key] = db.get_setting(receipt_key)
    assert dict(after["settings"]) == expected
    assert json.loads(expected[held["key"]]) == {"202": held["records"]["202"]}
    assert receipt["resolved_record"] == held["records"]["101"]
    assert receipt["resolved_record_sha256"] == db._item_hold_record_digest(held["records"]["101"])
    assert receipt["after_raw_holds_sha256"] == digest(expected[held["key"]])
    assert receipt["operator_resolution_only"] is True
    assert receipt["product_mutation_authorized"] is False
    assert receipt["canonical_progress_claimed"] is False
    assert receipt["wake_requested"] is False


@pytest.mark.parametrize("mode", [None, "0", "1"])
def test_hold_mode_and_paused_series_are_not_resumed_or_rewritten(held, mode):
    db = held["db"]
    with db._connect() as c:
        c.execute("UPDATE task_series SET paused=1 WHERE id=?", (held["task"].series_id,))
        if mode is None:
            c.execute("DELETE FROM settings WHERE key=?", (held["mode"],))
        else:
            c.execute("UPDATE settings SET value=? WHERE key=?", (mode, held["mode"]))
    before = database_snapshot(db)
    response = request("POST", held["url"], held["body"])
    assert response.status_code == 200, response.text
    assert db.get_setting(held["mode"]) == mode
    after = database_snapshot(db)
    assert after["task_series"] == before["task_series"]
    assert after["tasks"] == before["tasks"]


@pytest.mark.parametrize("field,value", [
    ("expected_head", "c" * 40), ("expected_task_id", 999),
    ("expected_raw_holds_sha256", "0" * 64),
])
def test_stale_expected_identity_or_ledger_returns_409_with_no_receipt(held, field, value):
    before = database_snapshot(held["db"])
    body = dict(held["body"], **{field: value})
    response = request("POST", held["url"], body)
    assert response.status_code == 409, response.text
    assert database_snapshot(held["db"]) == before


def test_other_entry_change_invalidates_whole_ledger_snapshot(held):
    db = held["db"]
    changed = dict(held["records"], **{"303": {"head": "c" * 40, "task_id": 789}})
    db.set_setting(held["key"], json.dumps(changed))
    before = database_snapshot(db)
    assert request("POST", held["url"], held["body"]).status_code == 409
    assert database_snapshot(db) == before


@pytest.mark.parametrize("raw", [
    "[]", "null", '{"101":null}', '{"0101":{}}',
    '{"101":{"head":"a"},"101":{"head":"b"}}',
    '{"101":{"head":"a","head":"b"}}', '{"101":{"at":NaN}}',
    '{"101":{"at":1e999}}',
])
def test_malformed_or_ambiguous_ledger_refuses_read_and_resolution_atomically(held, raw):
    db = held["db"]
    db.set_setting(held["key"], raw)
    before = database_snapshot(db)
    assert request("GET", f'/api/schedule/{held["task"].series_id}/holds').status_code == 409
    response = request("POST", held["url"], dict(held["body"], expected_raw_holds_sha256=digest(raw)))
    assert response.status_code == 409, response.text
    assert database_snapshot(db) == before


@pytest.mark.parametrize("record", [
    {"fingerprint": "c" * 64, "task_id": 123},
    {"head": "a" * 40, "task_id": True},
])
def test_resolve_does_not_guess_legacy_fingerprint_or_boolean_task_identity(held, record):
    db = held["db"]
    raw = json.dumps({"101": record})
    db.set_setting(held["key"], raw)
    before = database_snapshot(db)
    assert request("GET", f'/api/schedule/{held["task"].series_id}/holds').status_code == 200
    assert request("POST", held["url"], dict(held["body"], expected_raw_holds_sha256=digest(raw))).status_code == 409
    assert database_snapshot(db) == before


@pytest.mark.parametrize("change", [
    {"expected_task_id": True}, {"expected_task_id": 0}, {"expected_task_id": 2**63},
    {"expected_head": "A" * 40}, {"expected_raw_holds_sha256": "bad"},
    {"request_nonce": "with spaces"}, {"request_nonce": ""},
    {"request_nonce": "x" * 129}, {"retry_blocked": True},
])
def test_public_request_validation_never_mutates(held, change):
    before = database_snapshot(held["db"])
    assert request("POST", held["url"], dict(held["body"], **change)).status_code == 422
    assert database_snapshot(held["db"]) == before


def test_running_attempt_guard_is_atomic_and_does_not_touch_active_or_pending(held):
    db = held["db"]
    with db._connect() as c:
        c.execute("UPDATE tasks SET status='running',started_at=?,session_id='live' WHERE id=?",
                  (db._now(), held["task"].id))
    before = database_snapshot(db)
    response = request("POST", held["url"], held["body"])
    assert response.status_code == 409
    assert "running" in response.json()["detail"]
    assert database_snapshot(db) == before


def test_known_nonce_reconciles_lost_response_without_clearing_a_new_hold(held):
    db = held["db"]
    first = request("POST", held["url"], held["body"])
    assert first.status_code == 200
    receipt = first.json()
    # A later actor has added a new hold. Replaying the old identical request
    # must return its old outcome, not reinterpret it as another removal.
    db.set_setting(held["key"], held["raw"])
    with db._connect() as c:
        c.execute("UPDATE tasks SET status='running',started_at=? WHERE id=?",
                  (db._now(), held["task"].id))
    before = database_snapshot(db)
    again = request("POST", held["url"], held["body"])
    assert again.status_code == 200 and again.json() == receipt
    url = f'/api/schedule/{held["task"].series_id}/hold-resolutions/{held["body"]["request_nonce"]}'
    assert request("GET", url).json() == receipt
    assert database_snapshot(db) == before
    with closing(sqlite3.connect(db.DB_PATH)) as c:
        stored = c.execute("SELECT value FROM settings WHERE key=?",
                           (db._item_resolution_nonce_key(held["body"]["request_nonce"]),)).fetchone()[0]
    assert json.loads(stored) == receipt


def test_nonce_reuse_for_different_request_or_series_is_a_conflict(held):
    assert request("POST", held["url"], held["body"]).status_code == 200
    before = database_snapshot(held["db"])
    assert request("POST", held["url"], dict(held["body"], expected_task_id=789)).status_code == 409
    other = held["other"].series_id
    assert request("POST", f'/api/schedule/{other}/holds/101/resolve', held["body"]).status_code == 409
    assert request("GET", f'/api/schedule/{other}/hold-resolutions/{held["body"]["request_nonce"]}').status_code == 409
    assert database_snapshot(held["db"]) == before


@pytest.mark.parametrize("same_nonce", [True, False])
def test_concurrent_public_requests_commit_one_resolution_only(held, same_nonce):
    barrier = threading.Barrier(2)
    def send(index):
        body = dict(held["body"])
        if not same_nonce:
            body["request_nonce"] += "-" + str(index)
        barrier.wait(timeout=10)
        return request("POST", held["url"], body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [1, 2]))
    assert sorted(r.status_code for r in results) == ([200, 200] if same_nonce else [200, 409])
    if same_nonce:
        assert results[0].json() == results[1].json()
    with held["db"]._connect() as c:
        receipts = c.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'pipeline_item_hold_resolution:v1:%'").fetchone()[0]
    assert receipts == 1
    assert json.loads(held["db"].get_setting(held["key"])) == {"202": held["records"]["202"]}


def test_receipt_insert_failure_rolls_back_hold_cas_without_partial_result(held):
    db = held["db"]
    with db._connect() as c:
        c.execute("""CREATE TRIGGER fail_receipt BEFORE INSERT ON settings
                     WHEN NEW.key LIKE 'pipeline_item_hold_resolution:v1:%'
                     BEGIN SELECT RAISE(ABORT,'receipt fixture refused'); END""")
    before = database_snapshot(db)
    assert request("POST", held["url"], held["body"]).status_code == 500
    assert database_snapshot(db) == before
    assert request("GET", f'/api/schedule/{held["task"].series_id}/hold-resolutions/operator-retry-1').status_code == 404


def test_absent_series_item_or_receipt_has_no_side_effect(held):
    before = database_snapshot(held["db"])
    assert request("GET", "/api/schedule/999999/holds").status_code == 404
    assert request("POST", "/api/schedule/999999/holds/101/resolve", held["body"]).status_code == 404
    assert request("POST", held["url"].replace("/101/", "/999/"), held["body"]).status_code == 409
    assert request("GET", f'/api/schedule/{held["task"].series_id}/hold-resolutions/not-used').status_code == 404
    assert database_snapshot(held["db"]) == before


def test_last_item_resolution_keeps_empty_ledger_and_mode(held):
    db = held["db"]
    raw = json.dumps({"101": held["records"]["101"]})
    db.set_setting(held["key"], raw)
    response = request("POST", held["url"], dict(held["body"], expected_raw_holds_sha256=digest(raw)))
    assert response.status_code == 200
    assert db.get_setting(held["key"]) == "{}"
    assert db.get_setting(held["mode"]) == "1"


def test_existing_api_authentication_also_protects_new_controls(held, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", "fixture-token")
    before = database_snapshot(held["db"])
    assert request("GET", f'/api/schedule/{held["task"].series_id}/holds').status_code == 401
    assert request("POST", held["url"], held["body"]).status_code == 401
    assert database_snapshot(held["db"]) == before

def test_sqlite_writer_contention_returns_reconcilable_503_without_any_mutation(held, monkeypatch):
    db = held["db"]
    before = database_snapshot(db)
    original_connect = sqlite3.connect
    blocker = original_connect(db.DB_PATH)
    blocker.execute("BEGIN IMMEDIATE")
    # Keep the production connection/rollback/close behavior, but avoid the
    # normal ten-second busy timeout in this focused real SQLite fixture.
    def no_wait_connect(*args, **kwargs):
        kwargs["timeout"] = 0
        return original_connect(*args, **kwargs)
    try:
        monkeypatch.setattr(sqlite3, "connect", no_wait_connect)
        response = request("POST", held["url"], held["body"])
        assert response.status_code == 503, response.text
        assert response.headers["retry-after"] == "1"
        assert "nonce" in response.json()["detail"]
        assert database_snapshot(db) == before
        url = f'/api/schedule/{held["task"].series_id}/hold-resolutions/operator-retry-1'
        assert request("GET", url).status_code == 404
    finally:
        blocker.rollback()
        blocker.close()
    # No hidden retry or retained writer connection: the caller can explicitly
    # submit the original nonce after the lock is released.
    assert request("POST", held["url"], held["body"]).status_code == 200


@pytest.mark.parametrize("change", [
    {"canonical_progress_claimed": True},
    {"resolved_record_sha256": "0" * 64},
    {"resolved_at": "not-a-time"},
])
def test_malformed_persisted_receipt_never_reinterprets_nonce_as_new_authority(held, change):
    db = held["db"]
    first = request("POST", held["url"], held["body"])
    assert first.status_code == 200
    corrupted = dict(first.json(), **change)
    db.set_setting(db._item_resolution_nonce_key(held["body"]["request_nonce"]),
                   json.dumps(corrupted))
    db.set_setting(held["key"], held["raw"])
    before = database_snapshot(db)
    url = f'/api/schedule/{held["task"].series_id}/hold-resolutions/operator-retry-1'
    assert request("GET", url).status_code == 409
    assert request("POST", held["url"], held["body"]).status_code == 409
    assert database_snapshot(db) == before
