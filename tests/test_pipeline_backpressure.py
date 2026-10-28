from types import SimpleNamespace

import pytest

from promptpilot import pipeline_insights as pi


@pytest.fixture
def admission(monkeypatch):
    policy = {"max_active": 2,
              "active_fields": ["review_backlog", "merge_candidates", "fix_candidates"],
              "candidate_field": "fix_candidates",
              "allow_when_match": [{"key": "stage", "values": ["review"]},
                                   {"key": "priority", "values": [0]}]}
    profile = {"queues": [{"id": "fix", "series_contains": "Example - FIX",
                           "dispatch_gate": {"backpressure": policy}}]}
    data = {"cache": {"complete": True, "stale": False}, "queues": [],
            "diagnostics": {"review_backlog": [{"number": 1, "head": "a"},
                                               {"number": 2, "head": "b"}],
                            "merge_candidates": [{"number": 1, "head": "a"}],
                            "fix_candidates": [{"number": 3, "head": "", "stage": "fix-issue", "priority": 2}]}}
    monkeypatch.setattr(pi, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pi.db, "list_series", lambda: [])
    monkeypatch.setattr(pi, "read_cached", lambda *args: data)
    monkeypatch.setattr(pi, "analyze", lambda *args, **kwargs: data)
    task = SimpleNamespace(series_id=1, series_title="Example - FIX", prompt="Example - FIX")
    return task, data


def test_public_gate_stops_new_work_without_model(admission):
    task, _ = admission
    gate = pi.dispatch_gate(task)
    assert gate["action"] == "defer"
    assert "активных PR 2" in gate["reason"]  # duplicated number counted once


@pytest.mark.parametrize("candidate", [
    {"number": 4, "head": "c", "stage": "review", "priority": 2},
    {"number": 4, "head": "", "stage": "fix-issue", "priority": 0},
])
def test_exception_is_scoped_not_an_open_intake_gate(admission, candidate):
    task, data = admission
    data["diagnostics"]["fix_candidates"].append(candidate)
    gate = pi.dispatch_gate(task)
    assert gate["action"] == "restrict_prompt"
    assert gate["allowed_numbers"] == [4]
    assert "Остальные новые задачи не начинай" in gate["prompt_constraint"]


def test_below_threshold_preserves_existing_stage(admission):
    task, data = admission
    data["diagnostics"]["review_backlog"].pop()
    assert pi.dispatch_gate(task) is None


@pytest.mark.parametrize("cache", [
    {"complete": False}, {"complete": True, "stale": True},
    {"complete": True, "refresh_blocked": "budget"},
])
def test_unknown_snapshot_does_not_open_intake(admission, cache):
    task, data = admission
    data["cache"] = cache
    assert pi.dispatch_gate(task)["action"] == "defer"


def test_missing_diagnostic_is_not_zero_wip(admission):
    task, data = admission
    del data["diagnostics"]["review_backlog"]
    assert pi.dispatch_gate(task)["action"] == "defer"


def test_expired_cache_requests_real_budgeted_refresh(admission, monkeypatch):
    task, data = admission
    data["cache"]["stale"] = True
    captured = {}
    def analyze(*args, **kwargs):
        captured.update(kwargs)
        data["cache"]["stale"] = False
        return data
    monkeypatch.setattr(pi, "analyze", analyze)
    assert pi.dispatch_gate(task)["action"] == "defer"
    assert captured == {"use_cache": False, "refresh_diagnostics": True}


def test_refresh_exception_still_defers_without_agent(admission, monkeypatch):
    task, data = admission
    data["cache"]["stale"] = True
    def analyze(*args, **kwargs):
        raise RuntimeError("network unavailable")
    monkeypatch.setattr(pi, "analyze", analyze)
    assert pi.dispatch_gate(task)["action"] == "defer"


def test_worker_passes_exception_scope_to_real_provider_boundary(admission, monkeypatch):
    from promptpilot import worker
    task, data = admission
    task.id = 10
    task.provider = "test-provider"
    task.working_dir = None
    task.machine = None
    data["diagnostics"]["fix_candidates"].append(
        {"number": 4, "stage": "review", "head": "c"})
    monkeypatch.setattr(pi, "execution_route", lambda *_args, **_kwargs: {
        "action": "prompt", "mode": "skill", "prompt": "rewritten original",
        "profile_id": "example", "queue_id": "fix"})
    monkeypatch.setattr(worker, "load_providers", lambda: {"test-provider": {"executor": "herdr"}})
    captured = {}
    monkeypatch.setattr(worker, "_execute_herdr_task", lambda *_args, **kwargs: captured.update(kwargs))
    worker._execute_task_body(task)
    assert "только номера 4" in captured["prompt_override"]
    assert "Остальные новые задачи не начинай" in captured["prompt_override"]
