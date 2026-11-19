"""Test a final Source reservation through real Native dispatch and the public CLI."""
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from promptpilot import entrypoint, fallback_handoff, pipeline_insights as pi
from promptpilot import pipeline_source_refresh as source
from promptpilot.models import TaskCreate
from tests.source_cost_fixtures import observed_rates
from tests.test_fallback_handoff import health
from tests.test_github_budget import _limits
from tests.test_pipeline_source_refresh import (
    continuation as continuation, ledger, targeted_merge as targeted_merge,
)


@pytest.fixture
def merge_dispatch(targeted_merge, monkeypatch, tmp_path):
    f = targeted_merge
    assert f.db.release_pipeline_github_budget("github-default", token="actual-targeted-admission",
                                               task_id=f.task.id, task_started_at=f.task.started_at)
    f.profile["github_budget"]["starvation_timeout_seconds"] = 60
    f.profile["queues"][0]["execution"] = {"mode": "auto", "command": ["pipeline", "next", "merge"]}
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(tmp_path / "lease.key"))
    current = health("integration-merge-recovery")
    handoff = fallback_handoff.create({"repository": f.profile["repository"]}, current, "merge",
                                      current["integration_owner"], "synchronize integration owner")
    monkeypatch.setattr(pi, "_tool_available", lambda *_: (True, ""))
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: handoff)
    f.task = f.db.get_task(f.task.id)
    try:
        yield f
    finally:
        pi.release_execution_admission()


def admit(f):
    route = pi.execution_route(f.task, f.task.prompt, retain_budget=True)
    assert route["action"] == "prompt", route
    assert route["next_already_run"]
    costs = f.profile["github_budget"]["costs"]
    expected = {key: cost + costs["insights"][key] for key, cost in costs["fallback_targeted"].items()}
    reservations = f.db.pipeline_github_budget_reservations("github-default")
    assert reservations["count"] == 1
    assert reservations["totals"] == expected


def aged_waiter(f):
    f.db.create_task(TaskCreate(prompt="another queue", priority=5))
    other = f.db.get_next_runnable()
    f.db.arm_pipeline_github_budget_waiter("github-default", task_id=other.id,
                                          task_started_at=other.started_at)
    start = datetime.now(timezone.utc) - timedelta(minutes=2)
    with f.db._connect() as c:
        c.execute("UPDATE tasks SET budget_wait_started_at=? WHERE id=?", (start.isoformat(), other.id))
    return other.id, start.isoformat()


def test_dispatch_preallocates_one_final_source_that_can_finish_under_contention(
        merge_dispatch, monkeypatch, capsys):
    f = merge_dispatch
    admit(f)
    waiter_id, waited_since = aged_waiter(f)
    before = ledger(f)
    for budget_only in (True, False):
        ordinary = source.refresh("example", f.env, budget_only=budget_only)
        assert ordinary["state"] == "wait" and ordinary["budget"]["state"] == "fairness_waiter"
        assert ledger(f) == before

    # A new MERGE task requesting its own reservation must still yield.
    f.db.create_task(TaskCreate(prompt=f.task.prompt, priority=1, recurrence="1h"))
    new_task = f.db.get_next_runnable()
    new = pi.execution_route(new_task, new_task.prompt)
    assert new["action"] == "defer" and new["github_budget"]["state"] == "fairness_waiter"
    assert f.checks == f.searches == []

    monkeypatch.setenv("PP_TASK_ID", str(f.task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", f.task.started_at.isoformat())
    monkeypatch.setattr(sys, "argv", ["pp", "pipelinectl", "source-refresh", "--profile", "example", "--final"])
    with pytest.raises(SystemExit) as invoked:
        entrypoint.main()
    result = json.loads(capsys.readouterr().out)
    assert invoked.value.code == 0, result
    assert result["source_refreshed"] is True and result["budget"]["final_source_credit_used"] is True
    assert result["product_mutation_authorized"] is False
    assert len(f.checks) == len(f.searches) == 1

    replay = source.refresh("example", f.env, final=True)
    assert replay["state"] == "wait" and replay["budget"]["state"] == "fairness_waiter"
    assert len(f.db.list_pipeline_snapshots("example")) == 1
    assert len(f.checks) == len(f.searches) == 1
    with f.db._connect() as c:
        assert tuple(c.execute("SELECT budget_wait_scope,budget_wait_started_at FROM tasks WHERE id=?",
                               (waiter_id,)).fetchone()) == ("github-default", waited_since)


def test_existing_merge_reservation_without_final_allowance_keeps_queue_order(targeted_merge):
    f = targeted_merge
    f.profile["github_budget"]["starvation_timeout_seconds"] = 60
    aged_waiter(f)
    before = ledger(f)
    result = source.refresh("example", f.env, final=True)
    assert result["state"] == "wait" and result["budget"]["state"] == "fairness_waiter"
    assert ledger(f) == before and f.checks == f.searches == []


@pytest.mark.parametrize("resource,remaining", [("core", 1299), ("search", 19), ("graphql", 1199)])
def test_final_source_respects_current_hard_floor(merge_dispatch, monkeypatch, resource, remaining):
    f = merge_dispatch
    admit(f)
    before = ledger(f)
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(**{resource: remaining})))
    result = source.refresh("example", f.env, final=True)
    assert result["source_refreshed"] is False
    assert resource in {item["resource"] for item in result["budget"]["blocked_resources"]}
    assert ledger(f) == before and f.checks == f.searches == []


def test_busy_scan_retains_final_source_for_the_later_attempt(merge_dispatch):
    f = merge_dispatch
    admit(f)
    aged_waiter(f)
    assert f.db.acquire_pipeline_scan_lease("github-default", "other-active-scan", 60)["acquired"]
    before = ledger(f)
    blocked = source.refresh("example", f.env, final=True)
    assert blocked["state"] == "wait" and blocked["budget"]["state"] == "scan_in_progress"
    assert ledger(f) == before and f.checks == f.searches == []
    assert f.db.release_pipeline_scan_lease("github-default", "other-active-scan")
    assert source.refresh("example", f.env, final=True)["source_refreshed"] is True

def test_start_refuses_merge_when_only_the_provider_portion_fits(merge_dispatch, monkeypatch):
    f = merge_dispatch
    f.profile["github_budget"]["costs"]["tool_preflight"]["search"] = 1
    # 19 can pay 8 for execution, but cannot also pay 10 for Source and retain 2.
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(search=19)))
    route = pi.execution_route(f.task, f.task.prompt, retain_budget=True)
    assert route["action"] == "defer", route
    assert route["github_budget"]["requested_cost"]["search"] == 18
    assert route["defer_context"]["phase"] == "post_preflight"
    assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0


def test_final_source_counts_every_foreign_reservation(merge_dispatch, monkeypatch):
    f = merge_dispatch
    admit(f)
    f.db.create_task(TaskCreate(prompt="other work"))
    other = f.db.get_next_runnable()
    assert f.db.reserve_pipeline_github_budget("github-default", token="foreign-admission",
        task_id=other.id, task_started_at=other.started_at, profile_id="another", queue_id="fix",
        route="skill", cost={"core": 100, "search": 3, "graphql": 50}, limits=_limits(),
        minimum_remaining=f.profile["github_budget"]["minimum_remaining"])["allowed"]
    aged_waiter(f)
    before = ledger(f)
    # Without the foreign three requests, 22 would suffice for the terminal scan.
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(search=22)))
    denied = source.refresh("example", f.env, final=True)
    assert denied["source_refreshed"] is False
    assert denied["budget"]["state"] == "budget_in_flight"
    assert denied["budget"]["reserved_other"]["search"] == 11
    assert ledger(f) == before and f.checks == f.searches == []

@pytest.mark.parametrize("failure,expected", [
    ("budget", "wait"), ("lease", "refused"), ("error", "refused"),
])
def test_spent_final_credit_is_reported_after_failed_scan(merge_dispatch, monkeypatch, failure, expected):
    f = merge_dispatch
    admit(f)
    aged_waiter(f)

    def failed_scan(*args, **kwargs):
        if failure == "budget":
            raise pi._GitHubSearchBudgetDenied({"state": "budget_in_flight", "reason": "test quota"})
        if failure == "lease":
            raise pi._GitHubScanLeaseLost("test lease loss")
        raise ValueError("test failure after admission")

    monkeypatch.setattr(pi, "_analyze_without_budget", failed_scan)
    result = source.refresh("example", f.env, final=True)
    assert result["state"] == expected and result["source_refreshed"] is False
    assert result["budget"]["final_source_credit_used"] is True
    if failure == "budget":
        assert result["budget"]["state"] == "budget_in_flight"
    replay = source.refresh("example", f.env, final=True)
    assert replay["state"] == "wait" and replay["budget"]["state"] == "fairness_waiter"
    assert "final_source_credit_used" not in replay["budget"]
