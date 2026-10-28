"""Shared byte-level goldens and public read-only pipelinectl inventory paths."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from promptpilot import project_pipeline as pp
from promptpilot.pr_identity import PRIdentityV1, identity_from_graphql, identity_from_rest


FIXTURE = Path(__file__).parent / "fixtures" / "pr-identity-v1.json"
GOLDENS = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
CONFIG = {"repository": "owner/repo", "trusted_account": "owner",
          "health_command": ["health", "--json"], "sync_base_before_health": True}


@pytest.mark.parametrize("golden", GOLDENS, ids=lambda case: case["name"])
def test_shared_utf8_canonical_contract_is_identical_for_rest_and_graphql(golden):
    gql = identity_from_graphql(golden["graphql"], repository="owner/repo", repository_id="R_BASE")
    rest = identity_from_rest(golden["rest"], repository="owner/repo", repository_id="R_BASE")
    assert isinstance(gql, PRIdentityV1) and rest == gql
    assert gql.canonical_bytes() == bytes.fromhex(golden["canonical_utf8_hex"])
    assert gql.sha256 == golden["identity_sha256"]
    assert gql.canonical_bytes().endswith(b"\n") and b"\r" not in gql.canonical_bytes()
    assert not gql.canonical_bytes().startswith(b"\xef\xbb\xbf")


def test_identity_changes_with_repository_or_ref_even_when_head_stays_identical():
    same, fork = [identity_from_graphql(g["graphql"], repository="owner/repo", repository_id="R_BASE")
                  for g in GOLDENS]
    assert same.head_ref == fork.head_ref and same.head_sha == fork.head_sha
    assert same.sha256 != fork.sha256
    assert replace(fork, head_ref="other").sha256 != fork.sha256
    assert replace(fork, head_repository_id="R_OTHER").sha256 != fork.sha256
    assert replace(fork, head_repository="renamed/repo").sha256 != fork.sha256
    assert replace(fork, pr_id="PR_NEW").sha256 != fork.sha256
    assert replace(fork, head_ref="café").sha256 != replace(fork, head_ref="cafe\u0301").sha256


def _transport(monkeypatch, prs):
    calls = []

    class ReadOnlyGitHub:
        def json(self, *args):
            calls.append(args)
            assert args[:1] == ("api",) and "-X" not in args and "--method" not in args
            if args == ("api", "repos/owner/repo"):
                return {"full_name": "owner/repo", "node_id": "R_BASE"}
            assert args[1] == "graphql" and "mutation" not in args[3]
            for field in ("headRepository", "baseRepository", "headRefName", "isCrossRepository"):
                assert field in args[3]
            number = int(next(arg.split("=", 1)[1] for arg in args if arg.startswith("number=")))
            return {"data": {"repository": {"id": "R_BASE", "nameWithOwner": "owner/repo",
                                             "pullRequest": copy.deepcopy(prs[number])}}}

    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    return calls


def _run_inventory(tmp_path, monkeypatch, capsys, prs, health, *, health_file=True):
    calls = _transport(monkeypatch, prs)
    config_path, health_path = tmp_path / "config.json", tmp_path / "health.json"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    health_path.write_text(json.dumps(health), encoding="utf-8")
    args = ["--config", str(config_path), "inventory"]
    if health_file:
        args += ["--health-file", str(health_path)]
    else:
        def run_health(config, **kwargs):
            assert config["sync_base_before_health"] is False
            assert kwargs == {}
            return copy.deepcopy(health)
        monkeypatch.setattr(pp, "run_health", run_health)
    before = config_path.read_bytes()
    code = pp.run(args)
    result = json.loads(capsys.readouterr().out)
    assert config_path.read_bytes() == before
    return code, result, calls


@pytest.mark.parametrize("health_file", [True, False])
def test_public_inventory_preserves_order_without_leases_or_mutations(
        tmp_path, monkeypatch, capsys, health_file):
    prs = {n: dict(copy.deepcopy(GOLDENS[i % 2]["graphql"]), number=n, id=f"PR_{n}")
           for i, n in enumerate([19, 4, 42])}
    items = [{"number": n, "head": "a" * 40, "priority": i} for i, n in enumerate(prs)]
    health = {"state": "green", "findings": [], "review_candidates": items,
              "content_review_candidates": items, "merge_executable": items[1:],
              "integration_owner": items[1]}
    original = copy.deepcopy(health)
    code, result, calls = _run_inventory(tmp_path, monkeypatch, capsys, prs, health,
                                        health_file=health_file)
    assert code == 0 and health == original
    for field in ("review_candidates", "content_review_candidates", "merge_executable"):
        assert [x["number"] for x in result[field]] == [x["number"] for x in health[field]]
        assert all(x["identity_sha256"] and x["base_ref_oid"] == "b" * 40 for x in result[field])
    assert result["identity_authority"] == "read-only" and not result["identity_v2_enabled"]
    assert "lease" not in result and len(calls) == 4


@pytest.mark.parametrize("problem", [
    "non-main", "deleted-fork", "empty-sha", "uppercase-sha", "missing-ref", "surrogate",
    "newline", "wrong-repository", "wrong-repository-id", "wrong-number", "cross-flag",
    "head-moved", "closed", "draft", "missing-node-id", "bool-number", "bad-base-sha",
])
def test_public_inventory_rejects_invalid_targets_and_explains_why(
        tmp_path, monkeypatch, capsys, problem):
    pr = copy.deepcopy(GOLDENS[1]["graphql"])
    if problem == "non-main":
        pr["baseRefName"] = "release"
    elif problem == "deleted-fork":
        pr["headRepository"] = None
    elif problem in {"empty-sha", "uppercase-sha"}:
        pr["headRefOid"] = "" if problem == "empty-sha" else "A" * 40
    elif problem in {"missing-ref", "surrogate", "newline"}:
        pr["headRefName"] = {"missing-ref": "", "surrogate": "\ud800", "newline": "x\npr-id=other"}[problem]
    elif problem == "wrong-repository":
        pr["baseRepository"]["nameWithOwner"] = "attacker/repo"
    elif problem == "wrong-repository-id":
        pr["baseRepository"]["id"] = "R_OTHER"
    elif problem == "wrong-number":
        pr["number"] = 41
    elif problem == "cross-flag":
        pr["isCrossRepository"] = False
    elif problem == "head-moved":
        pr["headRefOid"] = "c" * 40
    elif problem in {"closed", "draft"}:
        pr.update(state="CLOSED" if problem == "closed" else "OPEN", isDraft=problem == "draft")
    elif problem == "missing-node-id":
        pr.pop("id")
    elif problem == "bool-number":
        pr["number"] = True
    elif problem == "bad-base-sha":
        pr["baseRefOid"] = None
    good = dict(copy.deepcopy(GOLDENS[0]["graphql"]), number=7, id="PR_7")
    items = [{"number": n, "head": "a" * 40} for n in [42, 7]]
    health = {"review_candidates": items, "parallel_review_candidates": items,
              "content_review_candidates": items, "merge_executable": items,
              "integration_owner": items[0], "findings": []}
    code, result, _ = _run_inventory(tmp_path, monkeypatch, capsys, {42: pr, 7: good}, health)
    assert code == 0
    for field in ("review_candidates", "parallel_review_candidates", "content_review_candidates", "merge_executable"):
        assert [x["number"] for x in result[field]] == [7]
    assert result["integration_owner"] is None
    assert result["findings"] == [{"code": "invalid_pr_identity", "number": 42,
                                    "detail": result["findings"][0]["detail"]}]
    assert result["findings"][0]["detail"]


def test_base_advance_is_adjacent_snapshot_only_and_does_not_change_identity():
    pr = copy.deepcopy(GOLDENS[1]["graphql"])
    before = identity_from_graphql(pr, repository="owner/repo", repository_id="R_BASE")
    pr["baseRefOid"] = "c" * 40
    after = identity_from_graphql(pr, repository="owner/repo", repository_id="R_BASE")
    assert before.canonical_bytes() == after.canonical_bytes() and before.sha256 == after.sha256


def test_rest_rejects_deleted_repository_and_does_not_substitute_numeric_ids():
    pr = copy.deepcopy(GOLDENS[1]["rest"])
    pr["head"]["repo"] = None
    with pytest.raises(pp.PipelineError, match="deleted fork"):
        identity_from_rest(pr, repository="owner/repo", repository_id="R_BASE")
    pr = copy.deepcopy(GOLDENS[1]["rest"])
    pr["head"]["repo"].pop("node_id")
    pr["head"]["repo"]["id"] = 123
    with pytest.raises(pp.PipelineError):
        identity_from_rest(pr, repository="owner/repo", repository_id="R_BASE")


def test_cli_rejects_lossy_transport_decode_as_explicit_error(tmp_path, monkeypatch, capsys):
    config_path, health_path = tmp_path / "config.json", tmp_path / "health.json"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    health_path.write_text(json.dumps({"review_candidates": []}), encoding="utf-8")
    monkeypatch.setattr(pp.shutil, "which", lambda name: "gh")

    def invalid_utf8(*args, **kwargs):
        assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "strict"
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(subprocess, "run", invalid_utf8)
    assert pp.run(["--config", str(config_path), "inventory", "--health-file", str(health_path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "error" and "codec" in result["error"]
