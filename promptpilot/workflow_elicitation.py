"""Terminal native questions bound to a single current run/attempt and pause.

An answer is a durable operator note. It never executes the displayed action,
alters task rights, applies a review verdict or resumes the workflow by itself.
"""

import hashlib
import json
import re
import uuid

from . import db
from .models import WorkflowElicitation, WorkflowStatus

PROTOCOL = "workflow-elicitation-v1"
IDENTITY_FIELDS = ("protocol", "version", "request_id", "question", "binding")


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def parse(report):
    if not isinstance(report, str) or len(report) > 200000:
        return None
    lines = report.rstrip().splitlines()
    if len(lines) < 2:
        return None
    from .herdr_exec import WORKFLOW_CLOSING_VERDICT_RE

    last = WORKFLOW_CLOSING_VERDICT_RE.fullmatch(lines[-1].strip())
    if (
        not last
        or last[1].upper()
        != "\u041d\u0423\u0416\u0415\u041d \u0427\u0415\u041b\u041e\u0412\u0415\u041a"
    ):
        return None
    # A quoted example or unclosed code fence must not become a request.
    fence = None
    for line in lines[:-2]:
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            if fence is None:
                fence = (match[1][0], len(match[1]))
            elif (
                match[1][0] == fence[0]
                and len(match[1]) >= fence[1]
                and not match[2].strip()
            ):
                fence = None
    if fence is not None or not lines[-2].startswith("ELICIT_JSON: "):
        return None

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate ELICIT field")
            value[key] = item
        return value

    try:
        return WorkflowElicitation.model_validate(
            json.loads(lines[-2][13:], object_pairs_hook=unique_pairs)
        ).model_dump(mode="json")
    except (ValueError, TypeError, RecursionError):
        return None


def _report(conn, task):
    row = conn.execute(
        "SELECT * FROM task_reports WHERE task_id=?", (task["id"],)
    ).fetchone()
    if row is None:
        return task["result"] or ""
    if (
        row["task_started_at"] != task["started_at"]
        or row["result_sha256"]
        != hashlib.sha256((task["result"] or "").encode()).hexdigest()
    ):
        return ""
    return row["report_text"]


def _binding(conn, workflow, run, task):
    return dict(
        workflow_id=workflow["id"],
        round_id=run["round_id"],
        run_id=run["id"],
        role=run["role"],
        run_attempt=run["attempt_no"],
        task_id=task["id"],
        task_retry_count=task["retry_count"],
        task_started_at=task["started_at"],
        task_status=task["status"],
        task_verdict=task["verdict"],
        task_exit_code=task["exit_code"],
        task_rights=task["rights"],
        task_skip_permissions=task["skip_permissions"],
        task_working_dir=task["working_dir"],
        task_provider=task["provider"],
        result_sha256=hashlib.sha256((task["result"] or "").encode()).hexdigest(),
        input_sha256=run["input_sha256"],
        provider_report_sha256=hashlib.sha256(_report(conn, task).encode()).hexdigest(),
        output_sha256=run["output_sha256"],
        config_sha256=hashlib.sha256(workflow["config_json"].encode()).hexdigest(),
    )


def capture(conn, workflow, run, task):
    if task["status"] != "completed" or task["exit_code"] != 0:
        return False
    if (run["role"], workflow["status"]) not in {
        ("executor", "executing"),
        ("reviewer", "reviewing"),
    }:
        return False
    latest = conn.execute(
        "SELECT id FROM workflow_runs WHERE workflow_id=? AND round_id=? AND role=? ORDER BY attempt_no DESC LIMIT 1",
        (workflow["id"], run["round_id"], run["role"]),
    ).fetchone()
    current_round = conn.execute(
        "SELECT id FROM workflow_rounds WHERE workflow_id=? AND round_no=?",
        (workflow["id"], workflow["current_round"]),
    ).fetchone()
    if (
        latest["id"] != run["id"]
        or not current_round
        or current_round["id"] != run["round_id"]
    ):
        return False
    question = parse(_report(conn, task))
    if question is None:
        return False
    source = _binding(conn, workflow, run, task)
    request_id = str(uuid.uuid4())
    payload = dict(
        protocol=PROTOCOL,
        version=workflow["state_version"] + 1,
        request_id=request_id,
        question=question,
        binding=source,
    )
    payload["binding_sha256"] = _digest(payload)
    from . import workflows

    workflows._transition(
        conn,
        workflow,
        WorkflowStatus.AWAITING_HUMAN,
        "elicit.requested",
        payload,
        round_id=run["round_id"],
    )
    return True


def active(conn, workflow):
    if workflow["status"] != "awaiting_human":
        return None
    event = conn.execute(
        "SELECT payload_json,round_id FROM workflow_events WHERE workflow_id=? AND event_type='elicit.requested' ORDER BY seq DESC LIMIT 1",
        (workflow["id"],),
    ).fetchone()
    if event is None:
        return None
    payload = json.loads(event["payload_json"])
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != PROTOCOL
        or not set(IDENTITY_FIELDS) | {"binding_sha256"} <= payload.keys()
        or type(payload["version"]) is not int
        or payload["version"] != workflow["state_version"]
        or not isinstance(payload["binding"], dict)
    ):
        return None
    source = payload["binding"]
    if (
        not isinstance(source.get("run_id"), str)
        or type(source.get("task_id")) is not int
    ):
        return None
    run = conn.execute(
        "SELECT * FROM workflow_runs WHERE id=? AND workflow_id=?",
        (source["run_id"], workflow["id"]),
    ).fetchone()
    task = conn.execute(
        "SELECT * FROM tasks WHERE id=?", (source["task_id"],)
    ).fetchone()
    current_round = conn.execute(
        "SELECT id FROM workflow_rounds WHERE workflow_id=? AND round_no=?",
        (workflow["id"], workflow["current_round"]),
    ).fetchone()
    if (
        run is None
        or task is None
        or run["status"] != "completed"
        or not current_round
        or current_round["id"] != run["round_id"]
        or event["round_id"] != run["round_id"]
        or _binding(conn, workflow, run, task) != source
        or parse(_report(conn, task)) != payload["question"]
    ):
        return None
    if _digest({k: payload[k] for k in IDENTITY_FIELDS}) != payload["binding_sha256"]:
        return None
    return {**payload, "expected_version": workflow["state_version"]}


def get(workflow_id):
    from . import workflows

    with db._connect() as conn:
        return active(conn, workflows._workflow_row(conn, workflow_id))


def answer(workflow_id, action):
    from . import workflows

    if not action.answer.strip():
        raise db.WorkflowConflictError("elicitation answer cannot be blank")
    with db._connect(immediate=True) as conn:
        workflow = workflows._workflow_row(conn, workflow_id)
        workflows._require_version(
            conn, workflow, action.expected_version, allow_elicitation=True
        )
        pending = active(conn, workflow)
        if (
            pending is None
            or pending["request_id"] != action.request_id
            or pending["binding_sha256"] != action.binding_sha256
        ):
            raise db.WorkflowConflictError(
                "elicitation run/attempt/awaiting decision changed or answer already consumed"
            )
        note = (
            "ELICIT question: "
            + pending["question"]["question"]
            + "\nRequested action (description only): "
            + (pending["question"].get("action") or "none")
            + "\nOperator answer: "
            + action.answer.strip()
        )
        workflow = workflows._touch(
            conn,
            workflow,
            "elicit.answered",
            dict(
                request_id=action.request_id,
                binding_sha256=action.binding_sha256,
                answer=action.answer.strip(),
                note=note,
                grants_permissions=False,
                resumes_workflow=False,
            ),
            round_id=pending["binding"]["round_id"],
            run_id=pending["binding"]["run_id"],
        )
        return db._row_to_workflow(workflow)
