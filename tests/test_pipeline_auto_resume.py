"""An automatic circuit breaker recovers only from externally proven change."""

import json
import time

from promptpilot import pipeline_insights, worker
from promptpilot.models import TaskCreate


def paused_series(isolated_db, monkeypatch, queue_id, reason):
    profile = {"repository": "owner/example", "queues": [{
        "id": queue_id, "series_contains": "Example - " + queue_id.upper(),
    }]}
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    created = isolated_db.create_task(TaskCreate(
        prompt="Example - " + queue_id.upper(), recurrence="15m"))
    for _ in range(2):
        assert isolated_db.series_action(created.series_id, "run_now")
        occurrence = isolated_db.get_next_runnable()
        assert occurrence is not None
        assert isolated_db.mark_completed(
            occurrence.id, f"ИТОГ: НУЖЕН ЧЕЛОВЕК ({reason})",
            verdict="НУЖЕН ЧЕЛОВЕК",
            expected_started_at=occurrence.started_at,
        )
        worker._recur_after_run(occurrence)
    series = isolated_db.get_series(created.series_id)
    assert series["paused"] and series["auto_pause_reason"] == reason
    return profile, series


def snapshot(*reviewable):
    return {"cache": {"complete": True, "stale": False},
            "diagnostics": {"content_review_candidates": [
                {"number": number} for number in reviewable]}}


def test_closed_blockers_resume_triage_once(isolated_db, monkeypatch):
    profile, series = paused_series(
        isolated_db, monkeypatch, "triage", "#1670 — route; #1683 — reply")
    calls = []

    def read_target(args):
        calls.append(args[0])
        number = int(args[0].rsplit("/", 1)[1])
        return {"number": number, "state": "closed"}

    monkeypatch.setattr(pipeline_insights, "_gh_api_json", read_target)
    observed = snapshot()
    observed["cache"]["token"] = {
        "profile_hash": pipeline_insights._profile_fingerprint(profile),
        "epoch": 1, "revision": 1, "generated_at": time.time(),
    }
    monkeypatch.setattr(pipeline_insights, "analyze", lambda *_a, **_kw: observed)
    assert pipeline_insights.sample_active_profiles([series]) == {
        "example": f"ok; resumed={series['id']}"}
    assert calls == ["repos/owner/example/issues/1670",
                     "repos/owner/example/issues/1683"]
    recovered = isolated_db.get_series(series["id"])
    assert not recovered["paused"]
    assert recovered["auto_pause_reason"] is None
    assert recovered["next_status"] == "pending"
    next_id = recovered["next_task_id"]
    assert pipeline_insights._resume_resolved_blockers(
        profile, snapshot(), [series]) == []
    assert isolated_db.get_series(series["id"])["next_task_id"] == next_id


def test_open_or_unverified_target_stays_paused(isolated_db, monkeypatch):
    profile, series = paused_series(
        isolated_db, monkeypatch, "triage", "#1670 — route; #1683 — reply")
    monkeypatch.setattr(pipeline_insights, "_gh_api_json", lambda args: {
        "number": int(args[0].rsplit("/", 1)[1]),
        "state": "open" if args[0].endswith("1683") else "closed",
    })
    assert pipeline_insights._resume_resolved_blockers(
        profile, snapshot(), [series]) == []
    assert isolated_db.get_series(series["id"])["paused"]
    stale = snapshot()
    stale["cache"]["stale"] = True
    assert pipeline_insights._resume_resolved_blockers(profile, stale, [series]) == []


def test_stale_ship_review_candidate_resumes_only_review(isolated_db, monkeypatch):
    profile, series = paused_series(
        isolated_db, monkeypatch, "review", "#1342 — снять устаревший ship")
    monkeypatch.setattr(pipeline_insights, "_gh_api_json", lambda *_: (_ for _ in ()).throw(
        AssertionError("review recovery needs no extra GitHub request")))
    assert pipeline_insights._resume_resolved_blockers(
        profile, snapshot(), [series]) == []
    assert pipeline_insights._resume_resolved_blockers(
        profile, snapshot(1342), [series]) == [series["id"]]
    assert isolated_db.get_series(series["id"])["next_status"] == "pending"


def test_new_triage_item_proceeds_while_old_blocker_remains_open(
        isolated_db, monkeypatch):
    profile, series = paused_series(
        isolated_db, monkeypatch, "triage", "#1670 — route blocked")
    isolated_db.set_setting(
        f"pipeline_item_baseline:v1:{series['id']}",
        json.dumps({"task_id": series["last_task_id"],
                    "states": {"7": "old-snapshot"}}),
    )
    monkeypatch.setattr(pipeline_insights, "_gh_api_json", lambda *_: (_ for _ in ()).throw(
        AssertionError("new admission needs no closed-target proof")))
    data = snapshot()
    data["queues"] = [{"id": "triage", "membership_complete": True,
                       "backlog": 2, "admission_items": [
                           {"number": 7}, {"number": 8}]}]
    assert pipeline_insights._resume_resolved_blockers(
        profile, data, [series]) == [series["id"]]
    assert isolated_db.get_series(series["id"])["next_status"] == "pending"


def test_explicit_pause_overrides_auto_recovery(isolated_db, monkeypatch):
    profile, series = paused_series(
        isolated_db, monkeypatch, "review", "#1342 — устаревший ship")
    assert isolated_db.series_action(series["id"], "pause")
    assert isolated_db.get_series(series["id"])["auto_pause_reason"] is None
    assert pipeline_insights._resume_resolved_blockers(
        profile, snapshot(1342), [series]) == []
    assert isolated_db.get_series(series["id"])["paused"]


def test_only_auto_paused_series_keeps_sampler_active(isolated_db, monkeypatch):
    profile, series = paused_series(
        isolated_db, monkeypatch, "triage", "#1670 — route")
    assert pipeline_insights._profile_active(profile, [series])
    assert isolated_db.series_action(series["id"], "pause")
    assert not pipeline_insights._profile_active(
        profile, [isolated_db.get_series(series["id"])])
