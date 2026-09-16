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
from pathlib import Path
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


def _budget_costs(*, core=500, search=0, graphql=0):
    return {
        route: {"core": core, "search": search, "graphql": graphql}
        for route in pipeline_insights._GITHUB_BUDGET_ROUTES
    }


def _profile_with_costs(*, execution=None, core=500):
    profile = _profile(
        execution=execution,
        minimum={"core": 100, "search": 0, "graphql": 0},
    )
    profile["github_budget"]["costs"] = _budget_costs(core=core)
    return profile


def _running_task(database, prompt):
    database.create_task(TaskCreate(prompt=prompt, recurrence="4h"))
    task = database.get_next_runnable()
    assert task is not None
    assert task.started_at is not None
    return task


def _reserve(database, task, token, *, core=500, remaining=1000,
             reset=None):
    return database.reserve_pipeline_github_budget(
        "github-default", token=token, task_id=task.id,
        task_started_at=task.started_at, profile_id="example",
        queue_id="review", route="skill",
        cost={"core": core, "search": 0, "graphql": 0},
        limits=_limits(core=remaining, reset=reset),
        minimum_remaining={"core": 100, "search": 0, "graphql": 0},
    )


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


def test_projected_budget_clamps_display_but_keeps_signed_admission_value():
    profile = _profile()
    policy = pipeline_insights._github_budget_policy(profile)

    decision = pipeline_insights._evaluate_github_budget(
        policy, _limits(core=10), now=1000,
        reserved_other={"core": 20, "search": 0, "graphql": 0})

    assert decision["allowed"] is False
    assert decision["state"] == "low"
    assert decision["effective_after"]["core"] == -10
    assert decision["projected_post_reservation"]["core"] == {
        "available": 0,
        "deficit": 10,
        "hard_reserve": 500,
        "headroom_above_hard_reserve": 0,
        "shortfall_to_hard_reserve": 510,
    }


def test_cost_schema_requires_every_known_route_and_exact_integer_vectors():
    profile = _profile_with_costs()

    policy = pipeline_insights._github_budget_policy(profile)

    assert set(policy["costs"]) == set(pipeline_insights._GITHUB_BUDGET_ROUTES)
    assert policy["costs"]["skill"] == {
        "core": 500, "search": 0, "graphql": 0,
    }


@pytest.mark.parametrize(
    "case",
    [
        "costs_not_object", "missing_route", "unknown_route",
        "route_not_object", "missing_resource", "unknown_resource",
        "bool", "string", "fraction", "negative",
    ],
)
def test_cost_schema_fails_closed_on_partial_or_non_integer_values(case):
    profile = _profile_with_costs()
    costs = profile["github_budget"]["costs"]
    if case == "costs_not_object":
        profile["github_budget"]["costs"] = []
    elif case == "missing_route":
        costs.pop("tool")
    elif case == "unknown_route":
        costs["other"] = {"core": 1, "search": 0, "graphql": 0}
    elif case == "route_not_object":
        costs["skill"] = []
    elif case == "missing_resource":
        costs["skill"].pop("search")
    elif case == "unknown_resource":
        costs["skill"]["other"] = 0
    elif case == "bool":
        costs["skill"]["core"] = True
    elif case == "string":
        costs["skill"]["core"] = "500"
    elif case == "fraction":
        costs["skill"]["core"] = 500.0
    elif case == "negative":
        costs["skill"]["core"] = -1

    with pytest.raises(ValueError):
        pipeline_insights._github_budget_policy(profile)


def test_shared_github_scope_uses_strongest_profile_hard_reserve(
        isolated_db, monkeypatch):
    low = _profile_with_costs()
    high = _profile_with_costs()
    high["github_budget"]["minimum_remaining"] = {
        "core": 900, "search": 3, "graphql": 700,
    }
    monkeypatch.setattr(
        pipeline_insights, "_profiles",
        lambda: {"low": low, "high": high})

    policy = pipeline_insights._with_shared_budget_floor(
        pipeline_insights._github_budget_policy(low))

    assert policy["minimum_remaining"] == {
        "core": 900, "search": 3, "graphql": 700,
    }
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=1000, search=30, graphql=5000))
    with pipeline_insights._github_scan_admission(
            low, "shared floor test", profile_id="low",
            budget_route="skill") as admission:
        assert admission["allowed"] is False
        assert admission["state"] == "low"
        assert admission["minimum_remaining"] == {
            "core": 900, "search": 3, "graphql": 700,
        }
    cached = pipeline_insights.read_cached("low", [])
    assert cached["github_budget"]["minimum_remaining"] == {
        "core": 900, "search": 3, "graphql": 700,
    }


def test_opt_in_default_core_floor_covers_onebase_full_workflow():
    profile = _profile()
    profile["github_budget"]["minimum_remaining"] = {}

    policy = pipeline_insights._github_budget_policy(profile)

    assert policy["minimum_remaining"]["core"] == 4000


def test_low_budget_blocks_live_analysis_before_health_or_search(
        isolated_db, monkeypatch):
    profile = _profile()
    assert isolated_db.record_pipeline_github_rate_snapshot(
        "github-default", _limits(core=1))
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
        "profile_id": "example", "queue_id": "review",
    }


def test_legacy_budget_without_costs_keeps_floor_check_but_never_reserves(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits(core=500))
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    route = pipeline_insights.execution_route(
        task, task.prompt, retain_budget=True)

    assert route == {
        "action": "prompt", "mode": "skill", "prompt": task.prompt,
        "profile_id": "example", "queue_id": "review",
    }
    assert isolated_db.pipeline_github_budget_reservations(
        "github-default") == {
            "count": 0,
            "totals": {"core": 0, "search": 0, "graphql": 0},
            "items": [],
        }


def test_two_running_tasks_atomically_compete_for_one_budget_reservation(
        isolated_db):
    first = _running_task(isolated_db, "Atomic first")
    second = _running_task(isolated_db, "Atomic second")
    barrier = threading.Barrier(2)

    def reserve(task, token):
        barrier.wait()
        return token, _reserve(
            isolated_db, task, token, core=600, remaining=1000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda pair: reserve(*pair),
            [(first, "first-owner"), (second, "second-owner")],
        ))

    winners = [(token, result) for token, result in results
               if result["allowed"]]
    denied = [result for _token, result in results if not result["allowed"]]
    assert len(winners) == 1
    assert len(denied) == 1
    assert winners[0][1]["state"] == "reserved"
    assert winners[0][1]["active_reservations"] == 1
    assert denied[0]["state"] == "budget_in_flight"
    assert denied[0]["active_reservations"] == 1
    assert denied[0]["blocked_resources"][0]["blocked_by"] == "reservation"

    ledger = isolated_db.pipeline_github_budget_reservations("github-default")
    assert ledger["count"] == 1
    assert ledger["totals"] == {"core": 600, "search": 0, "graphql": 0}
    winner_token = winners[0][0]
    winner_task = first if winner_token == "first-owner" else second
    assert isolated_db.release_pipeline_github_budget(
        "github-default", token=winner_token, task_id=winner_task.id,
        task_started_at=winner_task.started_at) is True


def test_reservation_is_fenced_to_exact_running_attempt_and_prunes_stale_rows(
        isolated_db):
    first_attempt = _running_task(isolated_db, "Attempt fencing")
    wrong_started_at = first_attempt.started_at + timedelta(microseconds=1)

    denied = isolated_db.reserve_pipeline_github_budget(
        "github-default", token="wrong-attempt", task_id=first_attempt.id,
        task_started_at=wrong_started_at, profile_id="example",
        queue_id="review", route="skill",
        cost={"core": 500, "search": 0, "graphql": 0},
        limits=_limits(core=1000),
        minimum_remaining={"core": 100, "search": 0, "graphql": 0},
    )
    assert denied["allowed"] is False
    assert denied["state"] == "task_fence_lost"

    reserved = _reserve(
        isolated_db, first_attempt, "first-attempt", remaining=1000)
    assert reserved["allowed"] is True
    assert isolated_db.release_pipeline_github_budget(
        "github-default", token="first-attempt", task_id=first_attempt.id,
        task_started_at=wrong_started_at) is False
    assert isolated_db.pipeline_github_budget_reservations(
        "github-default")["count"] == 1

    assert isolated_db.reset_task(first_attempt.id) is True
    assert isolated_db.pipeline_github_budget_reservations(
        "github-default")["count"] == 0
    second_attempt = isolated_db.get_next_runnable()
    assert second_attempt is not None
    assert second_attempt.id == first_attempt.id
    assert second_attempt.started_at != first_attempt.started_at
    assert isolated_db.release_pipeline_github_budget(
        "github-default", token="first-attempt", task_id=first_attempt.id,
        task_started_at=first_attempt.started_at) is False

    assert _reserve(
        isolated_db, second_attempt, "second-attempt",
        remaining=1000)["allowed"] is True
    isolated_db.mark_completed(second_attempt.id, "done")
    assert isolated_db.release_pipeline_github_budget(
        "github-default", token="second-attempt", task_id=second_attempt.id,
        task_started_at=second_attempt.started_at) is True
    assert isolated_db.pipeline_github_budget_reservations(
        "github-default")["count"] == 0


def test_execution_route_retains_and_explicitly_releases_provider_budget(
        isolated_db, monkeypatch):
    profile = _profile_with_costs(core=200)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits(core=1000))
    task = _running_task(isolated_db, "Example - REVIEW")
    pipeline_insights.release_execution_admission()

    try:
        route = pipeline_insights.execution_route(
            task, task.prompt, retain_budget=True)

        assert route["action"] == "prompt"
        assert route["mode"] == "skill"
        ledger = isolated_db.pipeline_github_budget_reservations(
            "github-default")
        assert ledger["count"] == 1
        assert ledger["totals"] == {
            "core": 200, "search": 0, "graphql": 0,
        }
        assert ledger["items"][0]["task_id"] == task.id
        assert ledger["items"][0]["route"] == "skill"
        assert ledger["items"][0]["queue_id"] == "review"
    finally:
        pipeline_insights.release_execution_admission()

    assert isolated_db.pipeline_github_budget_reservations(
        "github-default")["count"] == 0
    pipeline_insights.release_execution_admission()


def test_reservation_contention_retries_quickly_but_live_low_waits_for_reset(
        isolated_db, monkeypatch):
    profile = _profile_with_costs(core=500)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    owner = _running_task(isolated_db, "Budget owner")
    contender = _running_task(isolated_db, "Example - REVIEW")
    reset = int(time.time()) + 120
    current_core = {"value": 1000}
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=current_core["value"], reset=reset))
    assert _reserve(
        isolated_db, owner, "budget-owner", core=500,
        remaining=1000, reset=reset)["allowed"] is True

    before = datetime.now(timezone.utc)
    contention = pipeline_insights.execution_route(
        contender, contender.prompt, retain_budget=True)
    contention_until = datetime.fromisoformat(contention["defer_until"])

    assert contention["action"] == "defer"
    assert contention["github_budget"]["state"] == "budget_in_flight"
    assert contention["github_budget"]["active_reservations"] == 1
    assert timedelta(0) < contention_until - before <= timedelta(seconds=10)

    assert isolated_db.release_pipeline_github_budget(
        "github-default", token="budget-owner", task_id=owner.id,
        task_started_at=owner.started_at) is True
    current_core["value"] = 500
    genuinely_low = pipeline_insights.execution_route(
        contender, contender.prompt, retain_budget=True)

    assert genuinely_low["action"] == "defer"
    assert genuinely_low["github_budget"]["state"] == "low"
    assert genuinely_low["defer_until"] == datetime.fromtimestamp(
        reset + 17, timezone.utc).isoformat()
    assert isolated_db.pipeline_github_budget_reservations(
        "github-default")["count"] == 0


def test_cached_dashboard_overlays_latest_rate_snapshot_and_live_reservations(
        isolated_db, monkeypatch):
    profile = _profile_with_costs(core=200)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    observed_at = time.time() - 3
    assert isolated_db.record_pipeline_github_rate_snapshot(
        "github-default", _limits(core=1000), observed_at=observed_at)
    owner = _running_task(isolated_db, "Budget owner for dashboard")
    assert _reserve(
        isolated_db, owner, "dashboard-owner", core=200,
        remaining=1000)["allowed"] is True

    result = pipeline_insights.read_cached("example", [])

    assert result["github_rate_limit"]["core"]["remaining"] == 1000
    assert datetime.fromisoformat(
        result["github_rate_limit_observed_at"]).timestamp() == pytest.approx(
            observed_at)
    assert result["github_budget"]["active_reservations"] == 1
    assert result["github_budget"]["reserved_in_flight"]["core"] == 200
    assert result["github_budget"]["spendable_before_route"]["core"] == 700
    assert result["github_budget"]["projected_post_reservation"]["core"] == {
        "available": 800,
        "deficit": 0,
        "hard_reserve": 100,
        "headroom_above_hard_reserve": 700,
        "shortfall_to_hard_reserve": 0,
    }
    text = bot._pipeline_text(result)
    assert "прогноз после резервирования (не GitHub remaining)" in text
    assert "доступно 800, дефицит 0" in text


@pytest.mark.parametrize("stale_state", ["budget_in_flight", "low"])
def test_cached_dashboard_recomputes_stale_denial_from_current_local_state(
        isolated_db, monkeypatch, stale_state):
    profile = _profile_with_costs(core=200)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    observed_at = time.time() - 2
    assert isolated_db.record_pipeline_github_rate_snapshot(
        "github-default", _limits(core=1000), observed_at=observed_at)
    stale_budget = {
        "enabled": True, "allowed": False, "state": stale_state,
        "reason": "stale cached denial",
        "defer_until": "2099-01-01T00:00:00+00:00",
        "active_reservations": 7,
        "reserved_in_flight": {"core": 999, "search": 0, "graphql": 0},
        "projected_post_reservation": {
            "core": {"available": 1, "deficit": 0},
        },
        "ledger_state": "unavailable", "ledger_reason": "stale ledger error",
        "rate_snapshot_state": "unavailable",
        "rate_snapshot_reason": "stale snapshot error",
    }
    assert isolated_db.publish_pipeline_refresh_status(
        "example", "owner/example", {
            "refresh_blocked": stale_state,
            "refresh_blocked_reason": "stale cached denial",
            "refresh_deferred_until": "2099-01-01T00:00:00+00:00",
            "github_rate_limit": _limits(core=1),
            "github_budget": stale_budget,
        })

    result = pipeline_insights.read_cached("example", [])
    budget = result["github_budget"]

    # The cache notice is historical (the last refresh was deferred), while
    # the budget object is an explicitly zero-cost, before-route live summary.
    assert result["cache"]["refresh_blocked"] == stale_state
    assert budget["allowed"] is True
    assert budget["state"] == "ok"
    assert budget["reason"] != "stale cached denial"
    assert budget["defer_until"] is None
    assert budget["active_reservations"] == 0
    assert budget["reserved_in_flight"] == {
        "core": 0, "search": 0, "graphql": 0,
    }
    assert budget["requested_cost"] == {
        "core": 0, "search": 0, "graphql": 0,
    }
    assert budget["budget_route"] is None
    assert budget["effective_after"]["core"] == 1000
    assert budget["ledger_state"] == "ok"
    assert budget["rate_snapshot_state"] == "ok"
    assert "ledger_reason" not in budget
    assert "rate_snapshot_reason" not in budget
    assert result["github_rate_limit"]["core"]["remaining"] == 1000
    assert datetime.fromisoformat(
        result["github_rate_limit_observed_at"]).timestamp() == pytest.approx(
            observed_at)
    assert budget["projected_post_reservation"]["core"]["available"] == 1000
    assert budget["spendable_before_route"]["core"] == 900


def test_cached_dashboard_fails_closed_when_rate_snapshot_is_unavailable(
        isolated_db, monkeypatch):
    profile = _profile_with_costs(core=200)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        isolated_db, "get_pipeline_github_rate_snapshot",
        lambda _scope: (_ for _ in ()).throw(
            sqlite3.OperationalError("snapshot database is locked")))
    cached = {
        "github_rate_limit": _limits(core=1000),
        "github_rate_limit_observed_at": "2000-01-01T00:00:00+00:00",
        "github_budget": {
            "enabled": True, "allowed": True, "state": "ok",
            "reason": "stale success",
            "projected_post_reservation": {"core": {"available": 1000}},
            "spendable_before_route": {"core": 900},
        },
    }

    result = pipeline_insights._with_live_github_budget_state(cached, profile)
    budget = result["github_budget"]

    assert budget["allowed"] is False
    assert budget["state"] == "rate_limit_unavailable"
    assert budget["rate_snapshot_state"] == "unavailable"
    assert "snapshot database is locked" in budget["rate_snapshot_reason"]
    assert budget["ledger_state"] == "ok"
    assert result["github_rate_limit"] is None
    assert result["github_rate_limit_observed_at"] is None
    assert "projected_post_reservation" not in budget
    assert "spendable_before_route" not in budget


def test_cached_dashboard_fails_closed_when_reservation_ledger_is_unavailable(
        isolated_db, monkeypatch):
    profile = _profile_with_costs(core=200)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    assert isolated_db.record_pipeline_github_rate_snapshot(
        "github-default", _limits(core=1000))
    monkeypatch.setattr(
        isolated_db, "pipeline_github_budget_reservations",
        lambda _scope: (_ for _ in ()).throw(
            sqlite3.OperationalError("ledger database is locked")))

    result = pipeline_insights.read_cached("example", [])
    budget = result["github_budget"]

    assert budget["allowed"] is False
    assert budget["state"] == "ledger_unavailable"
    assert budget["ledger_state"] == "unavailable"
    assert "ledger database is locked" in budget["ledger_reason"]
    assert budget["rate_snapshot_state"] == "ok"
    assert result["github_rate_limit"]["core"]["remaining"] == 1000
    assert "active_reservations" not in budget
    assert "reserved_in_flight" not in budget
    assert "projected_post_reservation" not in budget
    assert "spendable_before_route" not in budget
    text = bot._pipeline_text(result)
    assert "активных резервов —" in text
    assert "REST зарезервировано —" in text


def test_web_dashboard_labels_projection_as_non_actual_github_remaining():
    html = (Path(__file__).parents[1] / "promptpilot" / "static" /
            "index.html").read_text(encoding="utf-8")

    assert "projected_post_reservation" in html
    assert "прогноз после активных резервов (не фактический GitHub remaining)" \
        in html
    assert "дефицит ${projectedNumber('core', 'deficit')}" in html
    assert "const ledgerKnown = githubBudgetState.ledger_state !== 'unavailable'" \
        in html
    assert "ledgerKnown ? budgetNumber(reservedBudget, 'core') : '—'" in html


def test_success_completion_uses_local_successor_graph_without_github_scan(
        isolated_db, monkeypatch):
    profile = _profile_with_costs()
    profile["queues"][0]["wake_after_success"] = ["merge"]
    profile["queues"].append({
        "id": "merge", "title": "Merge", "query": "is:pr label:ship",
        "series_contains": "Example - MERGE",
        "execution": {
            "mode": "auto", "command": ["pipelinectl", "next", "{stage}"],
        },
    })
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "is_paused", lambda: False)
    monkeypatch.setattr(pipeline_insights.db, "list_series", lambda: [{
        "id": 42, "title": "Example - MERGE", "paused": False,
        "ended": False, "ended_at": None,
    }])
    actions = []
    monkeypatch.setattr(
        pipeline_insights.db, "request_pipeline_series_wake",
        lambda series_id: actions.append(series_id) or {
            "accepted": True, "state": "scheduled",
        })
    invalidations = []
    monkeypatch.setattr(
        pipeline_insights, "_discard_cache",
        lambda profile_id: invalidations.append(profile_id))
    monkeypatch.setattr(
        pipeline_insights, "analyze",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("configured successor wake performed a GitHub scan")))
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    assert pipeline_insights.after_task_completed(task, "ГОТОВО") == ["merge"]
    assert actions == [42]
    assert invalidations == ["example"]


def test_success_completion_durably_marks_pre_completion_cache_stale(
        isolated_db, monkeypatch):
    execution = {
        "mode": "auto", "command": ["pipelinectl", "next", "{stage}"],
    }
    profile = _profile_with_costs(execution=execution)
    profile["queues"][0]["wake_after_success"] = ["merge"]
    profile["queues"].append({
        "id": "merge", "title": "Merge", "capacity": 1,
        "query": "is:pr label:ship", "series_contains": "Example - MERGE",
        "execution": execution,
    })
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits())
    monkeypatch.setattr(
        pipeline_insights, "_run_profile_health_check",
        lambda _profile: {"state": "green", "findings": []})
    monkeypatch.setattr(
        pipeline_insights, "_github_search",
        lambda *_args: {"count": 0, "items": [],
                        "membership_complete": True})
    isolated_db.create_task(TaskCreate(
        prompt="Example - MERGE", recurrence="4h",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=4)))
    series = isolated_db.list_series()
    fresh = pipeline_insights.analyze("example", series, use_cache=False)
    assert fresh["cache"]["stale"] is False
    task = SimpleNamespace(
        series_id=999, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    assert pipeline_insights.after_task_completed(
        task, "ГОТОВО") == ["merge"]
    pipeline_insights._cache.clear()
    cached = pipeline_insights.read_cached("example", isolated_db.list_series())

    assert cached["cache"]["source"] == "durable"
    assert cached["cache"]["invalidated"] is True
    assert cached["cache"]["stale"] is True


def test_successor_graph_rejects_queue_without_fresh_project_preflight(
        isolated_db, monkeypatch):
    profile = _profile_with_costs()
    profile["queues"][0]["wake_after_success"] = ["fix"]
    profile["queues"].append({
        "id": "fix", "title": "Fix", "query": "is:issue label:approved",
        "series_contains": "Example - FIX",
    })
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "is_paused", lambda: False)
    monkeypatch.setattr(pipeline_insights.db, "list_series", lambda: [{
        "id": 42, "title": "Example - FIX", "paused": False,
        "ended": False, "ended_at": None,
    }])
    invalidations = []
    monkeypatch.setattr(
        pipeline_insights, "_discard_cache",
        lambda profile_id: invalidations.append(profile_id))
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    with pytest.raises(ValueError, match="project execution preflight"):
        pipeline_insights.after_task_completed(task, "ГОТОВО")
    assert invalidations == ["example"]


def test_successor_graph_self_wake_moves_created_recurrence_to_now(
        isolated_db, monkeypatch):
    profile = _profile_with_costs(execution={
        "mode": "auto", "command": ["pipelinectl", "next", "{stage}"],
    })
    profile["queues"][0]["wake_after_success"] = ["review"]
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    task = _running_task(isolated_db, "Example - REVIEW")
    isolated_db.mark_completed(task.id, "ИТОГ: ГОТОВО (reviewed)")
    worker._recur_after_run(task)
    future = next(
        item for item in isolated_db.list_tasks(limit=10)
        if item.series_id == task.series_id and item.status.value == "pending")
    assert future.scheduled_at > datetime.now(timezone.utc) + timedelta(hours=3)

    woken = pipeline_insights.after_task_completed(task, "ГОТОВО")

    moved = isolated_db.get_task(future.id)
    assert woken == ["review"]
    assert moved.scheduled_at <= datetime.now(timezone.utc)


def test_successor_wake_is_durable_while_target_occurrence_is_running(
        isolated_db):
    task = _running_task(isolated_db, "Example - REVIEW")

    requested = isolated_db.request_pipeline_series_wake(task.series_id)

    assert requested == {"accepted": True, "state": "latched"}
    isolated_db.mark_completed(task.id, "ИТОГ: ПУСТО (old snapshot raced)")
    worker._recur_after_run(task)
    pending = next(
        item for item in isolated_db.list_tasks(limit=10)
        if item.series_id == task.series_id and item.status.value == "pending")
    assert pending.scheduled_at <= datetime.now(timezone.utc)
    assert isolated_db.consume_pipeline_series_wake(task.series_id) is False


def test_successor_wake_preserves_rate_limit_backoff_and_next_occurrence(
        isolated_db):
    task = _running_task(isolated_db, "Example - REVIEW")
    retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    isolated_db.mark_rate_limited(task.id, retry_at, "provider budget")

    requested = isolated_db.request_pipeline_series_wake(task.series_id)

    deferred = isolated_db.get_task(task.id)
    assert requested == {"accepted": True, "state": "latched_rate_limited"}
    assert deferred.status.value == "rate_limited"
    assert deferred.next_run_at > datetime.now(timezone.utc) + timedelta(minutes=50)

    isolated_db.mark_completed(task.id, "ИТОГ: ГОТОВО")
    worker._recur_after_run(task)
    pending = next(
        item for item in isolated_db.list_tasks(limit=10)
        if item.series_id == task.series_id and item.status.value == "pending")
    assert pending.scheduled_at <= datetime.now(timezone.utc)


def test_running_wake_survives_deterministic_pending_defer(isolated_db):
    task = _running_task(isolated_db, "Example - REVIEW")
    assert isolated_db.request_pipeline_series_wake(task.series_id) == {
        "accepted": True, "state": "latched"}
    retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    isolated_db.defer_task(task.id, retry_at, "dependency")

    worker._recur_after_run(task)

    deferred = isolated_db.get_task(task.id)
    assert deferred.status.value == "pending"
    assert deferred.scheduled_at > datetime.now(timezone.utc) + timedelta(minutes=50)
    assert isolated_db.consume_pipeline_series_wake(task.series_id) is True
    assert isolated_db.get_task(task.id).scheduled_at <= datetime.now(timezone.utc)


def test_successor_wake_repairs_active_series_without_an_occurrence(isolated_db):
    task = _running_task(isolated_db, "Example - REVIEW")
    isolated_db.mark_cancelled(task.id, "cancelled occurrence")

    requested = isolated_db.request_pipeline_series_wake(task.series_id)

    assert requested == {"accepted": True, "state": "recreated"}
    pending = [item for item in isolated_db.list_tasks(limit=10)
               if item.series_id == task.series_id
               and item.status.value == "pending"]
    assert len(pending) == 1
    assert pending[0].scheduled_at <= datetime.now(timezone.utc)


def test_successor_wake_and_recurrence_do_not_create_duplicate_occurrences(
        isolated_db):
    task = _running_task(isolated_db, "Example - REVIEW")
    isolated_db.mark_completed(task.id, "ИТОГ: ГОТОВО")

    requested = isolated_db.request_pipeline_series_wake(task.series_id)
    worker._recur_after_run(task)

    assert requested == {"accepted": True, "state": "recreated"}
    pending = [item for item in isolated_db.list_tasks(limit=10)
               if item.series_id == task.series_id
               and item.status.value == "pending"]
    assert len(pending) == 1
    assert pending[0].scheduled_at <= datetime.now(timezone.utc)


def test_startup_repairs_terminal_series_without_a_live_occurrence(isolated_db):
    task = _running_task(isolated_db, "Example - REVIEW")
    assert isolated_db.request_pipeline_series_wake(task.series_id) == {
        "accepted": True, "state": "latched"}
    isolated_db.mark_completed(task.id, "ИТОГ: ГОТОВО")

    assert isolated_db.repair_active_series_occurrences() == [task.series_id]
    assert isolated_db.repair_active_series_occurrences() == []
    assert isolated_db.consume_pipeline_series_wake(task.series_id) is False
    pending = [item for item in isolated_db.list_tasks(limit=10)
               if item.series_id == task.series_id
               and item.status.value == "pending"]
    assert len(pending) == 1
    assert pending[0].scheduled_at <= datetime.now(timezone.utc)


def test_startup_repair_preserves_generic_recurrence_without_wake(isolated_db):
    task = _running_task(isolated_db, "Generic recurring task")
    isolated_db.mark_completed(task.id, "done")

    assert isolated_db.repair_active_series_occurrences() == [task.series_id]
    pending = next(
        item for item in isolated_db.list_tasks(limit=10)
        if item.series_id == task.series_id and item.status.value == "pending")
    assert pending.scheduled_at > datetime.now(timezone.utc) + timedelta(hours=3)


def test_startup_does_not_repair_explicitly_cancelled_occurrence(isolated_db):
    task = _running_task(isolated_db, "Example - REVIEW")
    isolated_db.mark_cancelled(task.id, "operator cancelled")

    assert isolated_db.repair_active_series_occurrences() == []
    assert not [item for item in isolated_db.list_tasks(limit=10)
                if item.series_id == task.series_id
                and item.status.value in {"pending", "running", "rate_limited"}]


@pytest.mark.parametrize("body_raises", [False, True])
def test_worker_inner_always_releases_durable_provider_reservation(
        isolated_db, monkeypatch, body_raises):
    task = _running_task(isolated_db, "Example - REVIEW")
    assert _reserve(
        isolated_db, task, "worker-finally", core=200,
        remaining=1000)["allowed"] is True
    pipeline_insights.release_execution_admission()
    pipeline_insights._execution_budget_context.reservation = \
        pipeline_insights._GitHubBudgetReservation(
            "github-default", "worker-finally", task.id, task.started_at)

    def body(_task):
        if body_raises:
            raise RuntimeError("provider crashed")
        return "done"

    monkeypatch.setattr(worker, "_execute_task_body", body)
    if body_raises:
        with pytest.raises(RuntimeError, match="provider crashed"):
            worker._execute_task_inner(task)
    else:
        assert worker._execute_task_inner(task) == "done"

    assert isolated_db.pipeline_github_budget_reservations(
        "github-default")["count"] == 0
    assert getattr(
        pipeline_insights._execution_budget_context,
        "reservation", None) is None


def test_successful_budgeted_scan_publishes_under_lease(
        isolated_db, monkeypatch):
    profile = _profile()
    assert isolated_db.record_pipeline_github_rate_snapshot(
        "github-default", _limits())
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
    assert route["defer_policy"] == "hard_not_before"
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


def test_transient_lease_failure_after_admission_keeps_durable_denial(
        isolated_db, monkeypatch):
    execution = {"mode": "tool", "command": ["pipeline-tool"]}
    profile = _profile(execution=execution)
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits())
    monkeypatch.setattr(
        pipeline_insights, "_tool_available",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pipeline_insights._GitHubScanLeaseUnavailable(
                "database is temporarily locked")))
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    route = pipeline_insights.execution_route(task, task.prompt)

    assert route["action"] == "defer"
    assert route["github_budget"]["state"] == "lease_unavailable"
    event = isolated_db.get_pipeline_refresh_status("example")
    assert event["status"]["refresh_blocked"] == "lease_unavailable"
    assert pipeline_insights.read_cached(
        "example", [])["cache"]["refresh_blocked"] == "lease_unavailable"


@pytest.mark.parametrize(("failure", "expected_state"), [
    (pipeline_insights._GitHubScanLeaseUnavailable(
        "database is temporarily locked"), "lease_unavailable"),
    (pipeline_insights._GitHubScanLeaseLost(
        "scan lease ownership changed"), "lease_lost"),
])
def test_post_preflight_rate_recheck_preserves_exact_lease_failure(
        isolated_db, monkeypatch, failure, expected_state):
    execution = {"mode": "tool", "command": ["pipeline-tool"]}
    profile = _profile_with_costs(execution=execution, core=100)
    calls = []

    def rate_limits():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return _limits()
        raise failure

    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", rate_limits)
    monkeypatch.setattr(
        pipeline_insights, "_tool_available", lambda *_args: (True, ""))
    monkeypatch.setattr(
        pipeline_insights, "_tool_preflight",
        lambda *_args: {"action": "audit", "target": {"number": 42}})
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    route = pipeline_insights.execution_route(task, task.prompt)

    assert calls == [1, 2]
    assert route["action"] == "defer"
    assert route["github_budget"]["state"] == expected_state
    event = isolated_db.get_pipeline_refresh_status("example")
    assert event["status"]["refresh_blocked"] == expected_state


def test_post_preflight_budget_denial_keeps_sanitized_context(
        isolated_db, monkeypatch):
    execution = {"mode": "auto", "command": ["pipeline-tool"]}
    profile = _profile_with_costs(execution=execution, core=100)
    profile["github_budget"]["costs"]["fallback_targeted"] = {
        "core": 900, "search": 0, "graphql": 0,
    }
    preflight = {
        "action": "fallback",
        "reason": "complex\nstate",
        "target": {
            "number": 1414, "head": "a" * 40,
            "stage": "integration-review",
        },
        "handoff": {"lease": "TOP-SECRET-SIGNED-LEASE"},
        "extra": {"must_not": "persist"},
    }
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits",
        lambda: _limits(core=950, search=30, graphql=5000))
    monkeypatch.setattr(
        pipeline_insights, "_tool_available", lambda *_args: (True, ""))
    monkeypatch.setattr(
        pipeline_insights, "_tool_preflight", lambda *_args: preflight)
    task = SimpleNamespace(
        series_id=1, series_title="Example - REVIEW",
        prompt="Example - REVIEW")

    route = pipeline_insights.execution_route(task, task.prompt)

    assert route["action"] == "defer"
    assert route["defer_policy"] == "hard_not_before"
    assert route["defer_context"] == {
        "phase": "post_preflight",
        "budget_route": "fallback_targeted",
        "preflight_action": "fallback",
        "preflight_reason": "complex state",
        "handoff_present": True,
        "target": {
            "number": 1414, "head": "a" * 40,
            "stage": "integration-review",
        },
    }
    assert "target=#1414" in route["reason"]
    assert "stage=integration-review" in route["reason"]
    assert f"head={'a' * 40}" in route["reason"]
    assert "complex state" in route["reason"]
    serialized = json.dumps(route, ensure_ascii=False)
    assert "TOP-SECRET-SIGNED-LEASE" not in serialized
    assert "must_not" not in serialized


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
    reason = (
        "low budget; preflight: post_preflight, route=fallback_targeted, "
        f"target=#1414, stage=review, head={'a' * 40}"
    )
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route", lambda *_args, **_kwargs: {
            "action": "defer", "mode": "skill", "reason": reason,
            "defer_until": target.isoformat(),
            "defer_policy": "hard_not_before",
        })
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: (_ for _ in ()).throw(
            AssertionError("deferred task loaded a provider")))

    worker._execute_task_inner(task)

    deferred = isolated_db.get_task(created.id)
    assert deferred.status.value == "pending"
    assert deferred.scheduled_at == target
    assert deferred.next_run_at == target
    assert deferred.error == reason


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


def test_scan_lease_retries_transient_sqlite_failure_and_recovers(
        monkeypatch):
    lease = pipeline_insights._GitHubScanLease("github-default", "owner", 30)
    calls = []

    def renew(*_args):
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return time.time() + 30

    monkeypatch.setattr(
        pipeline_insights, "_GITHUB_SCAN_LEASE_RENEW_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(
        pipeline_insights.db, "renew_pipeline_scan_lease", renew)

    lease.ensure_owned(renew=True)

    assert calls == [1, 2, 3]
    assert lease.lost is False
    assert lease.unavailable is False
    assert lease.expires_at > time.time()


def test_scan_lease_does_not_repeat_after_retry_deadline(monkeypatch):
    lease = pipeline_insights._GitHubScanLease("github-default", "owner", 30)
    calls = []
    monotonic = iter([100.0, 102.0])

    def renew(*_args):
        calls.append("renew")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        pipeline_insights.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(
        pipeline_insights.db, "renew_pipeline_scan_lease", renew)

    with pytest.raises(pipeline_insights._GitHubScanLeaseUnavailable):
        lease.ensure_owned(renew=True)

    assert calls == ["renew"]
    assert lease.lost is False


def test_scan_lease_foreground_does_not_wait_indefinitely_for_heartbeat(
        monkeypatch):
    lease = pipeline_insights._GitHubScanLease("github-default", "owner", 30)
    assert lease._renew_lock.acquire(blocking=False)
    monkeypatch.setattr(
        pipeline_insights,
        "_GITHUB_SCAN_LEASE_RENEW_LOCK_TIMEOUT_SECONDS", 0)
    try:
        with pytest.raises(
                pipeline_insights._GitHubScanLeaseUnavailable,
                match="не завершилась вовремя"):
            lease.ensure_owned(renew=True)
    finally:
        lease._renew_lock.release()

    assert lease.lost is False
    assert lease.unavailable is True


def test_scan_lease_transient_failure_is_fail_closed_but_not_sticky(
        monkeypatch):
    lease = pipeline_insights._GitHubScanLease("github-default", "owner", 30)
    monkeypatch.setattr(
        pipeline_insights, "_GITHUB_SCAN_LEASE_RENEW_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(
        pipeline_insights.db, "renew_pipeline_scan_lease",
        lambda *_args: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))

    with pytest.raises(
            pipeline_insights._GitHubScanLeaseUnavailable,
            match="временно недоступна"):
        lease.ensure_owned(renew=True)

    assert lease.lost is False
    assert lease.unavailable is True
    with pytest.raises(pipeline_insights._GitHubScanLeaseUnavailable):
        lease.ensure_owned()

    monkeypatch.setattr(
        pipeline_insights.db, "renew_pipeline_scan_lease",
        lambda *_args: time.time() + 30)
    lease.ensure_owned(renew=True)

    assert lease.lost is False
    assert lease.unavailable is False


def test_scan_lease_none_is_a_sticky_fenced_loss(monkeypatch):
    lease = pipeline_insights._GitHubScanLease("github-default", "owner", 30)
    calls = []
    monkeypatch.setattr(
        pipeline_insights.db, "renew_pipeline_scan_lease",
        lambda *_args: calls.append("renew"))

    with pytest.raises(pipeline_insights._GitHubScanLeaseLost):
        lease.ensure_owned(renew=True)

    assert lease.lost is True
    assert lease.unavailable is False
    monkeypatch.setattr(
        pipeline_insights.db, "renew_pipeline_scan_lease",
        lambda *_args: time.time() + 30)
    with pytest.raises(pipeline_insights._GitHubScanLeaseLost):
        lease.ensure_owned(renew=True)
    assert calls == ["renew"]


def test_transient_lease_becomes_sticky_loss_after_successor_takes_over(
        isolated_db, monkeypatch):
    acquired = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "old-owner", 30)
    assert acquired["acquired"] is True
    lease = pipeline_insights._GitHubScanLease(
        "github-default", "old-owner", 30)
    lease.expires_at = acquired["expires_at"]
    real_renew = isolated_db.renew_pipeline_scan_lease
    monkeypatch.setattr(
        pipeline_insights, "_GITHUB_SCAN_LEASE_RENEW_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(
        isolated_db, "renew_pipeline_scan_lease",
        lambda *_args: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))

    with pytest.raises(pipeline_insights._GitHubScanLeaseUnavailable):
        lease.ensure_owned(renew=True)
    successor = isolated_db.acquire_pipeline_scan_lease(
        "github-default", "new-owner", 30, now=time.time() + 31)
    assert successor["acquired"] is True
    monkeypatch.setattr(
        isolated_db, "renew_pipeline_scan_lease", real_renew)

    with pytest.raises(pipeline_insights._GitHubScanLeaseLost):
        lease.ensure_owned(renew=True)

    assert lease.lost is True
    assert lease.unavailable is False
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "old-owner") is False
    assert isolated_db.release_pipeline_scan_lease(
        "github-default", "new-owner") is True


def test_scan_admission_reports_transient_lease_unavailable(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_GITHUB_SCAN_LEASE_RENEW_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(
        pipeline_insights, "_github_rate_limits", lambda: _limits())
    monkeypatch.setattr(
        pipeline_insights.db, "renew_pipeline_scan_lease",
        lambda *_args: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")))

    with pipeline_insights._github_scan_admission(
            profile, "test", profile_id="example",
            budget_route="insights") as admission:
        assert admission["allowed"] is False
        assert admission["state"] == "lease_unavailable"
        assert admission["_lease"].lost is False
        assert admission["_lease"].unavailable is True


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


def test_pipeline_sampler_reads_database_off_event_loop(monkeypatch):
    threads = {}

    def list_series():
        threads["database"] = threading.get_ident()
        return [{"id": 1}]

    def sample(series):
        threads["sample"] = threading.get_ident()
        assert series == [{"id": 1}]
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "PIPELINE_SNAPSHOT_INTERVAL", 0)
    monkeypatch.setattr(api.db, "list_series", list_series)
    monkeypatch.setattr(
        api.pipeline_insights, "sample_active_profiles", sample)

    async def run_once():
        threads["event_loop"] = threading.get_ident()
        with pytest.raises(asyncio.CancelledError):
            await api._pipeline_sampler()

    asyncio.run(run_once())

    assert threads["database"] == threads["sample"]
    assert threads["database"] != threads["event_loop"]


def test_bot_pipeline_snapshot_reads_database_off_event_loop(monkeypatch):
    threads = {}
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=status))

    def list_series():
        threads["database"] = threading.get_ident()
        return [{"id": 2}]

    def read_cached(profile_id, series):
        threads["snapshot"] = threading.get_ident()
        assert profile_id == "example"
        assert series == [{"id": 2}]
        raise asyncio.CancelledError

    monkeypatch.setattr(bot.db, "list_series", list_series)
    monkeypatch.setattr(bot.pipeline_insights, "read_cached", read_cached)

    async def run_once():
        threads["event_loop"] = threading.get_ident()
        with pytest.raises(asyncio.CancelledError):
            await bot._send_pipeline_insights(message, "example")

    asyncio.run(run_once())

    assert threads["database"] == threads["snapshot"]
    assert threads["database"] != threads["event_loop"]


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
