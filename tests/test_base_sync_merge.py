"""Base-sync owner MERGE lane (issue #42): BEHIND -> update -> merge -> done."""

import pytest

from promptpilot import project_pipeline as pp

HEAD = "3e7be634fba4504b0a768662122d3cc12d5572d9"
NEW_HEAD = "b" * 40
OWNER = {"number": 1232, "head": HEAD, "stage": "integration-review"}
FALLBACK = {"action": "fallback",
            "reason": "single-flight/base-sync owner requires the full skill"}


def owner_health(merge_executable=[]):
    return {
        "state": "yellow",
        "integration_owner": dict(OWNER),
        "review_candidates": [dict(OWNER)],
        "content_review_candidates": [],
        "merge_executable": merge_executable,
        "findings": [{"code": "single_flight_barrier", "severity": "yellow",
                      "pr": OWNER["number"]}],
    }


def base_config(**extra):
    config = {"repository": "owner/repo", "base_branch": "main",
              "trusted_account": "pp-bot"}
    config.update(extra)
    return config


def fake_snapshot():
    return {"state": "OPEN", "baseRefName": "main", "isDraft": False,
            "labelsComplete": True, "labels": ["ship"], "headRefOid": HEAD,
            "edges": []}


class ScriptedGH:
    """gh double: pops one scripted response per JSON call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def json(self, *args, input_value=None):
        self.calls.append((args, input_value))
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def install_common(monkeypatch, *, health=None, snapshot=None, checks=None):
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_a, **_k: health or owner_health())
    monkeypatch.setattr(pp, "stable_timeline",
                        lambda *_a, **_k: snapshot or fake_snapshot())
    monkeypatch.setattr(pp, "pr_checks", lambda *_a, **_k: checks or (
        {"mergeStateStatus": "BEHIND", "mergeable": "MERGEABLE", "body": ""}, []))


def test_behind_owner_issues_update_branch_lease(monkeypatch):
    install_common(monkeypatch)

    result = pp.next_merge(object(), base_config(base_sync_merge=True))

    assert result["action"] == "merge"
    lease = pp.decode_lease(result["lease"])
    assert lease["mode"] == "update-branch"
    assert lease["base_sync_owner"] is True
    assert lease["number"] == OWNER["number"]
    assert lease["head"] == HEAD


def test_behind_owner_without_optin_keeps_full_skill_fallback(monkeypatch):
    install_common(monkeypatch)

    result = pp.next_merge(object(), base_config())

    assert result == FALLBACK


def test_rest_only_owner_keeps_fallback_even_with_optin(monkeypatch):
    install_common(monkeypatch, health=owner_health(merge_executable=None))

    result = pp.next_merge(object(), base_config(
        base_sync_merge=True, fallback_handoff="target-v1"))

    assert result["action"] == "fallback"
    assert "full skill" in result["reason"]


def test_clean_owner_reserves_intent_and_issues_merge_lease(monkeypatch):
    install_common(monkeypatch, checks=(
        {"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE", "body": ""},
        [{"name": "ci", "conclusion": "SUCCESS"}]))
    intent = {"id": 7, "number": OWNER["number"], "head": HEAD,
              "proof_sha256": "p", "body_sha256": "b", "issues": []}
    monkeypatch.setattr(pp, "epoch",
                        lambda *_a, **_k: {"hash": "e", "anchor_id": "a", "edges": []})
    monkeypatch.setattr(pp, "proof", lambda *_a, **_k: {"fake": "proof"})
    monkeypatch.setattr(pp, "trusted_ship_authorized", lambda *_a, **_k: True)
    monkeypatch.setattr(pp, "reserve_merge_intent", lambda *_a, **_k: (intent, [intent]))

    result = pp.next_merge(object(), base_config(base_sync_merge=True))

    assert result["action"] == "merge"
    lease = pp.decode_lease(result["lease"])
    assert lease["intent"]["id"] == 7
    assert lease["base_sync_owner"] is True


def test_clean_owner_with_failing_checks_waits(monkeypatch):
    install_common(monkeypatch, checks=(
        {"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE", "body": ""},
        [{"name": "ci", "conclusion": "FAILED"}]))
    monkeypatch.setattr(pp, "epoch",
                        lambda *_a, **_k: {"hash": "e", "anchor_id": "a", "edges": []})
    monkeypatch.setattr(pp, "proof", lambda *_a, **_k: {"fake": "proof"})
    monkeypatch.setattr(pp, "trusted_ship_authorized", lambda *_a, **_k: True)

    result = pp.next_merge(object(), base_config(base_sync_merge=True))

    assert result["action"] == "wait"
    assert result["number"] == OWNER["number"]


def test_complete_update_branch_verifies_and_reports_new_head():
    gh = ScriptedGH([
        {"state": "open", "head": {"sha": HEAD}},
        {"message": "Updating pull request."},
        {"state": "open", "head": {"sha": NEW_HEAD}},
    ])
    lease = {"version": 1, "stage": "merge", "mode": "update-branch",
             "repository": "owner/repo", "number": OWNER["number"],
             "head": HEAD, "base_sync_owner": True}

    result = pp.complete_merge(gh, base_config(), pp.encode_lease(lease))

    assert result == {"action": "updated", "stage": "merge",
                      "number": OWNER["number"], "old_head": HEAD,
                      "new_head": NEW_HEAD,
                      "next": result["next"]}
    assert gh.calls[1][0][:2] == ("api", "repos/owner/repo/pulls/1232/update-branch")
    assert gh.calls[1][0][2:] == ("--method", "PUT", "--input", "-")
    assert gh.calls[1][1] == {"expected_head_sha": HEAD}


def test_complete_update_branch_conflict_publishes_nothing():
    gh = ScriptedGH([
        {"state": "open", "head": {"sha": HEAD}},
        pp.PipelineError("422 Merge conflict after committing the merge"),
    ])
    lease = {"version": 1, "stage": "merge", "mode": "update-branch",
             "repository": "owner/repo", "number": OWNER["number"],
             "head": HEAD, "base_sync_owner": True}

    with pytest.raises(pp.PipelineError, match="Merge conflict"):
        pp.complete_merge(gh, base_config(), pp.encode_lease(lease))

    # The failure is the branch update itself: no polls, no merge PUT, and
    # no done marker was ever published (the issue #42 incident).
    assert len(gh.calls) == 2


def test_complete_update_branch_refuses_changed_owner_head():
    gh = ScriptedGH([{"state": "open", "head": {"sha": NEW_HEAD}}])
    lease = {"version": 1, "stage": "merge", "mode": "update-branch",
             "repository": "owner/repo", "number": OWNER["number"],
             "head": HEAD, "base_sync_owner": True}

    with pytest.raises(pp.PipelineError, match="owner changed"):
        pp.complete_merge(gh, base_config(), pp.encode_lease(lease))

    assert len(gh.calls) == 1


def test_barrier_blocks_merge_matrix():
    health = owner_health()

    def lease(**extra):
        return {"stage": "merge", "number": OWNER["number"], **extra}

    assert pp._barrier_blocks_merge(health, lease()) is True
    assert pp._barrier_blocks_merge(
        health, lease(base_sync_owner=True)) is False
    other = owner_health()
    other["findings"][0]["pr"] = 999
    assert pp._barrier_blocks_merge(
        other, lease(base_sync_owner=True)) is True
    assert pp._barrier_blocks_merge(
        {"findings": []}, lease()) is False
