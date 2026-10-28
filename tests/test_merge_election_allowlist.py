"""MERGE election uses the same authority as its signed fallback gate."""

from promptpilot import project_pipeline as pp


FIRST = "a" * 40
OTHER = "b" * 40


def health():
    return {
        "state": "green", "findings": [], "integration_owner": None,
        "review_candidates": [], "content_review_candidates": [],
        "merge_executable": [
            {"number": 42, "head": FIRST, "stage": "merge"},
            {"number": 99, "head": OTHER, "stage": "merge"},
        ],
    }


def rest_queue():
    return [
        {"number": 99, "head": {"sha": OTHER}},
        {"number": 42, "head": {"sha": FIRST}},
    ]


def snapshot(number):
    head = FIRST if number == 42 else OTHER
    return {
        "headRefOid": head, "baseRefOid": "c" * 40,
        "baseRefName": "main", "state": "OPEN", "isDraft": False,
        "labels": ["ship"], "labelsComplete": True, "updatedAt": "now",
        "edges": [{"cursor": "c1", "node": {"__typename": "PullRequestCommit",
                                          "id": "anchor", "commit": {"oid": head}}}],
    }


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(tmp_path / "lease.key"))
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: health())
    monkeypatch.setattr(pp, "list_ship", lambda *_: rest_queue())
    monkeypatch.setattr(pp, "stable_timeline", lambda _gh, _config, number: snapshot(number))
    return {"repository": "owner/repo", "trusted_account": "owner",
            "base_branch": "main", "fallback_handoff": "target-v1"}


def test_next_merge_uses_health_first_even_when_rest_sort_disagrees(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    result = pp.next_merge(object(), config)
    assert result["action"] == "fallback"
    assert result["target"]["number"] == 42


def test_next_merge_waits_when_health_target_disagrees_with_rest(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pp, "list_ship", lambda *_: rest_queue()[:1])
    result = pp.next_merge(object(), config)
    assert result["action"] == "wait"
    assert result["number"] == 42


def test_next_merge_ignores_rest_only_priority_without_fallback_contract(monkeypatch, tmp_path):
    config = setup(monkeypatch, tmp_path)
    config.pop("fallback_handoff")
    result = pp.next_merge(object(), config)
    assert result["action"] == "fallback"
    assert "proof" in result["reason"]
