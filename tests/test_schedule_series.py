from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from promptpilot.models import TaskCreate
from promptpilot import pipeline_insights, worker


@pytest.fixture(autouse=True)
def no_live_github_rate_limit(monkeypatch):
    """Pipeline unit tests must never call the operator's real GitHub account."""
    real = pipeline_insights._github_rate_limits
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", lambda: None)
    return real


PIPELINE_PROFILE = {
    "title": "ExampleProject pipeline", "repository": "owner/example",
    "target_clear_hours": 8,
    "queues": [
        {"id": "triage", "title": "Triage", "capacity": 5,
         "query": "is:issue is:open label:triage", "series_contains": "TRIAGE",
         "manual_gate": True},
        {"id": "fix", "title": "Fix", "capacity": 1,
         "queries": ["is:issue is:open label:fix", "is:pr is:open label:fix"],
         "series_contains": "FIX"},
        {"id": "review", "title": "Review", "capacity": 2,
         "query": "is:pr is:open label:review", "series_contains": "REVIEW"},
        {"id": "merge", "title": "Merge", "capacity": 3,
         "query": "is:pr is:open label:merge", "series_contains": "MERGE"},
    ],
}


def _fresh_cache_data(profile_id: str, profile: dict,
                      diagnostics: dict | None = None) -> dict:
    """Publish a real durable cache token for wake-up race tests."""
    generated_at = datetime.now(timezone.utc).timestamp()
    result = {
        "profile_id": profile_id, "repository": profile.get("repository"),
        "queues": [{"id": queue.get("id")}
                   for queue in profile.get("queues", [])],
        "generated_at": generated_at, "diagnostics": diagnostics,
    }
    _cached, generation = pipeline_insights._cache_snapshot(profile_id)
    epoch = pipeline_insights._cache_epoch()
    revision = pipeline_insights.db.increment_int_setting(
        pipeline_insights._refresh_revision_key(profile_id))
    assert pipeline_insights._publish_cache(
        profile_id, profile, generation, epoch, revision, result)
    return {
        "cache": pipeline_insights._cache_metadata(
            "live", generated_at, epoch, revision, epoch,
            pipeline_insights._profile_fingerprint(profile),
        ),
        "diagnostics": diagnostics,
    }


def test_fresh_install_has_no_project_specific_pipeline_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_PIPELINE_PROFILES", str(tmp_path / "missing.json"))

    assert pipeline_insights.DEFAULT_PROFILES == {}
    assert pipeline_insights.list_profiles() == []


def test_first_valid_stale_verdict_reselects_immediately(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h"))
    claimed = isolated_db.get_next_runnable()
    isolated_db.mark_completed(
        claimed.id, "ИТОГ: УСТАРЕЛО (PR HEAD changed)", verdict="УСТАРЕЛО")

    before = datetime.now(timezone.utc)
    worker._recur_after_run(claimed)
    series = isolated_db.get_series(task.series_id)

    assert series["next_task_id"] != task.id
    assert datetime.fromisoformat(series["next_run_at"]) <= \
        before + timedelta(seconds=2)


def test_second_consecutive_stale_uses_normal_cadence_and_non_stale_resets(
        isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h"))
    first = isolated_db.get_next_runnable()
    isolated_db.mark_completed(
        first.id, "ИТОГ: УСТАРЕЛО (gate-fallback: first)",
        verdict="УСТАРЕЛО")
    worker._recur_after_run(first)

    second = isolated_db.get_next_runnable()
    assert second is not None
    isolated_db.mark_completed(
        second.id, "ИТОГ: УСТАРЕЛО (gate-fallback: second)",
        verdict="УСТАРЕЛО")

    before = datetime.now(timezone.utc)
    worker._recur_after_run(second)
    series = isolated_db.get_series(task.series_id)

    assert datetime.fromisoformat(series["next_run_at"]) >= \
        before + timedelta(hours=3, minutes=59)

    reset = isolated_db.prepare_series_recurrence(task.series_id, "ГОТОВО")
    after_reset = isolated_db.prepare_series_recurrence(
        task.series_id, "УСТАРЕЛО")
    assert reset["stale_reselect_immediate"] is False
    assert after_reset["stale_reselect_immediate"] is True


def test_adaptive_cadence_uses_busy_interval_then_two_empty_runs(
        isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - TRIAGE", recurrence="30m",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2)))

    active = isolated_db.apply_pipeline_series_cadence(
        task.series_id, idle_recurrence="30m", busy_recurrence="15m",
        boost=True, empty_runs_before_idle=2)
    first_empty = isolated_db.prepare_series_recurrence(task.series_id, "ПУСТО")
    second_empty = isolated_db.prepare_series_recurrence(task.series_id, "ПУСТО")

    assert active["effective_recurrence"] == "15m"
    assert first_empty["effective_recurrence"] == "15m"
    assert first_empty["temporary_empty_count"] == 1
    assert second_empty["effective_recurrence"] == "30m"
    assert second_empty["temporary_recurrence"] is None


def test_adaptive_fix_cadence_returns_to_idle_at_threshold(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - FIX", recurrence="30m"))
    matching = isolated_db.get_series(task.series_id)
    queue = {
        "adaptive_cadence": {
            "idle_recurrence": "30m", "busy_recurrence": "15m",
            "backlog_above": 3,
        },
    }

    busy = pipeline_insights._reconcile_adaptive_cadence(queue, matching, 4)
    idle = pipeline_insights._reconcile_adaptive_cadence(queue, matching, 3)

    assert busy["mode"] == "busy"
    assert busy["effective_recurrence"] == "15m"
    assert idle["mode"] == "idle"
    assert idle["effective_recurrence"] == "30m"


def test_busy_observation_resets_temporary_empty_streak(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - TRIAGE", recurrence="30m"))
    isolated_db.apply_pipeline_series_cadence(
        task.series_id, idle_recurrence="30m", busy_recurrence="15m",
        boost=True, empty_runs_before_idle=2)
    empty = isolated_db.prepare_series_recurrence(task.series_id, "ПУСТО")

    busy = isolated_db.apply_pipeline_series_cadence(
        task.series_id, idle_recurrence="30m", busy_recurrence="15m",
        boost=True, empty_runs_before_idle=2)

    assert empty["temporary_empty_count"] == 1
    assert busy["temporary_empty_count"] == 0
    assert busy["effective_recurrence"] == "15m"


def test_event_only_policy_clears_previous_temporary_boost(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - PLAN", recurrence="4h"))
    assert isolated_db.update_series(task.series_id, {
        "temporary_recurrence": "10m", "temporary_empty_limit": 3,
    })

    result = isolated_db.apply_pipeline_series_cadence(
        task.series_id, idle_recurrence="4h", busy_recurrence=None)

    assert result["effective_recurrence"] == "4h"
    assert result["temporary_recurrence"] is None
    assert result["temporary_empty_count"] == 0


def test_unrelated_series_update_preserves_temporary_boost(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - TRIAGE", recurrence="30m"))
    assert isolated_db.update_series(task.series_id, {
        "temporary_recurrence": "15m", "temporary_empty_limit": 2,
    })

    assert isolated_db.update_series(task.series_id, {"priority": 4})

    result = isolated_db.get_series(task.series_id)
    assert result["priority"] == 4
    assert result["temporary_recurrence"] == "15m"
    assert result["temporary_empty_limit"] == 2
    assert result["temporary_empty_count"] == 0


def test_adaptive_cadence_rejects_stale_profile_revision_guard(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - FIX", recurrence="30m"))
    profile_key = "test:published-profile"
    revision_key = "test:published-revision"
    epoch_key = "test:cache-epoch"
    isolated_db.set_setting(profile_key, "profile-a")
    isolated_db.set_setting(revision_key, "7")
    guard = {
        "epoch_key": epoch_key, "epoch": "0", "epoch_default": "0",
        "profile_key": profile_key, "profile_hash": "profile-a",
        "revision_key": revision_key, "revision": "7",
    }
    accepted = isolated_db.apply_pipeline_series_cadence(
        task.series_id, idle_recurrence="30m", busy_recurrence="15m",
        boost=True, publication_guard=guard)
    isolated_db.set_setting(revision_key, "8")

    rejected = isolated_db.apply_pipeline_series_cadence(
        task.series_id, idle_recurrence="30m", busy_recurrence="15m",
        boost=False, publication_guard=guard)

    assert accepted["effective_recurrence"] == "15m"
    assert rejected is None
    assert isolated_db.get_series(task.series_id)["effective_recurrence"] == "15m"


def test_series_completion_and_live_cadence_write_are_serialized(
        isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - TRIAGE", recurrence="30m"))
    isolated_db.apply_pipeline_series_cadence(
        task.series_id, idle_recurrence="30m", busy_recurrence="15m",
        boost=True, empty_runs_before_idle=2)
    real_connect = isolated_db._connect
    prepare_locked = threading.Event()
    release_prepare = threading.Event()
    cadence_done = threading.Event()
    errors = []

    @contextmanager
    def gated_connect(*args, **kwargs):
        with real_connect(*args, **kwargs) as conn:
            if threading.current_thread().name == "prepare-series":
                assert kwargs.get("immediate") is True
                prepare_locked.set()
                if not release_prepare.wait(5):
                    raise TimeoutError("test did not release series transaction")
            yield conn

    monkeypatch.setattr(isolated_db, "_connect", gated_connect)

    def prepare():
        try:
            isolated_db.prepare_series_recurrence(task.series_id, "ПУСТО")
        except BaseException as exc:
            errors.append(exc)

    def observe_busy():
        try:
            isolated_db.apply_pipeline_series_cadence(
                task.series_id, idle_recurrence="30m",
                busy_recurrence="15m", boost=True,
                empty_runs_before_idle=2)
        except BaseException as exc:
            errors.append(exc)
        finally:
            cadence_done.set()

    preparing = threading.Thread(target=prepare, name="prepare-series")
    observing = threading.Thread(target=observe_busy, name="observe-busy")
    preparing.start()
    assert prepare_locked.wait(5)
    observing.start()
    try:
        assert cadence_done.wait(0.1) is False
    finally:
        release_prepare.set()
        preparing.join(5)
        observing.join(5)

    assert not preparing.is_alive()
    assert not observing.is_alive()
    assert errors == []
    assert isolated_db.get_series(task.series_id)["temporary_empty_count"] == 0


def test_window_metrics_report_actual_hourly_rates(isolated_db, monkeypatch):
    now = datetime.now(timezone.utc)
    baseline = {
        "queues": {"review": {
            "backlog": 3,
            "items": [{"key": "pr:1"}, {"key": "pr:2"}, {"key": "pr:3"}],
            "membership_complete": True,
        }},
    }
    current = {
        "queues": {"review": {
            "backlog": 2,
            "items": [{"key": "pr:3"}, {"key": "pr:4"}],
            "membership_complete": True,
        }},
    }
    snapshots = [
        {"captured_at": (now - timedelta(hours=5)).isoformat(),
         "payload": baseline},
        {"captured_at": now.isoformat(), "payload": current},
    ]
    monkeypatch.setattr(
        isolated_db, "pipeline_run_metrics", lambda *_args, **_kwargs: {})

    metrics = pipeline_insights._window_metrics(
        snapshots, current, [], now, 5)

    assert metrics["backlog_delta_per_hour"] == -0.2
    assert metrics["entered_per_hour"] == 0.2
    assert metrics["exited_per_hour"] == 0.4
    assert metrics["queue_throughput_per_hour"]["review"] == 0.4


def test_window_rates_use_actual_elapsed_baseline_gap(isolated_db, monkeypatch):
    now = datetime.now(timezone.utc)
    baseline = {"queues": {"review": {
        "backlog": 3, "items": [{"key": "pr:1"}],
        "membership_complete": True,
    }}}
    current = {"queues": {"review": {
        "backlog": 2, "items": [], "membership_complete": True,
    }}}
    snapshots = [
        {"captured_at": (now - timedelta(hours=10)).isoformat(),
         "payload": baseline},
        {"captured_at": now.isoformat(), "payload": current},
    ]
    monkeypatch.setattr(
        isolated_db, "pipeline_run_metrics", lambda *_args, **_kwargs: {})

    metrics = pipeline_insights._window_metrics(
        snapshots, current, [], now, 5)

    assert metrics["coverage_hours"] == 5
    assert metrics["backlog_delta_per_hour"] == -0.1
    assert metrics["queue_throughput_per_hour"]["review"] == 0.1


def test_incomplete_membership_does_not_claim_set_based_throughput(
        isolated_db, monkeypatch):
    now = datetime.now(timezone.utc)
    baseline = {"queues": {"review": {
        "backlog": 3, "items": [{"key": "pr:1"}, {"key": "pr:2"}],
        "membership_complete": False,
    }}}
    current = {"queues": {"review": {
        "backlog": 2, "items": [{"key": "pr:2"}],
        "membership_complete": True,
    }}}
    snapshots = [
        {"captured_at": (now - timedelta(hours=5)).isoformat(),
         "payload": baseline},
        {"captured_at": now.isoformat(), "payload": current},
    ]
    monkeypatch.setattr(
        isolated_db, "pipeline_run_metrics", lambda *_args, **_kwargs: {})

    metrics = pipeline_insights._window_metrics(
        snapshots, current, [], now, 5)

    assert metrics["backlog_delta_per_hour"] == -0.2
    assert metrics["entered_per_hour"] is None
    assert metrics["exited_per_hour"] is None
    assert metrics["queue_throughput"]["review"] is None
    assert metrics["queue_throughput_per_hour"]["review"] is None


def test_project_health_check_is_immediate_and_accepts_red_json(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=1,
            stdout='{"state":"red","summary":"broken route","findings":[]}',
            stderr="",
        )

    monkeypatch.setattr(pipeline_insights.subprocess, "run", fake_run)
    profile = {"health_check": {"command": ["project-health", "-json"]}}

    diagnostics = pipeline_insights._run_profile_health_check(profile)
    health = pipeline_insights._health(
        10, {"5h": {"complete": False}}, 0, diagnostics=diagnostics)

    assert diagnostics["state"] == "red"
    assert diagnostics["exit_code"] == 1
    assert calls[0]["timeout"] == 180
    assert health["state"] == "red"
    assert health["label"] == "нарушен инвариант"


def test_profile_health_exposes_configured_gh_to_nested_checker(monkeypatch):
    calls = []
    gh = os.path.join("tools", "github", "gh.exe")
    monkeypatch.setenv("PP_GH_EXE", gh)
    monkeypatch.setenv("PATH", "existing")
    monkeypatch.setattr(
        pipeline_insights.subprocess, "run",
        lambda *args, **kwargs: calls.append(kwargs) or SimpleNamespace(
            returncode=0, stdout='{"state":"green","findings":[]}', stderr=""),
    )

    pipeline_insights._run_profile_health_check({
        "health_check": {"command": ["project-health", "-json"]},
    })

    assert calls[0]["env"]["GH_EXE"] == gh
    assert calls[0]["env"]["PATH"].split(os.pathsep)[0] == os.path.dirname(gh)


def test_health_check_failure_is_not_reported_as_broken_invariant(monkeypatch):
    monkeypatch.setattr(
        pipeline_insights.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            pipeline_insights.subprocess.TimeoutExpired(args[0], 180)),
    )

    diagnostics = pipeline_insights._run_profile_health_check({
        "health_check": {"command": ["project-health", "-json"]},
    })
    health = pipeline_insights._health(
        10, {"5h": {"complete": False}}, 0, diagnostics=diagnostics)

    assert diagnostics["checker_failed"] is True
    assert health["state"] == "red"
    assert health["label"] == "диагностика не выполнена"


def test_github_rate_limits_are_normalized(monkeypatch, no_live_github_rate_limit):
    calls = []

    def fake_api(args, input_value=None):
        calls.append((args, input_value))
        if args == ["rate_limit"]:
            return {
                "resources": {
                    "core": {"limit": 5000, "used": 125,
                             "remaining": 4875, "reset": 1},
                    "search": {"limit": 30, "used": 2,
                               "remaining": 28, "reset": 2},
                    # This endpoint can report a different GraphQL bucket.
                    "graphql": {"limit": 5000, "used": 0,
                                "remaining": 5000, "reset": 3},
                },
            }
        assert args == ["graphql"]
        return {
            "data": {
                "viewer": {"login": "owner"},
                "rateLimit": {
                    "limit": 5000, "used": 679, "remaining": 4321,
                    "resetAt": "1970-01-01T00:00:04Z",
                },
            },
        }

    monkeypatch.setattr(pipeline_insights, "_gh_api_json", fake_api)
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {})

    limits = no_live_github_rate_limit()

    assert limits["core"]["remaining"] == 4875
    assert limits["core"]["reset_at"] == "1970-01-01T00:00:01+00:00"
    assert limits["search"]["used"] == 2
    assert limits["graphql"]["limit"] == 5000
    assert limits["graphql"]["remaining"] == 4321
    assert limits["graphql"]["reset"] == 4
    assert calls[1][0] == ["graphql"]
    assert "viewer" in calls[1][1]["query"]
    assert "rateLimit" in calls[1][1]["query"]


def test_project_health_attention_overrides_warming_history():
    diagnostics = {"state": "yellow", "summary": "нужен человек", "findings": []}

    health = pipeline_insights._health(
        10, {"5h": {"complete": False}}, 0, diagnostics=diagnostics)

    assert health["state"] == "yellow"
    assert health["label"] == "есть ожидания"
    assert health["reason"] == "нужен человек"


def test_semantic_failure_is_not_hidden_by_yellow_project_diagnostics():
    diagnostics = {"state": "yellow", "summary": "есть ожидающие решения", "findings": []}
    health = pipeline_insights._health(
        10,
        {"5h": {"complete": True, "runs": {"failed": 0, "unable": 2}}},
        0,
        diagnostics=diagnostics,
    )

    assert health["state"] == "red"
    assert health["label"] == "прогон не отработал"
    assert "НЕ СМОГ: 2" in health["reason"]


def test_paused_series_is_visible_without_history():
    health = pipeline_insights._health(
        10, {"5h": {"complete": False}}, 0, paused_series=2)

    assert health["state"] == "yellow"
    assert health["label"] == "конвейер на паузе"


def test_global_worker_pause_is_visible_for_active_pipeline(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda _repo, _query: {
        "count": 1, "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: {
        "state": "yellow", "summary": "есть ожидающие решения", "findings": [],
    })
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
    ))
    isolated_db.touch_worker_heartbeat(1234)
    isolated_db.set_setting("worker_paused", "0")
    pipeline_insights._cache.clear()

    fresh = pipeline_insights.analyze(
        "example", isolated_db.list_series(), use_cache=False)
    isolated_db.set_setting("worker_paused", "1")
    result = pipeline_insights.read_cached(
        "example", isolated_db.list_series())

    assert fresh["runtime"]["paused"] is False
    assert result["runtime"]["required"] is True
    assert result["runtime"]["paused"] is True
    assert result["runtime"]["state"] == "online"
    assert result["queues"][0]["task_id"] == task.id
    assert result["health"] == {
        "state": "yellow",
        "label": "конвейер на паузе",
        "reason": "включена общая пауза: активные серии не запускаются",
    }


@pytest.mark.parametrize(
    ("windows", "broken_series", "diagnostics", "runtime", "expected_label"),
    [
        ({"5h": {"complete": True}}, 0, None,
         {"required": True, "paused": True, "state": "offline", "stalled": []},
         "worker не работает"),
        ({"5h": {"complete": True}}, 0, None,
         {"required": True, "paused": True, "state": "online",
          "stalled": [{"task_id": 77}]},
         "зависший запуск"),
        ({"5h": {"complete": True}}, 1, None,
         {"required": True, "paused": True, "state": "online", "stalled": []},
         "требует внимания"),
        ({"5h": {"complete": True}}, 0,
         {"state": "red", "summary": "нарушен порядок"},
         {"required": True, "paused": True, "state": "online", "stalled": []},
         "нарушен инвариант"),
        ({"5h": {"complete": True, "runs": {"unresolved_unable": 1}}}, 0, None,
         {"required": True, "paused": True, "state": "online", "stalled": []},
         "прогон не отработал"),
    ],
)
def test_global_worker_pause_does_not_hide_hard_failures(
        windows, broken_series, diagnostics, runtime, expected_label):
    health = pipeline_insights._health(
        10, windows, broken_series, diagnostics=diagnostics, runtime=runtime)

    assert health["state"] == "red"
    assert health["label"] == expected_label


def test_missing_worker_heartbeat_is_red_for_active_pipeline():
    health = pipeline_insights._health(
        10, {"5h": {"complete": True}}, 0,
        runtime={"required": True, "state": "offline", "age_seconds": 47})

    assert health["state"] == "red"
    assert health["label"] == "worker не работает"
    assert "47 сек" in health["reason"]


def test_worker_heartbeat_reports_online_stale_and_graceful_stop(isolated_db):
    now = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

    isolated_db.touch_worker_heartbeat(1234, now)
    online = isolated_db.worker_runtime_status(now + timedelta(seconds=20), 30)
    stale = isolated_db.worker_runtime_status(now + timedelta(seconds=31), 30)
    isolated_db.mark_worker_stopped(1234, now + timedelta(seconds=32))
    stopped = isolated_db.worker_runtime_status(now + timedelta(seconds=33), 30)

    assert online["state"] == "online"
    assert online["pid"] == 1234
    assert stale["state"] == "offline"
    assert stopped["state"] == "offline"


def test_eta_includes_execution_duration_and_exposes_capacity_limit():
    result = pipeline_insights._recommendation(
        {"id": "fix"}, backlog=26, capacity=1, current_interval="15m",
        target_hours=8, avg_duration_seconds=1800)

    assert result["cycle_hours"] == 0.75
    assert result["eta_hours"] == 19.5
    assert result["throughput_per_hour"] == 1.33
    assert result["recommended_interval"] == "15m"
    assert "увеличьте ёмкость" in result["recommendation"]


def test_running_past_timeout_is_reported_as_stalled(isolated_db):
    now = datetime.now(timezone.utc)
    isolated_db.touch_worker_heartbeat(1234, now)
    runtime = pipeline_insights._pipeline_runtime([{
        "id": 9, "title": "Project - FIX", "ended": False, "paused": False,
        "next_status": "running", "next_started_at": (now - timedelta(hours=2)).isoformat(),
        "next_task_id": 77, "task_timeout": 3600,
    }], now)

    assert runtime["state"] == "online"
    assert runtime["required"] is True
    assert runtime["stalled"][0]["task_id"] == 77


def test_recurring_task_creates_durable_series(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - FIX\nDo one fix", working_dir=r"D:\Projects\example",
        provider="codex", effort="high", recurrence="4h",
    ))

    assert task.series_id is not None
    series = isolated_db.get_series(task.series_id)
    assert series["title"] == "ExampleProject - FIX"
    assert series["recurrence"] == "4h"
    assert series["next_task_id"] == task.id


def test_list_series_preserves_occurrence_and_health_semantics(isolated_db):
    active = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\nReview the next pull request",
        working_dir=r"D:\Projects\example", provider="codex", model="gpt-test",
        effort="high", priority=2, task_timeout=3600, recurrence="2h",
        machine="active-machine",
    ))
    completed = isolated_db.create_task(TaskCreate(
        prompt="completed", recurrence="2h", series_id=active.series_id,
        machine="completed-machine",
    ))
    failed = isolated_db.create_task(TaskCreate(
        prompt="failed", recurrence="2h", series_id=active.series_id,
        machine="failed-machine",
    ))
    cancelled = isolated_db.create_task(TaskCreate(
        prompt="cancelled", recurrence="2h", series_id=active.series_id,
        machine="cancelled-machine",
    ))
    scheduled_at = "2026-09-16T15:00:00+00:00"
    active_started = "2026-09-16T14:59:00+00:00"
    cancelled_at = "2026-09-16T14:58:00+00:00"
    with isolated_db._connect() as conn:
        conn.execute(
            """UPDATE tasks
               SET status = 'pending', scheduled_at = ?, started_at = ?, error = ?
               WHERE id = ?""",
            (scheduled_at, active_started, "waiting for budget", active.id),
        )
        conn.execute(
            """UPDATE tasks
               SET status = 'completed', started_at = ?, completed_at = ?, verdict = ?
               WHERE id = ?""",
            ("2026-09-16T14:00:00+00:00", "2026-09-16T14:00:10+00:00",
             "пусто", completed.id),
        )
        conn.execute(
            """UPDATE tasks
               SET status = 'failed', started_at = ?, completed_at = ?, verdict = ?
               WHERE id = ?""",
            ("2026-09-16T14:10:00+00:00", "2026-09-16T14:10:20+00:00",
             "failed", failed.id),
        )
        conn.execute(
            """UPDATE tasks
               SET status = 'cancelled', completed_at = ?, verdict = ?
               WHERE id = ?""",
            (cancelled_at, "cancelled", cancelled.id),
        )

    series = isolated_db.get_series(active.series_id)

    assert series["runs"] == 4
    assert series["machine"] == "active-machine"
    assert series["next_task_id"] == active.id
    assert series["next_status"] == "pending"
    assert series["next_run_at"] == scheduled_at
    assert series["next_error"] == "waiting for budget"
    assert series["next_started_at"] == active_started
    assert series["last_task_id"] == cancelled.id
    assert series["last_status"] == "cancelled"
    assert series["last_at"] == cancelled_at
    assert series["last_verdict"] == "cancelled"
    assert series["failure_rate"] == 0.5
    assert series["empty_rate"] == 0.5
    assert series["avg_duration_seconds"] == 15
    assert series["broken"] is False


def test_list_series_uses_constant_lightweight_queries(
        isolated_db, monkeypatch):
    large_result = "x" * (1024 * 1024)
    expected_ids = []
    for index in range(6):
        historical = isolated_db.create_task(TaskCreate(
            prompt=f"Series {index}", recurrence="1h"))
        isolated_db.mark_completed(
            historical.id, large_result if index == 0 else "done")
        active = isolated_db.create_task(TaskCreate(
            prompt=f"Series {index}", recurrence="1h",
            series_id=historical.series_id,
        ))
        expected_ids.append(active.series_id)

    statements = []
    prohibited_reads = []
    real_connect = isolated_db._connect

    @contextmanager
    def traced_connect(*args, **kwargs):
        with real_connect(*args, **kwargs) as conn:
            def authorize(action, table, column, _database, _trigger):
                if (action == sqlite3.SQLITE_READ and table == "tasks"
                        and column in {"prompt", "result", "note"}):
                    prohibited_reads.append((table, column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(isolated_db, "_connect", traced_connect)

    listed = isolated_db.list_series()

    selects = [statement for statement in statements
               if statement.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert {item["id"] for item in listed} == set(expected_ids)
    assert len(selects) == 3
    assert prohibited_reads == []
    assert all("SELECT *" not in statement.upper() for statement in selects)


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


def test_series_provider_switch_clears_pending_provider_runtime(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="Review", recurrence="4h", provider="codex",
        model="gpt-old", effort="max",
    ))
    claimed = isolated_db.get_next_runnable()
    assert isolated_db.set_session_id(claimed.id, "old-provider-session") is True
    isolated_db.mark_rate_limited(
        claimed.id, datetime.now(timezone.utc) + timedelta(hours=1),
        "old provider exhausted",
    )

    assert isolated_db.update_series(task.series_id, {
        "provider": "agy", "model": None, "effort": None,
    })

    occurrence = isolated_db.get_task(task.id)
    assert occurrence.status.value == "pending"
    assert occurrence.provider == "agy"
    assert occurrence.model is None
    assert occurrence.effort is None
    assert occurrence.session_id is None
    assert occurrence.retry_count == 0
    assert occurrence.error is None
    assert occurrence.next_run_at is None


def test_shorter_series_interval_reschedules_existing_future_occurrence(isolated_db):
    original = datetime.now(timezone.utc) + timedelta(hours=4)
    task = isolated_db.create_task(TaskCreate(
        prompt="Review", recurrence="4h", scheduled_at=original,
    ))
    before = datetime.now(timezone.utc)

    assert isolated_db.update_series(task.series_id, {
        "temporary_recurrence": "30m", "temporary_empty_limit": 2,
    })

    updated = isolated_db.get_task(task.id)
    assert before + timedelta(minutes=29) <= updated.scheduled_at
    assert updated.scheduled_at <= before + timedelta(minutes=31)
    assert updated.scheduled_at < original


def test_longer_series_interval_never_postpones_existing_occurrence(isolated_db):
    original = datetime.now(timezone.utc) + timedelta(minutes=5)
    task = isolated_db.create_task(TaskCreate(
        prompt="Review", recurrence="15m", scheduled_at=original,
    ))

    assert isolated_db.update_series(task.series_id, {"base_recurrence": "1h"})

    updated = isolated_db.get_task(task.id)
    assert updated.scheduled_at == original


def test_dependency_defer_returns_claimed_task_without_retry(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Merge", recurrence="2h"))
    claimed = isolated_db.get_next_runnable()
    next_run = datetime.now(timezone.utc) + timedelta(minutes=10)

    isolated_db.defer_task(claimed.id, next_run, "waiting for review")

    deferred = isolated_db.get_task(task.id)
    assert deferred.status.value == "pending"
    assert deferred.started_at is None
    assert deferred.retry_count == 0
    assert deferred.scheduled_at == next_run
    assert deferred.error == "waiting for review"
    assert deferred.next_run_at is None


def test_hard_defer_is_not_shortened_by_successor_wake(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h"))
    claimed = isolated_db.get_next_runnable()
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)

    isolated_db.defer_task(
        claimed.id, deadline, "GitHub budget", hard_not_before=True)
    requested = isolated_db.request_pipeline_series_wake(task.series_id)

    deferred = isolated_db.get_task(task.id)
    assert requested == {"accepted": True, "state": "latched_deferred"}
    assert deferred.scheduled_at == deadline
    assert deferred.next_run_at == deadline
    assert isolated_db.consume_pipeline_series_wake(task.series_id) is False
    assert isolated_db.get_setting(
        f"pipeline_series_wake_intent:v1:{task.series_id}") == "1"


def test_manual_run_now_explicitly_bypasses_hard_defer(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h"))
    claimed = isolated_db.get_next_runnable()
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    isolated_db.defer_task(
        claimed.id, deadline, "GitHub budget", hard_not_before=True)

    assert isolated_db.series_action(task.series_id, "run_now")

    current = isolated_db.get_task(task.id)
    assert current.scheduled_at <= datetime.now(timezone.utc)
    assert current.next_run_at is None


def test_series_exposes_active_occurrence_defer_reason(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Review", recurrence="2h"))
    claimed = isolated_db.get_next_runnable()
    deferred_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    reason = "GitHub API-бюджет ниже безопасного остатка"

    isolated_db.defer_task(claimed.id, deferred_until, reason)

    series = isolated_db.get_series(task.series_id)
    assert series["next_task_id"] == task.id
    assert series["next_run_at"] == deferred_until.isoformat()
    assert series["next_error"] == reason


def test_reclaim_after_dependency_defer_clears_stale_error(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Merge"))
    claimed = isolated_db.get_next_runnable()
    deferred_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    isolated_db.defer_task(claimed.id, deferred_until, "waiting for review")

    reclaimed = isolated_db.get_next_runnable()
    persisted = isolated_db.get_task(task.id)

    assert reclaimed.status.value == "running"
    assert reclaimed.error is None
    assert reclaimed.next_run_at is None
    assert persisted.status.value == "running"
    assert persisted.error is None
    assert persisted.next_run_at is None
    assert persisted.scheduled_at == deferred_until


def test_successful_retry_clears_stale_error_and_backoff(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Fix"))
    claimed = isolated_db.get_next_runnable()
    isolated_db.mark_rate_limited(
        claimed.id, datetime.now(timezone.utc) - timedelta(minutes=1), "old failure",
    )
    retried = isolated_db.get_next_runnable()

    assert retried.status.value == "running"
    assert retried.error is None
    assert retried.next_run_at is None
    running = isolated_db.get_task(task.id)
    assert running.error is None
    assert running.next_run_at is None

    isolated_db.mark_completed(retried.id, "done")

    completed = isolated_db.get_task(task.id)
    assert completed.status.value == "completed"
    assert completed.error is None
    assert completed.next_run_at is None


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

    listed = next(item for item in isolated_db.list_tasks() if item.id == task.id)
    assert listed.series_title == "Triage"
    assert listed.series_paused is True

    assert isolated_db.series_action(task.series_id, "resume")
    assert isolated_db.get_next_runnable().id == task.id


def test_run_now_recreates_missing_occurrence_after_cancel(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="Review", working_dir="repo", recurrence="4h",
        provider="codex", priority=2, max_retries=3, effort="high",
        task_timeout=5400, skip_permissions=True, keep_pane=False,
        machine="builder", worktree=True,
    ))
    assert isolated_db.cancel_task(task.id)

    assert isolated_db.series_action(task.series_id, "run_now")

    series = isolated_db.get_series(task.series_id)
    recreated = isolated_db.get_task(series["next_task_id"])
    assert recreated.id != task.id
    assert recreated.status.value == "pending"
    assert recreated.series_id == task.series_id
    assert recreated.scheduled_at <= datetime.now(timezone.utc)
    assert recreated.prompt == "Review"
    assert recreated.working_dir == "repo"
    assert recreated.recurrence == "4h"
    assert recreated.provider == "codex"
    assert recreated.priority == 2
    assert recreated.max_retries == 3
    assert recreated.effort == "high"
    assert recreated.task_timeout == 5400
    assert recreated.skip_permissions is True
    assert recreated.keep_pane is False
    assert recreated.machine == "builder"
    assert recreated.worktree is True


def test_run_now_does_not_duplicate_running_occurrence(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Review", recurrence="4h"))
    assert isolated_db.get_next_runnable().id == task.id

    assert isolated_db.series_action(task.series_id, "run_now") is False
    assert len([item for item in isolated_db.list_tasks() if item.series_id == task.series_id]) == 1


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
    counts = iter([15, 5, 0, 17, 9])
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda repo, query: {
        "count": next(counts), "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(pipeline_insights, "_profiles",
                        lambda: {"example": PIPELINE_PROFILE})
    pipeline_insights._cache.clear()

    active_series = [
        {
            "id": index, "title": f"ExampleProject - {queue['series_contains']}",
            "ended": False, "paused": False, "broken": False,
            "next_task_id": index + 100, "next_status": "pending",
        }
        for index, queue in enumerate(PIPELINE_PROFILE["queues"], start=1)
    ]
    result = pipeline_insights.analyze(
        "example", active_series, use_cache=False)

    assert result["bottleneck"] == "review"
    review = next(q for q in result["queues"] if q["id"] == "review")
    triage = next(q for q in result["queues"] if q["id"] == "triage")
    assert review["runs_needed"] == 8.5
    assert review["recommended_interval"] == "1h"
    assert "оставить 1h" not in review["recommendation"]  # no matching series in this unit test
    assert "решения человека" in triage["recommendation"]


def test_forced_queue_refresh_reuses_expensive_diagnostics(isolated_db, monkeypatch):
    profile = {
        **PIPELINE_PROFILE,
        "health_check": {"command": ["health"], "cache_seconds": 1800},
    }
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda _repo, _query: {
        "count": 0, "items": [], "membership_complete": True,
    })
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", lambda: None)
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check",
        lambda _profile: calls.append("health") or {"state": "green", "findings": []},
    )
    pipeline_insights._cache.clear()

    first = pipeline_insights.analyze("example", [], use_cache=False)
    second = pipeline_insights.analyze("example", [], use_cache=False)
    refreshed = pipeline_insights.analyze(
        "example", [], use_cache=False, refresh_diagnostics=True)

    assert first["diagnostics"] == second["diagnostics"]
    assert len(calls) == 2
    assert refreshed["diagnostics_generated_at"] >= second["diagnostics_generated_at"]


def test_cache_only_miss_never_calls_github_or_health(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "health_check": {"command": ["health"]},
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
            "execution": {
                "mode": "auto", "command": ["pipeline-tool"],
                "probe_command": ["pipeline-tool", "capabilities"],
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"cold": profile})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cache-only read attempted external GitHub work")

    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", forbidden)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", forbidden)
    monkeypatch.setattr(pipeline_insights.subprocess, "run", forbidden)
    pipeline_insights._cache.clear()

    result = pipeline_insights.read_cached("cold", [])

    assert result["cache"] == {
        "source": "none", "available": False, "complete": False,
        "generated_at": None, "age_seconds": None, "ttl_seconds": 300,
        "invalidated": False, "stale": True,
        "profile_hash": pipeline_insights._profile_fingerprint(profile),
        "token": None,
    }
    assert result["backlog_total"] is None
    assert isolated_db.list_pipeline_snapshots("cold") == []


def test_full_cache_survives_process_restart_without_github(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"restart": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: (
        calls.append("search") or {
            "count": 2, "items": [], "membership_complete": False,
        }))
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()

    fresh = pipeline_insights.analyze("restart", [], use_cache=False)
    pipeline_insights._cache.clear()  # simulate a new server process
    restored = pipeline_insights.read_cached("restart", [])

    assert calls == ["search"]
    assert restored["cache"]["source"] == "durable"
    assert restored["cache"]["available"] is True
    assert restored["cache"]["stale"] is False
    assert restored["backlog_total"] == 2
    assert restored["generated_at"] == fresh["generated_at"]


def test_cache_only_reader_does_not_wait_for_inflight_refresh(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    entered = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    def search(*_args):
        calls["count"] += 1
        if calls["count"] == 2:
            entered.set()
            assert release.wait(5)
        return {
            "count": calls["count"], "items": [],
            "membership_complete": False,
        }

    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"concurrent": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", search)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()
    pipeline_insights.analyze("concurrent", [], use_cache=False)
    writer = threading.Thread(
        target=pipeline_insights.analyze,
        args=("concurrent", []), kwargs={"use_cache": False},
    )
    writer.start()
    assert entered.wait(5)
    reader_result = []
    reader = threading.Thread(
        target=lambda: reader_result.append(
            pipeline_insights.read_cached("concurrent", [])))
    reader.start()

    try:
        reader.join(1)
        assert not reader.is_alive()
        assert reader_result[0]["backlog_total"] == 1
    finally:
        release.set()
        writer.join(5)
        reader.join(5)


def test_older_cross_process_refresh_cannot_overwrite_newer_cache(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"ordered": profile})
    pipeline_insights._cache.clear()
    _cached, generation = pipeline_insights._cache_snapshot("ordered")
    epoch = pipeline_insights._cache_epoch()
    older = pipeline_insights._empty_cached_result("ordered", profile)
    older.update({"generated_at": 100.0, "backlog_total": 1})
    older["queues"][0]["backlog"] = 1
    newer = pipeline_insights._empty_cached_result("ordered", profile)
    newer.update({"generated_at": 200.0, "backlog_total": 2})
    newer["queues"][0]["backlog"] = 2

    assert pipeline_insights._publish_cache(
        "ordered", profile, generation, epoch, 2, newer) is True
    assert pipeline_insights._publish_cache(
        "ordered", profile, generation, epoch, 1, older) is False
    pipeline_insights._cache.clear()

    restored = pipeline_insights.read_cached("ordered", [])
    assert restored["backlog_total"] == 2


def test_older_different_profile_hash_cannot_reclaim_active_publication(
        isolated_db):
    old_profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "label:old", "series_contains": "REVIEW",
        }],
    }
    new_profile = {
        **old_profile,
        "queues": [{**old_profile["queues"][0], "query": "label:new"}],
    }
    pipeline_insights._cache.clear()
    _cached, generation = pipeline_insights._cache_snapshot("profile-race")
    epoch = pipeline_insights._cache_epoch()
    newer = pipeline_insights._empty_cached_result(
        "profile-race", new_profile)
    newer["generated_at"] = 200.0
    older = pipeline_insights._empty_cached_result(
        "profile-race", old_profile)
    older["generated_at"] = 100.0

    assert pipeline_insights._publish_cache(
        "profile-race", new_profile, generation, epoch, 2, newer) is True
    assert pipeline_insights._publish_cache(
        "profile-race", old_profile, generation, epoch, 1, older) is False
    assert isolated_db.get_setting(
        pipeline_insights._published_profile_key("profile-race")
    ) == pipeline_insights._profile_fingerprint(new_profile)
    assert isolated_db.get_setting(
        pipeline_insights._published_profile_revision_key("profile-race")
    ) == "2"


def test_stale_profile_analysis_uses_separate_durable_namespace(
        isolated_db, monkeypatch):
    old_profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "label:old", "series_contains": "REVIEW",
        }],
    }
    new_profile = {
        **old_profile,
        "queues": [{**old_profile["queues"][0], "query": "label:new"}],
    }
    current = {"profile": new_profile}
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"fingerprint": current["profile"]})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda _repo, query: {
        "count": 2 if query == "label:new" else 9,
        "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()
    newer = pipeline_insights.analyze("fingerprint", [], use_cache=False)
    new_hash = pipeline_insights._profile_fingerprint(new_profile)
    old_hash = pipeline_insights._profile_fingerprint(old_profile)
    new_key = pipeline_insights._cache_key("fingerprint", new_hash)
    old_key = pipeline_insights._cache_key("fingerprint", old_hash)
    durable_before = isolated_db.get_setting(
        new_key)

    current["profile"] = old_profile  # stale process still sees its old config
    stale = pipeline_insights.analyze("fingerprint", [], use_cache=False)
    stale_payload = json.loads(isolated_db.get_setting(old_key))

    current["profile"] = new_profile
    pipeline_insights._cache.clear()
    restored = pipeline_insights.read_cached("fingerprint", [])

    assert newer["backlog_total"] == 2
    assert stale["backlog_total"] == 9
    assert stale_payload["revision"] > json.loads(durable_before)["revision"]
    assert isolated_db.get_setting(new_key) == durable_before
    assert restored["backlog_total"] == 2
    assert restored["cache"]["source"] == "durable"
    assert json.loads(durable_before)["profile_hash"] == \
        new_hash


def test_process_memory_reloads_newer_cross_process_durable_revision(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"coherent": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: {
        "count": 1, "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()
    pipeline_insights.analyze("coherent", [], use_cache=False)
    old_memory = pipeline_insights._cache["coherent"]
    _cached, generation = pipeline_insights._cache_snapshot("coherent")
    epoch = pipeline_insights._cache_epoch()
    newer = pipeline_insights._empty_cached_result("coherent", profile)
    newer.update({"generated_at": 200.0, "backlog_total": 2})
    newer["queues"][0]["backlog"] = 2
    assert pipeline_insights._publish_cache(
        "coherent", profile, generation, epoch, 2, newer) is True
    pipeline_insights._cache["coherent"] = old_memory  # another process' RAM

    restored = pipeline_insights.read_cached("coherent", [])

    assert restored["cache"]["source"] == "durable"
    assert restored["backlog_total"] == 2


def test_profile_query_change_never_reuses_incompatible_full_or_raw_cache(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "label:old", "series_contains": "REVIEW",
        }],
    }
    current = {"profile": profile}
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"changed": current["profile"]})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda _repo, query: {
        "count": 9 if query == "label:new" else 7,
        "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()
    assert pipeline_insights.analyze(
        "changed", [], use_cache=False)["backlog_total"] == 7
    current["profile"] = {
        **profile,
        "queues": [{**profile["queues"][0], "query": "label:new"}],
    }
    pipeline_insights._cache.clear()

    result = pipeline_insights.read_cached("changed", [])

    assert result["cache"]["source"] == "none"
    assert result["backlog_total"] is None

    refreshed = pipeline_insights.analyze("changed", [], use_cache=False)
    pipeline_insights._cache.clear()
    restored = pipeline_insights.read_cached("changed", [])

    assert refreshed["backlog_total"] == 9
    assert restored["backlog_total"] == 9
    assert restored["cache"]["source"] == "durable"


def test_failed_refresh_preserves_previous_last_good_cache(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    fail = {"value": False}

    def search(*_args):
        if fail["value"]:
            raise RuntimeError("GitHub unavailable")
        return {"count": 4, "items": [], "membership_complete": False}

    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"last-good": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", search)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()
    fresh = pipeline_insights.analyze("last-good", [], use_cache=False)
    fail["value"] = True

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        pipeline_insights.analyze("last-good", [], use_cache=False)
    restored = pipeline_insights.read_cached("last-good", [])

    assert restored["generated_at"] == fresh["generated_at"]
    assert restored["backlog_total"] == 4


def test_rejected_refresh_returns_the_accepted_durable_state(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    counts = iter([2, 9])
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"accepted": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: {
        "count": next(counts), "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()
    accepted = pipeline_insights.analyze("accepted", [], use_cache=False)
    snapshots_before = isolated_db.list_pipeline_snapshots("accepted")
    monkeypatch.setattr(pipeline_insights, "_publish_cache", lambda *_args: False)

    rejected = pipeline_insights.analyze("accepted", [], use_cache=False)

    assert accepted["backlog_total"] == 2
    assert rejected["backlog_total"] == 2
    assert rejected["generated_at"] == accepted["generated_at"]
    assert rejected["cache"]["complete"] is True
    assert rejected["cache"]["stale"] is False
    assert isolated_db.list_pipeline_snapshots("accepted") == snapshots_before


def test_cache_only_read_uses_legacy_durable_snapshot_without_github(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 2,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    captured = datetime.now(timezone.utc) - timedelta(minutes=2)
    isolated_db.add_pipeline_snapshot("legacy", "owner/example", {
        "captured_at": captured.isoformat(),
        "queues": {"review": {
            "backlog": 3, "items": [], "membership_complete": True,
        }},
    }, captured)
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"legacy": profile})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy fallback attempted external work")

    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", forbidden)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", forbidden)
    pipeline_insights._cache.clear()

    result = pipeline_insights.read_cached("legacy", [{
        "id": 1, "title": "Example - REVIEW", "ended": False,
        "paused": False, "broken": False,
        "next_task_id": 101, "next_status": "pending",
    }])

    assert result["cache"]["source"] == "snapshot"
    assert result["cache"]["complete"] is False
    assert result["cache"]["stale"] is False
    assert result["backlog_total"] == 3
    assert result["queues"][0]["runs_needed"] == 1.5


def test_latest_snapshot_finds_old_exact_hash_behind_many_mismatches(
        isolated_db, monkeypatch):
    wanted_hash = "a" * 64
    exact_payload = json.dumps({
        "profile_hash": wanted_hash,
        "queues": {"review": {"backlog": 1}},
    }, separators=(",", ":"))
    mismatch_payload = json.dumps({
        "profile_hash": "b" * 64,
        "queues": {"review": {"backlog": 999}},
    }, separators=(",", ":"))
    with isolated_db._connect() as conn:
        target = conn.execute(
            """INSERT INTO pipeline_snapshots
               (profile_id, repository, captured_at, payload_json)
               VALUES (?, ?, ?, ?)""",
            ("deep-hash", "owner/example",
             "2026-01-01T00:00:00+00:00", exact_payload),
        ).lastrowid
        conn.executemany(
            """INSERT INTO pipeline_snapshots
               (profile_id, repository, captured_at, payload_json)
               VALUES (?, ?, ?, ?)""",
            [("deep-hash", "owner/example",
              "2026-01-02T00:00:00+00:00", mismatch_payload)] * 2500,
        )

    real_loads = isolated_db.json.loads
    decoded = []

    def counting_loads(value):
        decoded.append(value)
        return real_loads(value)

    monkeypatch.setattr(isolated_db.json, "loads", counting_loads)
    snapshot = isolated_db.latest_pipeline_snapshot(
        "deep-hash", profile_hash=wanted_hash, allow_legacy=False)

    assert snapshot["id"] == target
    assert snapshot["payload"]["profile_hash"] == wanted_hash
    assert len(decoded) == 1


def test_latest_snapshot_uses_separately_prefiltered_legacy_row(isolated_db):
    legacy = isolated_db.add_pipeline_snapshot(
        "legacy-lookup", "owner/example", {"queues": {"review": {
            "backlog": 3,
        }}}, datetime(2026, 1, 1, tzinfo=timezone.utc))
    isolated_db.add_pipeline_snapshot(
        "legacy-lookup", "owner/example", {
            "profile_hash": "newer-other-revision", "queues": {},
        }, datetime(2026, 1, 2, tzinfo=timezone.utc))

    snapshot = isolated_db.latest_pipeline_snapshot(
        "legacy-lookup", profile_hash="missing-revision", allow_legacy=True)

    assert snapshot["id"] == legacy["id"]
    assert "profile_hash" not in snapshot["payload"]


def test_latest_snapshot_skips_corrupt_exact_marker_candidate(isolated_db):
    wanted_hash = "exact-revision"
    valid = isolated_db.add_pipeline_snapshot(
        "corrupt-candidate", "owner/example", {
            "profile_hash": wanted_hash, "queues": {"review": {"backlog": 2}},
        }, datetime(2026, 1, 1, tzinfo=timezone.utc))
    marker = '"profile_hash":' + json.dumps(wanted_hash)
    with isolated_db._connect() as conn:
        conn.execute(
            """INSERT INTO pipeline_snapshots
               (profile_id, repository, captured_at, payload_json)
               VALUES (?, ?, ?, ?)""",
            ("corrupt-candidate", "owner/example",
             "2026-01-02T00:00:00+00:00", "{" + marker + ",broken"),
        )

    snapshot = isolated_db.latest_pipeline_snapshot(
        "corrupt-candidate", profile_hash=wanted_hash, allow_legacy=False)

    assert snapshot["id"] == valid["id"]
    assert snapshot["payload"]["queues"]["review"]["backlog"] == 2


def test_old_full_cache_suppresses_ambiguous_legacy_snapshot_fallback(
        isolated_db, monkeypatch):
    profile = {
        "title": "Changed", "repository": "owner/example",
        "queues": [{
            "id": "new", "title": "New", "capacity": 1,
            "query": "label:new", "series_contains": "NEW",
        }],
    }
    captured = datetime.now(timezone.utc) - timedelta(minutes=2)
    isolated_db.set_setting(
        pipeline_insights._legacy_cache_key("legacy-changed"),
        json.dumps({"profile_hash": "old-profile"}),
    )
    isolated_db.add_pipeline_snapshot("legacy-changed", "owner/example", {
        "captured_at": captured.isoformat(),
        "queues": {"new": {
            "backlog": 99, "items": [], "membership_complete": True,
        }},
    }, captured)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"legacy-changed": profile})
    pipeline_insights._cache.clear()

    result = pipeline_insights.read_cached("legacy-changed", [])

    assert result["cache"]["source"] == "none"
    assert result["cache"]["complete"] is False
    assert result["backlog_total"] is None


def test_stale_durable_cache_keeps_current_paused_runtime_without_github(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"stale": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: (
        calls.append("search") or {
            "count": 1, "items": [], "membership_complete": False,
        }))
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h"))
    isolated_db.touch_worker_heartbeat(1234)
    pipeline_insights._cache.clear()
    fresh = pipeline_insights.analyze(
        "stale", isolated_db.list_series(), use_cache=False)
    cache_key = pipeline_insights._cache_key(
        "stale", pipeline_insights._profile_fingerprint(profile))
    raw = json.loads(isolated_db.get_setting(cache_key))
    raw["generated_at"] -= 601
    raw["result"]["generated_at"] -= 601
    isolated_db.set_setting(
        cache_key,
        json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
    )
    pipeline_insights._cache.clear()
    isolated_db.set_setting("worker_paused", "1")

    cached = pipeline_insights.read_cached("stale", isolated_db.list_series())

    assert fresh["runtime"]["paused"] is False
    assert cached["runtime"]["paused"] is True
    assert cached["runtime"]["state"] == "online"
    assert cached["health"]["label"] == "конвейер на паузе"
    assert cached["queues"][0]["task_id"] == task.id
    assert cached["cache"]["source"] == "durable"
    assert cached["cache"]["stale"] is True
    assert cached["cache"]["age_seconds"] >= 601
    assert calls == ["search"]


def test_local_interval_overlay_recomputes_bottleneck(isolated_db):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [
            {"id": "a", "title": "A", "capacity": 1,
             "query": "label:a", "series_contains": "STAGE A"},
            {"id": "b", "title": "B", "capacity": 1,
             "query": "label:b", "series_contains": "STAGE B"},
        ],
    }
    isolated_db.create_task(TaskCreate(
        prompt="Example - STAGE A", recurrence="15m"))
    isolated_db.create_task(TaskCreate(
        prompt="Example - STAGE B", recurrence="4h"))
    result = pipeline_insights._empty_cached_result("bottleneck", profile)
    result["queues"][0]["backlog"] = 10
    result["queues"][1]["backlog"] = 2
    result["backlog_total"] = 12
    result["bottleneck"] = "a"  # value saved with now-obsolete intervals
    generated_at = datetime.now(timezone.utc).timestamp()
    result["generated_at"] = generated_at

    overlaid = pipeline_insights._refresh_local_state(
        result, profile, isolated_db.list_series(), source="durable",
        generated_at=generated_at, entry_epoch=0, entry_revision=1,
        current_epoch=0)

    assert overlaid["queues"][0]["eta_hours"] == 2.5
    assert overlaid["queues"][1]["eta_hours"] == 8.0
    assert overlaid["bottleneck"] == "b"


def test_global_pause_blocks_even_explicit_analysis(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "health_check": {"command": ["health"]},
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"paused": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: calls.append("search"))
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", lambda: calls.append("rate"))
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check",
        lambda _profile: calls.append("health"),
    )
    isolated_db.set_setting("worker_paused", "1")
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze(
        "paused", [], use_cache=False, refresh_diagnostics=True)

    assert calls == []
    assert result["cache"]["refresh_blocked"] == "worker_paused"


def test_pause_during_first_search_stops_all_subsequent_github_io(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "queries": ["label:first", "label:second"],
            "series_contains": "REVIEW",
        }],
    }
    calls = []

    def search(_repository, query):
        calls.append(f"search:{query}")
        isolated_db.set_setting("worker_paused", "1")
        return {"count": 1, "items": [], "membership_complete": False}

    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"mid-pause": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", search)
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: calls.append("rate-limit"),
    )
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("mid-pause", [], use_cache=False)

    assert calls == ["search:label:first"]
    assert result["cache"]["source"] == "none"
    assert result["cache"]["refresh_blocked"] == "worker_paused"
    assert isolated_db.list_pipeline_snapshots("mid-pause") == []


def test_github_search_does_not_start_page_two_after_pause(
        isolated_db, monkeypatch):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        isolated_db.set_setting("worker_paused", "1")
        return SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"total_count": 250, "items": [{}] * 100}),
        )

    monkeypatch.setattr(pipeline_insights, "_gh_executable", lambda: "gh")
    monkeypatch.setattr(pipeline_insights.subprocess, "run", run)

    with pytest.raises(
            pipeline_insights._GitHubScanPaused,
            match="interrupted by global pause"):
        pipeline_insights._github_search("owner/example", "is:pr")

    assert len(calls) == 1
    assert "page=1" in calls[0]


def test_interrupted_pagination_never_publishes_partial_snapshot(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        isolated_db.set_setting("worker_paused", "1")
        return SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"total_count": 250, "items": [{}] * 100}),
        )

    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"pagination": profile})
    monkeypatch.setattr(pipeline_insights, "_gh_executable", lambda: "gh")
    monkeypatch.setattr(pipeline_insights.subprocess, "run", run)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("pagination", [], use_cache=False)

    assert len(calls) == 1
    assert result["cache"]["source"] == "none"
    assert result["cache"]["complete"] is False
    assert result["cache"]["refresh_blocked"] == "worker_paused"
    assert isolated_db.get_setting(
        pipeline_insights._cache_key(
            "pagination", pipeline_insights._profile_fingerprint(profile))) is None
    assert isolated_db.list_pipeline_snapshots("pagination") == []


def test_completed_first_page_remains_a_valid_snapshot_when_pause_arrives(
        isolated_db, monkeypatch):
    item = {
        "number": 42, "title": "Ready", "labels": [],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "html_url": "https://example.test/42",
    }
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        isolated_db.set_setting("worker_paused", "1")
        return SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"total_count": 1, "items": [item]}),
        )

    monkeypatch.setattr(pipeline_insights, "_gh_executable", lambda: "gh")
    monkeypatch.setattr(pipeline_insights.subprocess, "run", run)

    result = pipeline_insights._github_search("owner/example", "is:pr")

    assert len(calls) == 1
    assert result["count"] == 1
    assert result["membership_complete"] is True
    assert result["items"][0]["key"] == "issue:42"


def test_invalidation_during_analysis_rejects_stale_full_cache_publish(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
            "adaptive_cadence": {
                "idle_recurrence": "30m", "busy_recurrence": "15m",
                "backlog_above": 0,
            },
        }],
    }
    isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="30m"))
    series = isolated_db.list_series()
    entered_search = threading.Event()
    release_search = threading.Event()
    search_calls = []
    cadence_calls = []
    errors = []

    def fake_search(repository, query):
        search_calls.append((repository, query))
        if len(search_calls) == 1:
            entered_search.set()
            if not release_search.wait(5):
                raise TimeoutError("test did not release the in-flight analysis")
        return {"count": 1, "items": [], "membership_complete": False}

    def run_analysis():
        try:
            pipeline_insights.analyze("cache-race", series, use_cache=False)
        except BaseException as exc:  # preserve the worker-thread failure for the assertion
            errors.append(exc)

    real_reconcile = pipeline_insights._reconcile_adaptive_cadence

    def track_reconcile(*args, **kwargs):
        cadence_calls.append((args, kwargs))
        return real_reconcile(*args, **kwargs)

    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"cache-race": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", fake_search)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    monkeypatch.setattr(
        pipeline_insights, "_reconcile_adaptive_cadence", track_reconcile)
    pipeline_insights._discard_cache()
    analysis = threading.Thread(target=run_analysis)
    analysis.start()

    try:
        assert entered_search.wait(5)
        pipeline_insights._discard_cache()
        release_search.set()
        analysis.join(5)
        assert not analysis.is_alive()
        assert errors == []
        assert isolated_db.get_setting(
            pipeline_insights._cache_key(
                "cache-race", pipeline_insights._profile_fingerprint(profile))) is None
        assert cadence_calls == []

        pipeline_insights.analyze("cache-race", series, use_cache=False)
        assert len(search_calls) == 2
        assert len(cadence_calls) == 1
    finally:
        release_search.set()
        analysis.join(5)
        pipeline_insights._discard_cache()


def test_invalidation_after_snapshot_fences_cadence_write(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "fix", "title": "Fix", "capacity": 1,
            "query": "is:issue", "series_contains": "FIX",
            "adaptive_cadence": {
                "idle_recurrence": "30m", "busy_recurrence": "15m",
                "backlog_above": 0,
            },
        }],
    }
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - FIX", recurrence="30m"))
    series = isolated_db.list_series()
    real_prune = isolated_db.prune_pipeline_snapshots
    invalidated = []

    def invalidate_before_cadence(cutoff):
        real_prune(cutoff)
        pipeline_insights._discard_cache()
        invalidated.append(True)

    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"cadence-race": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: {
        "count": 1, "items": [], "membership_complete": True,
    })
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    monkeypatch.setattr(
        isolated_db, "prune_pipeline_snapshots", invalidate_before_cadence)
    pipeline_insights._discard_cache()

    try:
        result = pipeline_insights.analyze(
            "cadence-race", series, use_cache=False)

        assert invalidated == [True]
        assert result["cache"]["invalidated"] is True
        assert isolated_db.get_series(task.series_id)[
            "effective_recurrence"] == "30m"
    finally:
        pipeline_insights._discard_cache()


def test_cache_read_observes_invalidation_with_payload_at_one_snapshot(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"coherent": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: {
        "count": 1, "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    pipeline_insights._cache.clear()
    pipeline_insights.analyze("coherent", [], use_cache=False)
    pipeline_insights._cache.clear()
    real_snapshot = isolated_db.get_settings_snapshot
    raced = {"done": False}

    def invalidate_after_settings_snapshot(keys):
        value = real_snapshot(keys)
        if not raced["done"]:
            raced["done"] = True
            pipeline_insights._discard_cache("coherent")
        return value

    monkeypatch.setattr(
        pipeline_insights.db, "get_settings_snapshot",
        invalidate_after_settings_snapshot)

    result = pipeline_insights.read_cached("coherent", [])

    assert raced["done"] is True
    assert result["cache"]["source"] in {"memory", "durable"}
    assert result["cache"]["invalidated"] is True
    assert result["cache"]["stale"] is True
    assert result["cache"]["token"]["epoch"] < \
        pipeline_insights._cache_epoch()


def test_external_pause_change_bypasses_cached_pipeline_state(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    search_calls = []

    def fake_search(repository, query):
        search_calls.append((repository, query))
        return {"count": 1, "items": [], "membership_complete": False}

    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"pause-cache": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", fake_search)
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", lambda: None)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    isolated_db.set_setting("worker_paused", "0")
    pipeline_insights.invalidate_cache()

    try:
        before = pipeline_insights.analyze("pause-cache", [], use_cache=False)
        isolated_db.set_setting("worker_paused", "1")  # another process changes runtime state
        pipeline_insights.invalidate_cache()
        after = pipeline_insights.read_cached("pause-cache", [])
        cached = pipeline_insights.read_cached("pause-cache", [])

        assert before["runtime"]["paused"] is False
        assert after["runtime"]["paused"] is True
        assert cached["generated_at"] == after["generated_at"] == before["generated_at"]
        assert len(search_calls) == 1
    finally:
        pipeline_insights.invalidate_cache()


def test_external_pause_during_analysis_keeps_last_good_with_live_pause_overlay(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    entered_search = threading.Event()
    release_search = threading.Event()
    search_calls = []
    results = []
    errors = []

    def fake_search(repository, query):
        search_calls.append((repository, query))
        if len(search_calls) == 1:
            entered_search.set()
            if not release_search.wait(5):
                raise TimeoutError("test did not release the in-flight analysis")
        return {"count": 1, "items": [], "membership_complete": False}

    def run_analysis():
        try:
            results.append(pipeline_insights.analyze(
                "pause-during-analysis", [], use_cache=False))
        except BaseException as exc:  # preserve the worker-thread failure for the assertion
            errors.append(exc)

    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"pause-during-analysis": profile})
    monkeypatch.setattr(pipeline_insights, "_github_search", fake_search)
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", lambda: None)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    isolated_db.set_setting("worker_paused", "0")
    pipeline_insights.invalidate_cache()
    analysis = threading.Thread(target=run_analysis)
    analysis.start()

    try:
        assert entered_search.wait(5)
        isolated_db.set_setting("worker_paused", "1")  # another process changes runtime state
        pipeline_insights.invalidate_cache()
        release_search.set()
        analysis.join(5)
        assert not analysis.is_alive()
        assert errors == []
        assert results[0]["runtime"]["paused"] is True
        assert isolated_db.get_setting(
            pipeline_insights._cache_key(
                "pause-during-analysis",
                pipeline_insights._profile_fingerprint(profile))) is not None

        cached = pipeline_insights.read_cached("pause-during-analysis", [])
        assert cached["runtime"]["paused"] is True
        assert cached["generated_at"] == results[0]["generated_at"]
        assert len(search_calls) == 1
    finally:
        release_search.set()
        analysis.join(5)
        pipeline_insights.invalidate_cache()


def test_paused_pipeline_is_not_background_sampled():
    profile = {"queues": [{"series_contains": "Example - REVIEW"}]}
    paused = [{"title": "Example - REVIEW", "paused": True, "ended": False}]
    active = [{"title": "Example - REVIEW", "paused": False, "ended": False}]

    assert pipeline_insights._profile_active(profile, paused) is False
    assert pipeline_insights._profile_active(profile, active) is True


def test_global_pause_skips_background_pipeline_sampling(isolated_db, monkeypatch):
    profile = {"always_sample": True,
               "queues": [{"series_contains": "Example - REVIEW"}]}
    active = [{"title": "Example - REVIEW", "paused": False, "ended": False}]
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "analyze",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )
    isolated_db.set_setting("worker_paused", "1")

    result = pipeline_insights.sample_active_profiles(active)

    assert result == {}
    assert calls == []


def test_sampler_rechecks_pause_after_analysis_before_wake(isolated_db, monkeypatch):
    profile = {"always_sample": True, "queues": []}
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    def analyze(*_args, **_kwargs):
        calls.append("analyze")
        isolated_db.set_setting("worker_paused", "1")
        return {"diagnostics": {}}

    monkeypatch.setattr(pipeline_insights, "analyze", analyze)
    monkeypatch.setattr(
        pipeline_insights, "_wake_ready_queues",
        lambda *_args, **_kwargs: calls.append("wake") or [],
    )

    result = pipeline_insights.sample_active_profiles([])

    assert result == {"example": "paused"}
    assert calls == ["analyze"]


def test_pipeline_insights_exposes_actual_series_task_status(isolated_db, monkeypatch):
    counts = iter([0, 0, 0, 1, 0])
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda repo, query: {
        "count": next(counts), "items": [], "membership_complete": False,
    })
    monkeypatch.setattr(pipeline_insights, "_profiles",
                        lambda: {"example": PIPELINE_PROFILE})
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
    ))
    claimed = isolated_db.get_next_runnable()
    assert claimed.id == task.id
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze(
        "example", isolated_db.list_series(), use_cache=False)

    review = next(q for q in result["queues"] if q["id"] == "review")
    assert review["task_id"] == task.id
    assert review["task_status"] == "running"


def test_pipeline_backlog_can_use_exact_project_diagnostics(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "health_check": {"command": ["health"]},
        "queues": [{
            "id": "review", "title": "Review", "capacity": 2,
            "query": "is:pr", "series_contains": "REVIEW",
            "backlog_diagnostic_field": "review_candidates",
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: {
        "state": "green", "review_candidates": [{"number": 2}], "findings": [],
    })
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda _repo, _query: {
        "count": 3, "membership_complete": True,
        "items": [
            {"key": f"pr:{number}", "kind": "pr", "number": number,
             "title": f"PR {number}", "labels": [], "created_at": "2026-09-01T00:00:00Z"}
            for number in (1, 2, 3)
        ],
    })
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("example", [], use_cache=False)
    review = result["queues"][0]
    assert review["backlog"] == 1
    assert [item["number"] for item in review["items"]] == []  # priority UI is opt-in
    snapshot = isolated_db.list_pipeline_snapshots("example")[-1]["payload"]
    assert snapshot["queues"]["review"]["backlog"] == 1


def test_pipeline_items_follow_executable_diagnostic_order(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "health_check": {"command": ["health"]},
        "priority_control": {"max_items": 1},
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
            "backlog_diagnostic_field": "review_candidates",
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", lambda _profile: {
        "state": "green",
        "review_candidates": [{"number": 1310}, {"number": 1218}],
        "findings": [],
    })
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda _repo, _query: {
        "count": 2, "membership_complete": True,
        "items": [
            {"key": f"pr:{number}", "kind": "pr", "number": number,
             "title": f"PR {number}", "labels": [], "created_at": created_at}
            for number, created_at in (
                (1218, "2026-08-01T00:00:00Z"),
                (1310, "2026-09-01T00:00:00Z"),
            )
        ],
    })
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("example", [], use_cache=False)

    assert [item["number"] for item in result["queues"][0]["items"]] == [1310]


def test_pipeline_item_priority_manual_override_and_aging():
    settings = pipeline_insights._priority_settings({"priority_control": {"aging_hours": 24}})
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    manual = pipeline_insights._item_priority({
        "labels": ["bug", "queue:p3"], "created_at": "2026-09-02T00:00:00Z",
    }, settings, now)
    aged = pipeline_insights._item_priority({
        "labels": ["enhancement"], "created_at": "2026-08-30T00:00:00Z",
    }, settings, now)

    assert manual["base_level"] == "p3"
    assert manual["level"] == "p3"
    assert manual["source"] == "manual"
    assert aged["base_level"] == "p2"
    assert aged["level"] == "p1"
    assert aged["age_boost"] == 1


def test_set_pipeline_item_priority_replaces_manual_label_and_wakes_series(monkeypatch):
    profile = {
        "repository": "owner/repo", "priority_control": {"trusted_account": "owner"},
        "queues": [{"id": "fix", "series_contains": "Project - FIX"}],
    }
    calls = []

    def fake_api(args, input_value=None):
        calls.append((args, input_value))
        if args == ["user"]:
            return {"login": "owner"}
        if args == ["repos/owner/repo/issues/42"]:
            return {"state": "open", "labels": [{"name": "queue:p2"}]}
        return None

    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "_gh_api_json", fake_api)
    monkeypatch.setattr(pipeline_insights.db, "series_action", lambda series_id, action: (series_id, action) == (7, "run_now"))

    result = pipeline_insights.set_item_priority(
        "example", "fix", "issue", 42, "p0", True,
        [{"id": 7, "title": "Project - FIX", "ended": False, "paused": False}],
    )

    assert result["series_woken"] is True
    assert (["repos/owner/repo/issues/42/labels", "--method", "POST"], {"labels": ["queue:p0"]}) in calls
    assert (["repos/owner/repo/issues/42/labels/queue%3Ap2", "--method", "DELETE"], None) in calls


def test_dispatch_gate_completes_empty_queue_without_provider(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - TRIAGE", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "triage", "title": "Triage", "query": "is:issue",
            "series_contains": "ExampleProject - TRIAGE",
            "dispatch_gate": {"skip_when_empty": True},
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "read_cached", lambda *args, **kwargs: {
        "queues": [{"id": "triage", "title": "Triage", "backlog": 0}],
        "diagnostics": {}, "cache": {"stale": False, "complete": True},
    })

    gate = pipeline_insights.dispatch_gate(task)

    assert gate["action"] == "complete_empty"
    assert "пуста" in gate["reason"]


def test_dispatch_gate_reuses_recent_pipeline_snapshot(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - TRIAGE", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "triage", "title": "Triage", "query": "is:issue",
            "series_contains": "ExampleProject - TRIAGE",
            "dispatch_gate": {"skip_when_empty": True},
        }],
    }
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "read_cached", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {
            "queues": [{"id": "triage", "title": "Triage", "backlog": 0}],
            "diagnostics": {}, "cache": {"stale": False, "complete": True},
        }
    ))

    pipeline_insights.dispatch_gate(task)

    assert len(calls) == 1
    assert calls[0][1] == {}


def test_dispatch_gate_ignores_stale_empty_snapshot(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - TRIAGE", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "triage", "title": "Triage", "query": "is:issue",
            "series_contains": "ExampleProject - TRIAGE",
            "dispatch_gate": {"skip_when_empty": True},
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "read_cached", lambda *args, **kwargs: {
        "queues": [{"id": "triage", "title": "Triage", "backlog": 0}],
        "diagnostics": {}, "cache": {"stale": True, "complete": True},
    })

    assert pipeline_insights.dispatch_gate(task) is None


def test_dispatch_gate_defers_dependency_without_provider(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - MERGE", recurrence="2h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "merge", "title": "Merge", "query": "is:pr label:ship",
            "series_contains": "ExampleProject - MERGE",
            "dispatch_gate": {
                "defer_when_diagnostics_nonempty": ["review_candidates"],
                "defer_for": "7m",
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "read_cached", lambda *args, **kwargs: {
        "queues": [{"id": "merge", "title": "Merge", "backlog": 3}],
        "diagnostics": {"review_candidates": [{"number": 42}]},
        "cache": {"stale": False, "complete": True},
    })

    gate = pipeline_insights.dispatch_gate(task)

    assert gate["action"] == "defer"
    assert gate["defer_for"] == "7m"
    assert "review_candidates" in gate["reason"]


def test_dispatch_gate_defers_only_matching_diagnostic_stages(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - MERGE", recurrence="2h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "merge", "title": "Merge", "query": "is:pr label:ship",
            "series_contains": "ExampleProject - MERGE",
            "dispatch_gate": {
                "defer_when_diagnostics_match": [{
                    "field": "review_candidates", "key": "stage",
                    "values": ["integration-review", "legacy-integration-review"],
                }],
                "defer_for": "7m",
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    diagnostics = {"review_candidates": [
        {"number": 42, "stage": "legacy-integration-review"},
        {"number": 43, "stage": "integration-merge-ready"},
    ]}
    monkeypatch.setattr(pipeline_insights, "read_cached", lambda *args, **kwargs: {
        "queues": [{"id": "merge", "title": "Merge", "backlog": 2}],
        "diagnostics": diagnostics,
        "cache": {"stale": False, "complete": True},
    })

    gate = pipeline_insights.dispatch_gate(task)
    assert gate["action"] == "defer"
    assert "совпал (1)" in gate["reason"]

    diagnostics["review_candidates"] = [
        {"number": 43, "stage": "integration-merge-ready"},
        {"number": 44, "stage": "integration-merge-recovery"},
    ]
    assert pipeline_insights.dispatch_gate(task) is None


def test_productive_completion_wakes_every_ready_stage(isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [
            {"id": "fix", "series_contains": "Example - FIX"},
            {"id": "review", "series_contains": "Example - REVIEW",
             "wake_when": {"field": "review_candidates"}},
            {"id": "merge", "series_contains": "Example - MERGE",
             "wake_when": {"field": "integration_owner", "key": "stage",
                           "values": ["integration-merge-ready"]}},
        ],
    }
    series = [
        {"id": 7, "title": "Example - REVIEW", "paused": False, "ended": False},
        {"id": 8, "title": "Example - MERGE", "paused": False, "ended": False},
    ]
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "list_series", lambda: series)
    monkeypatch.setattr(pipeline_insights.db, "is_paused", lambda: False)
    diagnostics = {
        "review_candidates": [{"number": 42}],
        "integration_owner": {"number": 10, "stage": "integration-merge-ready"},
    }
    data = _fresh_cache_data("example", profile, diagnostics)
    analysis_calls = []

    def fresh_analysis(*args, **kwargs):
        analysis_calls.append((args, kwargs))
        return data

    monkeypatch.setattr(
        pipeline_insights, "analyze", fresh_analysis)
    calls = []
    monkeypatch.setattr(
        pipeline_insights.db, "wake_series_once",
        lambda series_id, key, fingerprint, **_kwargs: calls.append(
            (series_id, key, fingerprint)) or True,
    )
    task = SimpleNamespace(series_id=1, series_title="Example - FIX", prompt="Example - FIX")

    assert pipeline_insights.after_task_completed(task, "ГОТОВО") == ["review", "merge"]
    assert [(series_id, key) for series_id, key, _fingerprint in calls] == [
        (7, "pipeline_wake:example:review"),
        (8, "pipeline_wake:example:merge"),
    ]
    assert len(analysis_calls) == 1
    assert analysis_calls[0][1] == {
        "use_cache": False, "refresh_diagnostics": True,
    }


def test_productive_completion_does_not_scan_or_wake_during_global_pause(monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{"id": "fix", "series_contains": "Example - FIX"}],
    }
    task = SimpleNamespace(
        series_id=1, series_title="Example - FIX", prompt="Example - FIX")
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "is_paused", lambda: True)
    monkeypatch.setattr(
        pipeline_insights, "analyze",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert pipeline_insights.after_task_completed(task, "ГОТОВО") == []
    assert calls == []


def test_productive_completion_rechecks_pause_after_analysis_before_wake(
        isolated_db, monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{"id": "fix", "series_contains": "Example - FIX"}],
    }
    task = SimpleNamespace(
        series_id=1, series_title="Example - FIX", prompt="Example - FIX")
    calls = []
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "list_series", lambda: [])

    def analyze(*_args, **_kwargs):
        calls.append("analyze")
        isolated_db.set_setting("worker_paused", "1")
        return {"diagnostics": {}}

    monkeypatch.setattr(pipeline_insights, "analyze", analyze)
    monkeypatch.setattr(
        pipeline_insights, "_wake_ready_queues",
        lambda *_args, **_kwargs: calls.append("wake") or [],
    )

    assert pipeline_insights.after_task_completed(task, "ГОТОВО") == []
    assert calls == ["analyze"]


def test_wakeup_skips_empty_paused_and_nonproductive_runs(monkeypatch):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [
            {"id": "fix", "series_contains": "Example - FIX"},
            {"id": "review", "series_contains": "Example - REVIEW",
             "wake_when": {"field": "review_candidates"}},
            {"id": "merge", "series_contains": "Example - MERGE",
             "wake_when": {"field": "merge_candidates"}},
        ],
    }
    series = [
        {"id": 7, "title": "Example - REVIEW", "paused": True, "ended": False},
        {"id": 8, "title": "Example - MERGE", "paused": False, "ended": False},
    ]
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "list_series", lambda: series)
    monkeypatch.setattr(pipeline_insights, "analyze", lambda *args, **kwargs: {
        "diagnostics": {"review_candidates": [{"number": 42}], "merge_candidates": []},
    })
    calls = []
    monkeypatch.setattr(
        pipeline_insights.db, "wake_series_once",
        lambda series_id, key, fingerprint, **_kwargs: calls.append(
            (series_id, key, fingerprint)) or True,
    )
    monkeypatch.setattr(pipeline_insights.db, "delete_setting", lambda _key: None)
    task = SimpleNamespace(series_id=1, series_title="Example - FIX", prompt="Example - FIX")

    assert pipeline_insights.after_task_completed(task, "ПУСТО") == []
    assert pipeline_insights.after_task_completed(task, "ГОТОВО") == []
    assert calls == []


def test_wake_fingerprint_changes_only_when_matched_work_changes():
    condition = {"field": "review_candidates", "key": "stage",
                 "values": ["integration-review"]}
    before = {"review_candidates": [
        {"number": 42, "stage": "integration-review", "updated_at": "t1"},
        {"number": 43, "stage": "review", "updated_at": "t1"},
    ]}
    unrelated = {"review_candidates": [
        {"number": 42, "stage": "integration-review", "updated_at": "t1"},
        {"number": 43, "stage": "review", "updated_at": "t2"},
    ]}
    changed = {"review_candidates": [
        {"number": 42, "stage": "integration-review", "updated_at": "t2"},
    ]}

    assert pipeline_insights._wake_fingerprint(before, condition) == \
        pipeline_insights._wake_fingerprint(unrelated, condition)
    assert pipeline_insights._wake_fingerprint(before, condition) != \
        pipeline_insights._wake_fingerprint(changed, condition)


def test_series_wake_latch_suppresses_unchanged_snapshot(isolated_db):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    data = _fresh_cache_data("example", profile)
    guard = pipeline_insights._wake_cache_guard(
        "example", profile, data["cache"])
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h",
    ))
    series_id = task.series_id

    assert isolated_db.wake_series_once(
        series_id, "wake:test", "snapshot-a", cache_guard=guard)
    assert not isolated_db.wake_series_once(
        series_id, "wake:test", "snapshot-a", cache_guard=guard)
    assert isolated_db.wake_series_once(
        series_id, "wake:test", "snapshot-b", cache_guard=guard)


@pytest.mark.parametrize("status", ["pending", "rate_limited"])
def test_diagnostic_wake_latches_future_hard_barrier(
        isolated_db, status):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    data = _fresh_cache_data("example", profile)
    guard = pipeline_insights._wake_cache_guard(
        "example", profile, data["cache"])
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h"))
    claimed = isolated_db.get_next_runnable()
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    if status == "pending":
        isolated_db.defer_task(
            claimed.id, deadline, "GitHub budget", hard_not_before=True)
    else:
        isolated_db.mark_rate_limited(claimed.id, deadline, "provider budget")

    assert isolated_db.wake_series_once(
        task.series_id, "wake:test", "snapshot-a", cache_guard=guard)
    assert not isolated_db.wake_series_once(
        task.series_id, "wake:test", "snapshot-a", cache_guard=guard)

    deferred = isolated_db.get_task(task.id)
    assert deferred.scheduled_at == (
        deadline if status == "pending" else task.scheduled_at)
    assert deferred.next_run_at == deadline
    assert isolated_db.get_setting(
        f"pipeline_series_wake_intent:v1:{task.series_id}") == "1"


def test_pipeline_wake_checks_pause_atomically_at_mutation(
        isolated_db, monkeypatch):
    scheduled = datetime.now(timezone.utc) + timedelta(hours=3)
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h", scheduled_at=scheduled))
    series = isolated_db.list_series()
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
            "wake_when": {"field": "review_candidates"},
        }],
    }
    data = _fresh_cache_data(
        "example", profile, {"review_candidates": [{"number": 42}]})
    real_wake = isolated_db.wake_series_once
    calls = []

    def pause_between_check_and_write(
            series_id, latch_key, fingerprint, *, cache_guard):
        calls.append("mutation")
        isolated_db.set_setting("worker_paused", "1")
        return real_wake(
            series_id, latch_key, fingerprint, cache_guard=cache_guard)

    monkeypatch.setattr(
        pipeline_insights.db, "wake_series_once", pause_between_check_and_write)

    assert pipeline_insights._wake_ready_queues(
        "example", profile, data, series) == []
    assert calls == ["mutation"]
    assert isolated_db.get_task(task.id).scheduled_at == task.scheduled_at
    assert isolated_db.get_setting("pipeline_wake:example:review") is None


def test_pipeline_wake_keeps_latch_and_task_when_cache_token_changes(
        isolated_db):
    scheduled = datetime.now(timezone.utc) + timedelta(hours=3)
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h", scheduled_at=scheduled))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "capacity": 1,
            "query": "is:pr", "series_contains": "REVIEW",
        }],
    }
    first = _fresh_cache_data("example", profile)
    stale_guard = pipeline_insights._wake_cache_guard(
        "example", profile, first["cache"])
    isolated_db.set_setting("pipeline_wake:example:review", "snapshot-old")
    _fresh_cache_data("example", profile)  # advances the accepted revision

    assert not isolated_db.wake_series_once(
        None, "pipeline_wake:example:review", None,
        cache_guard=stale_guard)
    assert not isolated_db.wake_series_once(
        task.series_id, "pipeline_wake:example:review", "snapshot-new",
        cache_guard=stale_guard)
    assert isolated_db.get_setting(
        "pipeline_wake:example:review") == "snapshot-old"
    assert isolated_db.get_task(task.id).scheduled_at == task.scheduled_at


def test_pipeline_wake_rejects_stale_or_partial_analysis(monkeypatch):
    profile = {
        "queues": [{
            "id": "review", "series_contains": "REVIEW",
            "wake_when": {"field": "review_candidates"},
        }],
    }
    series = [{
        "id": 7, "title": "Example - REVIEW",
        "paused": False, "ended": False,
    }]
    calls = []
    monkeypatch.setattr(pipeline_insights.db, "is_paused", lambda: False)
    monkeypatch.setattr(
        pipeline_insights.db, "wake_series_once",
        lambda *_args: calls.append("wake") or True,
    )
    diagnostics = {"review_candidates": [{"number": 42}]}

    assert pipeline_insights._wake_ready_queues(
        "example", profile,
        {"cache": {"stale": True, "complete": True},
         "diagnostics": diagnostics}, series) == []
    assert pipeline_insights._wake_ready_queues(
        "example", profile,
        {"cache": {"stale": False, "complete": False},
         "diagnostics": diagnostics}, series) == []
    assert calls == []


def test_worker_wakes_pipeline_only_after_next_recurrence_exists(monkeypatch):
    from promptpilot import workflows

    events = []
    task = SimpleNamespace(id=42)
    fresh = SimpleNamespace(id=42, status=SimpleNamespace(value="completed"), verdict="ГОТОВО")
    monkeypatch.setattr(worker, "_execute_task_inner", lambda _task: events.append("execute"))
    monkeypatch.setattr(worker, "_recur_after_run", lambda _task: events.append("recur"))
    monkeypatch.setattr(worker, "_notify_pipeline_completion",
                        lambda _task, _verdict: events.append("wake"))
    monkeypatch.setattr(worker.db, "get_task", lambda _task_id: fresh)
    monkeypatch.setattr(workflows, "sync_task", lambda _task_id: None)
    monkeypatch.setattr(workflows, "advance_linked_task", lambda _task_id: None)

    worker.execute_task(task)

    assert events == ["execute", "recur", "wake"]


def test_worker_internal_failure_keeps_recurring_series_alive(isolated_db, monkeypatch):
    from promptpilot import workflows

    created = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h",
    ))
    running = isolated_db.get_next_runnable()
    assert running is not None
    assert running.id == created.id
    monkeypatch.setattr(workflows, "sync_task", lambda _task_id: None)
    monkeypatch.setattr(workflows, "advance_linked_task", lambda _task_id: None)

    assert worker._fail_stuck(running, RuntimeError("boom")) is True

    failed = isolated_db.get_task(running.id)
    series = next(item for item in isolated_db.list_series()
                  if item["id"] == running.series_id)
    assert failed.status.value == "failed"
    assert series["broken"] is False
    assert series["next_task_id"] != running.id
    assert series["next_status"] == "pending"


def test_mark_completed_commits_verdict_and_clears_note_atomically(isolated_db):
    created = isolated_db.create_task(TaskCreate(prompt="atomic completion"))
    isolated_db.set_note(created.id, "late instruction")

    isolated_db.mark_completed(
        created.id, "ИТОГ: ГОТОВО (done)", verdict="ГОТОВО",
    )

    completed = isolated_db.get_task(created.id)
    assert completed.status.value == "completed"
    assert completed.verdict == "ГОТОВО"
    assert completed.note is None


def test_pipeline_execution_auto_uses_tool_when_available(isolated_db, monkeypatch, tmp_path):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text(
        "import json; print(json.dumps({'action':'audit','lease':'abc','target':{'number':42}}))",
        encoding="utf-8",
    )
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "query": "is:pr",
            "series_contains": "ExampleProject - REVIEW",
            "execution": {
                "mode": "auto", "stage": "review",
                "command": ["{python}", "pipelinectl.py", "next", "{stage}"],
                "required_paths": ["pipelinectl.py"],
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))

    assert route["action"] == "prompt"
    assert route["mode"] == "tool"
    assert "pipelinectl.py next review" in route["prompt"]
    assert '"lease": "abc"' in route["prompt"]
    assert "Не запускай next повторно" in route["prompt"]
    assert "/review-queue" in route["prompt"]
    assert route["profile_id"] == "example"
    assert route["queue_id"] == "review"


@pytest.mark.parametrize("execution", [None, {"mode": "skill"}])
def test_pipeline_skill_routes_keep_matched_queue_identity(
        isolated_db, monkeypatch, execution):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    queue = {
        "id": "review", "title": "Review", "query": "is:pr",
        "series_contains": "ExampleProject - REVIEW",
    }
    if execution is not None:
        queue["execution"] = execution
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [queue],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt)

    assert route == {
        "action": "prompt", "mode": "skill", "prompt": task.prompt,
        "profile_id": "example", "queue_id": "review",
    }


def test_pipeline_execution_auto_accepts_merge_cleanup_action(isolated_db, monkeypatch, tmp_path):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text(
        "import json; print(json.dumps({'action':'cleanup','lease':'abc','target':{'number':42}}))",
        encoding="utf-8",
    )
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - MERGE\n/merge-shepherd", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "merge", "title": "Merge", "query": "is:pr label:ship",
            "series_contains": "ExampleProject - MERGE",
            "execution": {
                "mode": "auto", "stage": "merge",
                "command": ["{python}", "pipelinectl.py", "next", "{stage}"],
                "required_paths": ["pipelinectl.py"],
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))

    assert route["action"] == "prompt"
    assert route["mode"] == "tool"
    assert '"action": "cleanup"' in route["prompt"]
    assert '"lease": "abc"' in route["prompt"]


def test_bundled_pipeline_command_routes_through_pp_cli(monkeypatch):
    monkeypatch.setattr(pipeline_insights.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pipeline_insights.sys, "executable", "C:\\PromptPilot\\pp.exe")

    command = pipeline_insights._expanded_command([
        "{python}", "-m", "promptpilot.project_pipeline",
        "--config", "pipelinectl.json", "next", "{stage}",
    ], "review")

    assert command == [
        "C:\\PromptPilot\\pp.exe", "pipelinectl",
        "--config", "pipelinectl.json", "next", "review",
    ]


def test_pipeline_execution_empty_completes_without_provider(isolated_db, monkeypatch, tmp_path):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text(
        "import json; print(json.dumps({'action':'empty','verdict':'ПУСТО','reason':'queue is empty'}))",
        encoding="utf-8",
    )
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "query": "is:pr",
            "series_contains": "ExampleProject - REVIEW",
            "execution": {
                "mode": "auto", "stage": "review",
                "command": ["{python}", "pipelinectl.py", "next", "{stage}"],
                "required_paths": ["pipelinectl.py"],
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))

    assert route["action"] == "complete_empty"
    assert route["verdict"] == "ПУСТО"
    assert route["reason"] == "queue is empty"


@pytest.mark.parametrize("action", ["empty", "wait"])
def test_pipeline_execution_empty_cannot_authorize_stale(
        isolated_db, monkeypatch, tmp_path, action):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text(
        "import json; print(json.dumps({"
        f"'action':'{action}','verdict':'УСТАРЕЛО','reason':'generic tool'"
        "}))",
        encoding="utf-8",
    )
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "query": "is:pr",
            "series_contains": "ExampleProject - REVIEW",
            "execution": {
                "mode": "auto", "stage": "review",
                "command": ["{python}", "pipelinectl.py", "next", "{stage}"],
                "required_paths": ["pipelinectl.py"],
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))

    assert route["action"] == "complete_empty"
    assert route["verdict"] == "НЕ СМОГ"


def test_worker_settles_preflight_empty_without_loading_provider(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
    ))
    task = isolated_db.get_next_runnable()
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route",
        lambda *_args, **_kwargs: {
            "action": "complete_empty", "mode": "tool",
            "reason": "queue is empty", "verdict": "ПУСТО",
        },
    )
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be loaded")),
    )

    worker._execute_task_inner(task)

    settled = isolated_db.get_task(task.id)
    assert settled.status.value == "completed"
    assert settled.verdict == "ПУСТО"
    assert "токены не потрачены" in settled.result


def test_worker_never_persists_stale_from_complete_empty(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
    ))
    task = isolated_db.get_next_runnable()
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route",
        lambda *_args, **_kwargs: {
            "action": "complete_empty", "mode": "tool",
            "reason": "generic tool", "verdict": "УСТАРЕЛО",
        },
    )
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be loaded")),
    )

    worker._execute_task_inner(task)

    settled = isolated_db.get_task(task.id)
    assert settled.status.value == "completed"
    assert settled.verdict == "НЕ СМОГ"
    assert "ИТОГ: НЕ СМОГ (generic tool)" in settled.result
    assert "ИТОГ: УСТАРЕЛО" not in settled.result


def test_pipeline_execution_tool_fallback_skips_preflight_prompt(isolated_db, monkeypatch, tmp_path):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text(
        "import json; print(json.dumps({'action':'fallback','reason':'complex state'}))",
        encoding="utf-8",
    )
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    execution = {
        "mode": "auto", "command": ["{python}", "pipelinectl.py"],
        "required_paths": ["pipelinectl.py"],
    }
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "query": "is:pr",
            "series_contains": "ExampleProject - REVIEW", "execution": execution,
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    automatic = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))
    assert automatic["mode"] == "skill"
    assert automatic["fallback_reason"] == "complex state"
    assert automatic["prompt"] == task.prompt

    execution["mode"] = "tool"
    strict = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))
    assert strict["action"] == "block"
    assert strict["reason"] == "complex state"


@pytest.mark.parametrize("next_already_run,expected,forbidden", [
    (True, "Pipeline tool selected target; continuing full skill",
     "Pipeline tool unavailable"),
    (False, "Pipeline tool unavailable, using skill",
     "Pipeline tool selected target"),
])
def test_worker_log_distinguishes_handoff_from_unavailable_tool(
        isolated_db, monkeypatch, capsys, next_already_run, expected, forbidden):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
    ))
    task = isolated_db.get_next_runnable()
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    route = {
        "action": "prompt", "mode": "skill", "prompt": task.prompt,
        "fallback_reason": "complex state",
    }
    if next_already_run:
        route["next_already_run"] = True
    monkeypatch.setattr(
        pipeline_insights, "execution_route", lambda *_args, **_kwargs: route)
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: {worker.DEFAULT_CLI: {"executor": "herdr"}},
    )
    monkeypatch.setattr(worker, "_execute_herdr_task", lambda *_args, **_kwargs: None)

    worker._execute_task_inner(task)

    output = capsys.readouterr().out
    assert expected in output
    assert forbidden not in output


def test_pipeline_execution_preflight_error_defers_without_skill_fallback(
        isolated_db, monkeypatch, tmp_path):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text(
        "import json; print(json.dumps({'action':'error','error':'cannot fast-forward main'}))",
        encoding="utf-8",
    )
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "query": "is:pr",
            "series_contains": "ExampleProject - REVIEW",
            "execution": {
                "mode": "auto", "command": ["{python}", "pipelinectl.py"],
                "required_paths": ["pipelinectl.py"], "error_defer_for": "12m",
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))

    assert route["action"] == "defer"
    assert route["defer_for"] == "12m"
    assert route["reason"] == "cannot fast-forward main"
    assert "prompt" not in route


def test_pipeline_execution_error_defers_without_skill_fallback(isolated_db, monkeypatch, tmp_path):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text(
        "import json; print(json.dumps({'action':'error','error':'GitHub rate limited'})); raise SystemExit(2)",
        encoding="utf-8",
    )
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "query": "is:pr",
            "series_contains": "ExampleProject - REVIEW",
            "execution": {
                "mode": "auto", "command": ["{python}", "pipelinectl.py"],
                "required_paths": ["pipelinectl.py"], "error_defer_for": "11m",
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))

    assert route["action"] == "defer"
    assert route["defer_for"] == "11m"
    assert route["reason"] == "GitHub rate limited"


def test_worker_defers_preflight_error_without_loading_provider(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW", recurrence="4h",
    ))
    task = isolated_db.get_next_runnable()
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route",
        lambda *_args, **_kwargs: {
            "action": "defer", "mode": "tool", "reason": "GitHub rate limited",
            "defer_for": "11m",
        },
    )
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be loaded")),
    )

    worker._execute_task_inner(task)

    deferred = isolated_db.get_task(task.id)
    assert deferred.status.value == "pending"
    assert deferred.error == "GitHub rate limited"


def test_pipeline_execution_auto_falls_back_but_tool_mode_blocks(isolated_db, monkeypatch, tmp_path):
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - MERGE\n/merge-shepherd", recurrence="4h",
    ))
    execution = {
        "mode": "auto", "command": ["{python}", "missing.py", "{stage}"],
        "required_paths": ["missing.py"],
    }
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "merge", "title": "Merge", "query": "is:pr label:ship",
            "series_contains": "ExampleProject - MERGE", "execution": execution,
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    automatic = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))
    assert automatic["mode"] == "skill"
    assert "не найден missing.py" in automatic["fallback_reason"]

    execution["mode"] = "tool"
    required = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))
    assert required["action"] == "block"
    assert required["mode"] == "tool"


def test_pipeline_execution_probe_failure_uses_skill_in_auto(isolated_db, monkeypatch, tmp_path):
    helper = tmp_path / "pipelinectl.py"
    helper.write_text("print('present')", encoding="utf-8")
    task = isolated_db.create_task(TaskCreate(
        prompt="ExampleProject - REVIEW\n/review-queue", recurrence="4h",
    ))
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{
            "id": "review", "title": "Review", "query": "is:pr",
            "series_contains": "ExampleProject - REVIEW",
            "execution": {
                "mode": "auto", "command": ["{python}", "pipelinectl.py"],
                "required_paths": ["pipelinectl.py"],
                "probe_command": ["{python}", "-c", "raise SystemExit(7)"],
            },
        }],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})

    route = pipeline_insights.execution_route(task, task.prompt, str(tmp_path))

    assert route["mode"] == "skill"
    assert "probe завершился с ошибкой" in route["fallback_reason"]

def test_pipeline_insights_history_is_profile_scoped_and_tracks_movement(isolated_db, monkeypatch):
    profile = {
        "title": "OtherProject pipeline", "repository": "owner/other",
        "target_clear_hours": 6,
        "queues": [
            {"id": "build", "title": "Build", "query": "label:build",
             "capacity": 2, "series_contains": "Other - BUILD"},
            {"id": "review", "title": "Review", "query": "label:review",
             "capacity": 1, "series_contains": "Other - REVIEW"},
        ],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"other": profile})
    responses = {
        "label:build": {
            "count": 2, "membership_complete": True,
            "items": [
                {"key": "issue:2", "kind": "issue", "number": 2,
                 "created_at": "2026-08-30T00:00:00Z", "updated_at": None, "url": None},
                {"key": "issue:3", "kind": "issue", "number": 3,
                 "created_at": "2026-08-31T00:00:00Z", "updated_at": None, "url": None},
            ],
        },
        "label:review": {
            "count": 1, "membership_complete": True,
            "items": [{"key": "issue:1", "kind": "issue", "number": 1,
                       "created_at": "2026-08-29T00:00:00Z", "updated_at": None, "url": None}],
        },
    }
    monkeypatch.setattr(pipeline_insights, "_github_search",
                        lambda repo, query: responses[query])
    profile_hash = pipeline_insights._profile_fingerprint(profile)
    old = {
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
        "profile_hash": profile_hash,
        "queues": {
            "build": {"backlog": 2, "membership_complete": True, "items": [
                {"key": "issue:1"}, {"key": "issue:2"}]},
            "review": {"backlog": 0, "membership_complete": True, "items": []},
        },
    }
    isolated_db.add_pipeline_snapshot(
        "other", "owner/other", old, datetime.now(timezone.utc) - timedelta(hours=6))
    wrong_profile = {**profile, "target_clear_hours": 12}
    wrong = {
        "captured_at": (
            datetime.now(timezone.utc) - timedelta(hours=5, minutes=45)
        ).isoformat(),
        "profile_hash": pipeline_insights._profile_fingerprint(wrong_profile),
        "queues": {
            "build": {"backlog": 100, "membership_complete": True, "items": []},
            "review": {"backlog": 100, "membership_complete": True, "items": []},
        },
    }
    isolated_db.add_pipeline_snapshot(
        "other", "owner/other", wrong,
        datetime.now(timezone.utc) - timedelta(hours=5, minutes=45))
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("other", [], use_cache=False)

    assert result["profile_id"] == "other"
    assert result["backlog_total"] == 3
    assert result["history"]["5h"]["complete"] is True
    assert result["history"]["5h"]["backlog_delta"] == 1
    assert result["history"]["5h"]["entered"] == 1
    assert result["history"]["5h"]["moved"] == 1
    assert result["history"]["5h"]["transitions"] == 1
    assert result["history"]["168h"]["complete"] is False
    assert result["history"]["720h"]["complete"] is False
    assert len(isolated_db.list_pipeline_snapshots("other")) == 3
    assert isolated_db.list_pipeline_snapshots("unrelated") == []


def test_pipeline_run_metrics_distinguish_semantic_failure_from_process_failure(isolated_db):
    ready = isolated_db.create_task(TaskCreate(prompt="Project - FIX", recurrence="1h"))
    unable = isolated_db.create_task(TaskCreate(
        prompt="Project - FIX", recurrence="1h", series_id=ready.series_id))
    failed = isolated_db.create_task(TaskCreate(
        prompt="Project - FIX", recurrence="1h", series_id=ready.series_id))
    isolated_db.set_verdict(ready.id, "ГОТОВО")
    isolated_db.mark_completed(ready.id, "ok")
    isolated_db.set_verdict(unable.id, "НЕ СМОГ")
    isolated_db.mark_completed(unable.id, "blocked")
    isolated_db.mark_failed(failed.id, "boom")

    metrics = isolated_db.pipeline_run_metrics(
        [ready.series_id], datetime.now(timezone.utc) - timedelta(hours=1))

    assert metrics["runs"] == 3
    assert metrics["ready"] == 1
    assert metrics["unable"] == 1
    assert metrics["failed"] == 1
    assert metrics["unresolved_unable"] == 1
    assert metrics["unresolved_failed"] == 1
    assert metrics["recovered_unable"] == 0
    assert metrics["recovered_failed"] == 0


def test_pipeline_run_metrics_count_stale_reselection_as_safe(isolated_db):
    task = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW", recurrence="1h"))
    isolated_db.mark_completed(
        task.id, "ИТОГ: УСТАРЕЛО", verdict="УСТАРЕЛО")

    metrics = isolated_db.pipeline_run_metrics(
        [task.series_id], datetime.now(timezone.utc) - timedelta(hours=1))

    assert metrics["runs"] == 1
    assert metrics["stale"] == 1
    assert metrics["unable"] == 0
    assert metrics["failed"] == 0


def test_pipeline_run_metrics_clear_incident_after_later_success(isolated_db):
    unable = isolated_db.create_task(TaskCreate(prompt="Project - REVIEW", recurrence="1h"))
    ready = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW", recurrence="1h", series_id=unable.series_id))
    isolated_db.set_verdict(unable.id, "НЕ СМОГ")
    isolated_db.mark_completed(unable.id, "blocked")
    isolated_db.set_verdict(ready.id, "ГОТОВО")
    isolated_db.mark_completed(ready.id, "recovered")

    metrics = isolated_db.pipeline_run_metrics(
        [unable.series_id], datetime.now(timezone.utc) - timedelta(hours=1))

    assert metrics["unable"] == 1
    assert metrics["unresolved_unable"] == 0
    assert metrics["recovered_unable"] == 1


def test_health_reports_only_unresolved_incidents():
    health = pipeline_insights._health(
        10,
        {"5h": {"complete": True, "runs": {
            "failed": 1, "unable": 2,
            "unresolved_failed": 0, "unresolved_unable": 1,
            "recovered_failed": 1, "recovered_unable": 1,
        }}},
        0,
    )

    assert health["state"] == "red"
    assert health["reason"] == "активно — упало: 0; НЕ СМОГ: 1; восстановлено: 2"


def test_health_does_not_keep_recovered_incident_red():
    health = pipeline_insights._health(
        0,
        {"5h": {"complete": True, "runs": {
            "failed": 0, "unable": 1,
            "unresolved_failed": 0, "unresolved_unable": 0,
            "recovered_failed": 0, "recovered_unable": 1,
        }}},
        0,
    )

    assert health["state"] == "green"
    assert health["label"] == "очередь пуста"


def test_pipeline_metrics_separate_successful_noop_and_sum_known_tokens(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Project - MERGE", recurrence="1h"))
    isolated_db.set_verdict(task.id, "ГОТОВО")
    isolated_db.mark_completed(
        task.id,
        "GitHub не изменялся, PR не вливали.\nИТОГ: ГОТОВО\n\n"
        "--- Meta ---\nTokens: 120 in / 30 out",
    )

    metrics = isolated_db.pipeline_run_metrics(
        [task.series_id], datetime.now(timezone.utc) - timedelta(hours=1))
    activity = isolated_db.pipeline_series_activity([task.series_id])[task.series_id]

    assert metrics["ready"] == 0
    assert metrics["no_change"] == 1
    assert metrics["tokens_known_runs"] == 1
    assert metrics["total_tokens"] == 150
    assert activity["summary"] == "ИТОГ: ГОТОВО"
    assert activity["input_tokens"] == 120
