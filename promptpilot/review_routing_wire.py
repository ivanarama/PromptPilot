"""Strict, additive review-routing-v1 wire records.

Parsing authenticates nothing: server actor/immutability, coherent PR identity,
history, ownership, and project mutation gates remain separate requirements.
No legacy review marker is emitted by this protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing import VERSION, inspect_report, _unique_object, _reject_constant
from .review_routing_lease import decimal_id, owner_id, timestamp


MAX_BODY_BYTES = 60000
MARKER = re.compile(r"<!-- pp:review-routing-v1 kind=([a-z-]+) sha256=([0-9a-f]{64}) -->\n")
KINDS = {"publication", "lease", "claim", "completion", "attempt", "phase", "resume"}
OUTCOMES = {"reviewed", "changes-requested", "needs-decision",
            "technical-handoff", "engineering-handoff"}


def canonical(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PipelineError("routing record is not canonical UTF-8 JSON") from exc


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise PipelineError("invalid routing wire fields")


def _text(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PipelineError("invalid routing wire text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PipelineError("invalid routing wire UTF-8") from exc


def hash64(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PipelineError("invalid routing SHA-256")
    return value


def head40(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise PipelineError("invalid routing HEAD")
    return value


def pr_key(identity):
    return {"base_repository_id": identity.base_repository_id,
            "pr_id": identity.pr_id, "pr_number": identity.pr_number}


def _pr(value):
    _fields(value, "base_repository_id pr_id pr_number")
    _text(value["base_repository_id"])
    _text(value["pr_id"])
    if type(value["pr_number"]) is not int or not 1 <= value["pr_number"] <= 2147483647:
        raise PipelineError("invalid routing PR number")


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise PipelineError("invalid routing integer")


def audit_binding(value):
    _fields(value,'snapshot_sha256 last_edge_cursor last_edge_node_id base_ref_oid')
    hash64(value['snapshot_sha256']);head40(value['base_ref_oid'])
    cursor,ident=value['last_edge_cursor'],value['last_edge_node_id']
    if (cursor is None)!=(ident is None):
        raise PipelineError('routing audit boundary requires both cursor and node ID')
    if cursor is not None:
        _text(cursor);_text(ident)
    return value


def outcome(report, *, depth, budget_used, max_attempts=2):
    """Classify explicit independently reviewed input; never infer evidence."""
    _integer(depth, 0, 2147483647)
    _integer(max_attempts, 1, 16)
    # A smaller current limit cannot erase already spent lifetime slots.
    _integer(budget_used, 0, 16)
    inspect_report(report, report.get("head"))
    kinds = {item["kind"] for item in report["blocking"]}
    if not kinds:
        return "reviewed"
    if depth < 2:
        return "changes-requested"
    if kinds & {"product", "safety_exception"}:
        return "needs-decision"
    if kinds == {"technical"} and budget_used < max_attempts:
        return "technical-handoff"
    return "engineering-handoff"


def _source(value):
    _fields(value, "publication_id claim_id completion_id head identity_sha256 epoch report_sha256")
    for name in ("publication_id", "claim_id", "completion_id"):
        decimal_id(value[name])
    head40(value["head"])
    for name in ("identity_sha256", "epoch", "report_sha256"):
        hash64(value[name])


def validate(payload):
    if not isinstance(payload, dict) or payload.get("protocol") != VERSION:
        raise PipelineError("unsupported routing wire protocol")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise PipelineError("unsupported routing wire kind")
    common = "protocol kind pr "
    fields = {
        "publication": "owner identity author epoch anchor audit depth budget_used max_attempts report report_sha256 outcome resume_id",
        "lease": "owner context_sha256 purpose root previous",
        "claim": "owner context_sha256 publication_id lease_id",
        "completion": "owner context_sha256 publication_id claim_id lease_id",
        "attempt": "owner context_sha256 source series previous attempt executor",
        "phase": "owner context_sha256 attempt_id lease_id phase details",
        "resume": "head parking_id previous reason",
    }
    _fields(payload, common + fields[kind])
    _pr(payload["pr"])
    if kind != "resume":
        owner_id(payload["owner"])
    if kind not in {"publication", "resume"}:
        hash64(payload["context_sha256"])
    if kind == "publication":
        if payload["resume_id"] is not None:
            decimal_id(payload["resume_id"])
        try:
            identity = PRIdentityV1(**payload["identity"])
        except (TypeError, KeyError) as exc:
            raise PipelineError("invalid routing publication identity") from exc
        if pr_key(identity) != payload["pr"]:
            raise PipelineError("routing publication lifetime identity differs")
        _fields(payload["author"], "id login")
        _text(payload["author"]["id"])
        login = payload["author"]["login"]
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
            raise PipelineError("invalid routing PR author login")
        hash64(payload["epoch"])
        _text(payload["anchor"])
        audit_binding(payload['audit'])
        inspected = inspect_report(payload["report"], identity.head_sha)
        if payload["report_sha256"] != inspected["report_sha256"]:
            raise PipelineError("routing report hash differs")
        expected = outcome(payload["report"], depth=payload["depth"],
                           budget_used=payload["budget_used"], max_attempts=payload["max_attempts"])
        if payload["outcome"] != expected:
            raise PipelineError("routing outcome differs from explicit report/budget")
    elif kind == "lease":
        if not isinstance(payload["purpose"], str) or payload["purpose"] not in {"review-publication", "technical-attempt"}:
            raise PipelineError("invalid routing lease purpose")
        decimal_id(payload["root"])
        decimal_id(payload["previous"])
    elif kind in {"claim", "completion"}:
        decimal_id(payload["publication_id"])
        decimal_id(payload["lease_id"])
        if kind == "completion":
            decimal_id(payload["claim_id"])
    elif kind == "attempt":
        _source(payload["source"])
        decimal_id(payload["series"])
        if payload["previous"] is not None:
            decimal_id(payload["previous"])
        _integer(payload["attempt"], 1, 16)
        _fields(payload["executor"], "kind login")
        if not isinstance(payload["executor"]["kind"], str) or payload["executor"]["kind"] not in {"author", "pipeline"}:
            raise PipelineError("invalid routing executor kind")
        login = payload["executor"]["login"]
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
            raise PipelineError("invalid routing executor login")
    elif kind == "phase":
        decimal_id(payload["attempt_id"])
        decimal_id(payload["lease_id"])
        details = payload["details"]
        phase = payload["phase"]
        if not isinstance(phase, str):
            raise PipelineError("invalid routing phase")
        if phase in {"assigned", "handoff-done"}:
            _fields(details, "")
        elif phase == "started":
            _fields(details, "task_id attempt_uuid")
            _integer(details["task_id"], 1, 9223372036854775807)
            owner_id(details["attempt_uuid"])
        elif phase == "fix-done":
            _fields(details, "new_head transition source_trailer")
            head40(details["new_head"])
            _text(details["transition"])
            hash64(details["source_trailer"])
        elif phase == "barrier":
            _fields(details, "reason")
            _text(details["reason"])
        else:
            raise PipelineError("unsupported routing phase")
    else:
        head40(payload["head"])
        decimal_id(payload["parking_id"])
        if payload["previous"] is not None:
            decimal_id(payload["previous"])
        _text(payload["reason"])
    return payload


def context(payload):
    validate(payload)
    if payload["kind"] not in {"publication", "attempt"}:
        raise PipelineError("only routing roots define a context")
    return sha({key: value for key, value in payload.items() if key != "owner"})


def render(payload):
    validate(payload)
    data = canonical(payload)
    body = (f"<!-- pp:review-routing-v1 kind={payload['kind']} "
            f"sha256={hashlib.sha256(data).hexdigest()} -->\n").encode("ascii") + data
    if len(body) > MAX_BODY_BYTES:
        raise PipelineError("complete routing publication exceeds comment size; evidence was not truncated")
    return body.decode("utf-8")


def parse(body):
    if not isinstance(body, str):
        raise PipelineError("routing record body must be text")
    try:
        encoded = body.encode("utf-8", errors="strict")
        if len(encoded) > MAX_BODY_BYTES:
            raise PipelineError("routing record exceeds comment size")
        marker = MARKER.match(body)
        if not marker:
            raise PipelineError("malformed routing wire marker")
        payload = json.loads(body[marker.end():], object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
        validate(payload)
        if payload["kind"] != marker[1] or render(payload) != body:
            raise PipelineError("routing record is not exact canonical wire encoding")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PipelineError("invalid routing wire encoding") from exc
    return payload


@dataclass(frozen=True)
class Record:
    id: str
    created_at: str
    payload: dict

    def __post_init__(self):
        decimal_id(self.id)
        timestamp(self.created_at)
        validate(self.payload)

    @property
    def order(self):
        return timestamp(self.created_at), len(self.id), self.id


def records(snapshot, *, trusted, expected_pr):
    """Read immutable trusted records in a complete coherent PR timeline.

    Deletions are returned separately: absence of a surviving record never
    establishes an empty lifetime ledger. Untrusted prose grants no authority.
    """
    _pr(expected_pr)
    result, deleted, ids = [], [], set()
    for edge in snapshot["edges"]:
        node = edge.get("node") or {}
        if node.get("__typename") == "CommentDeletedEvent":
            timestamp(node.get("createdAt"))
            deleted.append(node["createdAt"])
        if (node.get("__typename") != "IssueComment"
                or (node.get("author") or {}).get("login") != trusted):
            continue
        body = node.get("body") or ""
        if "pp:review-routing-" not in body:
            continue
        if node.get("lastEditedAt") is not None:
            raise PipelineError("trusted routing wire record was edited")
        payload = parse(body)
        if payload["pr"] != expected_pr:
            raise PipelineError("routing record belongs to another lifetime PR identity")
        returned_id = node.get("fullDatabaseId")
        # GitHub BigInt can arrive as exact Python int or decimal text; never
        # accept floats/bools or round through a JavaScript Number.
        if type(returned_id) is int:
            returned_id = str(returned_id)
        record = Record(returned_id, node.get("createdAt"), payload)
        if record.id in ids:
            raise PipelineError("duplicate routing server ID")
        ids.add(record.id)
        result.append(record)
    return sorted(result, key=lambda record: record.order), tuple(deleted)
