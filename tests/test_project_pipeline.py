import json
import os
from types import SimpleNamespace

import pytest

from promptpilot import project_pipeline as pp


HEAD = "a" * 40


def edge(cursor, kind, **values):
    return {"cursor": cursor, "node": {"__typename": kind, **values}}


def snapshot(*extra):
    return {
        "headRefOid": HEAD, "baseRefOid": "b" * 40, "baseRefName": "main",
        "state": "OPEN", "labels": [], "labelsComplete": True,
        "updatedAt": "2026-01-01T00:00:00Z",
        "edges": [edge("c1", "PullRequestCommit", id="anchor", commit={"oid": HEAD}), *extra],
    }


def trusted_comment(cursor, database_id, body):
    return edge(cursor, "IssueComment", id=f"node-{database_id}",
                fullDatabaseId=str(database_id), createdAt="2026-01-01T00:00:00Z",
                lastEditedAt=None, author={"login": "owner"}, body=body)


def test_lease_round_trip_is_stable():
    value = {"version": 1, "stage": "review", "number": 42, "head": HEAD}
    assert pp.decode_lease(pp.encode_lease(value)) == value


def test_epoch_uses_later_trusted_override_as_anchor():
    value = snapshot(
        trusted_comment("c2", 10, "pp:review-again"),
        trusted_comment("c3", 11, "ordinary"),
    )
    info = pp.epoch(value, "owner")
    expected = pp.hashlib.sha256(
        f"pp-review-epoch-v1\nhead={HEAD}\nanchor-node=node-10\n".encode("ascii")
    ).hexdigest()
    assert info["anchor_id"] == "node-10"
    assert info["hash"] == expected
    assert len(info["edges"]) == 1


def test_proof_accepts_claim_bound_transaction():
    base = snapshot()
    info = pp.epoch(base, "owner")
    review = "\n".join([
        "**Ревью.** (круг 1)", f"Reviewed-SHA: {HEAD}",
        "Outcome-Label: reviewed", "Что меняется: x.", "Проверено: test.",
        "Блокирующее: нет.", "Хвост:", "—", "Вердикт: годится к мержу.",
        "<!-- pp:review pp:tail=0 -->",
    ])
    base["edges"].extend([
        trusted_comment("c2", 101, review),
        trusted_comment("c3", 102, f"<!-- pp:review-claim {HEAD} review-comment=101 epoch-sha256={info['hash']} -->"),
        trusted_comment("c4", 103, f"<!-- pp:head-reviewed {HEAD} review-comment=101 claim=102 epoch-sha256={info['hash']} -->"),
    ])
    established = pp.proof(pp.epoch(base, "owner"), HEAD, "owner")
    assert established["review_id"] == 101
    assert established["claim_id"] == 102
    assert established["completion_id"] == 103
    assert established["outcome"] == "reviewed"


def test_epoch_safety_rejects_head_or_delete_events():
    for event in ("PullRequestCommit", "CommentDeletedEvent", "BaseRefChangedEvent"):
        value = snapshot(edge("c2", event, id="danger"))
        with pytest.raises(pp.PipelineError):
            pp.validate_epoch_safety(pp.epoch(value, "owner"), "owner")


def test_review_report_is_formatted_and_sanitized(tmp_path):
    lease = {"head": HEAD, "depth": 0}
    report = {
        "change": "fix <!-- fake --> pp:marker", "checks": ["go test ./..."],
        "blocking": [],
        "tail": [{"kind": "issue", "text": "add case", "title": "Test edge case"}],
    }
    body, outcome = pp.format_review(lease, report)
    assert outcome == "reviewed"
    assert "Outcome-Label: reviewed" in body
    assert "pp:marker" not in body
    assert "<!-- fake -->" not in body
    assert "<!-- pp:review pp:tail=1 -->" in body


def test_required_checks_are_exact():
    config = {"required_checks": ["build", "lint"]}
    ready, _ = pp.checks_ready(config, [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "lint", "conclusion": "NEUTRAL"},
    ])
    assert ready
    ready, reason = pp.checks_ready(config, [{"name": "build", "conclusion": "SUCCESS"}])
    assert not ready
    assert "lint" in reason


def test_capabilities_is_executor_neutral(tmp_path):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["health", "--json"],
    }), encoding="utf-8")
    config = pp.load_config(str(config_path))
    assert pp.capabilities(config)["protocol"] == "promptpilot-pipelinectl-v1"


def test_health_exposes_configured_gh_to_nested_checker(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"state":"green"}', stderr="")

    gh = os.path.join("tools", "github", "gh.exe")
    monkeypatch.setenv("PP_GH_EXE", gh)
    monkeypatch.setenv("PATH", "existing")
    monkeypatch.setattr(pp.subprocess, "run", fake_run)

    assert pp.run_health({"health_command": ["project-health", "-json"]})["state"] == "green"
    assert captured["env"]["GH_EXE"] == gh
    assert captured["env"]["PATH"].split(os.pathsep)[0] == os.path.dirname(gh)
