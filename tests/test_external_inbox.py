"""Real SQLite/Native lifecycle tests; no external client or provider is started."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

from fastapi import FastAPI
import httpx
import pytest

from promptpilot import api, db, external_inbox as inbox, workflows
from promptpilot.models import WorkflowCreate, WorkflowStartRequest, WorkflowTaskDispatch


class ASGIClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://localhost") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(inbox.router)
    return ASGIClient(app)


def rpc(client, method, params=None, session=None, client_id="glm", notification=False):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notification:
        body["id"] = 1
    return client.post(f"/api/external-clients/{client_id}/mcp", json=body,
                       headers={"Mcp-Session-Id": session} if session else {})


def session(client, client_id="glm"):
    response = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}}, client_id=client_id)
    assert response.status_code == 200
    return response.headers["Mcp-Session-Id"]


def tool(client, token, name="pilot_next_task", args=None, client_id="glm"):
    return rpc(client, "tools/call", {"name": name, "arguments": args or {}}, token, client_id).json()["result"]


def data(response):
    assert not response["isError"], response
    return response["structuredContent"]


def waiting(tmp_path, *, slug="example", client_id="glm", advance=False, opted=True):
    cfg = {"planning": {"enabled": False}, "gate": {"enabled": False, "commands": []},
           "automation": {"enabled": advance, "stage_checkpoint": False}}
    if opted:
        cfg["external_executor"] = {"client_id": client_id, "advance_after_submit": advance}
    workflow = db.create_workflow(WorkflowCreate(slug=slug, objective="Find a small documentation fix",
        repository_path=str(tmp_path), candidate_branch="draft/external", config=cfg))
    workflow = workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=workflow.state_version))
    dispatch = workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(expected_version=workflow.state_version,
        role="executor", prompt="Read the supplied assignment; return a proposed note, no public changes.",
        execution_mode="external"))
    return dispatch.workflow


def result(job, **kwargs):
    return {"job_id": job["job_id"], "claim_token": job["claim_token"], "outcome": "completed",
            "summary": "Proposed documentation adjustment, awaiting independent review.",
            "artifacts": [{"path": "notes/proposal.md", "text": "Unreviewed proposal & exact labels."}], **kwargs}


def test_readonly_activity_does_not_initialize_or_write(client):
    with db._connect() as conn:
        before = list(conn.iterdump())
    assert inbox.client_activity() == []
    response = client.get("/api/external-clients/glm/jobs/none/artifacts/proposal.md")
    assert response.status_code == 404
    with db._connect() as conn:
        assert list(conn.iterdump()) == before


def test_protocol_notifications_and_no_implicit_execution(client, tmp_path):
    wf = waiting(tmp_path)
    assert rpc(client, "tools/call", {"name": "pilot_next_task"}, notification=True).status_code == 202
    assert inbox.client_activity() == []
    token = session(client)
    listed = rpc(client, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in listed] == ["pilot_next_task", "pilot_status", "pilot_read_input", "pilot_submit_result"]
    assert rpc(client, "ping").json()["result"] == {}
    assert tool(client, None)["isError"]
    assert inbox.client_activity() == []
    job = data(tool(client, token))
    assert job["workflow_id"] == wf.id and job["assignment_current"]
    assert job["expected_version"] == wf.state_version
    assert inbox.client_activity(wf.id)[0]["expected_version"] == wf.state_version
    assert len(job["job_id"]) == 32 and len(job["claim_token"]) == 48
    assert data(tool(client, token)) == job
    assert db.list_tasks() == []


def test_exact_issued_input_only_and_client_session_isolation(client, tmp_path):
    waiting(tmp_path)
    first, second, other = session(client), session(client), session(client, "other")
    job = data(tool(client, first))
    assert data(tool(client, second))["state"] == "idle"
    assert data(tool(client, other, client_id="other"))["state"] == "idle"
    args = {"job_id": job["job_id"], "claim_token": job["claim_token"], "path": "assignment.txt", "offset": 0, "length": 12}
    read = data(tool(client, first, "pilot_read_input", args))
    assert read["text"] == job["instructions"][:12]
    assert read["sha256"] == hashlib.sha256(job["instructions"].encode()).hexdigest()
    for changed in ({"path": "../secret"}, {"length": 16385}, {"offset": True}, {"claim_token": "0" * 48}):
        assert tool(client, first, "pilot_read_input", args | changed)["isError"]
    assert tool(client, second, "pilot_read_input", args)["isError"]
    assert tool(client, other, "pilot_read_input", args, client_id="other")["isError"]
    assert tool(client, first, "pilot_status", {"job_id": job["job_id"]}, client_id="other")["isError"]


def test_concurrent_clients_claim_exactly_one_native_run(client, tmp_path):
    waiting(tmp_path)
    tokens = [session(client) for _ in range(4)]
    barrier = threading.Barrier(4)
    def claim(token):
        barrier.wait()
        return data(tool(client, token))
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(claim, tokens))
    assert sum(j["state"] == "claimed" for j in jobs) == 1
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_inbox_claims_v1").fetchone()[0] == 1
    assert db.list_tasks() == []


def test_real_submit_exact_retry_artifacts_and_redacted_activity(client, tmp_path, monkeypatch):
    wf = waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    original = workflows.submit_external_result
    calls = []
    def submit(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(workflows, "submit_external_result", submit)
    first = data(tool(client, token, "pilot_submit_result", result(job)))
    assert first["state"] == "submitted" and first["unreviewed"]
    assert data(tool(client, token, "pilot_submit_result", result(job))) == first
    assert len(calls) == 1
    assert tool(client, token, "pilot_submit_result", result(job, summary="changed"))["isError"]
    assert db.get_workflow(wf.id).status.value == "gating"
    runs = db.list_workflow_runs(db.list_workflow_rounds(wf.id)[0].id)
    assert runs[0].status.value == "completed" and runs[0].task_id is None
    assert "UNREVIEWED EXTERNAL RESULT" in runs[0].output["result"]
    assert runs[0].output["external"]["performer"] == "external-client:glm"
    content = client.get(f"/api/external-clients/glm/jobs/{job['job_id']}/artifacts/notes/proposal.md")
    assert content.content == b"Unreviewed proposal & exact labels."
    assert client.get(f"/api/external-clients/other/jobs/{job['job_id']}/artifacts/notes/proposal.md").status_code == 404
    before = inbox.client_activity(wf.id)
    raw = json.dumps(before)
    assert token not in raw and job["claim_token"] not in raw and job["instructions"] not in raw
    assert "token_sha" not in raw and "input_sha" not in raw
    assert before[0]["state"] == "submitted" and before[0]["last_signal_at"] and not before[0]["execution_observed"]
    with db._connect() as conn:
        stored = conn.execute("SELECT token_sha FROM external_inbox_sessions_v1").fetchone()[0]
        assert stored == hashlib.sha256(token.encode()).hexdigest() and stored != token


def test_lost_native_ack_is_proven_not_replayed(client, tmp_path, monkeypatch):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    original = workflows.submit_external_result
    calls = []
    def lost_ack(*args):
        calls.append(args)
        original(*args)
        raise TimeoutError("PRIVATE EXCEPTION DATA")
    monkeypatch.setattr(workflows, "submit_external_result", lost_ack)
    first = data(tool(client, token, "pilot_submit_result", result(job)))
    assert data(tool(client, token, "pilot_submit_result", result(job))) == first
    assert len(calls) == 1


def test_submitting_after_process_death_reconciles_native_run(client, tmp_path, monkeypatch):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    original_ack = inbox._ack
    def crash(*args):
        raise SystemExit("simulated process stop after Native commit")
    monkeypatch.setattr(inbox, "_ack", crash)
    with pytest.raises(SystemExit):
        inbox._submit("glm", token, result(job))
    monkeypatch.setattr(inbox, "_ack", original_ack)
    monkeypatch.setattr(workflows, "submit_external_result", lambda *a: pytest.fail("must not repeat Native submit"))
    assert data(tool(client, token, "pilot_status"))["state"] == "submitting"
    assert data(tool(client, token, "pilot_submit_result", result(job)))["state"] == "submitted"


def test_unknown_not_accepted_stays_owned_without_replay(client, tmp_path, monkeypatch):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise TimeoutError("PRIVATE TOKEN OR PATH")
    monkeypatch.setattr(workflows, "submit_external_result", unavailable)
    response = tool(client, token, "pilot_submit_result", result(job))
    assert response["isError"] and "PRIVATE" not in json.dumps(response)
    assert tool(client, token, "pilot_submit_result", result(job))["isError"]
    assert len(calls) == 1
    assert data(tool(client, token, "pilot_status"))["state"] == "unknown"
    assert data(tool(client, session(client)))["state"] == "idle"
    uncertain = data(tool(client, token))
    assert uncertain["state"] == "unknown" and uncertain["task"] is None and uncertain["inputs"] == []
    assert "Reconcile" in uncertain["instructions"]


def test_concurrent_identical_submit_has_one_native_effect(client, tmp_path, monkeypatch):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    entered, release = threading.Event(), threading.Event()
    original = workflows.submit_external_result
    calls = []
    def pending(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(workflows, "submit_external_result", pending)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(inbox._submit, "glm", token, result(job))
        assert entered.wait(5)
        with pytest.raises(inbox.Rejected, match="unknown"):
            inbox._submit("glm", token, result(job))
        release.set()
        accepted = first.result(timeout=5)
    assert len(calls) == 1 and accepted["state"] == "submitted"


@pytest.mark.parametrize("change", ["cancel", "version", "client", "workorder", "run", "input"])
def test_stale_or_revoked_assignment_never_submits(client, tmp_path, monkeypatch, change):
    wf = waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    if change == "cancel":
        workflows.cancel_workflow(wf.id, wf.state_version)
    else:
        with db._connect(immediate=True) as conn:
            if change == "version":
                conn.execute("UPDATE workflows SET state_version=state_version+1 WHERE id=?", (wf.id,))
            elif change in {"client", "workorder"}:
                cfg = db.get_workflow(wf.id).config
                if change == "client":
                    cfg["external_executor"]["client_id"] = "other"
                else:
                    cfg["workorder_guard"] = {"binding": "must not be delegated"}
                conn.execute("UPDATE workflows SET config_json=? WHERE id=?", (json.dumps(cfg), wf.id))
            elif change == "run":
                conn.execute("UPDATE workflow_runs SET status='cancelled' WHERE workflow_id=?", (wf.id,))
            else:
                conn.execute("UPDATE workflow_runs SET input_sha256=? WHERE workflow_id=?", ("0" * 64, wf.id))
    monkeypatch.setattr(workflows, "submit_external_result", lambda *a: pytest.fail("stale submit"))
    assert tool(client, token, "pilot_submit_result", result(job))["isError"]
    with db._connect() as conn:
        row = conn.execute("SELECT state,payload_sha FROM external_inbox_claims_v1").fetchone()
        assert row["state"] == "claimed" and row["payload_sha"] is None


def test_unselected_and_workorder_are_never_claimed(client, tmp_path):
    waiting(tmp_path, opted=False)
    guarded = waiting(tmp_path, slug="guarded")
    with db._connect(immediate=True) as conn:
        cfg = db.get_workflow(guarded.id).config
        cfg["workorder_guard"] = {"binding": "protected"}
        conn.execute("UPDATE workflows SET config_json=? WHERE id=?", (json.dumps(cfg), guarded.id))
    assert data(tool(client, session(client)))["state"] == "idle"


@pytest.mark.parametrize("path", ["../x", "/x", "a\\b", "a:b", ".git/config", "NUL.txt", "a/../b", "a.", "a//b"])
def test_artifact_paths_never_reach_repository(client, tmp_path, path):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    assert tool(client, token, "pilot_submit_result", result(job, artifacts=[{"path": path, "text": "no"}]))["isError"]
    assert not (tmp_path / "notes").exists()
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_inbox_artifacts_v1").fetchone()[0] == 0


@pytest.mark.parametrize("artifacts", [
    [{"path": "a", "text": "x"}, {"path": "A", "text": "y"}],
    [{"path": "a", "text": "x"}, {"path": "a/b", "text": "y"}],
    [{"path": "a", "text": "x", "base64": "eA=="}],
    [{"path": "a", "base64": "bad!"}],
    [{"path": "a", "text": "x" * (2 * 1024 * 1024 + 1)}],
    [{"path": str(n), "text": ""} for n in range(65)],
])
def test_artifact_bounds_are_atomic(client, tmp_path, artifacts):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    assert tool(client, token, "pilot_submit_result", result(job, artifacts=artifacts))["isError"]
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_inbox_artifacts_v1").fetchone()[0] == 0
        assert conn.execute("SELECT payload_sha FROM external_inbox_claims_v1").fetchone()[0] is None


def test_binary_artifact_exact_bytes_and_normal_engine_optin(client, tmp_path, monkeypatch):
    wf = waiting(tmp_path, advance=True)
    token = session(client)
    job = data(tool(client, token))
    # Advance the exact accepted workflow through the real disabled gate to independent reviewer dispatch.
    ack = data(tool(client, token, "pilot_submit_result", result(job, artifacts=[
        {"path": "binary.dat", "base64": base64.b64encode(b"\x00\xff").decode()}])))
    assert ack["workflow_id"] == wf.id
    assert db.get_workflow(wf.id).status.value == "reviewing"
    tasks = db.list_tasks()
    assert len(tasks) == 1 and "UNREVIEWED EXTERNAL RESULT" in tasks[0].prompt
    monkeypatch.setattr(workflows, "advance_workflow", lambda *a: pytest.fail("must not repeat engine from duplicate result"))
    assert data(tool(client, token, "pilot_submit_result", result(job, artifacts=[
        {"path": "binary.dat", "base64": base64.b64encode(b"\x00\xff").decode()}]))) == ack
    assert client.get(f"/api/external-clients/glm/jobs/{job['job_id']}/artifacts/binary.dat").content == b"\x00\xff"


def test_actual_global_auth_wraps_all_router_paths(isolated_db, monkeypatch):
    app = FastAPI()
    app.middleware("http")(api._auth)
    app.include_router(inbox.router)
    monkeypatch.setattr(api, "API_TOKEN", "private-api-token")
    with ASGIClient(app) as client:
        assert rpc(client, "initialize", {}).status_code == 401
        assert client.get("/api/external-clients/glm/jobs/x/artifacts/a").status_code == 401
        response = client.post("/api/external-clients/glm/mcp", headers={"Authorization": "Bearer private-api-token"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 200 and "private-api-token" not in response.text
        cross_site = client.post("/api/external-clients/glm/mcp", headers={"Authorization": "Bearer private-api-token", "Sec-Fetch-Site": "cross-site"}, json={})
        assert cross_site.status_code == 403


def test_body_cap_and_malformed_protocol(client):
    assert client.post("/api/external-clients/glm/mcp", content=b"x" * (inbox.MAX_BODY + 1)).status_code == 413
    assert client.post("/api/external-clients/glm/mcp", content=b"{").json()["error"]["code"] == -32700
    assert rpc(client, "unknown").json()["error"]["code"] == -32601
    assert rpc(client, "tools/call", {"name": "pilot_next_task", "arguments": {"arbitrary": 1}}, session(client)).json()["result"]["isError"]

def test_canonical_native_output_mismatch_is_not_an_acceptance_receipt(client, tmp_path, monkeypatch):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    original_ack = inbox._ack
    def stop(*args):
        raise SystemExit("after commit, before receipt")
    monkeypatch.setattr(inbox, "_ack", stop)
    with pytest.raises(SystemExit):
        inbox._submit("glm", token, result(job))
    monkeypatch.setattr(inbox, "_ack", original_ack)
    with db._connect(immediate=True) as conn:
        run = conn.execute("SELECT * FROM workflow_runs WHERE id=?", (job["run_id"],)).fetchone()
        output = json.loads(run["output_json"])
        output["external"]["performer"] = "different actor"
        raw = db._json_dump(output)
        conn.execute("UPDATE workflow_runs SET output_json=?,output_sha256=? WHERE id=?",
                     (raw, hashlib.sha256(raw.encode()).hexdigest(), job["run_id"]))
    monkeypatch.setattr(workflows, "submit_external_result", lambda *a: pytest.fail("uncertain result must never repeat"))
    assert tool(client, token, "pilot_submit_result", result(job))["isError"]
    assert data(tool(client, token, "pilot_status"))["state"] == "submitting"


def test_revision_is_a_new_native_assignment_same_workflow_not_old_claim_refresh(client, tmp_path):
    from promptpilot.models import WorkflowPlanApproval
    cfg = {"planning": {"enabled": True, "require_approval": True},
           "automation": {"enabled": True, "stage_checkpoint": False},
           "gate": {"enabled": False, "commands": []},
           "external_executor": {"client_id": "glm", "advance_after_submit": True},
           "limits": {"max_rounds": 10}}
    wf = api.api_create_workflow(WorkflowCreate(slug="revision", objective="Independent proposed note",
        repository_path=str(tmp_path), candidate_branch="draft/revision", config=cfg, plan=[
            {"code": "S1", "title": "Research", "objective": "Propose a sourced note", "execution_mode": "external"},
            {"code": "FINAL", "title": "Integrate", "objective": "Integrate after review", "stage_type": "integration", "dependencies": ["S1"]}]))
    workflows.approve_plan(wf.id, WorkflowPlanApproval(expected_version=wf.state_version))
    workflows.advance_workflow(wf.id)
    token = session(client)
    first = data(tool(client, token))
    accepted = data(tool(client, token, "pilot_submit_result", result(first)))
    reviewer = db.get_next_runnable()
    assert reviewer is not None
    workflows.sync_task(reviewer.id)
    db.mark_completed(reviewer.id, 'AUDIT_FINDINGS_JSON: [{"fingerprint":"sources","severity":"medium","category":"research","title":"Add a source"}]\nAUDIT_VERDICT: REVISION_REQUIRED\nИТОГ: ГОТОВО', exit_code=0)
    db.set_verdict(reviewer.id, "ГОТОВО")
    workflows.sync_task(reviewer.id)
    workflows.advance_linked_task(reviewer.id)
    assert db.get_workflow(wf.id).status.value == "awaiting_external"
    second = data(tool(client, token))
    assert first["workflow_id"] == second["workflow_id"] == wf.id
    assert first["run_id"] != second["run_id"] and first["job_id"] != second["job_id"]
    assert inbox.client_result(wf.id)["run_id"] == first["run_id"]  # labelled historical result, not this revision
    assert "Add a source" in second["task"]["instructions"]
    assert data(tool(client, token, "pilot_submit_result", result(first))) == accepted
    assert tool(client, token, "pilot_read_input", {"job_id": first["job_id"], "claim_token": first["claim_token"],
        "path": "assignment.txt", "offset": 0, "length": 20})["isError"]
    data(tool(client, token, "pilot_submit_result", result(second, summary="Revised note with source X")))
    reviewer = db.get_next_runnable()
    workflows.sync_task(reviewer.id)
    db.mark_completed(reviewer.id, 'AUDIT_FINDINGS_JSON: [{"fingerprint":"sources","severity":"medium","category":"research","title":"Add a source","status":"resolved"}]\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО', exit_code=0)
    db.set_verdict(reviewer.id, "ГОТОВО")
    workflows.sync_task(reviewer.id)
    workflows.advance_linked_task(reviewer.id)
    assert db.get_workflow(wf.id).status.value == "executing"  # normal final stage, not claimed by the inbox
    assert data(tool(client, token))["state"] == "idle"
    rows = inbox.client_activity(wf.id)
    assert len(rows) == 2 and {row["state"] for row in rows} == {"submitted"}
    assert not list(tmp_path.glob("*.md"))  # no submitted files were applied


def test_legacy_tool_schema_fingerprint(client):
    # Canonicalized exact TOOLS from the independently captured 86e566 legacy bridge.
    encoded = json.dumps(inbox.TOOLS, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "9d07cd78d73977b91d075c8ffafd0ed951cf2680744fea9edb9e16f6c8472d72"


def test_activity_and_artifact_reads_never_create_missing_database(tmp_path, monkeypatch):
    missing_dir = tmp_path / "never-created"
    monkeypatch.setattr(db, "DB_DIR", missing_dir)
    monkeypatch.setattr(db, "DB_PATH", missing_dir / "missing.sqlite3")
    app = FastAPI()
    app.include_router(inbox.router)
    client = ASGIClient(app)
    assert inbox.client_activity() == []
    assert client.get("/api/external-clients/glm/jobs/unknown/artifacts/result.md").status_code == 404
    assert not missing_dir.exists()


def test_activity_and_artifact_use_literal_query_only_connection(client, tmp_path, monkeypatch):
    waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    data(tool(client, token, "pilot_submit_result", result(job)))
    with inbox._read_connection() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(inbox.sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE external_inbox_claims_v1 SET state='wrong'")
    # Neither read path may fall back to the writable Native helper.
    monkeypatch.setattr(db, "_connect", lambda *a, **k: pytest.fail("read attempted writable connection"))
    assert inbox.client_activity(job["workflow_id"])[0]["state"] == "submitted"
    assert client.get(f"/api/external-clients/glm/jobs/{job['job_id']}/artifacts/notes/proposal.md").status_code == 200


def test_deeply_nested_mcp_json_is_finite_parse_error_without_writes(client):
    with db._connect() as conn:
        before = list(conn.iterdump())
    response = client.post("/api/external-clients/glm/mcp", content="[" * 5000 + "0" + "]" * 5000)
    assert response.status_code == 200 and response.json()["error"]["code"] == -32700
    with db._connect() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_mcp_json_number_never_reaches_response_serializer(client, number):
    with db._connect() as conn:
        before = list(conn.iterdump())
    response = client.post("/api/external-clients/glm/mcp",
        content='{"jsonrpc":"2.0","id":' + number + ',"method":"ping"}')
    assert response.status_code == 200 and response.json()["error"]["code"] == -32700
    with db._connect() as conn:
        assert list(conn.iterdump()) == before


def test_client_result_missing_database_is_literal_read_only(tmp_path, monkeypatch):
    folder = tmp_path / "not-created"
    monkeypatch.setattr(db, "DB_DIR", folder)
    monkeypatch.setattr(db, "DB_PATH", folder / "absent.sqlite3")
    assert inbox.client_result("no-workflow") is None
    assert not folder.exists()


def test_client_result_proven_report_and_artifacts_without_secrets_or_writes(client, tmp_path, monkeypatch):
    wf = waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    assert inbox.client_result(wf.id) is None  # claimed is not a submitted report
    path = "notes/with space #?.md"
    data(tool(client, token, "pilot_submit_result", result(job, artifacts=[{"path": path, "text": "Report body"}])))
    with db._connect() as conn:
        before = list(conn.iterdump())
    monkeypatch.setattr(db, "_connect", lambda *a, **k: pytest.fail("read used writable connector"))
    report = inbox.client_result(wf.id)
    assert report["run_id"] == job["run_id"] and report["client_id"] == "glm"
    assert report["unreviewed"] and not report["text_truncated"]
    assert "UNREVIEWED EXTERNAL RESULT" in report["text"] and "Report body" in report["text"]
    artifact = report["artifacts"][0]
    assert artifact["name"] == "with space #?.md" and artifact["path"] == path
    assert artifact["sha256"] == hashlib.sha256(b"Report body").hexdigest()
    assert artifact["size_bytes"] == len(b"Report body")
    assert "%20" in artifact["download_url"] and "%23" in artifact["download_url"] and "%3F" in artifact["download_url"]
    assert client.get(artifact["download_url"]).content == b"Report body"
    raw = json.dumps(report)
    assert token not in raw and job["claim_token"] not in raw and job["instructions"] not in raw
    assert "session_id" not in report and "claim_id" not in report and "native_json" not in raw
    with inbox._read_connection() as conn:
        assert list(conn.iterdump()) == before


def test_client_result_requires_matching_native_proof_not_only_submitted_flag(client, tmp_path):
    wf = waiting(tmp_path)
    token = session(client)
    job = data(tool(client, token))
    data(tool(client, token, "pilot_submit_result", result(job)))
    assert inbox.client_result(wf.id) is not None
    with db._connect(immediate=True) as conn:
        row = conn.execute("SELECT native_json FROM external_inbox_claims_v1").fetchone()
        native = json.loads(row["native_json"])
        original = native["result"]
        native["result"] = "fabricated UI result"
        conn.execute("UPDATE external_inbox_claims_v1 SET native_json=?", (json.dumps(native),))
    assert inbox.client_result(wf.id) is None
    with db._connect(immediate=True) as conn:
        native["result"] = original
        conn.execute("UPDATE external_inbox_claims_v1 SET native_json=?", (json.dumps(native),))
        conn.execute("UPDATE workflow_runs SET output_sha256=? WHERE id=?", ("0" * 64, job["run_id"]))
    assert inbox.client_result(wf.id) is None
