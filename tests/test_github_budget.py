import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import api, bot, pipeline_insights, worker
from promptpilot.models import TaskCreate


def _limits(*, core=5000, search=30, graphql=5000, reset=None):
    reset = int(reset if reset is not None else time.time() + 3600)
    return {
        "core": {"limit": 5000, "used": 5000 - core,
                 "remaining": core, "reset": reset,
                 "reset_at": datetime.fromtimestamp(reset, timezone.utc).isoformat()},
        "search": {"limit": 30, "used": 30 - search,
                   "remaining": search, "reset": reset,
                   "reset_at": datetime.fromtimestamp(reset, timezone.utc).isoformat()},
        "graphql": {"limit": 5000, "used": 5000 - graphql,
                    "remaining": graphql, "reset": reset,
                    "reset_at": datetime.fromtimestamp(reset, timezone.utc).isoformat()},
    }


def _profile(*, execution=None, minimum=None):
    queue = {
        "id": "review", "title": "Review", "capacity": 1,
        "query": "is:pr", "series_contains": "Example - REVIEW",
        "wake_when": {"field": "review_candidates"},
    }
    if execution is not None:
        queue["execution"] = execution
    return {
        "title": "Example", "repository": "owner/example",
        "github_budget": {
            "minimum_remaining": minimum or {
                "core": 500, "search": 5, "graphql": 500,
            },
            "reset_grace_seconds": 17,
            "busy_retry_seconds": 5,
            "unavailable_retry_seconds": 30,
            "lease_seconds": 30,
        },
        "queues": [queue],
    }


def test_budget_decision_defers_to_exact_latest_reset_plus_grace():
    profile = _profile()
    policy = pipeline_insights._github_budget_policy(profile)
    limits = _limits(core=10, search=1, graphql=5000, reset=1200)
    limits["search"]["reset"] = 1300

    decision = pipeline_insights._evaluate_github_budget(
        policy, limits, now=1000)

    assert decision["allowed"] is False
    assert decision["state"] == "low"
    assert decision["defer_until"] == \
        datetime.fromtimestamp(1317, timezone.utc).isoformat()
    assert {item["resource"] for item in decision["blocked_resources"]} == {
        "core", "search",
    }


def test_opt_in_default_core_floor_covers_onebase_full_workflow():
    profile = _profile()
    profile["github_budget"]["minimum_remaining"] = {}

    policy = pipeline_insights._github_budget_policy(profile)

    assert policy["minimum_remaining"]["core"] == 4000


def test_low_budget_blocks_live_analysis_before_health_or_search(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=1))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("low budget started an expensive GitHub scan")

    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    pipeline_insights._cache.clear()

    result = pipeline_insights.analyze("example", [], use_cache=False)

    assert result["cache"]["refresh_blocked"] == "low"
    assert result["cache"]["refresh_deferred_until"]
    assert result["github_rate_limit"]["core"]["remaining"] == 1
    assert result["generated_at"] is None
    assert isolated_db.list_pipeline_snapshots("example") == []
    cached = pipeline_insights.read_cached("example", [])
    assert cached["cache"]["refresh_blocked"] == "low"
    assert cached["cache"]["refresh_blocked_reason"] == \
        result["cache"]["refresh_blocked_reason"]
    assert cached["cache"]["refresh_deferred_until"] == \
        result["cache"]["refresh_deferred_until"]
    assert cached["github_rate_limit"]["core"]["remaining"] == 1


def test_unavailable_rate_limit_fails_closed_without_scan(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("missing rate_limit response started a scan")

    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)

    result = pipeline_insights.analyze("example", [], use_cache=False)

    assert result["cache"]["refresh_blocked"] == "rate_limit_unavailable"
    assert result["generated_at"] is None


@pytest.mark.parametrize("invalid", ["not-an-object", float("inf"), 1.5])
def test_invalid_budget_config_fails_closed_before_github_or_lease(
        isolated_db, monkeypatch, invalid):
    profile = _profile()
    if invalid == "not-an-object":
        profile["github_budget"]["minimum_remaining"] = invalid
    else:
        profile["github_budget"]["lease_seconds"] = invalid
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid policy queried GitHub")))
    monkeypatch.setattr(
        isolated_db, "acquire_pipeline_scan_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid policy acquired a lease")))

    result = pipeline_insights.analyze("example", [], use_cache=False)

    assert result["cache"]["refresh_blocked"] == "invalid_config"


@pytest.mark.parametrize("budget", [None, False, {"enabled": False}])
def test_legacy_or_disabled_budget_does_not_add_worker_admission_calls(
        isolated_db, monkeypatch, budget):
    profile = _profile()
    if budget is None:
        profile.pop("github_budget")
    else:
        profile["github_budget"] = budget
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy worker route queried rate limits")))
    monkeypatch.setattr(
        isolated_db, "acquire_pipeline_scan_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy worker route acquired a lease")))
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    route = pipeline_insights.execution_route(task, task.prompt)

    assert route == {
        "action": "prompt", "mode": "skill", "prompt": task.prompt,
    }


def test_successful_budgeted_scan_publishes_under_lease(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits())
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check", lambda _profile: {
            "state": "green", "findings": [], "review_candidates": [],
        })
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: {
        "count": 1, "items": [], "membership_complete": False,
    })

    result = pipeline_insights.analyze("example", [], use_cache=False)

    assert result["cache"]["source"] == "live"
    assert result["github_budget"]["state"] == "ok"
    assert result["backlog_total"] == 1
    assert len(isolated_db.list_pipeline_snapshots("example")) == 1
    acquired = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "after-success", 30)
    assert acquired["acquired"] is True
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "after-success") is True


def test_lost_lease_fences_live_cache_and_snapshot_publication(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits())
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check",
        lambda _profile: {"state": "green", "findings": [],
                          "review_candidates": []})

    def steal_lease(*_args):
        isolated_db.set_setting(
            isolated_db._pipeline_scan_lease_key("github-default"),
            json.dumps({
                "version": 1, "token": "new-owner",
                "expires_at": time.time() + 60,
            }),
        )
        return {"count": 1, "items": [], "membership_complete": False}

    monkeypatch.setattr(pipeline_insights, "_github_search", steal_lease)

    try:
        result = pipeline_insights.analyze("example", [], use_cache=False)

        assert result["cache"]["refresh_blocked"] == "lease_lost"
        assert result["generated_at"] is None
        assert isolated_db.list_pipeline_snapshots("example") == []
    finally:
        isolated_db.release_pipeline_scan_lease(
            "github-default", "new-owner")


def test_enabling_budget_keeps_last_good_cache_and_does_not_advance_revision(
        isolated_db, monkeypatch):
    legacy = _profile()
    legacy.pop("github_budget")
    current = {"profile": legacy}
    rate_limits = iter([_limits(), _limits(core=1)])
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": current["profile"]})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: next(rate_limits))
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check",
        lambda _profile: {"state": "green", "findings": [],
                          "review_candidates": []})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: {
        "count": 2, "items": [], "membership_complete": False,
    })
    pipeline_insights._cache.clear()

    fresh = pipeline_insights.analyze("example", [], use_cache=False)
    profile_hash = pipeline_insights._profile_fingerprint(legacy)
    revision_key = pipeline_insights._refresh_revision_key("example")
    revision = isolated_db.get_setting(revision_key)
    current["profile"] = _profile()

    blocked = pipeline_insights.analyze("example", [], use_cache=False)

    assert pipeline_insights._profile_fingerprint(current["profile"]) == profile_hash
    assert blocked["generated_at"] == fresh["generated_at"]
    assert blocked["backlog_total"] == 2
    assert blocked["cache"]["refresh_blocked"] == "low"
    assert isolated_db.get_setting(revision_key) == revision
    assert len(isolated_db.list_pipeline_snapshots("example")) == 1


@pytest.mark.parametrize("with_execution", [False, True])
def test_low_budget_defers_skill_and_tool_routes_without_preflight(
        isolated_db, monkeypatch, with_execution):
    execution = ({"mode": "auto", "command": ["pipeline-tool"]}
                 if with_execution else None)
    profile = _profile(execution=execution)
    reset = int(time.time()) + 120
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=1, reset=reset))
    monkeypatch.setattr(
        pipeline_insights, "_tool_available",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("low budget probed the pipeline tool")))
    monkeypatch.setattr(
        pipeline_insights, "_tool_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("low budget ran pipeline preflight")))
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    route = pipeline_insights.execution_route(task, task.prompt)

    assert route["action"] == "defer"
    assert route["mode"] == ("tool" if with_execution else "skill")
    assert route["defer_until"] == datetime.fromtimestamp(
        reset + 17, timezone.utc).isoformat()
    assert "defer_for" not in route
    cached = pipeline_insights.read_cached("example", [])
    assert cached["cache"]["refresh_blocked"] == "low"
    assert cached["cache"]["refresh_deferred_until"] == route["defer_until"]


def test_low_budget_defer_survives_refresh_status_database_error(
        isolated_db, monkeypatch):
    profile = _profile()
    reset = int(time.time()) + 120
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=1, reset=reset))
    monkeypatch.setattr(
        isolated_db, "publish_pipeline_refresh_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    route = pipeline_insights.execution_route(task, task.prompt)

    assert route["action"] == "defer"
    assert route["reason"]
    assert route["defer_until"] == datetime.fromtimestamp(
        reset + 17, timezone.utc).isoformat()


def test_cached_read_ignores_refresh_status_database_error(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        isolated_db, "get_pipeline_refresh_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))

    cached = pipeline_insights.read_cached("example", [])

    assert cached["profile_id"] == "example"
    assert cached["cache"].get("refresh_blocked") is None


def test_worker_uses_exact_budget_defer_without_loading_provider(
        isolated_db, monkeypatch):
    created = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h"))
    task = isolated_db.get_next_runnable()
    target = datetime.now(timezone.utc) + timedelta(minutes=7)
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route", lambda *_args, **_kwargs: {
            "action": "defer", "mode": "skill", "reason": "low budget",
            "defer_until": target.isoformat(),
        })
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: (_ for _ in ()).throw(
            AssertionError("deferred task loaded a provider")))

    worker._execute_task_inner(task)

    deferred = isolated_db.get_task(created.id)
    assert deferred.status.value == "pending"
    assert deferred.scheduled_at == target
    assert deferred.error == "low budget"


def test_worker_low_budget_stays_deferred_when_status_database_is_locked(
        isolated_db, monkeypatch):
    created = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h"))
    task = isolated_db.get_next_runnable()
    profile = _profile()
    reset = int(time.time()) + 120
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=1, reset=reset))
    monkeypatch.setattr(
        isolated_db, "publish_pipeline_refresh_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: (_ for _ in ()).throw(
            AssertionError("deferred task loaded a provider")))

    worker._execute_task_inner(task)

    deferred = isolated_db.get_task(created.id)
    assert deferred.status.value == "pending"
    assert deferred.scheduled_at == datetime.fromtimestamp(
        reset + 17, timezone.utc)
    assert "GitHub" in deferred.error


def test_past_exact_defer_is_clamped_instead_of_becoming_human_block():
    before = datetime.now(timezone.utc)

    deferred = worker._pipeline_defer_time({
        "defer_until": (before - timedelta(seconds=10)).isoformat(),
    })

    assert deferred is not None
    assert deferred > before


def test_productive_completion_skips_refresh_and_wake_at_low_budget(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=1))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("completion crossed the low-budget barrier")

    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    monkeypatch.setattr(pipeline_insights, "_wake_ready_queues", forbidden)
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    assert pipeline_insights.after_task_completed(task, "ГОТОВО") == []
    assert isolated_db.list_pipeline_snapshots("example") == []


def test_sampler_does_not_wake_from_budget_blocked_cache(
        isolated_db, monkeypatch):
    profile = {**_profile(), "always_sample": True}
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=1))
    monkeypatch.setattr(
        pipeline_insights, "_wake_ready_queues",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sampler woke from blocked cache")))

    outcome = pipeline_insights.sample_active_profiles([])

    assert outcome == {"example": "deferred: low"}
    cached = pipeline_insights.read_cached("example", [])
    assert cached["cache"]["refresh_blocked"] == "low"
    assert "GitHub scan отложен:" in bot._pipeline_text(cached)
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=status))
    asyncio.run(bot._send_pipeline_insights(message, "example"))
    rendered = status.edit_text.await_args.args[0]
    assert "GitHub scan отложен:" in rendered
    assert cached["cache"]["refresh_deferred_until"] in rendered


def test_successful_refresh_clears_persisted_budget_denial(
        isolated_db, monkeypatch):
    profile = _profile()
    current_limits = {"value": _limits(core=1)}
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: current_limits["value"])
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check",
        lambda _profile: {"state": "green", "findings": [],
                          "review_candidates": []})
    monkeypatch.setattr(pipeline_insights, "_github_search", lambda *_args: {
        "count": 3, "items": [], "membership_complete": False,
    })

    denied = pipeline_insights.analyze("example", [], use_cache=False)
    assert denied["cache"]["refresh_blocked"] == "low"
    assert pipeline_insights.read_cached(
        "example", [])["cache"]["refresh_blocked"] == "low"

    current_limits["value"] = _limits()
    refreshed = pipeline_insights.analyze("example", [], use_cache=False)
    cached = pipeline_insights.read_cached("example", [])

    assert refreshed["cache"].get("refresh_blocked") is None
    assert cached["cache"].get("refresh_blocked") is None
    assert cached["backlog_total"] == 3
    status = isolated_db.get_pipeline_refresh_status("example")
    assert status is not None
    assert status["status"] is None


def test_disabling_opt_in_hides_persisted_budget_overlay(
        isolated_db, monkeypatch):
    current = {"profile": _profile()}
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": current["profile"]})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits(core=1))

    denied = pipeline_insights.analyze("example", [], use_cache=False)
    assert denied["cache"]["refresh_blocked"] == "low"
    current["profile"] = {**current["profile"], "github_budget": False}

    cached = pipeline_insights.read_cached("example", [])

    assert cached["cache"].get("refresh_blocked") is None


def test_sqlite_scan_lease_serializes_concurrent_connections(isolated_db):
    barrier = threading.Barrier(12)

    def acquire(index):
        barrier.wait()
        return index, isolated_db.acquire_pipeline_scan_lease(
            "github-default", f"owner-{index}", 60)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(acquire, range(12)))

    winners = [(index, result) for index, result in results
               if result["acquired"]]
    assert len(winners) == 1
    winner = winners[0][0]
    assert all(result["state"] == "busy" for index, result in results
               if index != winner)
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", f"owner-{winner}") is True


def test_sqlite_scan_lease_is_visible_to_another_process(isolated_db):
    acquired = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "parent", 60)
    assert acquired["acquired"] is True
    script = (
        "import json; from promptpilot import db; db.init_db(); "
        "print(json.dumps(db.acquire_pipeline_scan_lease("
        "'github-default','child',60)))"
    )
    env = os.environ.copy()
    env["PP_DATA_DIR"] = str(isolated_db.DB_DIR)
    try:
        child = subprocess.run(
            [sys.executable, "-c", script], cwd=os.getcwd(), env=env,
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="strict", timeout=30,
        )
        assert json.loads(child.stdout)["state"] == "busy"
    finally:
        isolated_db.release_pipeline_scan_lease(
            "github-default", "parent")


def test_stale_or_corrupt_scan_lease_is_recovered_and_old_owner_is_fenced(
        isolated_db):
    first = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "old", 10, now=100)
    assert first["acquired"] is True
    assert isolated_db.acquire_pipeline_scan_lease(
        "github-default", "new", 10, now=105)["state"] == "busy"
    assert isolated_db.renew_pipeline_scan_lease(
        "github-default", "old", 10, now=111) is None
    assert isolated_db.acquire_pipeline_scan_lease(
        "github-default", "new", 10, now=111)["acquired"] is True
    assert isolated_db.renew_pipeline_scan_lease(
        "github-default", "old", 10, now=112) is None
    assert isolated_db.set_setting_if_newer_revision(
        "result", "value", revision_key="revision", revision=1,
        guard_key="epoch", expected_guard="0", guard_default="0",
        lease_guard={"scope": "github-default", "token": "old"},
    ) is False
    assert isolated_db.add_pipeline_snapshot(
        "example", "owner/example", {"queues": {}},
        lease_guard={"scope": "github-default", "token": "old"},
    ) is None
    assert isolated_db.list_pipeline_snapshots("example") == []
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "old") is False
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "new") is True

    isolated_db.set_setting(
        isolated_db._pipeline_scan_lease_key("github-default"), "not-json")
    recovered = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "recovered", 10, now=200)
    assert recovered["acquired"] is True
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "recovered") is True
    with pytest.raises(ValueError, match="ttl"):
        isolated_db.acquire_pipeline_scan_lease(
            "github-default", "infinite", float("inf"))


@pytest.mark.parametrize("corrupt_token", [None, {}, "", "x" * 257])
def test_corrupt_persisted_lease_token_is_recovered_immediately(
        isolated_db, corrupt_token):
    isolated_db.set_setting(
        isolated_db._pipeline_scan_lease_key("github-default"),
        json.dumps({
            "version": 1, "token": corrupt_token,
            "expires_at": time.time() + 60,
        }),
    )

    recovered = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "valid-owner", 30)

    assert recovered["acquired"] is True
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "valid-owner") is True


@pytest.mark.parametrize("invalid_token", [None, {}, "", "x" * 257])
def test_new_lease_token_must_be_bounded_string(isolated_db, invalid_token):
    with pytest.raises(ValueError, match="token"):
        isolated_db.acquire_pipeline_scan_lease(
            "github-default", invalid_token, 30)
    assert isolated_db.renew_pipeline_scan_lease(
        "github-default", invalid_token, 30) is None
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", invalid_token) is False


def test_refresh_status_tombstone_wins_over_delayed_older_denial(isolated_db):
    blocked = {
        "refresh_blocked": "scan_in_progress",
        "refresh_blocked_reason": "busy",
        "refresh_deferred_until": (
            datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    }
    assert isolated_db.publish_pipeline_refresh_status(
        "example", "owner/example", blocked) is True
    blocked_revision = isolated_db.get_pipeline_refresh_status(
        "example")["revision"]
    assert isolated_db.publish_pipeline_refresh_status(
        "example", "owner/example", None) is True

    assert isolated_db.publish_pipeline_refresh_status(
        "example", "owner/example", blocked,
        revision=blocked_revision) is False
    assert isolated_db.get_pipeline_refresh_status("example")["status"] is None


def test_lease_release_linearizes_status_clear_before_next_owner(isolated_db):
    owner = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "owner", 30)
    contender = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "contender", 30)
    assert contender["state"] == "busy"

    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "owner", refresh_status={
            "profile_id": "example", "repository": "owner/example",
            "status": None,
        }) is True
    blocked = {
        "refresh_blocked": "scan_in_progress",
        "refresh_blocked_reason": "busy",
        "refresh_deferred_until": (
            datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    }
    assert isolated_db.publish_pipeline_refresh_status(
        "example", "owner/example", blocked,
        revision=contender["status_revision"]) is False
    assert isolated_db.get_pipeline_refresh_status("example")["status"] is None

    successor = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "successor", 30)
    assert isolated_db.publish_pipeline_refresh_status(
        "example", "owner/example", blocked,
        revision=successor["status_revision"],
        lease_guard={"scope": "github-default", "token": "successor"},
    ) is True
    assert isolated_db.get_pipeline_refresh_status(
        "example")["status"]["refresh_blocked"] == "scan_in_progress"
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "successor") is True


def test_refresh_revision_order_ignores_frozen_wall_clock(
        isolated_db, monkeypatch):
    monkeypatch.setattr(isolated_db.time, "time", lambda: 1000.0)
    monkeypatch.setattr(isolated_db.time, "time_ns", lambda: 7)
    owner = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "owner", 30)
    contender = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "contender", 30)
    assert owner["status_revision"] < contender["status_revision"]

    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "owner", refresh_status={
            "profile_id": "example", "repository": "owner/example",
            "status": None,
        }) is True
    cleared = isolated_db.get_pipeline_refresh_status("example")
    assert cleared["revision"] > contender["status_revision"]


def test_refresh_revision_order_survives_wall_clock_rollback(
        isolated_db, monkeypatch):
    clock = {"value": 1000.0}
    monkeypatch.setattr(
        isolated_db.time, "time", lambda: clock["value"])
    monkeypatch.setattr(
        isolated_db.time, "time_ns", lambda: int(clock["value"] * 10**9))
    owner = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "owner", 30)
    clock["value"] = 1001.0
    contender = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "contender", 30)
    clock["value"] = 10.0

    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "owner", refresh_status={
            "profile_id": "example", "repository": "owner/example",
            "status": None,
        }) is True
    successor = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "successor", 30)

    assert owner["status_revision"] < contender["status_revision"]
    assert contender["status_revision"] < \
        isolated_db.get_pipeline_refresh_status("example")["revision"]
    assert isolated_db.get_pipeline_refresh_status(
        "example")["revision"] < successor["status_revision"]
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "successor") is True


def test_dispatch_gate_ignores_last_good_while_refresh_is_blocked(monkeypatch):
    profile = _profile()
    profile["queues"][0]["dispatch_gate"] = {"skip_when_empty": True}
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "list_series", lambda: [])
    monkeypatch.setattr(
        pipeline_insights, "read_cached", lambda *_args, **_kwargs: {
            "cache": {"complete": True, "stale": False,
                      "refresh_blocked": "low"},
            "queues": [{"id": "review", "title": "Review", "backlog": 0}],
            "diagnostics": {},
        })
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    assert pipeline_insights.dispatch_gate(task) is None


def test_pause_during_scan_releases_lease(isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits())

    def pause_during_health(_profile):
        isolated_db.set_setting("worker_paused", "1")
        return {"state": "green", "findings": [], "review_candidates": []}

    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check", pause_during_health)
    monkeypatch.setattr(
        pipeline_insights, "_github_search",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("paused scan continued to GitHub search")))

    try:
        result = pipeline_insights.analyze("example", [], use_cache=False)
        assert result["cache"]["refresh_blocked"] == "worker_paused"
        acquired = isolated_db.acquire_pipeline_scan_lease(
            "github-default", "after-pause", 30)
        assert acquired["acquired"] is True
        assert isolated_db.release_pipeline_scan_lease(
            "github-default", "after-pause") is True
    finally:
        isolated_db.set_setting("worker_paused", "0")


def test_malformed_rate_limit_response_is_treated_as_unavailable(monkeypatch):
    monkeypatch.setattr(pipeline_insights, "_gh_api_json", lambda *_args: {
        "resources": {
            "core": {"limit": 5000, "used": 1, "remaining": 4999,
                     "reset": "not-an-epoch"},
            "search": {"limit": 30, "used": 0, "remaining": 30,
                       "reset": 2000},
            "graphql": {"limit": 5000, "used": 0, "remaining": 5000,
                        "reset": 2000},
        },
    })

    assert pipeline_insights._github_rate_limits() is None


def test_scan_exception_releases_lease(isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits())
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check", lambda _profile: None)
    monkeypatch.setattr(
        pipeline_insights, "_github_search",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("scan failed")))

    with pytest.raises(RuntimeError, match="scan failed"):
        pipeline_insights.analyze("example", [], use_cache=False)

    acquired = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "after-error", 30)
    assert acquired["acquired"] is True
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "after-error") is True


def test_busy_global_lease_blocks_different_profile_without_rate_check(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"second": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: (_ for _ in ()).throw(
            AssertionError("busy contender queried GitHub /rate_limit")))
    owner = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "other-profile", 60)
    assert owner["acquired"] is True
    try:
        result = pipeline_insights.analyze("second", [], use_cache=False)
        assert result["cache"]["refresh_blocked"] == "scan_in_progress"
        assert isolated_db.list_pipeline_snapshots("second") == []
    finally:
        isolated_db.release_pipeline_scan_lease(
            "github-default", "other-profile")


def test_cached_api_and_bot_reads_do_not_touch_github(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"cached": profile})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cached endpoint touched GitHub")

    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check", forbidden)
    monkeypatch.setattr(pipeline_insights, "_tool_preflight", forbidden)
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=status))

    web = api.api_pipeline_insights("cached", refresh=False)
    asyncio.run(bot._send_pipeline_insights(message, "cached"))

    assert web["cache"]["source"] == "none"
    status.edit_text.assert_awaited_once()


def test_blocked_cache_can_never_wake_a_series(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipeline_insights.db, "wake_series_once",
        lambda *_args, **_kwargs: calls.append("wake") or True)

    result = pipeline_insights._wake_ready_queues(
        "example",
        {"queues": [{"id": "review", "series_contains": "REVIEW",
                     "wake_when": {"field": "review_candidates"}}]},
        {"cache": {"complete": True, "stale": False,
                   "refresh_blocked": "low"},
         "diagnostics": {"review_candidates": [{"number": 1}]}},
        [{"id": 1, "title": "REVIEW", "paused": False, "ended": False}],
    )

    assert result == []
    assert calls == []
