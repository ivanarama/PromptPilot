from datetime import datetime, timedelta, timezone

from promptpilot.models import TaskCreate
from promptpilot import pipeline_insights


def test_recurring_task_creates_durable_series(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="OneBase - FIX\nDo one fix", working_dir=r"D:\Projects\onebase",
        provider="codex", effort="high", recurrence="4h",
    ))

    assert task.series_id is not None
    series = isolated_db.get_series(task.series_id)
    assert series["title"] == "OneBase - FIX"
    assert series["recurrence"] == "4h"
    assert series["next_task_id"] == task.id


def test_series_settings_persist_and_update_pending_occurrence(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Review", recurrence="4h"))

    assert isolated_db.update_series(task.series_id, {
        "base_recurrence": "1h", "effort": "max", "priority": 2,
        "temporary_recurrence": "30m", "temporary_empty_limit": 2,
    })

    series = isolated_db.get_series(task.series_id)
    occurrence = isolated_db.get_task(task.id)
    assert series["effective_recurrence"] == "30m"
    assert series["effort"] == "max"
    assert occurrence.recurrence == "1h"
    assert occurrence.effort == "max"
    assert occurrence.priority == 2


def test_temporary_boost_returns_to_base_after_consecutive_empty(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Merge", recurrence="2h"))
    isolated_db.update_series(task.series_id, {
        "temporary_recurrence": "30m", "temporary_empty_limit": 2,
    })

    first = isolated_db.prepare_series_recurrence(task.series_id, "ПУСТО")
    second = isolated_db.prepare_series_recurrence(task.series_id, "ПУСТО")

    assert first["effective_recurrence"] == "30m"
    assert first["temporary_empty_count"] == 1
    assert second["effective_recurrence"] == "2h"
    assert second["temporary_recurrence"] is None


def test_pause_hides_series_task_from_runnable_queue(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Triage", recurrence="1h"))
    assert isolated_db.series_action(task.series_id, "pause")
    assert isolated_db.get_next_runnable() is None

    assert isolated_db.series_action(task.series_id, "resume")
    assert isolated_db.get_next_runnable().id == task.id


def test_temporary_boost_expiry_uses_base_interval(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Review", recurrence="4h"))
    isolated_db.update_series(task.series_id, {
        "temporary_recurrence": "1h",
        "temporary_until": datetime.now(timezone.utc) - timedelta(minutes=1),
    })

    state = isolated_db.prepare_series_recurrence(task.series_id, None)
    assert state["effective_recurrence"] == "4h"
    assert state["temporary_recurrence"] is None


def test_pipeline_insights_finds_capacity_bottleneck(isolated_db, monkeypatch):
    counts = iter([15, 5, 17, 9])
    monkeypatch.setattr(pipeline_insights, "_github_count", lambda repo, query: next(counts))
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("onebase", [], use_cache=False)

    assert result["bottleneck"] == "review"
    review = next(q for q in result["queues"] if q["id"] == "review")
    triage = next(q for q in result["queues"] if q["id"] == "triage")
    assert review["runs_needed"] == 8.5
    assert review["recommended_interval"] == "1h"
    assert "оставить 1h" not in review["recommendation"]  # no matching series in this unit test
    assert "решения человека" in triage["recommendation"]
