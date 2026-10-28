import copy
import json
from types import SimpleNamespace

import pytest

from promptpilot import pipeline_insights as pi


@pytest.fixture
def gate(monkeypatch):
    profile = {"queues": [{"id": "plan", "series_contains": "Example - PLAN",
                            "dispatch_gate": {"backpressure": {
                                "max_active": 10, "active_fields": ["review_backlog"],
                                "candidate_field": "fix_candidates"}}}]}
    data = {"cache": {"complete": True, "stale": True, "age_seconds": 354,
                       "ttl_seconds": 300, "refresh_blocked": "budget_in_flight",
                       "refresh_deferred_until": "2026-10-28T22:14:22Z"},
            "github_budget": {"allowed": True, "requested_cost": {"search": 0}},
            "diagnostics": {"review_backlog": [{"number": n, "head": f"{n:040x}"}
                                               for n in range(1, 27)],
                            "fix_candidates": []}, "queues": []}
    calls = []
    monkeypatch.setattr(pi, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pi.db, "list_series", lambda: [])
    monkeypatch.setattr(pi, "read_cached", lambda *_: data)

    def refresh(*args, **kwargs):
        calls.append(kwargs)
        return data

    monkeypatch.setattr(pi, "analyze", refresh)
    task = SimpleNamespace(series_id=1, series_title="Example - PLAN", prompt="Example - PLAN")
    return task, data, calls


def denial(remaining=28, reserved=25, cost=10, projected=-7, floor=2):
    return {"budget_route": "insights", "allowed": False, "state": "budget_in_flight",
            "minimum_remaining": {"search": floor}, "base_minimum_remaining": {"search": 2},
            "priority_one_headroom": {"search": 12}, "priority_headroom_applied": floor == 14,
            "requested_cost": {"search": cost}, "reserved_other": {"search": reserved},
            "effective_after": {"search": projected}, "blocked_resources": [{
                "resource": "search", "remaining": remaining, "reserved_other": reserved,
                "requested_cost": cost, "effective_after": projected, "minimum_remaining": floor}]}


@pytest.mark.parametrize("values", [(28, 25, 10, -7, 2), (30, 20, 12, -2, 14)])
def test_public_gate_uses_actual_scan_decision_not_zero_cost_dashboard(gate, values):
    task, data, calls = gate
    data["cache"]["refresh_budget"] = denial(*values)
    before = copy.deepcopy(data)
    result = pi.dispatch_gate(task)
    assert result["action"] == "defer"
    assert result["defer_for"] == "10m"
    info = result["wip_diagnostics"]
    assert info["authoritative"] is False
    assert info["snapshot"]["state"] == "stale"
    assert info["snapshot"]["age_seconds"] == 354
    assert info["snapshot"]["ttl_seconds"] == 300
    assert info["snapshot"]["retry_at"] == "2026-10-28T22:14:22+00:00"
    assert info["refresh_budget"]["blocked_resources"][0]["minimum_remaining"] == values[-1]
    assert info["wip"] == {"max_active": 10, "observed_active": 26,
                           "observation": "complete_snapshot", "limit_reached_in_observation": True}
    for text in (f"остаток {values[0]}", f"резервы {values[1]}", f"стоимость {values[2]}",
                 f"прогноз {values[3]}", f"безопасный остаток {values[4]}", "полный WIP: 26"):
        assert text in result["reason"]
    assert calls == [{"use_cache": False, "refresh_diagnostics": True}]
    assert data == before


@pytest.mark.parametrize("cache,state", [
    ({}, "missing"), ({"source": "none", "available": False}, "missing"),
    ({"complete": False}, "partial"), ({"complete": True, "stale": True}, "stale"),
])
def test_public_gate_distinguishes_missing_partial_and_stale_without_admission(gate, cache, state):
    task, data, _ = gate
    data["cache"] = cache
    result = pi.dispatch_gate(task)
    assert result["action"] == "defer"
    assert result["wip_diagnostics"]["snapshot"]["state"] == state
    assert "снимок: " + state in result["reason"]
    if not cache.get("complete"):
        assert result["wip_diagnostics"]["wip"]["observation"] == "partial_snapshot"


def test_zero_cost_budget_cannot_invent_legacy_scan_cost_or_empty_wip(gate):
    task, data, _ = gate
    data["diagnostics"] = {}
    result = pi.dispatch_gate(task)
    assert result["action"] == "defer"
    assert result["wip_diagnostics"]["refresh_budget"] is None
    assert result["wip_diagnostics"]["wip"]["observed_active"] is None
    assert result["wip_diagnostics"]["wip"]["observation"] == "unavailable"
    assert "стоимость 0" not in result["reason"]


def test_refresh_exception_is_separate_and_does_not_publish_raw_error_or_old_budget(gate, monkeypatch):
    task, data, _ = gate
    data["cache"]["refresh_budget"] = denial()

    def failed(*args, **kwargs):
        raise RuntimeError("https://user:SECRETTOKEN@api.example.invalid")

    monkeypatch.setattr(pi, "analyze", failed)
    result = pi.dispatch_gate(task)
    assert result["action"] == "defer"
    assert result["wip_diagnostics"]["snapshot"]["refresh_state"] == "failed"
    assert result["wip_diagnostics"]["refresh_budget"] is None
    assert result["wip_diagnostics"]["snapshot"]["retry_at"] is None
    assert "SECRETTOKEN" not in json.dumps(result)


def test_new_diagnostics_publish_only_allowlisted_scalar_fields(gate):
    task, data, _ = gate
    raw = denial()
    raw.update(token="SECRETTOKEN", reason="SECRETTOKEN", state=["SECRETTOKEN"])
    raw["blocked_resources"].append({"resource": ["SECRETTOKEN"]})
    raw["blocked_resources"][0]["private_error"] = "SECRETTOKEN"
    data["cache"].update(refresh_budget=raw, refresh_blocked_reason="SECRETTOKEN",
                         refresh_deferred_until="SECRETTOKEN", age_seconds="SECRETTOKEN")
    result = pi.dispatch_gate(task)
    assert result["action"] == "defer"
    assert "SECRETTOKEN" not in json.dumps(result)
    assert result["wip_diagnostics"]["snapshot"]["age_seconds"] is None


def test_a_different_denial_does_not_reuse_the_previous_scan_arithmetic(gate):
    task, data, _ = gate
    data["cache"]["refresh_budget"] = denial()
    data["cache"]["refresh_blocked"] = "worker_paused"
    result = pi.dispatch_gate(task)
    assert result["action"] == "defer"
    assert result["wip_diagnostics"]["snapshot"]["refresh_state"] == "worker_paused"
    assert result["wip_diagnostics"]["refresh_budget"] is None


def test_fresh_wip_limit_remains_separate_from_snapshot_refusal(gate, isolated_db):
    task, data, calls = gate
    data["cache"] = {"complete": True, "stale": False}
    result = pi.dispatch_gate(task)
    assert result["action"] == "defer"
    assert "активных PR 26, порог новых задач 10" in result["reason"]
    assert result["wip_diagnostics"]["snapshot"]["state"] == "fresh"
    assert result["wip_diagnostics"]["wip"]["limit_reached_in_observation"] is True
    assert calls == []
    data["diagnostics"]["review_backlog"] = []
    assert pi.dispatch_gate(task) is None
