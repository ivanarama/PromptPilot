"""Real disposable SQLite + public worker route, using the public RO CLI."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import copy
import io
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from promptpilot import db, pipeline_insights as pi, pipeline_item_holds as holds
from promptpilot import project_pipeline as pp, fallback_handoff
from promptpilot.models import TaskCreate
from test_merge_admission_progress import ArchiveGitHub, FIXTURE


@pytest.fixture
def worker_replay(isolated_db, tmp_path, monkeypatch):
    archive = json.loads(FIXTURE.read_text(encoding="utf8"))
    path = tmp_path / "pipelinectl.json"
    path.write_text(json.dumps({"repository": "ivanarama/onebase", "trusted_account": "ivanarama",
        "health_command": [sys.executable, "-c", "raise SystemExit('not used')"],
        "required_checks": archive["required_checks"], "fallback_handoff": "target-v1"}), encoding="utf8")
    key = tmp_path / "existing-test-key"
    key.write_bytes(bytes(range(32)))
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(key))
    monkeypatch.chdir(tmp_path)
    queue = {"id": "merge", "title": "Merge", "series_contains": "Example - MERGE",
        "item_blockers": True, "dispatch_gate": {"skip_when_empty": True},
        "execution": {"mode": "auto", "stage": "merge", "command": [sys.executable, "-m",
            "promptpilot.project_pipeline", "--config", str(path), "next", "merge"]}}
    profile = {"repository": "ivanarama/onebase", "queues": [queue]}
    state = {"pending": False, "probe": 0, "next": 0, "budget": True,
             "available": True, "mismatch": False, "next_error": False, "next_calls": []}
    inside = threading.local()

    def data():
        stage = "integration-merge-recovery" if state["pending"] else "merge"
        target = {"number": 2480, "head": archive["snapshot"]["headRefOid"], "stage": stage}
        health = {"state": "yellow", "findings": [], "review_candidates": [],
            "content_review_candidates": [], "merge_executable": [target], "merge_candidates": [target],
            "integration_owner": None, "human_waiting": []}
        if state["pending"]:
            health.update(integration_owner=target,
                          findings=[{"code": "single_flight_barrier", "pr": 2480, "severity": "yellow"}])
        return {"repository": profile["repository"], "cache": {"complete": True, "stale": False,
            "invalidated": False, "age_seconds": 0, "ttl_seconds": 300, "profile_hash": "f" * 64},
            "queues": [{"id": "merge", "title": "Merge", "membership_complete": True,
                "backlog": 1, "admission_items": [{"number": 2480, "kind": "pr", "updated_at": "now"}]}],
            "diagnostics": health}

    @contextmanager
    def admission(*_a, **_k):
        prior = getattr(inside, "active", False)
        inside.active = True
        try:
            yield {"enabled": state.get("costed", True), "allowed": state["budget"],
                   "budget_route": "tool_preflight", "requested_cost":
                   {"core": 32 if state.get("costed", True) else 0, "graphql": 32, "search": 0}}
        finally:
            inside.active = prior

    def available(*_a, **_k):
        assert inside.active
        return state["available"], "test tool unavailable"

    def preflight(execution, command, working_dir, **_kwargs):
        assert inside.active
        if "inspect-merge-progress" in command:
            state["probe"] += 1
            gh = ArchiveGitHub(archive, pending=state.get("actual_pending", state["pending"]))
            monkeypatch.setattr(pp, "GitHub", lambda: gh)
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = pp.run(command[3:])
            assert code == 0
            return json.loads(output.getvalue())
        assert command[-2:] == ["next", "merge"]
        if state.get("next_entered"):
            state["next_entered"].set()
        state["next_calls"].append(command)
        state["next"] = len(state["next_calls"])
        baseline = json.loads(db.get_setting(holds._attempt_key(getattr(inside, "actual_task", -1)))
                              or db.get_setting(holds.BASELINE + str(state["series"])))
        fact = baseline["canonical_progress"]["2480"]
        assert fact["phase"] == ("pending-base-sync" if state["pending"] else "reviewed-source")
        if state["pending"]:
            saved = json.loads(db.get_setting(holds.PREFIX + str(state["series"])))
            assert saved["2480"]["progress_attempt"]["task_id"] == getattr(inside, "actual_task", baseline["task_id"])
        if state["next_error"]:
            raise RuntimeError("unknown next outcome")
        health = data()["diagnostics"]
        target = copy.deepcopy(health["merge_executable"][0])
        if state["mismatch"]:
            target["head"] = "d" * 40
            health["merge_executable"] = [target]
            health["integration_owner"] = target
        return fallback_handoff.create(pp.load_config(str(path)), health, "merge", target, "test election")

    monkeypatch.setattr(pi, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pi, "read_cached", lambda *_: data())
    monkeypatch.setattr(pi, "_github_scan_admission", admission)
    monkeypatch.setattr(pi, "_tool_available", available)
    monkeypatch.setattr(pi, "_tool_preflight", preflight)
    monkeypatch.setattr(pi, "_reserve_execution_admission", lambda *_a, **_kw: {"allowed": True})
    state.update(data=data, queue=queue, config=path, thread_context=inside)
    return state


def first_failure(state, verdict="НЕ СМОГ"):
    pending = db.create_task(TaskCreate(prompt="Example - MERGE", recurrence="15m",
                                      working_dir=str(state["config"].parent)))
    state["series"] = pending.series_id
    assert pi.dispatch_gate(pending) is None  # Probe is RO, before any next or claim.
    first = db.get_next_runnable()
    assert first.id == pending.id and first.started_at is not None
    route = pi.execution_route(first, "original skill", str(state["config"].parent))
    assert route["action"] == "prompt" and route["next_already_run"]
    assert state["probe"] == 1 and state["next"] == 1
    state["pending"] = True  # Authentic archived intent becomes visible after prelaunch.
    db.mark_completed(first.id, f"ИТОГ: {verdict} (#2480 — runtime invalidated after intent)", verdict=verdict)
    db.pause_pipeline_series_on_repeated_blocker(first.series_id, first.id)
    return first, json.loads(db.get_setting(holds.PREFIX + str(first.series_id)))["2480"]


def successor(state):
    assert db.series_action(state["series"], "run_now")  # Does NOT clear item holds.
    pending = db.get_task(db.get_series(state["series"])["next_task_id"])
    return pending


def test_public_worker_replays_real_progress_once_and_preserves_history(worker_replay):
    state = worker_replay
    first, original = first_failure(state)
    assert original["canonical_progress"]["phase"] == "reviewed-source"
    pending = successor(state)
    assert pi.dispatch_gate(pending) is None
    second = db.get_next_runnable()
    route = pi.execution_route(second, "original skill", str(state["config"].parent))
    assert route["action"] == "prompt" and route["target"]["stage"] == "integration-merge-recovery"
    assert state["probe"] == 2 and state["next"] == 2  # Dispatch + execution share one proof.
    db.mark_completed(second.id, "ИТОГ: НЕ СМОГ (#2480 — same pending intent)", verdict="НЕ СМОГ")
    db.pause_pipeline_series_on_repeated_blocker(second.series_id, second.id)
    saved = db.get_setting(holds.PREFIX + str(first.series_id))
    held = json.loads(saved)["2480"]
    assert held["at"] == original["at"] and held["task_id"] == first.id and held["reason"] == original["reason"]
    assert held["canonical_progress"]["phase"] == "pending-base-sync"
    db.pause_pipeline_series_on_repeated_blocker(second.series_id, second.id)
    assert db.get_setting(holds.PREFIX + str(first.series_id)) == saved
    third = successor(state)
    assert pi.dispatch_gate(third)["action"] == "defer"
    assert state["probe"] == 2 and state["next"] == 2
    assert db.get_setting(holds.PREFIX + str(first.series_id)) == saved


@pytest.mark.parametrize("kind", ["legacy", "human", "budget", "stale", "other_owner"])
def test_public_dispatch_does_not_release_unproved_legacy_or_human_hold(worker_replay, monkeypatch, kind):
    state = worker_replay
    first, original = first_failure(state, "НУЖЕН ЧЕЛОВЕК" if kind == "human" else "НЕ СМОГ")
    if kind == "legacy":
        original.pop("canonical_progress")
        original.pop("verdict")
        db.set_setting(holds.PREFIX + str(first.series_id), json.dumps({"2480": original}))
    if kind == "budget": state["budget"] = False
    if kind in {"stale", "other_owner"}:
        old = state["data"]
        def changed():
            value = old()
            if kind == "stale": value["cache"]["stale"] = True
            else:
                value["diagnostics"]["integration_owner"]["number"] = 9
                value["queues"][0]["admission_items"][0]["number"] = 9
            return value
        monkeypatch.setattr(pi, "read_cached", lambda *_: changed())
    saved = db.get_setting(holds.PREFIX + str(first.series_id))
    pending = successor(state)
    outcome = pi.dispatch_gate(pending)
    # Generic dispatch may fall back to project election on a stale cache;
    # it must never release/consume a canonical hint or invoke next here.
    assert outcome is None if kind in {"stale", "other_owner"} else outcome["action"] == "defer"
    assert state["next"] == 1 and state["probe"] == 1
    assert db.get_setting(holds.PREFIX + str(first.series_id)) == saved


@pytest.mark.parametrize("failure", ["availability", "next_error", "target_mismatch"])
def test_recovery_never_becomes_generic_llm_fallback(worker_replay, failure):
    state = worker_replay
    first_failure(state)
    pending = successor(state)
    assert pi.dispatch_gate(pending) is None
    task = db.get_next_runnable()
    if failure == "availability": state["available"] = False
    if failure == "next_error": state["next_error"] = True
    if failure == "target_mismatch": state["mismatch"] = True
    route = pi.execution_route(task, "original generic skill", str(state["config"].parent))
    assert route["action"] == "block" and "prompt" not in route


def test_two_claimed_tasks_cannot_both_spend_one_progress(worker_replay, monkeypatch):
    state = worker_replay
    first, original = first_failure(state)
    created = [db.create_task(TaskCreate(prompt="Example - MERGE", series_id=state["series"],
        working_dir=str(state["config"].parent))) for _ in range(2)]
    for task in created:
        assert pi.dispatch_gate(task) is None
    claimed = [db.get_next_runnable(), db.get_next_runnable()]
    assert {t.id for t in claimed} == {t.id for t in created}
    # Recurrence intentionally settles only the latest occurrence. Make that
    # contender win while the older contender is already prepared: this also
    # exercises an overwritten series baseline and the winner's task copy.
    latest = max(t.id for t in claimed)
    older_prepared, next_entered, latest_prepared = (threading.Event() for _ in range(3))
    state["next_entered"] = next_entered
    original_available = pi._tool_available
    def ordered_available(*args, **kwargs):
        value = original_available(*args, **kwargs)
        if state["thread_context"].actual_task == latest:
            latest_prepared.set()
            assert older_prepared.wait(10)
        else:
            older_prepared.set()
            assert next_entered.wait(10)
        return value
    monkeypatch.setattr(pi, "_tool_available", ordered_available)
    def execute(task):
        state["thread_context"].actual_task = task.id
        if task.id != latest:
            assert latest_prepared.wait(10)
        return pi.execution_route(task, "skill", str(state["config"].parent))
    with ThreadPoolExecutor(max_workers=2) as pool:
        routes = list(pool.map(execute, claimed))
    assert sum(r["action"] == "prompt" for r in routes) == 1
    assert len(state["next_calls"]) == 2  # First failure plus exactly one recovery next.
    winner = next(task for task, route in zip(claimed, routes) if route["action"] == "prompt")
    db.mark_completed(winner.id, "ИТОГ: НЕ СМОГ (#2480 — same intent)", verdict="НЕ СМОГ")
    db.pause_pipeline_series_on_repeated_blocker(first.series_id, winner.id)
    saved = json.loads(db.get_setting(holds.PREFIX + str(first.series_id)))["2480"]
    assert saved["canonical_progress"]["phase"] == "pending-base-sync"
    assert saved["last_progress_task_id"] == winner.id
    assert (saved["at"], saved["task_id"], saved["reason"]) == (original["at"], first.id, original["reason"])



@pytest.mark.parametrize("change", ["human", "baseline", "head"])
def test_changed_hold_after_prepare_blocks_before_next(worker_replay, monkeypatch, change):
    state = worker_replay
    first_failure(state)
    pending = successor(state)
    assert pi.dispatch_gate(pending) is None
    task = db.get_next_runnable()
    def changed(*_a, **_k):
        key = holds.PREFIX + str(state["series"])
        saved = json.loads(db.get_setting(key))
        held = saved["2480"]
        if change == "human": held["verdict"] = "НУЖЕН ЧЕЛОВЕК"
        if change == "baseline": held["canonical_progress"]["source"]["epoch"] = "b" * 64
        if change == "head": held["head"] = "b" * 40
        db.set_setting(key, json.dumps(saved))
        return True, ""
    monkeypatch.setattr(pi, "_tool_available", changed)
    route = pi.execution_route(task, "generic skill", str(state["config"].parent))
    assert route["action"] in {"block", "defer"} and "prompt" not in route
    assert state["next"] == 1


@pytest.mark.parametrize("change", ["stale", "invalidated", "partial", "no_actual_progress", "unknown_protocol", "uncosted", "wrapper"])
def test_unproved_new_pending_hold_stops_before_next_or_llm(worker_replay, monkeypatch, change):
    state = worker_replay
    first_failure(state)
    if change == "unknown_protocol":
        key = holds.PREFIX + str(state["series"])
        saved = json.loads(db.get_setting(key))
        saved["2480"]["canonical_progress"]["protocol"] = "unknown-v99"
        db.set_setting(key, json.dumps(saved))
    if change == "no_actual_progress": state["actual_pending"] = False
    if change == "uncosted": state["costed"] = False
    if change == "wrapper": state["queue"]["execution"]["command"][0] = "unknown-wrapper"
    original = state["data"]
    def changed():
        value = original()
        if change == "stale": value["cache"]["stale"] = True
        if change == "invalidated": value["cache"]["invalidated"] = True
        if change == "partial": value["cache"]["complete"] = False
        return value
    monkeypatch.setattr(pi, "read_cached", lambda *_: changed())
    pending = successor(state)
    task = db.get_next_runnable()
    assert task.id == pending.id
    route = pi.execution_route(task, "generic skill", str(state["config"].parent))
    assert route["action"] == "defer" and "prompt" not in route
    assert state["next"] == 1
