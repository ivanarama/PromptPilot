"""A real sampler cycle must finish before the previous observation expires."""

from datetime import datetime, timezone
import asyncio

import pytest

from promptpilot import pipeline_insights as pi, pipeline_sampling as sampling
from tests.test_github_budget import _limits, _profile_with_costs, _running_task


@pytest.fixture
def clock(monkeypatch):
    value = [1_800_000_000.0]
    monkeypatch.setattr(sampling.time, "time", lambda: value[0])
    return value


def profile(lead=150):
    value = _profile_with_costs()
    value["always_sample"] = True
    value["github_budget"].update(essential_snapshot_headroom=True,
                                  refresh_ahead_seconds=lead)
    return value


def install(monkeypatch, profiles, clock, *, duration=40):
    generated = {key: clock[0] for key in profiles}
    blocked = {}
    calls = []
    monkeypatch.setattr(pi, "_profiles", lambda: profiles)

    def cached(key, _series):
        age = clock[0] - generated[key]
        return {"cache": {"available": True, "complete": True,
                          "age_seconds": age, "ttl_seconds": 300,
                          "stale": age >= 300, **blocked.get(key, {})}}

    def scan(key, _series, **kwargs):
        assert kwargs == {"use_cache": False}
        calls.append((key, clock[0]))
        # Like production, generated_at precedes the scan's actual duration.
        generated[key] = clock[0]
        clock[0] += duration
        return cached(key, [])

    monkeypatch.setattr(pi, "read_cached", cached)
    monkeypatch.setattr(pi, "analyze", scan)
    monkeypatch.setattr(pi, "_wake_ready_queues", lambda *_: [])
    monkeypatch.setattr(pi, "_wake_cache_guard", lambda *_: False)
    return generated, blocked, calls


def test_early_refresh_keeps_a_long_running_preflight_fresh(isolated_db, monkeypatch, clock):
    generated, _, calls = install(monkeypatch, {"example": profile()}, clock)
    state = {}
    assert sampling.sample_scheduled_profiles([], 300, state) == 150
    # Run twelve minutes of preparation while the real sampler entry points
    # coordinate forty-second observations. Sampling does not extend the TTL.
    start = clock[0]
    while clock[0] < start + 720:
        delay = sampling.sample_scheduled_profiles([], 300, state)
        assert clock[0] - generated["example"] < 300
        clock[0] += delay
    assert len(calls) >= 4
    assert all(b[1] - a[1] == 150 for a, b in zip(calls, calls[1:]))


def test_manual_refresh_reschedules_without_duplicate_scan(isolated_db, monkeypatch, clock):
    generated, _, calls = install(monkeypatch, {"example": profile()}, clock)
    state = {}
    sampling.sample_scheduled_profiles([], 300, state)
    clock[0] += 149
    generated["example"] = clock[0]  # Another owned process published a new Source.
    clock[0] += 1
    assert sampling.sample_scheduled_profiles([], 300, state) == 149
    assert calls == []


def test_api_sampler_sleeps_until_refresh_due_not_a_full_interval_after_work(
        isolated_db, monkeypatch, clock):
    from promptpilot import api
    _, _, calls = install(monkeypatch, {"example": profile()}, clock)
    monkeypatch.setattr(api.db, "list_series", lambda: [])
    monkeypatch.setattr(api, "PIPELINE_SNAPSHOT_INTERVAL", 300)
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError
        clock[0] += delay

    monkeypatch.setattr(api.asyncio, "sleep", sleep)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await api._pipeline_sampler()

    asyncio.run(run())
    assert sleeps == [150, 110, 110]
    assert [stamp for _, stamp in calls] == [1_800_000_150.0, 1_800_000_300.0]


def test_legacy_profiles_keep_their_own_cadence(isolated_db, monkeypatch, clock):
    profiles = {"early": profile(), "legacy": profile(0)}
    _, _, calls = install(monkeypatch, profiles, clock, duration=0)
    state = {}
    assert sampling.sample_scheduled_profiles([], 300, state) == 150
    clock[0] += 150
    sampling.sample_scheduled_profiles([], 300, state)
    assert [key for key, _ in calls] == ["early"]
    clock[0] += 150
    sampling.sample_scheduled_profiles([], 300, state)
    assert [key for key, _ in calls] == ["early", "early", "legacy"]


def test_shared_scan_wait_is_honored_without_resampling(isolated_db, monkeypatch, clock):
    _, blocked, calls = install(monkeypatch, {"example": profile()}, clock)
    clock[0] += 180
    blocked["example"] = {"refresh_blocked": "scan_in_progress",
                          "refresh_deferred_until": datetime.fromtimestamp(
                              clock[0] + 120, timezone.utc).isoformat()}
    assert sampling.sample_scheduled_profiles([], 300, {}) == 120
    assert calls == []


def test_explicit_defer_retries_at_its_deadline_once(isolated_db, monkeypatch, clock):
    _, blocked, calls = install(monkeypatch, {"example": profile()}, clock, duration=0)
    clock[0] += 180
    blocked["example"] = {"refresh_blocked": "scan_in_progress",
                          "refresh_deferred_until": datetime.fromtimestamp(
                              clock[0] + 120, timezone.utc).isoformat()}
    state = {}
    assert sampling.sample_scheduled_profiles([], 300, state) == 120
    clock[0] += 120
    # The public read_cached overlay drops an expired deferred status before
    # the coordinator reads it. It must now sample, without another full wait.
    blocked.clear()
    assert sampling.sample_scheduled_profiles([], 300, state) == 150
    assert len(calls) == 1


def test_failed_scan_waits_instead_of_retrying_until_green(isolated_db, monkeypatch, clock):
    _, _, calls = install(monkeypatch, {"example": profile()}, clock)
    clock[0] += 180

    def failed(*args, **kwargs):
        calls.append("failed")
        raise RuntimeError("checker failed")

    monkeypatch.setattr(pi, "analyze", failed)
    assert sampling.sample_scheduled_profiles([], 300, {}) == 300
    assert calls == ["failed"]


def test_global_pause_does_not_sample_or_wake(isolated_db, monkeypatch, clock):
    _, _, calls = install(monkeypatch, {"example": profile()}, clock)
    isolated_db.set_setting("worker_paused", "1")
    clock[0] += 500
    assert sampling.sample_scheduled_profiles([], 300, {}) == 300
    assert calls == []


@pytest.mark.parametrize("value", [True, -1, 300, 1.5, "150"])
def test_invalid_lead_does_not_start_requests(isolated_db, monkeypatch, clock, value, caplog):
    _, _, calls = install(monkeypatch, {"example": profile(value)}, clock)
    assert sampling.sample_scheduled_profiles([], 300, {}) == 300
    assert calls == []
    assert "refresh_ahead_seconds" in caplog.text


def test_tuning_does_not_change_source_or_effective_budget_contract():
    before = _profile_with_costs()
    after = _profile_with_costs()
    after["github_budget"].update(essential_snapshot_headroom=True,
                                 refresh_ahead_seconds=150)
    before["github_budget"]["essential_snapshot_headroom"] = True
    assert pi._profile_fingerprint(before) == pi._profile_fingerprint(after)
    assert pi._github_budget_policy(before) == pi._github_budget_policy(after)


def test_proactive_scan_preserves_foreign_reservations_and_refuses_admission(
        isolated_db, monkeypatch):
    # Exercise the real Source sampler, cache publication and SQLite budget
    # ledger; only the external GitHub transport is replaced.
    import time
    current = [time.time()]
    monkeypatch.setattr(sampling.time, "time", lambda: current[0])
    value = _profile_with_costs(core=0)
    value.update(always_sample=True)
    value["queues"][0].pop("wake_when")
    value["github_budget"].update(essential_snapshot_headroom=True,
                                  refresh_ahead_seconds=150)
    value["github_budget"]["minimum_remaining"]["search"] = 2
    value["github_budget"]["costs"]["insights"]["search"] = 10
    monkeypatch.setattr(pi, "_profiles", lambda: {"example": value})
    monkeypatch.setattr(pi, "_cache", {})
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: _limits(search=15))
    calls = []
    monkeypatch.setattr(pi, "_github_search", lambda *_: calls.append("search") or {
        "count": 0, "items": [], "membership_complete": True})
    initial = pi.analyze("example", [], use_cache=False)
    assert initial["cache"]["complete"] and not initial["cache"]["stale"]
    token = initial["cache"]["token"]
    task = _running_task(isolated_db, "Foreign reservation")
    reservation = isolated_db.reserve_pipeline_github_budget(
        "github-default", token="foreign", task_id=task.id,
        task_started_at=task.started_at, profile_id="example", queue_id="review",
        route="skill", cost={"core": 0, "search": 4, "graphql": 0},
        limits=_limits(search=15), minimum_remaining={"core": 0, "search": 2, "graphql": 0})
    assert reservation["allowed"]
    current[0] += 200
    calls.clear()
    delay = sampling.sample_scheduled_profiles([], 300, {})
    after = pi.read_cached("example", [])
    assert calls == []  # 15 - foreign4 - scan10 = 1 < hard minimum2.
    assert after["cache"]["token"] == token
    assert after["cache"]["age_seconds"] >= 200
    assert after["cache"]["ttl_seconds"] == 300
    assert after["cache"]["refresh_blocked"] == "budget_in_flight"
    assert pi._required_snapshot_state(after, value, "review").startswith("refresh_blocked=")
    assert 1 <= delay <= 300
    assert isolated_db.pipeline_github_budget_reservations("github-default")["totals"]["search"] == 4
