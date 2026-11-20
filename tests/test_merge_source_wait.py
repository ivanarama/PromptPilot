"""Ordinary, signed MERGE dispatch through its passive Source read and recurring recovery."""
import json
import sys
import time

import pytest

from promptpilot import entrypoint, pipeline_insights as pi, pipeline_item_holds as holds
from tests.test_merge_checkpoint import OLD, elect
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import continuation as continuation, targeted_merge as targeted_merge


@pytest.fixture
def attempt(merge_dispatch, monkeypatch):
    f = merge_dispatch
    q = f.profile["queues"][0]
    q["item_blockers"] = True
    current = elect(f, monkeypatch, OLD, "merge")
    cache = pi._cache_metadata("durable", time.time(), 2, 3, 2, pi._profile_fingerprint(f.profile))
    data = {"profile_id": "example", "repository": f.profile["repository"], "diagnostics": current, "cache": cache,
            "queues": [{"id": "merge", "membership_complete": True, "backlog": 0, "items": []}]}
    monkeypatch.setattr(pi, "read_cached", lambda *_: data)
    f.route = pi.execution_route(f.task, f.task.prompt, retain_budget=True)
    assert f.route["action"] == "prompt", f.route
    f.envelope = json.loads(f.route["prompt"].split("```json\n")[-1].split("\n```")[0])
    f.data = data
    monkeypatch.setenv("PP_TASK_ID", str(f.task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", f.task.started_at.isoformat())
    return f


def observer(f, monkeypatch, capsys, *, number=None, head=None):
    cmd = list(f.envelope["source_wait_command"])
    pos = cmd.index("merge-checkpoint")
    assert cmd[:pos] == [sys.executable, "-m", "promptpilot.project_pipeline"]
    assert cmd[0] != f.envelope["gate_command"][0]
    if number is not None:
        cmd[cmd.index("--number") + 1] = str(number)
    if head is not None:
        cmd[cmd.index("--from") + 1] = head
    monkeypatch.setattr(sys, "argv", ["pp", "pipelinectl", *cmd[pos:]])
    with pytest.raises(SystemExit) as invoked:
        entrypoint.main()
    result = json.loads(capsys.readouterr().out)
    assert result["product_mutation_authorized"] is False
    assert invoked.value.code == int(result["state"] == "refused")
    assert "lease" not in result
    return result


def finish(f, verdict, reason):
    f.db.mark_completed(f.task.id, "ИТОГ: " + verdict + " (" + reason + ")", verdict=verdict)
    return f.db.repair_active_series_occurrences(repeat_guard_series_ids=[f.task.series_id])


@pytest.mark.parametrize("wait", ["invalidated", "scan_in_progress", "budget_in_flight", "stale"])
def test_source_wait_recreates_scheduled_occurrence_without_park(attempt, monkeypatch, capsys, wait):
    f = attempt
    if wait in {"invalidated", "stale"}:
        f.data["cache"][wait] = True
    else:
        f.data["cache"]["refresh_blocked"] = wait
    before = f.db.pipeline_github_budget_reservations("github-default")
    measured = observer(f, monkeypatch, capsys)
    assert measured["state"] == "wait" and measured["reason"] == wait
    assert measured["result_line"] == f"ИТОГ: ПУСТО — нет допуска: {wait}"
    assert f.db.pipeline_github_budget_reservations("github-default") == before
    assert f.searches == f.checks == []
    old = {"42": {"stage": "merge", "at": time.time() - holds.TTL - 60, "head": OLD, "task_id": 91},
           "99": {"stage": "merge", "at": time.time(), "head": OLD, "reason": "human", "task_id": 92}}
    raw = json.dumps(old)
    key = holds.PREFIX + str(f.task.series_id)
    f.db.set_setting(key, raw)
    assert finish(f, "НЕ СМОГ", "#42 — нет допуска: " + wait) == [f.task.series_id]
    with f.db._connect() as conn:
        successor = conn.execute("select id,status,scheduled_at from tasks where series_id=? order by id desc",
                                 (f.task.series_id,)).fetchone()
    assert successor["id"] != f.task.id and successor["status"] == "pending"
    assert successor["scheduled_at"] > f.db.get_task(f.task.id).completed_at.isoformat()
    assert f.db.get_setting(key) == raw
    assert not f.db.get_series(f.task.series_id)["paused"]


@pytest.mark.parametrize("fault", ["unobserved", "other-head", "other-attempt", "foreign-source",
    "replaced-target", "incomplete", "reconciled", "human", "ambiguous", "changed-cause", "unknown",
    "pending-send", "updated-head"])
def test_unmeasured_or_other_failures_keep_normal_holds(attempt, monkeypatch, capsys, fault):
    f = attempt
    f.data["cache"]["invalidated"] = True
    verdict, reason = "НЕ СМОГ", "#42 — нет допуска: invalidated"
    if fault == "foreign-source":
        f.data["repository"] = "foreign/repo"
    if fault == "other-attempt":
        monkeypatch.setenv("PP_TASK_STARTED_AT", "another attempt")
    if fault == "pending-send":
        from promptpilot import merge_checkpoint
        f.db.set_setting(merge_checkpoint.pending_key(f.profile["repository"], {"number": 42, "head": OLD}), "pending")
    if fault == "updated-head":
        recovery = elect(f, monkeypatch, OLD, "integration-merge-recovery")
        recovery["integration_owner"]["base_sync_candidate"] = {"source": "head_parents", "to": OLD}
        f.data["diagnostics"] = recovery
    if fault == "replaced-target":
        f.data["diagnostics"]["merge_executable"][0]["head"] = "c" * 40
    if fault == "incomplete":
        f.data["queues"][0]["membership_complete"] = False
    if fault != "unobserved":
        got = observer(f, monkeypatch, capsys, head="c" * 40 if fault == "other-head" else None)
        if fault in ("foreign-source", "other-attempt", "other-head", "incomplete", "replaced-target", "pending-send", "updated-head"):
            assert got["state"] == "refused"
        else:
            assert got["state"] == "wait"
    if fault == "reconciled":
        f.data["cache"]["invalidated"] = False
        assert observer(f, monkeypatch, capsys)["state"] == "observed"
    if fault == "human":
        verdict = "НУЖЕН ЧЕЛОВЕК"
    if fault == "ambiguous":
        reason += "; результат отправки неизвестен"
    if fault == "changed-cause":
        reason = "#42 — нет допуска: scan_in_progress"
    if fault == "unknown":
        reason = "#42 — не удалось проверить результат отправки"
    assert finish(f, verdict, reason) == [f.task.series_id]
    hold = json.loads(f.db.get_setting(holds.PREFIX + str(f.task.series_id)))["42"]
    assert hold["head"] == OLD and hold["task_id"] == f.task.id

def test_two_verified_waits_keep_series_active_and_elect_again(attempt, monkeypatch, capsys):
    f = attempt
    sid = f.task.series_id
    first_task = f.task.id
    for cycle in range(2):
        f.data["cache"]["invalidated"] = True
        assert observer(f, monkeypatch, capsys)["state"] == "wait"
        assert finish(f, "НЕ СМОГ", "#42 — нет допуска: invalidated") == [sid]
        assert not f.db.get_series(sid)["paused"] and f.db.get_setting(holds.PREFIX + str(sid)) is None
        if cycle == 0:
            pi.release_execution_admission()
            # Simulate the next scheduled time; keep the same recurrence, holds and budget gates.
            assert f.db.series_action(sid, "run_now")
            task = f.db.get_next_runnable()
            assert task.id != first_task and task.series_id == sid
            f.db.set_session_id(task.id, "next-native-session")
            f.task = f.db.get_task(task.id)
            f.data["cache"]["invalidated"] = False
            current = elect(f, monkeypatch, OLD, "integration-merge-recovery")
            f.data["diagnostics"] = current
            f.route = pi.execution_route(f.task, f.task.prompt, retain_budget=True)
            assert f.route["action"] == "prompt" and f.route["next_already_run"]
            f.envelope = json.loads(f.route["prompt"].split("```json\n")[-1].split("\n```")[0])
            monkeypatch.setenv("PP_TASK_ID", str(f.task.id))
            monkeypatch.setenv("PP_TASK_STARTED_AT", f.task.started_at.isoformat())

@pytest.mark.parametrize("invalidated_before_election", [False, True])
def test_real_cached_source_invalidation_is_bound_to_new_attempt(
        merge_dispatch, monkeypatch, capsys, invalidated_before_election):
    f = merge_dispatch
    f.profile["queues"][0]["item_blockers"] = True
    health = elect(f, monkeypatch, OLD, "merge")
    _, generation = pi._cache_snapshot("example")
    revision = f.db.increment_int_setting(pi._refresh_revision_key("example"))
    assert pi._publish_cache("example", f.profile, generation, pi._cache_epoch(), revision, {
        "profile_id": "example", "repository": f.profile["repository"], "generated_at": time.time(),
        "queues": [{"id": "merge", "backlog": 1, "membership_complete": True, "items": [
            {"kind": "pr", "number": 42, "updated_at": OLD, "head": OLD}]}], "diagnostics": health})
    if invalidated_before_election:
        f.db.increment_int_setting(pi._CACHE_EPOCH_KEY)
    route = pi.execution_route(f.task, f.task.prompt, retain_budget=True)
    assert route["action"] == "prompt"
    f.envelope = json.loads(route["prompt"].split("```json\n")[-1].split("\n```")[0])
    monkeypatch.setenv("PP_TASK_ID", str(f.task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", f.task.started_at.isoformat())
    monkeypatch.setattr(pi, "_github_search", lambda *_: pytest.fail("passive read searched GitHub"))
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: pytest.fail("passive read refreshed rates"))
    if not invalidated_before_election:
        f.db.increment_int_setting(pi._CACHE_EPOCH_KEY)
    result = observer(f, monkeypatch, capsys)
    assert result["state"] == ("refused" if invalidated_before_election else "wait"), result


def test_frozen_observer_uses_running_native_executable(merge_dispatch, monkeypatch):
    f = merge_dispatch
    f.profile["queues"][0]["item_blockers"] = True
    elect(f, monkeypatch, OLD, "merge")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    route = pi.execution_route(f.task, f.task.prompt, retain_budget=True)
    envelope = json.loads(route["prompt"].split("```json\n")[-1].split("\n```")[0])
    assert envelope["gate_command"][0] == "pipeline"
    observer = envelope["source_wait_command"]
    assert observer[:4] == [sys.executable, "pipelinectl", "merge-checkpoint", "source-wait"]
    assert observer[-2:] == route["gate_command"][-2:]
