import copy
from types import SimpleNamespace
import pytest

from promptpilot import pipeline_item_holds as holds, pipeline_insights, project_pipeline as pp
from promptpilot.models import TaskCreate


def snapshot(*numbers):
    return {"cache": {"complete": True, "stale": False},
            "queues": [{"id": "triage", "membership_complete": True,
                        "backlog": len(numbers), "items": [
                {"number": n, "kind": "issue", "updated_at": "2026-09-27T12:00:00Z"} for n in numbers]}]}


def test_public_dispatch_parks_item_and_keeps_series_and_new_work(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="Example - TRIAGE", recurrence="15m"))
    queue = {"id": "triage", "series_contains": "Example - TRIAGE", "item_blockers": True,
             "dispatch_gate": {"skip_when_empty": True}}
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": {"queues": [queue]}})
    data = snapshot(7)
    monkeypatch.setattr(pipeline_insights, "read_cached", lambda *_: data)
    assert pipeline_insights.dispatch_gate(task) is None
    isolated_db.mark_completed(task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#7 — invalid route)", verdict="НУЖЕН ЧЕЛОВЕК")
    state = isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    assert not state["suppress_recurrence"]
    assert not isolated_db.list_series()[0]["paused"]
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id, prompt=task.prompt)
    assert pipeline_insights.dispatch_gate(successor)["action"] == "defer"
    data = snapshot(7, 8)
    assert pipeline_insights.dispatch_gate(successor) is None
    assert holds.prepare(successor, queue, data) == [7]
    data["queues"][0]["items"][0]["updated_at"] = "2026-09-27T13:00:00Z"
    assert holds.prepare(successor, queue, data) == []


def test_partial_snapshot_never_excludes_and_manual_run_clears_hold(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Triage", recurrence="15m"))
    queue = {"item_blockers": True}
    data = snapshot(7)
    holds.prepare(task, queue, data)
    isolated_db.mark_completed(task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#7 — blocked)", verdict="НУЖЕН ЧЕЛОВЕК")
    isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id)
    assert holds.prepare(successor, queue, data) == [7]
    partial = copy.deepcopy(data)
    partial["cache"]["complete"] = False
    assert holds.prepare(successor, queue, partial) == []
    assert isolated_db.series_action(task.series_id, "run_now")
    assert holds.prepare(successor, queue, data) == []


def test_review_candidate_outside_search_is_held_without_pausing_series(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Example - REVIEW", recurrence="15m"))
    queue = {"id": "review", "item_blockers": True}
    data = {"cache": {"complete": True, "stale": False},
            "queues": [{"id": "review", "membership_complete": True,
                        "backlog": 0, "admission_items": []}],
            "diagnostics": {"review_candidates": [
                {"number": 1759, "stage": "review", "head": "a" * 40,
                 "review_depth": 2}]}}
    assert holds.prepare(task, queue, data) == []
    isolated_db.mark_completed(
        task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#1759 — требуется решение)",
        verdict="НУЖЕН ЧЕЛОВЕК")
    result = isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    assert not result["suppress_recurrence"]
    assert not isolated_db.list_series()[0]["paused"]
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id)
    assert holds.prepare(successor, queue, data) == [1759]
    data["diagnostics"]["review_candidates"][0]["head"] = "b" * 40
    assert holds.prepare(successor, queue, data) == []


def test_review_hold_requires_fresh_exact_review_candidate(isolated_db):
    isolated_db.create_task(TaskCreate(prompt="Example - REVIEW", recurrence="15m"))
    queue = {"id": "review", "item_blockers": True}
    data = {"cache": {"complete": True, "stale": False},
            "queues": [{"id": "review", "membership_complete": True,
                        "backlog": 0, "admission_items": []}],
            "diagnostics": {"review_candidates": [
                {"number": 7, "stage": "review", "head": "a" * 40}]}}
    assert "7" in holds.fingerprints(data, queue)
    for change in ({"cache": {"complete": False}},
                   {"cache": {"stale": True}},
                   {"diagnostics": {"checker_failed": True}},
                   {"diagnostics": {"review_candidates": [
                       {"number": 7, "stage": "integration-merge-ready", "head": "a" * 40}]}},
                   {"diagnostics": {"review_candidates": [
                       {"number": 7, "stage": "review", "head": "not-a-sha"}]}}):
        changed = copy.deepcopy(data)
        for key, value in change.items():
            changed[key].update(value)
        assert "7" not in (holds.fingerprints(changed, queue) or {})


def test_exact_review_election_survives_stale_queue_snapshot(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Example - REVIEW", recurrence="15m"))
    queue = {"id": "review", "item_blockers": True}
    stale = {"cache": {"complete": False, "stale": True}, "queues": [],
             "diagnostics": None}
    assert holds.prepare(task, queue, stale) == []
    holds.register_review_target(task, 1759, "a" * 40)
    isolated_db.mark_completed(
        task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#1759 — решение владельца)",
        verdict="НУЖЕН ЧЕЛОВЕК")
    assert not isolated_db.pause_pipeline_series_on_repeated_blocker(
        task.series_id, task.id)["suppress_recurrence"]
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id)
    fresh = {"cache": {"complete": True, "stale": False},
             "queues": [{"id": "review", "membership_complete": True,
                         "backlog": 0, "admission_items": []}],
             "diagnostics": {"review_candidates": [
                 {"number": 1759, "stage": "review", "head": "a" * 40}]}}
    assert holds.prepare(successor, queue, fresh) == [1759]
    fresh["diagnostics"]["review_candidates"][0]["head"] = "b" * 40
    assert holds.prepare(successor, queue, fresh) == []


def test_exact_review_election_rejects_another_pr_in_human_result(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Example - REVIEW", recurrence="15m"))
    queue = {"id": "review", "item_blockers": True}
    data = {"cache": {"complete": True, "stale": False},
            "queues": [{"id": "review", "membership_complete": True,
                        "backlog": 2, "admission_items": [
                            {"number": number, "updated_at": "2026-09-30T00:00:00Z"}
                            for number in (7, 8)]}]}
    holds.prepare(task, queue, data)
    holds.register_review_target(task, 7, "a" * 40)
    isolated_db.mark_completed(
        task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#8 — не та цель)",
        verdict="НУЖЕН ЧЕЛОВЕК")
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id)
    isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    assert holds.prepare(successor, queue, data) == []


def test_excluded_integration_owner_is_not_skipped(monkeypatch):
    monkeypatch.setenv("PP_PIPELINE_EXCLUDED_NUMBERS", "[7]")
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_a, **_kw: {
        "state": "yellow", "integration_owner": {"number": 7, "stage": "integration-merge-ready"},
        "findings": [{"code": "single_flight_barrier", "pr": 7}]})
    result = pp.next_merge(object(), {})
    assert result["action"] == "wait"
    assert result["number"] == 7


@pytest.mark.parametrize("priority_ui", [False, True])
@pytest.mark.parametrize("held_number", [1, 2])
def test_public_scan_holds_use_full_membership_not_display_limit(
        isolated_db, monkeypatch, priority_ui, held_number):
    queue = {"id": "triage", "title": "Triage", "query": "is:issue",
             "series_contains": "Example - TRIAGE", "item_blockers": True,
             "dispatch_gate": {"skip_when_empty": True}}
    profile = {"title": "Example", "repository": "owner/example", "queues": [queue]}
    if priority_ui:
        profile["priority_control"] = {"max_items": 1}
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda *_: None)
    members = [{"number": n, "key": f"issue:{n}", "kind": "issue",
                "title": f"Task {n}", "labels": [],
                "created_at": f"2026-09-{20 + n:02}T00:00:00Z",
                "updated_at": "2026-09-27T12:00:00Z"} for n in [1, 2]]
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_: {
        "count": 2, "items": members, "membership_complete": True})
    task = isolated_db.create_task(TaskCreate(prompt="Example - TRIAGE", recurrence="15m"))
    observed = pipeline_insights.analyze("example", isolated_db.list_series(), use_cache=False)
    visible = observed["queues"][0]
    assert [item["number"] for item in visible["items"]] == ([1] if priority_ui else [])
    assert [item["number"] for item in visible["admission_items"]] == [1, 2]
    assert pipeline_insights.dispatch_gate(task) is None
    isolated_db.mark_completed(task.id, f"ИТОГ: НУЖЕН ЧЕЛОВЕК (#{held_number} — route needs decision)",
                               verdict="НУЖЕН ЧЕЛОВЕК")
    assert not isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)["suppress_recurrence"]
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id, prompt=task.prompt)
    assert holds.prepare(successor, queue, observed) == [held_number]
    # Cross-process readers must use the same full admission projection.
    pipeline_insights._cache.clear()
    assert pipeline_insights.dispatch_gate(successor) is None


@pytest.mark.parametrize("partial, admission", [
    (False, False), (False, True), (True, True),
])
def test_unproven_legacy_or_partial_membership_cannot_exclude_items(
        isolated_db, partial, admission):
    task = isolated_db.create_task(TaskCreate(prompt="Example - TRIAGE", recurrence="15m"))
    queue = {"item_blockers": True}
    data = snapshot(1)
    assert holds.prepare(task, queue, data) == []
    isolated_db.mark_completed(task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#1 — blocked)", verdict="НУЖЕН ЧЕЛОВЕК")
    isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id)
    if partial:
        data["queues"][0]["membership_complete"] = False
    if admission:
        data["queues"][0]["admission_items"] = data["queues"][0]["items"]
    if not partial:
        # An incomplete diagnostic projection or a deduplicated multi-query
        # search cannot prove that every active member is held.
        data["queues"][0]["backlog"] = 2
    assert holds.prepare(successor, queue, data) == []
