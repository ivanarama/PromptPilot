"""A pending confirmation can request Source; the next signed election still chooses the PR."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from promptpilot import config, fallback_handoff, merge_checkpoint, pipeline_insights as pi
from tests.source_cost_fixtures import observed_rates
from tests.test_fallback_handoff import HEAD, health
from tests.test_github_budget import _limits
from tests.test_merge_checkpoint import BASE, OLD, dispatch, elect, new_claim, run_cli, start
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import continuation as continuation, targeted_merge as targeted_merge


@pytest.fixture
def waiting(merge_dispatch, monkeypatch, capsys):
    f = merge_dispatch
    monkeypatch.setattr(config, "PIPELINE_SNAPSHOT_INTERVAL", 120)
    f.profile["github_budget"]["refresh_ahead_seconds"] = 150
    original = start(f, monkeypatch)
    assert run_cli(monkeypatch, capsys, f, original, "record")["state"] == "recorded"
    new_claim(f)
    source = elect(f, monkeypatch, HEAD, "integration-merge-recovery")
    source["integration_owner"]["base_sync_candidate"] = {
        "from": OLD, "to": HEAD, "base": BASE, "source": "head_parents"}
    snapshot = {
        "repository": f.profile["repository"], "diagnostics": source,
        "cache": {"profile_hash": pi._profile_fingerprint(f.profile), "complete": True,
                  "available": True, "stale": True, "ttl_seconds": 300, "age_seconds": 301},
        "queues": [{"id": "merge", "membership_complete": True, "items": []}],
    }
    monkeypatch.setattr(pi, "read_cached", lambda *_: snapshot)
    assert dispatch(f)["action"] == "defer"
    assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0
    snapshot["cache"].update(stale=False, age_seconds=298)
    return f, snapshot


def test_new_election_after_recovery_refresh_can_select_another_pr(waiting, monkeypatch, capsys):
    f, snapshot = waiting
    other = health("merge")
    target = other["merge_executable"][0]
    target.update(number=43, head="e" * 40)
    signed = fallback_handoff.create({"repository": f.profile["repository"]}, other,
                                     "merge", target, "next ordinary PR")
    calls = []

    def refresh(*args, **kwargs):
        calls.append("source")
        snapshot.update(diagnostics=other)
        snapshot["cache"].update(stale=False, age_seconds=0)
        return snapshot

    def next_merge(*args, **kwargs):
        calls.append("next")
        return signed

    monkeypatch.setattr(pi, "analyze", refresh)
    monkeypatch.setattr(pi, "_tool_preflight", next_merge)
    route = dispatch(f)
    assert calls == ["source", "next"]
    assert route["action"] == "prompt" and route["preflight"]["target"] == signed["target"]
    assert '"merge_checkpoint"' not in route["prompt"]
    assert run_cli(monkeypatch, capsys, f, route, "read")["state"] == "refused"


def test_budget_denial_before_recovery_election_starts_no_provider(waiting, monkeypatch):
    f, _ = waiting
    reset = int((datetime.now(timezone.utc) + timedelta(minutes=20)).timestamp())
    analyze = pi.analyze
    refreshes = []

    def budgeted_refresh(*args, **kwargs):
        refreshes.append(kwargs["snapshot_queue_id"])
        return analyze(*args, **kwargs)

    monkeypatch.setattr(pi, "analyze", budgeted_refresh)
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(core=0, reset=reset)))
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: pytest.fail("election ran before the Source gate"))
    route = dispatch(f)
    assert refreshes == ["merge"]
    assert route["action"] == "defer"
    assert route["defer_policy"] == "hard_not_before"
    assert datetime.fromisoformat(route["defer_until"]).timestamp() >= reset
    assert not f.checks and not f.searches
    assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0


@pytest.mark.parametrize("expired", [False, True])
def test_contended_scan_keeps_recovery_preparation_for_one_ttl(waiting, monkeypatch, expired):
    f, snapshot = waiting
    now = merge_checkpoint.time.time()
    monkeypatch.setattr(merge_checkpoint, "time", SimpleNamespace(time=lambda: now))
    assert f.db.acquire_pipeline_scan_lease("github-default", "sampler", 120)["acquired"]
    preflight = pi._tool_preflight
    calls = []

    def next_merge(*args, **kwargs):
        calls.append("next")
        return preflight(*args, **kwargs)

    monkeypatch.setattr(pi, "_tool_preflight", next_merge)
    for elapsed in (0, 250):
        monkeypatch.setattr(merge_checkpoint.time, "time", lambda: now + elapsed)
        wait = dispatch(f)
        assert wait["action"] == "defer" and wait["defer_policy"] == "retry"
        assert wait["github_budget"]["state"] == "scan_in_progress"
        assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0
    assert calls == []
    assert f.db.release_pipeline_scan_lease("github-default", "sampler")

    snapshot["cache"].pop("refresh_blocked")
    monkeypatch.setattr(merge_checkpoint.time, "time", lambda: now + (301 if expired else 251))

    def refresh(*args, **kwargs):
        calls.append("source")
        snapshot["cache"]["age_seconds"] = 0
        return snapshot

    monkeypatch.setattr(pi, "analyze", refresh)
    route = dispatch(f)
    assert calls == (["next"] if expired else ["source", "next"])
    assert route["action"] == "prompt" and '"merge_checkpoint"' in route["prompt"]
