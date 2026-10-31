"""Questions and one-use answers through the actual workflow HTTP lifecycle."""

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess

import pytest

from promptpilot import workflows
from test_workflow_pause_race import request

HUMAN = "\u041d\u0423\u0416\u0415\u041d \u0427\u0415\u041b\u041e\u0412\u0415\u041a"


def report(**changes):
    question = dict(
        schema_version=1,
        kind="permission",
        question="May the operator run the check?",
        action="python check.py",
        options=["yes", "no"],
    )
    question.update(changes)
    return (
        "Context for the operator.\nELICIT_JSON: "
        + json.dumps(question)
        + "\n\u0418\u0422\u041e\u0413: "
        + HUMAN
        + " (operator answer required)"
    )


def waiting(
    db,
    tmp_path,
    *,
    text=None,
    slug="elicit",
    role="executor",
    autonomous=False,
    provider_boundary=False,
):
    created = request(
        "POST",
        "/api/workflows",
        json=dict(
            slug=slug,
            objective="Bound operator question",
            repository_path=str(tmp_path),
            candidate_branch="feature/elicit",
            config={
                "automation": {
                    "enabled": autonomous,
                    "auto_dispatch_executor": False,
                    "auto_dispatch_reviewer": False,
                },
                "gate": {"enabled": False},
                "roles": {"executor": {"rights": "write"}},
            },
        ),
    )
    assert created.status_code == 201, created.text
    wf = created.json()
    prefix = "/api/workflows/" + wf["id"]
    started = request("POST", prefix + "/start", json={"expected_version": 0})
    assert started.status_code == 200, started.text
    dispatched = request(
        "POST",
        prefix + "/dispatch",
        json=dict(
            expected_version=started.json()["state_version"],
            role="executor",
            prompt="Ask the operator",
            rights="write",
        ),
    )
    assert dispatched.status_code == 200, dispatched.text
    task = dispatched.json()["task"]
    assert db.get_next_runnable().id == task["id"]
    if role == "reviewer":
        db.mark_completed(task["id"], "Executor complete", exit_code=0)
        db.set_verdict(task["id"], "\u0413\u041e\u0422\u041e\u0412\u041e")
        assert request("POST", prefix + "/sync").status_code == 200
        current = request("GET", prefix).json()
        checked = request(
            "POST",
            prefix + "/gate",
            json=dict(expected_version=current["state_version"], verdict="PASS"),
        )
        assert checked.status_code == 200, checked.text
        dispatched = request(
            "POST",
            prefix + "/dispatch",
            json=dict(
                expected_version=checked.json()["state_version"],
                role="reviewer",
                prompt="Independent review question",
                rights="read",
            ),
        )
        assert dispatched.status_code == 200, dispatched.text
        task = dispatched.json()["task"]
        assert db.get_next_runnable().id == task["id"]
    body = text if text is not None else report()
    displayed = (
        body + "\n\nMeta: unrelated display footer" if provider_boundary else body
    )
    db.mark_completed(task["id"], displayed, exit_code=0)
    if provider_boundary:
        with db._connect(immediate=True) as conn:
            started_at = conn.execute(
                "SELECT started_at FROM tasks WHERE id=?", (task["id"],)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO task_reports(task_id,task_started_at,result_sha256,report_text) VALUES (?,?,?,?)",
                (
                    task["id"],
                    started_at,
                    hashlib.sha256(displayed.encode()).hexdigest(),
                    body,
                ),
            )
    db.set_verdict(task["id"], HUMAN)
    synced = request("POST", prefix + "/sync")
    assert synced.status_code == 200, synced.text
    wf = request("GET", prefix).json()
    pending = request("GET", prefix + "/elicitation")
    assert pending.status_code == 200, pending.text
    return wf, task, pending.json()


def reply(wf, pending, **changes):
    body = dict(
        expected_version=pending["expected_version"],
        request_id=pending["request_id"],
        binding_sha256=pending["binding_sha256"],
        answer="Use the approved separate gate; keep write rights.",
    )
    body.update(changes)
    return request(
        "POST", "/api/workflows/" + wf["id"] + "/elicitation/answer", json=body
    )


def test_bound_answer_is_durable_once_and_cannot_grant_tools(
    isolated_db, tmp_path, monkeypatch
):
    wf, task, pending = waiting(isolated_db, tmp_path)
    assert (
        wf["status"] == "awaiting_human" and pending["binding"]["task_id"] == task["id"]
    )
    before_tasks = len(isolated_db.list_tasks())
    original = workflows.run_owned
    monkeypatch.setattr(
        workflows,
        "run_owned",
        lambda *a, **kw: pytest.fail("answer executed a command"),
    )
    saved = reply(wf, pending)
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "awaiting_human"
    assert len(isolated_db.list_tasks()) == before_tasks
    assert isolated_db.get_task(task["id"]).rights == "write"
    assert request("GET", "/api/workflows/" + wf["id"] + "/elicitation").json() is None
    assert reply(wf, pending).status_code == 409
    # Reconciliation of historical completed output must not resurrect a question.
    assert request("POST", "/api/workflows/" + wf["id"] + "/sync").status_code == 200
    assert request("GET", "/api/workflows/" + wf["id"] + "/elicitation").json() is None
    events = isolated_db.list_workflow_events(wf["id"], limit=1000)
    answers = [e for e in events if e.event_type == "elicit.answered"]
    assert len(answers) == 1 and answers[0].payload["grants_permissions"] is False
    assert answers[0].payload["resumes_workflow"] is False
    current = request("GET", "/api/workflows/" + wf["id"]).json()
    resumed = request(
        "POST",
        "/api/workflows/" + wf["id"] + "/human-input",
        json=dict(
            expected_version=current["state_version"],
            text="Continue with the recorded answer",
            resume=True,
        ),
    )
    assert resumed.status_code == 200, resumed.text
    monkeypatch.setattr(workflows, "run_owned", original)
    config = resumed.json()["config"]
    config["automation"].update(enabled=True, auto_dispatch_executor=True)
    updated = request(
        "PATCH",
        "/api/workflows/" + wf["id"],
        json=dict(expected_version=resumed.json()["state_version"], config=config),
    )
    assert updated.status_code == 200, updated.text
    dispatched = request("POST", "/api/workflows/" + wf["id"] + "/advance")
    assert dispatched.status_code == 200, dispatched.text
    tasks = [t for t in isolated_db.list_tasks() if t.id != task["id"]]
    assert len(tasks) == 1
    new = tasks[0]
    assert new.rights == "write" and "approved separate gate" in new.prompt
    assert "ELICIT question:" in new.prompt


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "wrong"},
        {"binding_sha256": "0" * 64},
        {"expected_version": 0},
        {"answer": "   "},
    ],
)
def test_wrong_or_stale_answer_is_not_applied(isolated_db, tmp_path, change):
    wf, task, pending = waiting(isolated_db, tmp_path)
    version = wf["state_version"]
    assert reply(wf, pending, **change).status_code == 409
    assert isolated_db.get_workflow(wf["id"]).state_version == version
    assert isolated_db.get_task(task["id"]).rights == "write"


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("human-input", {"text": "unbound resume", "resume": True}),
        ("review", {"verdict": "PASS", "allow_no_diff": True}),
        ("dispatch", {"role": "executor", "prompt": "Do it"}),
    ],
)
def test_generic_actions_cannot_consume_a_pending_question(
    isolated_db, tmp_path, suffix, body
):
    wf, _task, pending = waiting(isolated_db, tmp_path)
    result = request(
        "POST",
        "/api/workflows/" + wf["id"] + "/" + suffix,
        json=dict(expected_version=wf["state_version"], **body),
    )
    assert result.status_code == 409, result.text
    assert (
        request("GET", "/api/workflows/" + wf["id"] + "/elicitation").json()[
            "request_id"
        ]
        == pending["request_id"]
    )


@pytest.mark.parametrize(
    "field,value",
    [("retry_count", 1), ("result", "Changed terminal report"), ("rights", "read")],
)
def test_changed_native_attempt_or_output_invalidates_question(
    isolated_db, tmp_path, field, value
):
    wf, task, pending = waiting(isolated_db, tmp_path)
    with isolated_db._connect(immediate=True) as conn:
        conn.execute("UPDATE tasks SET " + field + "=? WHERE id=?", (value, task["id"]))
    assert request("GET", "/api/workflows/" + wf["id"] + "/elicitation").json() is None
    assert reply(wf, pending).status_code == 409


@pytest.mark.parametrize(
    "text",
    [
        report(schema_version=True),
        report(rights="full"),
        "```json\n" + report(),
        report() + "\nFurther instructions",
        "Quoted example: "
        + report().splitlines()[-2]
        + "\n\u0418\u0422\u041e\u0413: "
        + HUMAN,
    ],
)
def test_quoted_or_malformed_report_cannot_create_a_request(
    isolated_db, tmp_path, text
):
    wf, _task, pending = waiting(isolated_db, tmp_path, text=text)
    assert wf["status"] == "awaiting_human" and pending is None


def test_cancel_remains_available_and_invalidates_answer(isolated_db, tmp_path):
    wf, _task, pending = waiting(isolated_db, tmp_path)
    cancelled = request(
        "POST",
        "/api/workflows/" + wf["id"] + "/cancel",
        json={"expected_version": wf["state_version"]},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert reply(wf, pending).status_code == 409


def test_real_question_form():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node needed for actual question form")
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, "tests/workflow_elicitation.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reviewer_question_is_bound_to_its_own_run(isolated_db, tmp_path):
    wf, task, pending = waiting(isolated_db, tmp_path, role="reviewer")
    assert (
        pending["binding"]["role"] == "reviewer"
        and pending["binding"]["task_id"] == task["id"]
    )
    assert isolated_db.get_task(task["id"]).rights == "read"
    assert reply(wf, pending).status_code == 200


def test_automatic_reconciliation_waits_without_invalidating_question(
    isolated_db, tmp_path
):
    wf, _task, pending = waiting(isolated_db, tmp_path, autonomous=True)
    for _ in range(3):
        advanced = request("POST", "/api/workflows/" + wf["id"] + "/advance")
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["state_version"] == wf["state_version"]
    assert (
        request("GET", "/api/workflows/" + wf["id"] + "/elicitation").json()[
            "request_id"
        ]
        == pending["request_id"]
    )


def test_two_public_answers_consume_one_current_decision(isolated_db, tmp_path):
    wf, _task, pending = waiting(isolated_db, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reply(wf, pending).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert (
        len(
            [
                e
                for e in isolated_db.list_workflow_events(wf["id"], limit=1000)
                if e.event_type == "elicit.answered"
            ]
        )
        == 1
    )


def test_indented_example_is_not_a_plain_request(isolated_db, tmp_path):
    text = "    " + report().splitlines()[-2] + "\n" + report().splitlines()[-1]
    wf, _task, pending = waiting(isolated_db, tmp_path, text=text)
    assert wf["status"] == "awaiting_human" and pending is None


def test_real_provider_report_boundary_is_bound_before_answer(isolated_db, tmp_path):
    wf, task, pending = waiting(isolated_db, tmp_path, provider_boundary=True)
    assert pending is not None and pending["binding"]["provider_report_sha256"]
    with isolated_db._connect(immediate=True) as conn:
        conn.execute(
            "UPDATE task_reports SET report_text=replace(report_text,'Context for the operator.','Changed context.') WHERE task_id=?",
            (task["id"],),
        )
    # Same question/terminal task text is insufficient after report context drift.
    assert request("GET", "/api/workflows/" + wf["id"] + "/elicitation").json() is None
    assert reply(wf, pending).status_code == 409


def test_old_unversioned_event_does_not_break_legacy_wait(isolated_db, tmp_path):
    from promptpilot.models import WorkflowEventCreate

    wf, _task, pending = waiting(
        isolated_db, tmp_path, text="Old human question without structured data"
    )
    assert pending is None
    with isolated_db._connect(immediate=True) as conn:
        isolated_db._append_workflow_event(
            conn,
            WorkflowEventCreate(
                workflow_id=wf["id"],
                event_type="elicit.requested",
                idempotency_key="legacy-elicit-prototype",
                payload={
                    "version": wf["state_version"],
                    "question": "Historical prototype",
                },
            ),
        )
    assert request("GET", "/api/workflows/" + wf["id"] + "/elicitation").json() is None
    resumed = request(
        "POST",
        "/api/workflows/" + wf["id"] + "/human-input",
        json=dict(
            expected_version=wf["state_version"],
            text="Continue legacy workflow",
            resume=True,
        ),
    )
    assert resumed.status_code == 200, resumed.text
