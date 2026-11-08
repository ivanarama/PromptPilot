"""Keep a final queue scan possible before admitting long provider work."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import copy
import threading

import pytest

from promptpilot import pipeline_insights as insights
from promptpilot.models import TaskCreate
from test_github_budget import (
    _arm_and_defer_budget_waiter, _budget_costs, _limits, _profile, _running_task,
)


def profile():
    value = _profile(minimum={"core": 250, "search": 2, "graphql": 100})
    value["github_budget"].update({
        "essential_snapshot_headroom": True,
        "costs": _budget_costs(core=0, search=8),
    })
    value["github_budget"]["costs"]["insights"] = {
        "core": 50, "search": 10, "graphql": 100,
    }
    return value


def reserve_fix(db, search):
    task = _running_task(db, "Existing FIX")
    result = db.reserve_pipeline_github_budget(
        "github-default", token="existing-fix", task_id=task.id,
        task_started_at=task.started_at, profile_id="example", queue_id="fix",
        route="skill", cost={"core": 0, "search": search, "graphql": 0},
        limits=_limits(),
        minimum_remaining={"core": 250, "search": 2, "graphql": 100})
    assert result["allowed"]


def provider_task(db, priority=1):
    db.create_task(TaskCreate(prompt="Example - REVIEW", recurrence="4h", priority=priority))
    task = db.get_next_runnable()
    return SimpleNamespace(**{**task.model_dump(), "series_title": "Example - REVIEW"})


@pytest.mark.parametrize("remaining", [28, 30])
def test_public_provider_defers_before_work_when_final_scan_cannot_fit(
        isolated_db, monkeypatch, remaining):
    value = profile()
    reserve_fix(isolated_db, 12)
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    monkeypatch.setattr(insights, "_github_rate_limits", lambda: _limits(search=remaining))
    task = provider_task(isolated_db)
    result = insights.execution_route(task, task.prompt)
    # Previously 28 - (FIX12 + provider8) >= 2 admitted this work, although
    # even a fully reset Search bucket cannot fit ALL20 + finalScan10 + min2.
    assert result["action"] == "defer"
    assert result["github_budget"]["minimum_remaining"]["search"] == 12
    assert isolated_db.pipeline_github_budget_reservations("github-default")["count"] == 1


@pytest.mark.parametrize("remaining, allowed", [(25, False), (26, True)])
def test_public_provider_exact_scan_floor_boundary(
        isolated_db, monkeypatch, remaining, allowed):
    value = profile()
    reserve_fix(isolated_db, 6)
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    monkeypatch.setattr(insights, "_github_rate_limits", lambda: _limits(search=remaining))
    result = insights.execution_route(provider_task(isolated_db), "Example - REVIEW")
    assert (result["action"] == "prompt") is allowed


def test_scan_spends_shared_gap_once_without_refunding_provider_reservations(
        isolated_db, monkeypatch):
    value = profile()
    reserve_fix(isolated_db, 18)
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    monkeypatch.setattr(insights, "_github_rate_limits", _limits)
    monkeypatch.setattr(insights, "_cached_entry", lambda *_: (None, None, 0))
    with insights._github_scan_admission(
            value, "final scan", profile_id="example", budget_route="insights") as result:
        assert result["allowed"]
        assert result["minimum_remaining"]["search"] == 2
        assert result["requested_cost"]["search"] == 10
        assert result["reserved_other"]["search"] == 18
        assert result["effective_after"]["search"] == 2
    ledger = isolated_db.pipeline_github_budget_reservations("github-default")
    assert ledger["count"] == 1 and ledger["totals"]["search"] == 18


def execution_policy(value, route="skill", priority=1):
    policy = insights._with_shared_budget_floor(insights._github_budget_policy(value))
    policy = insights._budget_policy_for_route(policy, route)
    policy = insights._budget_policy_for_admission_priority(policy, priority)
    return insights._with_execution_snapshot_floor(policy)


def test_parallel_reservations_cannot_consume_last_scan_gap(isolated_db, monkeypatch):
    value = profile()
    value["github_budget"]["costs"]["skill"]["search"] = 12
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    tasks = [provider_task(isolated_db), provider_task(isolated_db)]
    barrier = threading.Barrier(2)

    def compete(index):
        route = ("skill", "fallback_targeted")[index]
        policy = execution_policy(value, route)
        # Both stale early observations fit on their own. Only the SQLite
        # transaction can prevent a combined ALL20 from consuming scan10.
        assert insights._evaluate_github_budget(policy, _limits(), now=1000)["allowed"]
        barrier.wait(timeout=10)
        task = tasks[index]
        return isolated_db.reserve_pipeline_github_budget(
            "github-default", token=f"concurrent-{index}", task_id=task.id,
            task_started_at=task.started_at, profile_id="example", queue_id="review",
            route=route, cost=policy["requested_cost"], limits=_limits(),
            minimum_remaining=policy["minimum_remaining"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, range(2)))
    assert sum(item["allowed"] for item in results) == 1
    denied = next(item for item in results if not item["allowed"])
    assert denied["state"] == "budget_in_flight"
    assert denied["minimum_remaining"]["search"] == 12


def test_final_route_recheck_keeps_gap_after_early_admission(isolated_db, monkeypatch):
    value = profile()
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    monkeypatch.setattr(insights, "_github_rate_limits", _limits)
    task = provider_task(isolated_db)
    try:
        with insights._github_scan_admission(
                value, "provider", profile_id="example", budget_route="skill",
                task=task) as decision:
            assert decision["allowed"]
            reserve_fix(isolated_db, 12)
            decision.pop("_budget_reservation_revision", None)
            result = insights._reserve_execution_admission(
                task, "example", value, value["queues"][0], decision, "skill",
                retain_budget=True)
            assert not result["allowed"]
            assert result["state"] == "budget_in_flight"
            assert result["minimum_remaining"]["search"] == 12
        assert isolated_db.pipeline_github_budget_reservations("github-default")["count"] == 1
    finally:
        insights.release_execution_admission()


@pytest.mark.parametrize("priority, floor", [(1, 12), (2, 16)])
def test_p1_borrows_soft_reserve_but_not_essential_gap(monkeypatch, priority, floor):
    value = profile()
    value["github_budget"]["priority_one_headroom"] = {"core": 0, "search": 4, "graphql": 0}
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    policy = execution_policy(value, priority=priority)
    assert policy["minimum_remaining"]["search"] == floor
    result = insights._evaluate_github_budget(
        policy, _limits(), now=1000, reserved_other={"core": 0, "search": 12, "graphql": 0})
    assert not result["allowed"]


def test_aged_waiter_still_pays_scan_gap(isolated_db, monkeypatch):
    value = profile()
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    waiter = provider_task(isolated_db)
    _arm_and_defer_budget_waiter(isolated_db, waiter, wait_started_at=1000)
    selected = isolated_db.get_next_runnable()
    policy = execution_policy(value)
    result = isolated_db.reserve_pipeline_github_budget(
        "github-default", token="aged-owner", task_id=selected.id,
        task_started_at=selected.started_at, profile_id="example", queue_id="review",
        route="skill", cost=policy["requested_cost"], limits=_limits(search=19),
        minimum_remaining=policy["minimum_remaining"],
        starvation_timeout_seconds=600, now=1600)
    assert not result["allowed"] and result["state"] == "low"
    assert result["minimum_remaining"]["search"] == 12


def test_shared_gap_is_componentwise_maximum_not_sum_and_not_soft(monkeypatch):
    first, second = profile(), profile()
    second["github_budget"]["costs"]["insights"] = {"core": 90, "search": 7, "graphql": 200}
    unrelated = copy.deepcopy(second)
    unrelated["github_budget"]["essential_snapshot_headroom"] = False
    unrelated["github_budget"]["costs"]["insights"] = {"core": 2000, "search": 29, "graphql": 4000}
    monkeypatch.setattr(insights, "_profiles", lambda: {
        "first": first, "second": second, "ordinary": unrelated})
    policy = execution_policy(unrelated)
    assert policy["snapshot_headroom"] == {"core": 90, "search": 10, "graphql": 200}
    assert policy["minimum_remaining"] == {"core": 340, "search": 12, "graphql": 300}


@pytest.mark.parametrize("essential, scan_cost", [(False, 10), (True, 0)])
def test_no_essential_scan_cost_keeps_old_provider_floor(monkeypatch, essential, scan_cost):
    value = profile()
    value["github_budget"]["essential_snapshot_headroom"] = essential
    value["github_budget"]["costs"]["insights"] = {"core": 0, "search": scan_cost, "graphql": 0}
    monkeypatch.setattr(insights, "_profiles", lambda: {"example": value})
    policy = execution_policy(value)
    assert policy["minimum_remaining"] == {"core": 250, "search": 2, "graphql": 100}
    assert not policy["snapshot_headroom_applied"]


def test_shared_essential_gap_cannot_be_bypassed_by_missing_route_costs(monkeypatch):
    required, legacy = profile(), _profile()
    monkeypatch.setattr(insights, "_profiles", lambda: {"required": required, "legacy": legacy})
    with pytest.raises(ValueError, match="snapshot headroom"):
        execution_policy(legacy)
