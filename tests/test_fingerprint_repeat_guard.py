import copy
import json

import pytest

from promptpilot import pipeline_item_holds as holds
from promptpilot.models import TaskCreate


@pytest.mark.parametrize("change", ["unchanged", "revision", "expired", "future"])
def test_fingerprint_reelection_preserves_repeat_guard(isolated_db, monkeypatch, change):
    now = 100000.0
    monkeypatch.setattr(holds.time, "time", lambda: now)
    first = isolated_db.create_task(TaskCreate(prompt="Example FIX", recurrence="15m"))
    queue = {"id": "fix", "item_blockers": True}
    data = {"cache": {"complete": True, "stale": False}, "queues": [],
            "diagnostics": {"review_candidates": [
                {"number": 7, "stage": "review", "head": "a" * 40}]}}
    human = "\u041d\u0423\u0416\u0415\u041d \u0427\u0415\u041b\u041e\u0412\u0415\u041a"
    result = f"\u0418\u0422\u041e\u0413: {human} (#7 - route needs recovery)"
    assert holds.prepare(first, queue, data) == []
    isolated_db.mark_completed(first.id, result, verdict=human)
    assert not isolated_db.pause_pipeline_series_on_repeated_blocker(
        first.series_id, first.id)["suppress_recurrence"]
    key = holds.PREFIX + str(first.series_id)
    original = isolated_db.get_setting(key)
    # Reconciliation of the same attempt is idempotent and cannot extend TTL.
    assert not isolated_db.pause_pipeline_series_on_repeated_blocker(
        first.series_id, first.id)["suppress_recurrence"]
    assert isolated_db.get_setting(key) == original
    changed = copy.deepcopy(data)
    if change == "revision":
        changed["diagnostics"]["review_candidates"][0]["head"] = "b" * 40
    elif change in {"expired", "future"}:
        old = json.loads(original)
        old["7"]["at"] = now - holds.TTL if change == "expired" else now + 1
        isolated_db.set_setting(key, json.dumps(old))
    before_second = isolated_db.get_setting(key)
    second = isolated_db.create_task(TaskCreate(
        prompt=first.prompt, recurrence="15m", series_id=first.series_id))
    assert holds.prepare(second, queue, changed) == ([7] if change == "unchanged" else [])
    # A skill may ignore the exclusion: completion must retain the series guard.
    isolated_db.mark_completed(second.id, result, verdict=human)
    state = isolated_db.pause_pipeline_series_on_repeated_blocker(first.series_id, second.id)
    assert state["suppress_recurrence"] is (change == "unchanged")
    if change == "unchanged":
        assert isolated_db.get_setting(key) == before_second
        assert isolated_db.list_series()[0]["paused"]
    else:
        assert not isolated_db.list_series()[0]["paused"]
        renewed = json.loads(isolated_db.get_setting(key))["7"]
        assert renewed["task_id"] == second.id
        assert renewed["at"] == now
        assert holds.prepare(second, queue, changed) == [7]


def test_unmeasured_target_cannot_renew_fingerprint_hold(isolated_db):
    first = isolated_db.create_task(TaskCreate(prompt="Example FIX", recurrence="15m"))
    queue = {"id": "fix", "item_blockers": True}
    data = {"cache": {"complete": True}, "queues": [], "diagnostics": {
        "review_candidates": [{"number": 7, "stage": "review", "head": "a" * 40}]}}
    human = "\u041d\u0423\u0416\u0415\u041d \u0427\u0415\u041b\u041e\u0412\u0415\u041a"
    result = f"\u0418\u0422\u041e\u0413: {human} (#8 - wrong target)"
    for attempt in range(2):
        task = first if attempt == 0 else isolated_db.create_task(TaskCreate(
            prompt=first.prompt, recurrence="15m", series_id=first.series_id))
        holds.prepare(task, queue, data)
        isolated_db.mark_completed(task.id, result, verdict=human)
        state = isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
        assert state["suppress_recurrence"] is bool(attempt)
    assert isolated_db.get_setting(holds.PREFIX + str(first.series_id)) is None


@pytest.mark.parametrize("verdict", ["\u041d\u0415 \u0421\u041c\u041e\u0413",
                                      "\u041d\u0423\u0416\u0415\u041d \u0427\u0415\u041b\u041e\u0412\u0415\u041a"])
def test_legacy_review_hold_without_stage_retains_repeat_guard(isolated_db, monkeypatch, verdict):
    now = 100000.0
    monkeypatch.setattr(holds.time, "time", lambda: now)
    first = isolated_db.create_task(TaskCreate(prompt="review", recurrence="15m"))
    holds.register_target(first, 7, "a" * 40, "review")
    result = f"\u0418\u0422\u041e\u0413: {verdict} (#7 - route needs recovery)"
    isolated_db.mark_completed(first.id, result, verdict=verdict)
    assert not isolated_db.pause_pipeline_series_on_repeated_blocker(
        first.series_id, first.id)["suppress_recurrence"]
    key = holds.PREFIX + str(first.series_id)
    legacy = json.loads(isolated_db.get_setting(key))
    del legacy["7"]["stage"]
    isolated_db.set_setting(key, json.dumps(legacy))
    saved = isolated_db.get_setting(key)
    now += 10
    second = isolated_db.create_task(TaskCreate(
        prompt=first.prompt, recurrence="15m", series_id=first.series_id))
    data = {"cache": {"complete": True}, "queues": [], "diagnostics": {
        "review_candidates": [{"number": 7, "stage": "review", "head": "a" * 40}]}}
    assert holds.prepare(second, {"id": "review", "item_blockers": True}, data) == [7]
    holds.register_target(second, 7, "a" * 40, "review")
    isolated_db.mark_completed(second.id, result, verdict=verdict)
    assert isolated_db.pause_pipeline_series_on_repeated_blocker(
        second.series_id, second.id)["suppress_recurrence"]
    assert isolated_db.get_setting(key) == saved
