from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace

from promptpilot.models import TaskCreate
from promptpilot import pipeline_insights, worker


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


def test_fresh_install_has_no_project_specific_pipeline_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_PIPELINE_PROFILES", str(tmp_path / "missing.json"))

    assert pipeline_insights.DEFAULT_PROFILES == {}
    assert pipeline_insights.list_profiles() == []


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


def test_successful_retry_clears_stale_error_and_backoff(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Fix"))
    claimed = isolated_db.get_next_runnable()
    isolated_db.mark_rate_limited(
        claimed.id, datetime.now(timezone.utc) - timedelta(minutes=1), "old failure",
    )
    retried = isolated_db.get_next_runnable()

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

    result = pipeline_insights.analyze("example", [], use_cache=False)

    assert result["bottleneck"] == "review"
    review = next(q for q in result["queues"] if q["id"] == "review")
    triage = next(q for q in result["queues"] if q["id"] == "triage")
    assert review["runs_needed"] == 8.5
    assert review["recommended_interval"] == "1h"
    assert "оставить 1h" not in review["recommendation"]  # no matching series in this unit test
    assert "решения человека" in triage["recommendation"]


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
    monkeypatch.setattr(pipeline_insights, "analyze", lambda *args, **kwargs: {
        "queues": [{"id": "triage", "title": "Triage", "backlog": 0}],
        "diagnostics": {},
    })

    gate = pipeline_insights.dispatch_gate(task)

    assert gate["action"] == "complete_empty"
    assert "пуста" in gate["reason"]


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
    monkeypatch.setattr(pipeline_insights, "analyze", lambda *args, **kwargs: {
        "queues": [{"id": "merge", "title": "Merge", "backlog": 3}],
        "diagnostics": {"review_candidates": [{"number": 42}]},
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
    monkeypatch.setattr(pipeline_insights, "analyze", lambda *args, **kwargs: {
        "queues": [{"id": "merge", "title": "Merge", "backlog": 2}],
        "diagnostics": diagnostics,
    })

    gate = pipeline_insights.dispatch_gate(task)
    assert gate["action"] == "defer"
    assert "совпал (1)" in gate["reason"]

    diagnostics["review_candidates"] = [
        {"number": 43, "stage": "integration-merge-ready"},
        {"number": 44, "stage": "integration-merge-recovery"},
    ]
    assert pipeline_insights.dispatch_gate(task) is None


def test_productive_completion_wakes_every_ready_stage(monkeypatch):
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
    monkeypatch.setattr(pipeline_insights, "analyze", lambda *args, **kwargs: {
        "diagnostics": {
            "review_candidates": [{"number": 42}],
            "integration_owner": {"number": 10, "stage": "integration-merge-ready"},
        },
    })
    calls = []
    monkeypatch.setattr(
        pipeline_insights.db, "wake_series_once",
        lambda series_id, key, fingerprint: calls.append(
            (series_id, key, fingerprint)) or True,
    )
    task = SimpleNamespace(series_id=1, series_title="Example - FIX", prompt="Example - FIX")

    assert pipeline_insights.after_task_completed(task, "ГОТОВО") == ["review", "merge"]
    assert [(series_id, key) for series_id, key, _fingerprint in calls] == [
        (7, "pipeline_wake:example:review"),
        (8, "pipeline_wake:example:merge"),
    ]


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
        lambda series_id, key, fingerprint: calls.append(
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
    task = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h",
    ))
    series_id = task.series_id

    assert isolated_db.wake_series_once(series_id, "wake:test", "snapshot-a")
    assert not isolated_db.wake_series_once(series_id, "wake:test", "snapshot-a")
    assert isolated_db.wake_series_once(series_id, "wake:test", "snapshot-b")


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
    old = {
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
        "queues": {
            "build": {"backlog": 2, "membership_complete": True, "items": [
                {"key": "issue:1"}, {"key": "issue:2"}]},
            "review": {"backlog": 0, "membership_complete": True, "items": []},
        },
    }
    isolated_db.add_pipeline_snapshot(
        "other", "owner/other", old, datetime.now(timezone.utc) - timedelta(hours=6))
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("other", [], use_cache=False)

    assert result["profile_id"] == "other"
    assert result["backlog_total"] == 3
    assert result["history"]["5h"]["complete"] is True
    assert result["history"]["5h"]["backlog_delta"] == 1
    assert result["history"]["5h"]["entered"] == 1
    assert result["history"]["5h"]["moved"] == 1
    assert result["history"]["5h"]["transitions"] == 1
    assert len(isolated_db.list_pipeline_snapshots("other")) == 2
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
