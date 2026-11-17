"""Public native MCP submission -> actual read-only CLI gate."""
import asyncio
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
import httpx
import pytest

from promptpilot import db, external_inbox as inbox, workflows
from promptpilot.models import WorkflowCreate, WorkflowStartRequest, WorkflowTaskDispatch


def rpc(app, method, params, session=None):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post("/api/external-clients/test/mcp",
                headers={"Mcp-Session-Id": session} if session else {},
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            assert response.status_code == 200, response.text
            return response
    return asyncio.run(send())


@pytest.fixture
def submitted(isolated_db, tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "docs").mkdir()
    files = {"README.md": "Example\n", "docs/guide.md": "Existing guide\n"}
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
    git("init", "-b", "main")
    git("add", ".")
    git("-c", "user.name=Gate fixture", "-c", "user.email=test@example.invalid",
        "commit", "--no-gpg-sign", "-m", "Gate fixture")
    patch = ""
    for name, text in files.items():
        patch += "diff --git a/" + name + " b/" + name + "\n"
        patch += "".join(difflib.unified_diff(text.splitlines(keepends=True),
            (text + "A documentation improvement.\n").splitlines(keepends=True),
            fromfile="a/" + name, tofile="b/" + name))
    config = {"planning": {"enabled": False}, "automation": {"enabled": False},
              "external_executor": {"client_id": "test", "advance_after_submit": False},
              "external_result_policy": {"artifacts": ["changes.patch", "RESULT.md"],
                  "text_artifacts": ["RESULT.md"],
                  "patch": {"artifact": "changes.patch", "allowed_paths": ["README.md", "docs/"]}},
              "gate": {"enabled": True, "commands": ["echo project-check"]}}
    wf = db.create_workflow(WorkflowCreate(slug="gate-example", objective="Improve documentation",
        repository_path=str(repo), candidate_branch="main", config=config))
    wf = workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=wf.state_version))
    wf = workflows.dispatch_task(wf.id, WorkflowTaskDispatch(expected_version=wf.state_version,
        role="executor", execution_mode="external", prompt="Return the two agreed artifacts.")).workflow
    app = FastAPI()
    app.include_router(inbox.router)
    session = rpc(app, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "fixture", "version": "1"}}).headers["Mcp-Session-Id"]
    claim = rpc(app, "tools/call", {"name": "pilot_next_task", "arguments": {}}, session).json()["result"]
    assert not claim["isError"], claim
    job = claim["structuredContent"]
    response = rpc(app, "tools/call", {"name": "pilot_submit_result", "arguments": {
        "job_id": job["job_id"], "claim_token": job["claim_token"], "outcome": "completed",
        "summary": "Two proposed documentation changes.", "artifacts": [
            {"path": "changes.patch", "text": patch},
            {"path": "RESULT.md", "text": "Proposed changes; content review is still required."}]}}, session).json()["result"]
    assert not response["isError"], response
    return wf.id, job["job_id"], repo


def cli(workflow_id, data_dir):
    env = os.environ.copy()
    env["PP_DATA_DIR"] = str(data_dir)
    env["PYTHONUTF8"] = "1"
    return subprocess.run([sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "main.py"),
                           "workflow", "verify-external", workflow_id], env=env,
                          capture_output=True, timeout=30)


def test_public_cli_verifies_actual_native_submission_without_writes(submitted, isolated_db):
    workflow_id, _, repo = submitted
    before = {name: (repo / name).read_bytes() for name in ("README.md", "docs/guide.md")}
    with db._connect() as conn:
        dump = list(conn.iterdump())
    result = cli(workflow_id, isolated_db.DB_DIR)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = json.loads(result.stdout)
    assert output["receipt_integrity"] == "verified"
    assert output["repository_files_applied"] is False
    assert output["changed_paths"] == ["README.md", "docs/guide.md"]
    assert before == {name: (repo / name).read_bytes() for name in before}
    with db._connect() as conn:
        assert list(conn.iterdump()) == dump


@pytest.mark.parametrize("damage", ["unconfirmed", "artifact-hash", "output-hash", "assignment", "wrong-round", "policy"])
def test_public_cli_refuses_damaged_receipt(submitted, isolated_db, damage):
    workflow_id, job_id, _ = submitted
    with db._connect(immediate=True) as conn:
        if damage == "unconfirmed":
            conn.execute("UPDATE external_inbox_claims_v1 SET state='unknown' WHERE job_id=?", (job_id,))
        elif damage == "artifact-hash":
            conn.execute("UPDATE external_inbox_artifacts_v1 SET sha256=? WHERE job_id=?", ("0" * 64, job_id))
        elif damage == "output-hash":
            conn.execute("UPDATE external_inbox_claims_v1 SET expected_output_sha=? WHERE job_id=?", ("0" * 64, job_id))
        elif damage == "assignment":
            conn.execute("UPDATE external_inbox_claims_v1 SET input_sha256=? WHERE job_id=?", ("0" * 64, job_id))
        elif damage == "wrong-round":
            conn.execute("UPDATE workflows SET current_round=2 WHERE id=?", (workflow_id,))
        else:
            row = conn.execute("SELECT config_json FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            config = json.loads(row[0])
            config["external_result_policy"]["patch"]["allowed_paths"] = ["other/"]
            conn.execute("UPDATE workflows SET config_json=? WHERE id=?", (json.dumps(config), workflow_id))
    assert cli(workflow_id, isolated_db.DB_DIR).returncode == 2


def test_public_cli_missing_database_creates_nothing(tmp_path):
    missing = tmp_path / "missing"
    result = cli("wf_" + "1" * 32, missing)
    assert result.returncode == 2
    assert not missing.exists()


def test_binary_or_outside_patch_refused(submitted, isolated_db):
    workflow_id, job_id, _ = submitted
    with db._connect(immediate=True) as conn:
        raw = b"diff --git a/../private b/../private\nGIT binary patch\n"
        conn.execute("UPDATE external_inbox_artifacts_v1 SET content=?,sha256=? WHERE job_id=? AND path='changes.patch'",
                     (raw, hashlib.sha256(raw).hexdigest(), job_id))
    assert cli(workflow_id, isolated_db.DB_DIR).returncode == 2


def test_public_cli_reports_only_safe_logical_names_for_artifact_mismatch(submitted, isolated_db):
    workflow_id, _, repo = submitted
    with db._connect(immediate=True) as conn:
        row = conn.execute("SELECT config_json FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        configuration = json.loads(row[0])
        configuration["external_result_policy"]["artifacts"] = ["renamed.patch", "RESULT.md"]
        conn.execute("UPDATE workflows SET config_json=? WHERE id=?", (json.dumps(configuration), workflow_id))
    result = cli(workflow_id, isolated_db.DB_DIR)
    assert result.returncode == 2
    diagnostic = json.loads(result.stderr)
    assert diagnostic == {"schema": "external-result-error-v1", "code": "artifact_paths_mismatch",
                          "expected_paths": ["RESULT.md", "renamed.patch"],
                          "received_paths": ["RESULT.md", "changes.patch"]}
    assert str(repo).encode() not in result.stderr
