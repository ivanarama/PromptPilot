"""A waiting ordinary FIX gets the next usable Source for an unchanged PR."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from promptpilot import pipeline_insights as pi, worker
from promptpilot.models import TaskCreate
from tests.source_cost_fixtures import checker_receipt
from tests.test_required_snapshot_refresh import gate as gate


@pytest.fixture
def unchanged_pr(gate, monkeypatch):
    """The cached candidate was already signalled before the next scan was taken."""
    db, profile, fix, _, calls = gate
    profile["repository"] = "ivanarama/onebase"
    monkeypatch.setattr(pi, "_profiles", lambda: {"onebase": profile})
    queue = profile["queues"][0]
    queue["wake_when"] = {"field": "fix_candidates"}
    candidates = [{"number": 2285, "head": "a" * 40}]
    monkeypatch.setattr(pi, "_run_profile_health_check", lambda _: checker_receipt({
        "state": "green", "findings": [], "fix_candidates": candidates,
    }))

    def publish():
        return pi.analyze("onebase", db.list_series(), use_cache=False, refresh_diagnostics=True)

    # The ordinary candidate-fingerprint wake has already been delivered.
    first = publish()
    latch = pi._wake_latch_key("onebase", "fix")
    fingerprint = pi._wake_fingerprint(first["diagnostics"], queue["wake_when"])
    assert fingerprint
    db.set_setting(latch, fingerprint)
    review = db.create_task(TaskCreate(prompt="Other review", priority=1))
    pi._discard_cache("onebase")
    assert db.acquire_pipeline_scan_lease("github-default", "background", 120)["acquired"]

    def finish_background_scan():
        assert db.release_pipeline_scan_lease("github-default", "background")
        fresh = publish()
        assert fresh["cache"]["token"]["revision"] > first["cache"]["token"]["revision"]
        assert pi._wake_fingerprint(fresh["diagnostics"], queue["wake_when"]) == fingerprint
        assert db.get_setting(latch) == fingerprint

    return SimpleNamespace(db=db, fix=fix, review=review, calls=calls,
                           finish_scan=finish_background_scan, publish=publish)


@pytest.mark.parametrize("publish_before_defer", [False, True])
def test_same_pr_on_new_publication_reaches_ordinary_admission(unchanged_pr, monkeypatch, publish_before_defer):
    state = unchanged_pr
    if publish_before_defer:
        dispatch = pi.dispatch_gate

        def raced(task):
            decision = dispatch(task)
            assert "scan_in_progress" in decision["reason"]
            state.finish_scan()
            return decision

        with monkeypatch.context() as patch:
            patch.setattr(pi, "dispatch_gate", raced)
            worker._execute_task_inner(state.fix)
    else:
        worker._execute_task_inner(state.fix)
        assert state.db.get_task(state.fix.id).status.value == "pending"
        assert state.db.get_task(state.fix.id).scheduled_at > state.fix.started_at
        state.finish_scan()

    assert "execution-admission" not in state.calls
    # The scheduler sees a higher-priority competitor and the original FIX.
    retry = state.db.get_next_runnable()
    assert retry is not None and retry.id == state.fix.id
    worker._execute_task_inner(retry)
    assert state.calls[-3:] == ["execution-admission", "load-provider", "provider"]
    assert state.db.get_task(state.review.id).status.value == "pending"


def test_fresh_publications_do_not_repeat_a_consumed_wake_past_a_quota_deadline(unchanged_pr, monkeypatch):
    state = unchanged_pr
    worker._execute_task_inner(state.fix)
    state.finish_scan()
    retry = state.db.get_next_runnable()
    assert retry is not None and retry.id == state.fix.id
    deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
    monkeypatch.setattr(pi, "execution_route", lambda *_args, **_kwargs: {
        "action": "defer", "reason": "remaining quota is below the configured floor",
        "defer_policy": "hard_not_before", "defer_until": deadline.isoformat(),
    })
    worker._execute_task_inner(retry)
    assert state.db.get_task(state.fix.id).next_run_at == deadline
    state.publish()
    assert state.db.get_task(state.fix.id).next_run_at == deadline
    assert state.db.get_next_runnable().id == state.review.id
    assert state.db.get_next_runnable() is None
    assert "load-provider" not in state.calls
