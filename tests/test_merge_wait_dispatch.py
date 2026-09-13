"""A fresh, unambiguous integration REVIEW wait must not start a MERGE model."""

import copy

import pytest

from promptpilot import pipeline_insights, project_pipeline as pp, worker
from promptpilot.models import TaskCreate


HEAD = "a" * 40


def waiting_health(stage="integration-review", executable=None):
    owner = {"number": 42, "head": HEAD, "stage": stage}
    return {
        "state": "yellow", "integration_owner": owner,
        "review_candidates": [dict(owner)], "merge_executable": executable,
        "findings": [{"code": "single_flight_barrier", "severity": "yellow", "pr": 42}],
    }


@pytest.mark.parametrize("stage", ["integration-review", "legacy-integration-review"])
@pytest.mark.parametrize("executable", [None, []])
def test_next_merge_waits_after_one_fresh_health_without_reading_ship(monkeypatch, stage, executable):
    calls = []
    health = waiting_health(stage, executable)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])

    def fresh(config, **kwargs):
        calls.append(kwargs)
        return health

    monkeypatch.setattr(pp, "run_health", fresh)
    monkeypatch.setattr(pp, "list_ship", lambda *_: pytest.fail("wait must not scan ship queue"))
    result = pp.next_merge(object(), {}, config_path="pipelinectl.json")
    assert calls == [{"config_path": "pipelinectl.json"}]
    assert result["action"] == "wait"
    assert result["number"] == 42 and result["head"] == HEAD
    assert stage in result["reason"]
    assert "lease" not in result


@pytest.mark.parametrize("change", [
    {"state": "red"}, {"state": "green"}, {"state": None},
    {"integration_owner": None},
    {"integration_owner": {"number": True, "head": HEAD, "stage": "integration-review"}},
    {"integration_owner": {"number": 0, "head": HEAD, "stage": "integration-review"}},
    {"integration_owner": {"number": 42, "head": "short", "stage": "integration-review"}},
    {"review_candidates": []}, {"review_candidates": None},
    {"review_candidates": [{"number": 43, "head": HEAD, "stage": "integration-review"}]},
    {"review_candidates": [{"number": 42, "head": "b" * 40, "stage": "integration-review"}]},
    {"review_candidates": [{"number": 42, "head": HEAD, "stage": "review"}]},
    {"review_candidates": [None]},
    {"merge_executable": [{"number": 42}]}, {"merge_executable": {}},
    {"merge_executable": False}, {"merge_executable": ""},
    {"findings": []}, {"findings": None}, {"findings": [None]},
    {"findings": [{"code": "single_flight_barrier", "pr": 43}]},
    {"findings": [{"code": "single_flight_barrier", "pr": 42, "severity": "red"}]},
])
def test_ambiguous_or_conflicting_health_does_not_short_circuit(change):
    health = waiting_health()
    health.update(copy.deepcopy(change))
    assert pp.integration_review_wait(health) is None


@pytest.mark.parametrize("field", ["state", "integration_owner", "review_candidates", "merge_executable", "findings"])
def test_older_incomplete_health_does_not_short_circuit(field):
    health = waiting_health()
    del health[field]
    assert pp.integration_review_wait(health) is None


@pytest.mark.parametrize("field", ["review_candidates", "findings"])
def test_multiple_candidates_or_barriers_do_not_short_circuit(field):
    health = waiting_health()
    health[field].append(dict(health[field][0]))
    assert pp.integration_review_wait(health) is None


def test_red_health_keeps_full_skill_fallback(monkeypatch):
    health = waiting_health()
    health["state"] = "red"
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: health)
    assert pp.next_merge(object(), {}) == {"action": "fallback", "reason": "health check is red"}


@pytest.mark.parametrize("stage", [
    "integration-merge-ready", "legacy-integration-merge-ready", "integration-merge-recovery", "unknown",
])
def test_other_owner_stages_keep_full_skill_fallback(monkeypatch, stage):
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: waiting_health(stage))
    monkeypatch.setattr(pp, "list_ship", lambda *_: pytest.fail("owner requires full skill"))
    assert pp.next_merge(object(), {}) == {
        "action": "fallback", "reason": "single-flight/base-sync owner requires the full skill",
    }


def test_no_owner_keeps_ordinary_ship_election(monkeypatch):
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: {"state": "green", "findings": []})
    calls = []
    monkeypatch.setattr(pp, "list_ship", lambda *_: calls.append("ship") or [])
    assert pp.next_merge(object(), {})["action"] == "empty"
    assert calls == ["ship"]


def test_pending_cleanup_still_precedes_integration_wait(monkeypatch):
    intent = {"number": 7}
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [intent])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: pytest.fail("recovery precedes health"))
    monkeypatch.setattr(pp, "pending_merge_action", lambda _gh, _config, found: {"action": "cleanup", "target": found})
    assert pp.next_merge(object(), {}) == {"action": "cleanup", "target": intent}


def test_next_merge_rechecks_health_when_owner_becomes_ready(monkeypatch):
    snapshots = iter([waiting_health(), waiting_health("integration-merge-ready", [{"number": 42}])])
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: next(snapshots))
    assert pp.next_merge(object(), {})["action"] == "wait"
    assert pp.next_merge(object(), {})["action"] == "fallback"


def test_worker_routes_real_merge_wait_without_loading_provider(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="Example - MERGE\n/merge-shepherd", recurrence="4h"))
    task = isolated_db.get_next_runnable()
    queue = {"id": "merge", "execution": {"mode": "auto", "command": ["project-pipelinectl", "next", "merge"]}}
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _: None)
    monkeypatch.setattr(pipeline_insights, "_matching_queue", lambda _: ("example", {}, queue))
    monkeypatch.setattr(pipeline_insights, "_tool_available", lambda *_: (True, ""))
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: waiting_health())
    monkeypatch.setattr(pipeline_insights, "_tool_preflight", lambda *_: pp.next_merge(object(), {}))
    monkeypatch.setattr(worker, "load_providers", lambda: pytest.fail("waiting MERGE must not load provider"))

    worker._execute_task_inner(task)

    settled = isolated_db.get_task(task.id)
    assert settled.status.value == "completed"
    assert settled.verdict == "ПУСТО"
    assert "integration owner #42 is waiting for integration-review" in settled.result
    assert "Провайдер не запускался" in settled.result
