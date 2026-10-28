import pytest

from promptpilot import project_pipeline as pp

HEAD = "a" * 40


def health():
    owner = {"number": 42, "head": HEAD, "stage": "integration-merge-ready", "review_depth": 2}
    return {"state": "yellow", "integration_owner": owner,
            "findings": [{"severity": "yellow", "code": "single_flight_barrier", "pr": 42}],
            "merge_executable": [owner], "review_candidates": [], "content_review_candidates": []}


@pytest.mark.parametrize("authorized,outcome,state,expected", [
    (True, "reviewed", "CLEAN", "merge"),
    (False, "reviewed", "CLEAN", "fallback"),
    (True, "changes-requested", "CLEAN", "fallback"),
    (True, "reviewed", "BEHIND", "fallback"),
])
def test_public_next_merge_ready_owner_never_updates_or_infers_carry(monkeypatch, authorized, outcome, state, expected):
    config = {"repository": "owner/repo", "trusted_account": "owner", "base_branch": "main", "ready_owner_merge": True}
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_a, **_kw: health())
    monkeypatch.setattr(pp, "stable_timeline", lambda *_: {
        "state": "OPEN", "baseRefName": "main", "isDraft": False,
        "labelsComplete": True, "labels": ["ship", "reviewed"], "headRefOid": HEAD, "edges": []})
    monkeypatch.setattr(pp, "epoch", lambda *_: {"hash": "e", "anchor_id": "a", "edges": []})
    monkeypatch.setattr(pp, "proof", lambda *_: {"outcome": outcome})
    monkeypatch.setattr(pp, "trusted_ship_authorized", lambda *_: authorized)
    monkeypatch.setattr(pp, "pr_checks", lambda *_: ({"mergeStateStatus": state, "mergeable": "MERGEABLE"}, []))
    monkeypatch.setattr(pp, "checks_ready", lambda *_: (True, ""))
    result = pp.next_merge(object(), config)
    assert result["action"] == expected
    if expected == "merge":
        lease = pp.decode_lease(result["lease"])
        assert lease["ready_owner"] is True
        assert "mode" not in lease
        assert not pp._barrier_blocks_merge(health(), lease)
        changed = health()
        changed["integration_owner"]["head"] = "b" * 40
        assert pp._barrier_blocks_merge(changed, lease)


def test_public_completion_refuses_changed_owner_before_any_write(monkeypatch):
    lease = {"version": 1, "stage": "merge", "repository": "owner/repo", "number": 42,
             "head": HEAD, "ready_owner": True, "owner_stage": "integration-merge-ready"}
    changed = health()
    changed["integration_owner"]["head"] = "b" * 40
    monkeypatch.setattr(pp, "ensure_identity", lambda *_: None)
    monkeypatch.setattr(pp, "run_health", lambda *_a, **_kw: changed)
    with pytest.raises(pp.PipelineError, match="single-flight owner appeared"):
        pp.complete_merge(object(), {"repository": "owner/repo", "trusted_account": "owner"},
                          pp.encode_lease(lease))
