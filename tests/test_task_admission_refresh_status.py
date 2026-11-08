"""Task admission and shared queue-refresh status have separate owners."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from promptpilot import pipeline_insights as insights
from promptpilot.models import TaskCreate
from test_github_budget import _budget_costs, _limits, _profile, _running_task


def configured_profile(mode="auto"):
    profile = _profile(
        execution={"mode": mode, "stage": "review", "command": ["fixture-tool"]},
        minimum={"core": 250, "search": 2, "graphql": 100},
    )
    profile["github_budget"].update({
        "essential_snapshot_headroom": True,
        "priority_one_headroom": {"core": 1200, "search": 12, "graphql": 1000},
        "costs": _budget_costs(core=0, search=12),
    })
    profile["github_budget"]["costs"]["insights"] = {"core": 50, "search": 10, "graphql": 100}
    profile["github_budget"]["costs"]["tool_preflight"] = {"core": 600, "search": 4, "graphql": 150}
    return profile


def setup_snapshot(database, monkeypatch, profile):
    limits = {"value": _limits()}
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(insights, "_cache", {})
    def observed_limits():
        # Production's HTTP reader also persists this observation for the
        # separate zero-cost cached/dashboard view. Keep that real DB path.
        value = limits["value"]
        assert database.record_pipeline_github_rate_snapshot("github-default", value)
        return value

    monkeypatch.setattr(insights, "_github_rate_limits", observed_limits)
    monkeypatch.setattr(insights, "_run_profile_health_check", lambda *_: {
        "state": "green", "findings": [], "review_candidates": [],
    })
    monkeypatch.setattr(insights, "_github_search", lambda *_: {
        "count": 0, "items": [], "membership_complete": True,
    })
    monkeypatch.setattr(insights, "_tool_available", lambda *_args, **_kwargs: (True, "fixture"))
    result = insights.analyze("example", [], use_cache=False)
    assert result["cache"]["complete"] and not result["cache"]["stale"]
    assert database.get_pipeline_refresh_status("example")["status"] is None
    return limits, result


def reserve_running_owners(database):
    owners = []
    for index, cost in enumerate((8, 5)):
        task = _running_task(database, f"Existing budget owner {index}")
        result = database.reserve_pipeline_github_budget(
            "github-default", token=f"actual-fixture-owner-{index}", task_id=task.id,
            task_started_at=task.started_at, profile_id="example", queue_id="merge",
            route="fallback_targeted" if index == 0 else "tool",
            cost={"core": 0, "search": cost, "graphql": 0}, limits=_limits(),
            minimum_remaining={"core": 250, "search": 2, "graphql": 100},
        )
        assert result["allowed"]
        owners.append(task)
    return owners


def new_provider_task(database):
    database.create_task(TaskCreate(prompt="Example - REVIEW", recurrence="4h", priority=1))
    task = database.get_next_runnable()
    assert task is not None
    return SimpleNamespace(**{**task.model_dump(), "series_title": task.prompt})


def test_public_preflight_denial_preserves_scan_status_and_real_epoch_guard(isolated_db, monkeypatch):
    profile = configured_profile()
    limits, published = setup_snapshot(isolated_db, monkeypatch, profile)
    reserve_running_owners(isolated_db)
    task = new_provider_task(isolated_db)
    before = isolated_db.get_pipeline_refresh_status("example")
    epoch = insights._cache_epoch()
    limits["value"] = _limits(search=22)

    denied = insights.execution_route(task, task.prompt)

    assert denied["action"] == "defer"
    assert denied["github_budget"]["budget_route"] == "tool_preflight"
    assert denied["github_budget"]["reserved_other"]["search"] == 13
    assert denied["github_budget"]["requested_cost"]["search"] == 4
    assert denied["github_budget"]["effective_after"]["search"] == 5
    assert denied["github_budget"]["minimum_remaining"]["search"] == 12
    assert isolated_db.get_pipeline_refresh_status("example") == before
    assert insights._cache_epoch() == epoch

    # A real external-state invalidation remains authoritative. This test does
    # not invent a fresh epoch or reinterpret provider denial as a scan failure.
    insights._discard_cache("example")
    stale = insights.read_cached("example", [])
    assert stale["cache"]["invalidated"] is True
    assert stale["cache"]["stale"] is True
    assert stale["cache"]["token"] == published["cache"]["token"]
    assert stale["cache"].get("refresh_blocked") is None
    assert stale["github_budget"]["allowed"] is True


def test_public_successful_task_does_not_clear_actual_scan_denial(isolated_db, monkeypatch):
    profile = configured_profile("skill")
    limits, _ = setup_snapshot(isolated_db, monkeypatch, profile)
    limits["value"] = _limits(search=1)
    failed_scan = insights.analyze("example", [], use_cache=False)
    assert failed_scan["cache"]["refresh_blocked"] == "low"
    before = isolated_db.get_pipeline_refresh_status("example")
    assert before["status"]["github_budget"]["budget_route"] == "insights"
    epoch = insights._cache_epoch()
    limits["value"] = _limits()
    task = new_provider_task(isolated_db)

    allowed = insights.execution_route(task, task.prompt)

    assert allowed["action"] == "prompt"
    assert isolated_db.get_pipeline_refresh_status("example") == before
    assert insights._cache_epoch() == epoch
    assert insights.read_cached("example", [])["cache"]["refresh_blocked"] == "low"


@pytest.mark.parametrize("route", ["skill", "tool_preflight", "tool", "fallback_targeted"])
def test_legacy_task_denial_is_not_a_refresh_outcome_for_invalidated_cache(isolated_db, monkeypatch, route):
    profile = configured_profile()
    _, published = setup_snapshot(isolated_db, monkeypatch, profile)
    # Model a persisted row from the old writer, using the real versioned DB
    # publisher rather than replacing the cached read response.
    assert isolated_db.publish_pipeline_refresh_status("example", profile["repository"], {
        "refresh_blocked": "budget_in_flight", "refresh_blocked_reason": "task route denied",
        "refresh_deferred_until": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        "github_budget": {"budget_route": route},
    })
    insights._discard_cache("example")

    result = insights.read_cached("example", [])

    assert result["cache"]["complete"] is True
    assert result["cache"]["invalidated"] is True and result["cache"]["stale"] is True
    assert result["cache"]["token"] == published["cache"]["token"]
    assert result["cache"].get("refresh_blocked") is None


def test_global_pause_still_blocks_actual_refresh(isolated_db, monkeypatch):
    profile = configured_profile()
    setup_snapshot(isolated_db, monkeypatch, profile)
    isolated_db.set_setting("worker_paused", "1")

    paused = insights.analyze("example", [], use_cache=False)

    assert paused["cache"]["refresh_blocked"] == "worker_paused"


def test_early_queue_config_error_without_route_stays_durable(isolated_db, monkeypatch):
    profile = configured_profile()
    setup_snapshot(isolated_db, monkeypatch, profile)
    # An early scan-policy failure has no budget_route in its fallback
    # decision. Its actual queue caller still owns the denial publication.
    profile["github_budget"]["costs"].pop("insights")

    denied = insights.analyze("example", [], use_cache=False)

    assert denied["cache"]["refresh_blocked"] == "invalid_config"
    event = isolated_db.get_pipeline_refresh_status("example")
    assert event["status"]["refresh_blocked"] == "invalid_config"
    assert event["status"]["github_budget"].get("budget_route") is None
    cached = insights.read_cached("example", [])
    assert cached["cache"]["refresh_blocked"] == "invalid_config"
    assert cached["github_budget"]["allowed"] is False
