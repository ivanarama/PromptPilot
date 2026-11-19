"""Recheck completion evidence independently of a pause on new TAIL work."""
import pytest

from promptpilot.pipeline_errors import PipelineError
from tail_admission_fixture import add_comment, source_fixture
from test_tail_drain_cli import change_source, gh_bridge as gh_bridge, http_cli as http_cli
from test_tail_drain_cli import local_cli as local_cli, wire_bridge as wire_bridge


@pytest.mark.parametrize("label", ["hold", "no-tail"])
def test_paused_completions_survive_public_cli_recheck_and_restart(label, http_cli):
    empty = source_fixture(items=[])
    legacy = source_fixture(protocol="legacy", number=43, merged_at="2026-02-01T00:00:00Z")
    add_comment(legacy, "<!-- pp:tail-done -->")
    versioned = source_fixture(number=44)
    review = versioned["comments"][0]
    add_comment(versioned, f"<!-- pp:tail-done review-comment={review['id']} review-updated={review['updated_at']} -->")
    cli, peer = http_cli(empty, legacy, versioned)

    first = cli.run("tail-next")
    assert first["ready"] == []
    assert {obs["number"]: obs["completion_kind"] for obs in first["observations"]} == {
        42: "empty", 43: "legacy", 44: "versioned",
    }
    for obs in first["observations"]:
        number = obs["number"]
        change_source(peer.sources[number], label)
        current = cli.run("tail-gate", "--number", str(number), "--version", obs["version"])
        assert (current["state"], current["completion_kind"]) == ("resolved", obs["completion_kind"])
    backlog = cli.backlog()
    assert backlog["counts"] == {} and backlog["candidates"] == []
    assert backlog["completions"] == {"empty": 1, "legacy": 1, "versioned": 1}
    assert peer.writes == []


def test_paused_version_loses_completion_when_review_is_deleted(local_cli):
    run, peer = local_cli
    source = peer.sources[42]
    review = source["comments"][0]
    add_comment(source, f"<!-- pp:tail-done review-comment={review['id']} review-updated={review['updated_at']} -->")
    initial, = run("tail-next")["observations"]
    assert initial["completion_kind"] == "versioned"

    change_source(source, "hold")
    change_source(source, "delete")
    assert run("tail-gate", "--number", "42", "--version", initial["version"], code=2)["action"] == "error"
    backlog = run("tail-backlog")
    assert backlog["counts"] == {"invalid-source": 1} and backlog["completions"] == {}
    assert "review or proof is missing" in backlog["candidates"][0]["last_reason"]
    assert peer.writes == []


def test_failed_recheck_preserves_newer_completion(local_cli, monkeypatch):
    run, peer = local_cli
    candidate, = run("tail-next")["ready"]
    target = ("--number", "42", "--version", candidate["version"])
    source = peer.sources[42]
    review = source["comments"][0]
    add_comment(source, f"<!-- pp:tail-done review-comment={review['id']} review-updated={review['updated_at']} -->")
    change_source(source, "no-tail")
    original = peer.reply

    def fail_after_other_gate(args, payload):
        assert args == ("api", "repos/owner/project/pulls/42")
        monkeypatch.setattr(peer, "reply", original)
        assert run("tail-gate", *target)["completion_kind"] == "versioned"
        raise PipelineError("GitHub timed out during the older read")

    monkeypatch.setattr(peer, "reply", fail_after_other_gate)
    assert "timed out" in run("tail-gate", *target, code=2)["error"]
    backlog = run("tail-backlog")
    assert backlog["counts"] == {} and backlog["completions"] == {"versioned": 1}
    assert peer.writes == []
