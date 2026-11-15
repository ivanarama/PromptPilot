"""Session-owned MCP handoff to native external workflow runs.

The API's global authentication middleware protects every route in this router.
No broker, worker, filesystem checkout or independent task queue is involved.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import db, workflows
from .models import WorkflowExternalResult

router = APIRouter()
PROTOCOLS = ("2025-06-18", "2025-03-26")
MAX_BODY = 4 * 1024 * 1024
MAX_RESULT = 2 * 1024 * 1024
MAX_FILES = 64
TOOLS = [{'name': 'pilot_next_task',
  'description': 'Claim or resume the single Root-queued task. Returns instructions and input index; no '
                 'commands are executed.',
  'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
 {'name': 'pilot_status',
  'description': 'Read the latest task status or one job_id; never returns credentials.',
  'inputSchema': {'type': 'object',
                  'properties': {'job_id': {'type': 'string'}},
                  'additionalProperties': False}},
 {'name': 'pilot_read_input',
  'description': 'Read one listed task input by character offset; max16384 characters per call.',
  'inputSchema': {'type': 'object',
                  'properties': {'job_id': {'type': 'string'},
                                 'claim_token': {'type': 'string'},
                                 'path': {'type': 'string'},
                                 'offset': {'type': 'integer', 'minimum': 0},
                                 'length': {'type': 'integer', 'minimum': 1, 'maximum': 16384}},
                  'required': ['job_id', 'claim_token', 'path', 'offset', 'length'],
                  'additionalProperties': False}},
 {'name': 'pilot_submit_result',
  'description': 'Durably submit unreviewed result files (64files,total2MiB). Exact retry is idempotent; '
                 'changed retry rejected.',
  'inputSchema': {'type': 'object',
                  'properties': {'job_id': {'type': 'string'},
                                 'claim_token': {'type': 'string'},
                                 'outcome': {'enum': ['completed', 'blocked', 'failed']},
                                 'summary': {'type': 'string'},
                                 'artifacts': {'type': 'array',
                                               'maxItems': 64,
                                               'items': {'type': 'object',
                                                         'properties': {'path': {'type': 'string'},
                                                                        'text': {'type': 'string'},
                                                                        'base64': {'type': 'string'}},
                                                         'required': ['path'],
                                                         'additionalProperties': False}}},
                  'required': ['job_id', 'claim_token', 'outcome', 'summary', 'artifacts'],
                  'additionalProperties': False}}]


class Rejected(ValueError):
    """A finite public error; never interpolate private inputs or exceptions."""


def _require(ok, message):
    if not ok:
        raise Rejected(message)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _fields(value, required=(), optional=()):
    _require(type(value) is dict and set(required) <= value.keys()
             and value.keys() <= set(required) | set(optional), "invalid fields")


def _client(client_id):
    _require(isinstance(client_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", client_id),
             "invalid client identity")


def _ensure(conn):
    # DDL is reachable from MCP POST only, never from the activity/artifact reads.
    conn.execute("""CREATE TABLE IF NOT EXISTS external_inbox_sessions_v1 (
        session_id TEXT PRIMARY KEY, token_sha TEXT UNIQUE NOT NULL,
        client_id TEXT NOT NULL, created_at TEXT NOT NULL, last_signal_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS external_inbox_claims_v1 (
        job_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, session_id TEXT NOT NULL,
        claim_id TEXT NOT NULL, workflow_id TEXT NOT NULL, run_id TEXT UNIQUE NOT NULL,
        expected_version INTEGER NOT NULL, input_sha256 TEXT NOT NULL,
        assignment TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, payload_sha TEXT, native_json TEXT,
        expected_output_sha TEXT, receipt_id TEXT, outcome TEXT,
        advance_state TEXT, FOREIGN KEY(session_id) REFERENCES external_inbox_sessions_v1(session_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS external_inbox_artifacts_v1 (
        job_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
        content BLOB NOT NULL, PRIMARY KEY(job_id,path),
        FOREIGN KEY(job_id) REFERENCES external_inbox_claims_v1(job_id))""")


@contextmanager
def _read_connection():
    """Literal SQLite RO snapshot: no mkdir, file creation, schema or commit."""
    path = db.DB_PATH.resolve()
    if not path.is_file():
        yield None
        return
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError:
        if not path.exists():
            yield None
            return
        raise
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_inbox_claims_v1'").fetchone() is not None


def _session(conn, client_id, token):
    _require(isinstance(token, str) and re.fullmatch(r"[0-9a-f]{64}", token), "initialize a session first")
    row = conn.execute("SELECT * FROM external_inbox_sessions_v1 WHERE client_id=? AND token_sha=?",
                       (client_id, _sha(token))).fetchone()
    _require(row is not None, "unknown client session")
    conn.execute("UPDATE external_inbox_sessions_v1 SET last_signal_at=? WHERE session_id=?",
                 (db._now(), row["session_id"]))
    return dict(row)


def _claim_token(token, row):
    return hmac.new(token.encode(), (row["job_id"] + ":" + row["claim_id"]).encode(), hashlib.sha256).hexdigest()[:48]


def _owned(conn, client_id, session, job_id, token=None, claim_token=None):
    _require(isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{32}", job_id), "invalid job identity")
    row = conn.execute("SELECT * FROM external_inbox_claims_v1 WHERE job_id=? AND client_id=? AND session_id=?",
                       (job_id, client_id, session["session_id"])).fetchone()
    _require(row is not None, "assignment not owned by this session")
    row = dict(row)
    if token is not None:
        _require(isinstance(claim_token, str) and hmac.compare_digest(_claim_token(token, row), claim_token),
                 "claim identity does not match")
    return row


def _assignment(conn, workflow_id, client_id):
    wf = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if wf is None or wf["status"] != "awaiting_external":
        return None
    config = json.loads(wf["config_json"])
    executor = config.get("external_executor", {})
    if (config.get("workorder_guard") is not None or type(executor) is not dict
            or executor.get("client_id") != client_id):
        return None
    run = conn.execute("""SELECT r.* FROM workflow_runs r JOIN workflow_rounds q ON q.id=r.round_id
        WHERE r.workflow_id=? AND q.round_no=? AND r.role='executor'
        ORDER BY r.attempt_no DESC LIMIT 1""", (workflow_id, wf["current_round"])).fetchone()
    if run is None or run["status"] != "awaiting_external" or run["task_id"] is not None:
        return None
    data = json.loads(run["input_json"] or "{}")
    if (data.get("execution_mode") != "external" or not isinstance(data.get("assignment"), str)
            or not data["assignment"].strip() or len(data["assignment"].encode()) > 128 * 1024):
        return None
    return {"workflow_id": workflow_id, "run_id": run["id"], "expected_version": wf["state_version"],
            "input_sha256": run["input_sha256"], "assignment": data["assignment"]}


def _live(conn, row):
    current = _assignment(conn, row["workflow_id"], row["client_id"])
    return current is not None and all(current[key] == row[key] for key in (
        "run_id", "expected_version", "input_sha256", "assignment"))


def _public(row, *, live=None):
    data = {key: row[key] for key in ("job_id", "workflow_id", "run_id", "client_id", "session_id",
        "claim_id", "state", "created_at", "updated_at", "outcome", "receipt_id", "expected_version")}
    data["result_digest"] = row["payload_sha"]
    if live is not None:
        data["assignment_current"] = live
    data["execution_observed"] = False  # Polls/claims are signals, not proof of coding.
    return data


def _next(conn, client_id, session, token):
    # A session keeps uncertain submissions until explicitly reconciled; time never steals ownership.
    existing = conn.execute("""SELECT * FROM external_inbox_claims_v1
        WHERE client_id=? AND session_id=? AND state!='submitted' ORDER BY created_at,job_id""",
        (client_id, session["session_id"])).fetchall()
    for item in existing:
        row = dict(item)
        if row["state"] != "claimed" or _live(conn, row):
            result = _public(row, live=_live(conn, row))
            result["claim_token"] = _claim_token(token, row)
            result["inputs"] = [{"path": "assignment.txt", "sha256": _sha(row["assignment"]),
                                 "size_bytes": len(row["assignment"].encode()), "bytes": len(row["assignment"].encode()),
                                 "chars": len(row["assignment"])}]
            actionable = result["assignment_current"] and row["state"] == "claimed"
            result["instructions"] = row["assignment"] if actionable else "Reconcile this previous submission; it is not a new assignment."
            result["task"] = ({"title": "Native external workflow assignment", "instructions": row["assignment"],
                               "metadata": {"workflow_id": row["workflow_id"], "run_id": row["run_id"]},
                               "inputs": result["inputs"]} if actionable else None)
            if not actionable:
                result["inputs"] = []
            return result
    for wf in conn.execute("SELECT id FROM workflows WHERE status='awaiting_external' ORDER BY created_at,id").fetchall():
        assignment = _assignment(conn, wf["id"], client_id)
        if assignment is None:
            continue
        if conn.execute("SELECT 1 FROM external_inbox_claims_v1 WHERE run_id=?", (assignment["run_id"],)).fetchone():
            continue
        now = db._now()
        job_id = _sha(_dump([client_id, assignment["workflow_id"], assignment["run_id"], assignment["input_sha256"]]))[:32]
        conn.execute("""INSERT INTO external_inbox_claims_v1
            (job_id,client_id,session_id,claim_id,workflow_id,run_id,expected_version,input_sha256,
             assignment,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'claimed',?,?)""",
            (job_id, client_id, session["session_id"], secrets.token_hex(16), assignment["workflow_id"],
             assignment["run_id"], assignment["expected_version"], assignment["input_sha256"], assignment["assignment"], now, now))
        return _next(conn, client_id, session, token)
    return {"state": "idle", "task": None, "execution_observed": False}


def _path(name):
    _require(isinstance(name, str) and 0 < len(name) <= 512 and "\\" not in name and ":" not in name
             and not any(ord(ch) < 32 for ch in name), "invalid artifact path")
    parts = name.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    _require(not PurePosixPath(name).is_absolute() and len(parts) <= 12 and all(
        p not in {"", ".", ".."} and p.casefold() != ".git" and p[-1:] not in {".", " "}
        and p.split(".")[0].upper() not in reserved for p in parts), "invalid artifact path")
    return name


def _result(args):
    _fields(args, ("job_id", "claim_token", "outcome", "summary", "artifacts"))
    _require(args["outcome"] in ("completed", "blocked", "failed"), "invalid outcome")
    _require(isinstance(args["summary"], str) and bool(args["summary"].strip()) and len(args["summary"].encode()) <= 65536, "invalid summary")
    _require(type(args["artifacts"]) is list and len(args["artifacts"]) <= MAX_FILES, "too many artifacts")
    artifacts, manifest, seen, total = [], [], set(), 0
    for item in args["artifacts"]:
        _fields(item, ("path",), ("text", "base64"))
        name = _path(item["path"])
        _require(name.casefold() not in seen and not any(
            name.casefold().startswith(old + "/") or old.startswith(name.casefold() + "/") for old in seen), "colliding artifact paths")
        seen.add(name.casefold())
        _require(("text" in item) != ("base64" in item), "choose text or base64")
        if "text" in item:
            _require(isinstance(item["text"], str), "invalid text artifact")
            raw = item["text"].encode("utf-8")
        else:
            _require(isinstance(item["base64"], str), "invalid binary artifact")
            try:
                raw = base64.b64decode(item["base64"], validate=True)
            except (ValueError, binascii.Error):
                raise Rejected("invalid base64 artifact") from None
        total += len(raw)
        _require(total <= MAX_RESULT, "result exceeds 2 MiB")
        artifacts.append((name, raw))
        manifest.append({"path": name, "sha256": _sha(raw), "size_bytes": len(raw)})
    manifest.sort(key=lambda item: item["path"])
    content = {"job_id": args["job_id"], "outcome": args["outcome"], "summary": args["summary"], "artifacts": manifest}
    return content, artifacts, _sha(_dump(content))


def _native_submission(row, content, artifacts, digest):
    # Native audit gets the report and bounded text, plus immutable authenticated artifact URLs.
    report = ["UNREVIEWED EXTERNAL RESULT", "Outcome: " + content["outcome"], content["summary"],
              "Artifacts (immutable; never applied to the repository):", _dump(content["artifacts"])]
    remaining = 150000 - sum(len(part) for part in report)
    for path, raw in sorted(artifacts):
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            continue
        if len(text) <= remaining:
            report.append("\nArtifact " + path + ":\n" + text)
            remaining -= len(text) + len(path) + 30
    report.append(f"Artifact route: /api/external-clients/{row['client_id']}/jobs/{row['job_id']}/artifacts/{{path}}")
    return WorkflowExternalResult(expected_version=row["expected_version"], run_id=row["run_id"],
        input_sha256=row["input_sha256"], result="\n".join(report), performer="external-client:" + row["client_id"],
        comment=f"external-inbox-v1 job={row['job_id']} digest={digest}")


def _output_sha(submission):
    output = {"task_status": "completed", "result": submission.result, "error": None, "verdict": "ГОТОВО",
              "external": {"performer": submission.performer.strip(), "comment": submission.comment.strip()}}
    return _sha(db._json_dump(output))


def _accepted(conn, row):
    run = conn.execute("SELECT * FROM workflow_runs WHERE id=? AND workflow_id=?",
                       (row["run_id"], row["workflow_id"])).fetchone()
    if run is None or run["status"] != "completed" or run["input_sha256"] != row["input_sha256"]:
        return False
    try:
        output = json.loads(run["output_json"])
        canonical = db._json_dump(output)
    except (ValueError, TypeError):
        return False
    return _sha(canonical) == run["output_sha256"] == row["expected_output_sha"]


def _ack(conn, row):
    conn.execute("UPDATE external_inbox_claims_v1 SET state='submitted',updated_at=? WHERE job_id=?",
                 (db._now(), row["job_id"]))
    return {"state": "submitted", "receipt_id": row["receipt_id"], "result_digest": row["payload_sha"],
            "workflow_id": row["workflow_id"], "run_id": row["run_id"], "unreviewed": True}


def _submit(client_id, token, args):
    content, artifacts, digest = _result(args)
    with db._connect(immediate=True) as conn:
        _ensure(conn)
        session = _session(conn, client_id, token)
        row = _owned(conn, client_id, session, args["job_id"], token, args["claim_token"])
        if row["payload_sha"] is not None:
            _require(row["payload_sha"] == digest, "changed submission cannot replace a previous result")
            if row["state"] == "submitted" or _accepted(conn, row):
                return _ack(conn, row)
            raise Rejected("submission outcome unknown; reconcile Native run before any further action")
        _require(row["state"] == "claimed" and _live(conn, row), "assignment is stale, cancelled or reassigned")
        submission = _native_submission(row, content, artifacts, digest)
        receipt_id = secrets.token_hex(24)
        conn.execute("""UPDATE external_inbox_claims_v1 SET state='submitting', payload_sha=?, native_json=?,
            expected_output_sha=?, receipt_id=?, outcome=?, updated_at=? WHERE job_id=?""",
            (digest, submission.model_dump_json(), _output_sha(submission), receipt_id, content["outcome"], db._now(), row["job_id"]))
        for path, raw in artifacts:
            conn.execute("INSERT INTO external_inbox_artifacts_v1(job_id,path,sha256,content) VALUES (?,?,?,?)",
                         (row["job_id"], path, _sha(raw), raw))
    # SUBMITTING is durable before entering the non-idempotent Native transaction.
    try:
        workflows.submit_external_result(row["workflow_id"], submission)
    except Exception:
        # An exception is not evidence of rollback. Prove actual canonical output first.
        pass
    with db._connect(immediate=True) as conn:
        row = dict(conn.execute("SELECT * FROM external_inbox_claims_v1 WHERE job_id=?", (row["job_id"],)).fetchone())
        if not _accepted(conn, row):
            conn.execute("UPDATE external_inbox_claims_v1 SET state='unknown',updated_at=? WHERE job_id=?", (db._now(), row["job_id"]))
            ack = None
        else:
            ack = _ack(conn, row)
    _require(ack is not None, "submission outcome unknown; reconcile Native run before any further action")
    _advance_once(row)
    return ack


def _advance_once(row):
    with db._connect(immediate=True) as conn:
        wf = conn.execute("SELECT * FROM workflows WHERE id=?", (row["workflow_id"],)).fetchone()
        config = json.loads(wf["config_json"])
        executor = config.get("external_executor", {})
        if (config.get("workorder_guard") is not None or type(executor) is not dict
                or executor.get("client_id") != row["client_id"] or executor.get("advance_after_submit") is not True):
            return
        changed = conn.execute("UPDATE external_inbox_claims_v1 SET advance_state='requested' WHERE job_id=? AND advance_state IS NULL",
                               (row["job_id"],)).rowcount
    if changed:
        try:
            workflows.advance_workflow(row["workflow_id"])
        except Exception:
            # Normal engine may have an unknown side effect. Never replay it from a result retry.
            return
        with db._connect(immediate=True) as conn:
            conn.execute("UPDATE external_inbox_claims_v1 SET advance_state='completed' WHERE job_id=?", (row["job_id"],))


def _tool(client_id, token, name, args):
    if name == "pilot_submit_result":
        return _submit(client_id, token, args)
    with db._connect(immediate=True) as conn:
        _ensure(conn)
        session = _session(conn, client_id, token)
        if name == "pilot_next_task":
            _fields(args)
            return _next(conn, client_id, session, token)
        if name == "pilot_status":
            _fields(args, optional=("job_id",))
            if "job_id" in args:
                row = _owned(conn, client_id, session, args["job_id"])
            else:
                found = conn.execute("SELECT * FROM external_inbox_claims_v1 WHERE client_id=? AND session_id=? ORDER BY created_at DESC,job_id DESC LIMIT 1",
                                     (client_id, session["session_id"])).fetchone()
                if found is None:
                    return {"state": "idle", "task": None, "execution_observed": False}
                row = dict(found)
            return _public(row, live=_live(conn, row))
        _require(name == "pilot_read_input", "unknown tool")
        _fields(args, ("job_id", "claim_token", "path", "offset", "length"))
        row = _owned(conn, client_id, session, args["job_id"], token, args["claim_token"])
        _require(row["state"] == "claimed" and _live(conn, row), "assignment is no longer current")
        _require(args["path"] == "assignment.txt", "input is not declared")
        _require(type(args["offset"]) is int and args["offset"] >= 0 and type(args["length"]) is int
                 and 1 <= args["length"] <= 16384, "invalid input range")
        _require(args["offset"] <= len(row["assignment"]), "input offset out of range")
        text = row["assignment"][args["offset"]:args["offset"] + args["length"]]
        return {"path": "assignment.txt", "text": text, "offset": args["offset"], "length": len(text),
                "total_characters": len(row["assignment"]), "next_offset": args["offset"] + len(text),
                "eof": args["offset"] + len(text) == len(row["assignment"]), "sha256": _sha(row["assignment"])}


def _rpc(client_id, token, body):
    rid = None
    try:
        _client(client_id)
        _fields(body, ("jsonrpc",), ("id", "method", "params", "result", "error"))
        _require(body["jsonrpc"] == "2.0", "invalid JSON-RPC version")
        if "id" not in body or "method" not in body:
            return Response(status_code=202)  # Notifications never claim or run tools.
        candidate_id = body["id"]
        _require(type(candidate_id) in (str, int) and (not isinstance(candidate_id, str) or len(candidate_id) <= 128), "invalid request id")
        rid = candidate_id
        method, params = body["method"], body.get("params", {})
        _require(isinstance(method, str), "invalid method")
        _require(type(params) is dict and ("_meta" not in params or type(params["_meta"]) is dict), "invalid parameters")
        headers = {}
        if method == "initialize":
            _fields(params, ("protocolVersion", "capabilities", "clientInfo"), ("_meta",))
            _require(isinstance(params["protocolVersion"], str) and type(params["capabilities"]) is dict
                     and type(params["clientInfo"]) is dict, "invalid initialize")
            token = secrets.token_hex(32)
            now = db._now()
            with db._connect(immediate=True) as conn:
                _ensure(conn)
                conn.execute("INSERT INTO external_inbox_sessions_v1 VALUES (?,?,?,?,?)",
                             (secrets.token_hex(16), _sha(token), client_id, now, now))
            headers["Mcp-Session-Id"] = token
            result = {"protocolVersion": params["protocolVersion"] if params["protocolVersion"] in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "promptpilot-native-inbox", "version": "1.0.0"},
                      "instructions": "Claim the assigned Native external run, read its declared input and submit unreviewed result artifacts. Keep this session header for reconciliation."}
        elif method in ("ping", "tools/list"):
            _fields(params, optional=("_meta",))
            result = {} if method == "ping" else {"tools": TOOLS}
        elif method == "tools/call":
            _fields(params, ("name",), ("arguments", "_meta"))
            _require(params["name"] in {tool["name"] for tool in TOOLS}, "unknown tool")
            try:
                data = _tool(client_id, token, params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": _dump(data)}], "structuredContent": data, "isError": False}
            except (Rejected, ValueError, TypeError, KeyError, UnicodeError):
                # Never echo arbitrary client input or internal exception details.
                result = {"content": [{"type": "text", "text": "Rejected: invalid, stale or uncertain assignment; reconcile the job status."}], "isError": True}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers=headers)
    except (Rejected, ValueError, TypeError, KeyError, UnicodeError):
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid request or parameters"}})


def _reject_json_constant(_value):
    raise ValueError("non-finite JSON constant")


def _finite_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


@router.post("/api/external-clients/{client_id}/mcp")
async def mcp(client_id: str, request: Request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(413, "MCP request exceeds limit")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks), parse_constant=_reject_json_constant, parse_float=_finite_json_float)
    except (ValueError, UnicodeError, RecursionError):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    return await run_in_threadpool(_rpc, client_id, request.headers.get("Mcp-Session-Id"), body)


def client_activity(workflow_id: str | None = None):
    """Read-only, redacted feed projection. Does not initialize the inbox schema."""
    with _read_connection() as conn:
        if conn is None or not _exists(conn):
            return []
        where, args = (" WHERE c.workflow_id=?", (workflow_id,)) if workflow_id else ("", ())
        rows = conn.execute("""SELECT c.*,s.last_signal_at FROM external_inbox_claims_v1 c
            JOIN external_inbox_sessions_v1 s ON s.session_id=c.session_id""" + where
            + " ORDER BY c.created_at,c.job_id", args).fetchall()
        return [{**_public(dict(row), live=_live(conn, row)), "last_signal_at": row["last_signal_at"]} for row in rows]


def client_result(workflow_id: str):
    """Latest proven submitted report, explicitly labelled with its historical run.

    Literal RO only. Submission means received unreviewed data, never stage acceptance.
    """
    with _read_connection() as conn:
        if conn is None or not _exists(conn):
            return None
        rows = conn.execute("""SELECT c.* FROM external_inbox_claims_v1 c
            JOIN workflow_runs r ON r.id=c.run_id AND r.workflow_id=c.workflow_id
            JOIN workflow_rounds q ON q.id=r.round_id
            WHERE c.workflow_id=? AND c.state='submitted'
            ORDER BY q.round_no DESC,r.attempt_no DESC,c.created_at DESC,c.job_id DESC""",
            (workflow_id,)).fetchall()
        for row in rows:
            if not _accepted(conn, row):
                continue
            try:
                submission = WorkflowExternalResult.model_validate_json(row["native_json"])
                if (submission.run_id != row["run_id"] or submission.input_sha256 != row["input_sha256"]
                        or submission.expected_version != row["expected_version"]
                        or _output_sha(submission) != row["expected_output_sha"]):
                    continue
                _client(row["client_id"])
                artifacts = []
                total = 0
                for item in conn.execute("SELECT path,sha256,content FROM external_inbox_artifacts_v1 WHERE job_id=? ORDER BY path", (row["job_id"],)):
                    path = _path(item["path"])
                    raw = bytes(item["content"])
                    total += len(raw)
                    _require(total <= MAX_RESULT and len(artifacts) < MAX_FILES and _sha(raw) == item["sha256"], "artifact integrity changed")
                    artifacts.append({"name": PurePosixPath(path).name, "path": path,
                        "size_bytes": len(raw), "sha256": item["sha256"],
                        "download_url": f"/api/external-clients/{row['client_id']}/jobs/{row['job_id']}/artifacts/" + quote(path, safe="/")})
            except (ValueError, TypeError, KeyError, UnicodeError):
                continue
            return {"client_id": row["client_id"], "job_id": row["job_id"], "run_id": row["run_id"],
                    "outcome": row["outcome"], "receipt_id": row["receipt_id"], "unreviewed": True,
                    "text": submission.result[:150000], "text_truncated": len(submission.result) > 150000,
                    "artifacts": artifacts}
        return None


@router.get("/api/external-clients/{client_id}/jobs/{job_id}/artifacts/{path:path}")
def read_artifact(client_id: str, job_id: str, path: str):
    """Authenticated immutable result bytes, never a repository file lookup."""
    try:
        _client(client_id)
        _path(path)
    except Rejected:
        raise HTTPException(404, "Artifact not found") from None
    with _read_connection() as conn:
        if conn is None or not _exists(conn):
            raise HTTPException(404, "Artifact not found")
        row = conn.execute("""SELECT a.content,a.sha256 FROM external_inbox_artifacts_v1 a
            JOIN external_inbox_claims_v1 c ON c.job_id=a.job_id
            WHERE c.client_id=? AND c.job_id=? AND a.path=?""", (client_id, job_id, path)).fetchone()
        if row is None:
            raise HTTPException(404, "Artifact not found")
        return Response(bytes(row["content"]), media_type="application/octet-stream",
                        headers={"ETag": '"' + row["sha256"] + '"', "X-Content-Type-Options": "nosniff"})