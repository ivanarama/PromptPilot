"""Resume the captured OneBase intent with the CI result that held up #2172."""
import copy
import json

import pytest

from promptpilot import project_pipeline as pp
from promptpilot.merge_finish_proof import REQUIRED
from tests.test_current_review_ship_cli import NUMBER, REPO, cli as cli


@pytest.fixture
def reserved_bench(cli, tmp_path, monkeypatch):
    gh, invoke = cli
    path = tmp_path / "pipelinectl.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["required_checks"] = sorted(REQUIRED)
    path.write_text(json.dumps(config), encoding="utf-8")

    status = gh.cap["status"]
    status.update(mergeStateStatus="UNSTABLE", headRefOid=gh.snapshot["headRefOid"])
    rows = []
    for check in status["statusCheckRollup"]:
        if check["name"] == "bench":
            check["conclusion"] = "FAILURE"
        rows.append({**check, "isRequired": check["name"] in REQUIRED,
                     "checkSuite": {"app": {"databaseId": 15368}}})
    head = gh.snapshot["headRefOid"]
    contexts = {"totalCount": len(rows), "pageInfo": {"hasNextPage": False}, "nodes": rows}
    pr = {**status, "state": "OPEN", "isDraft": False, "baseRefName": "main",
          "baseRefOid": gh.main_head, "reviewDecision": None,
          "commits": {"nodes": [{"commit": {"oid": head, "statusCheckRollup": {"contexts": contexts}}}]}}
    protection = {"requiresStatusChecks": True, "requiresStrictStatusChecks": True,
                  "requiredStatusChecks": [{"context": name, "app": {"databaseId": 15368}}
                                           for name in sorted(REQUIRED)]}
    repo = {"pullRequest": pr, "ref": {"target": {"oid": gh.main_head}, "branchProtectionRule": protection,
                                       "compare": {"status": "AHEAD", "behindBy": 0}}}
    original_json = gh.json

    def json_call(*args, input_value=None):
        if args[:2] == ("api", "graphql"):
            assert input_value["variables"]["head"] == head
            assert input_value["variables"]["number"] == NUMBER
            return {"data": {"repository": copy.deepcopy(repo)}}
        return original_json(*args, input_value=input_value)

    monkeypatch.setattr(gh, "json", json_call)
    return gh, invoke, repo, contexts


def test_reserved_bench_failure_reaches_exactly_one_merge_and_cleanup(reserved_bench):
    gh, invoke, _, _ = reserved_bench
    code, result, _ = invoke(pp, "next", "merge")
    assert code == 0 and result["action"] == "merge", result
    assert "bench" in result["warning"]
    lease = pp.decode_lease(result["lease"])
    assert lease["ready_owner"] and lease["intent"] == gh.cap["intent"]
    assert gh.mutations == []
    code, done, _ = invoke(pp, "complete", "merge", "--lease", result["lease"])
    assert code == 0 and done["action"] == "completed", done
    assert "bench" in done["warning"]
    assert [m for m in gh.mutations if m[0] == "PUT"] == [
        ("PUT", f"repos/{REPO}/pulls/{NUMBER}/merge", {"merge_method": "merge", "sha": lease["head"]})]
    posts = [m for m in gh.mutations if m[0] == "POST"]
    assert len(posts) == 1 and "pp:merge-cleanup-done" in posts[0][2]["body"]


@pytest.mark.parametrize("name,changes", [
    ("build", {"conclusion": "FAILURE"}),
    ("lint", {"status": "IN_PROGRESS", "conclusion": None}),
    ("smoke", {"conclusion": "SKIPPED"}),
    ("build", {"checkSuite": {"app": {"databaseId": 999}}}),
    ("bench", {"checkSuite": {"app": {"databaseId": 999}}}),
    ("bench", {"isRequired": True}),
    ("bench", {"conclusion": "SUCCESS"}),
    ("classify", {"conclusion": "FAILURE"}),
])
def test_ci_must_prove_only_the_optional_bench_failed(reserved_bench, name, changes):
    gh, invoke, _, contexts = reserved_bench
    next(row for row in contexts["nodes"] if row["name"] == name).update(changes)
    code, result, _ = invoke(pp, "next", "merge")
    assert code == 0 and result["action"] == "wait", result
    assert "lease" not in result and gh.mutations == []
    assert "checks are green" not in result["reason"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "truncated", "changed-head", "behind",
                                   "new-required-check", "review-requested"])
def test_incomplete_or_changed_github_evidence_stops_recovery(reserved_bench, fault):
    gh, invoke, repo, contexts = reserved_bench
    rows = contexts["nodes"]
    build = next(row for row in rows if row["name"] == "build")
    if fault == "missing":
        rows.remove(build)
        contexts["totalCount"] -= 1
    elif fault == "duplicate":
        rows.append(copy.deepcopy(build))
        contexts["totalCount"] += 1
    elif fault == "truncated":
        contexts["pageInfo"]["hasNextPage"] = True
    elif fault == "changed-head":
        repo["pullRequest"]["headRefOid"] = "d" * 40
    elif fault == "behind":
        repo["ref"]["compare"].update(status="DIVERGED", behindBy=1)
    elif fault == "new-required-check":
        repo["ref"]["branchProtectionRule"]["requiredStatusChecks"].append(
            {"context": "bench", "app": {"databaseId": 15368}})
    else:
        repo["pullRequest"]["reviewDecision"] = "REVIEW_REQUIRED"
    code, result, _ = invoke(pp, "next", "merge")
    assert code == 0 and result["action"] == "wait", result
    assert "lease" not in result and gh.mutations == []
    assert "checks are green" not in result["reason"]


@pytest.mark.parametrize("state", ["BLOCKED", "BEHIND", "DIRTY", "UNKNOWN"])
def test_other_merge_states_stay_visible_when_required_ci_passes(reserved_bench, state):
    gh, invoke, _, _ = reserved_bench
    gh.cap["status"]["mergeStateStatus"] = state
    code, result, _ = invoke(pp, "next", "merge")
    assert code == 0 and result["action"] == "wait", result
    assert state in result["reason"] and not gh.mutations


def test_unreserved_owner_with_failed_bench_gets_no_merge_lease(reserved_bench):
    gh, invoke, _, _ = reserved_bench
    gh.snapshot["edges"] = [
        edge for edge in gh.snapshot["edges"] if edge["node"].get("body") != gh.cap["intent"]["body"]]
    code, result, _ = invoke(pp, "next", "merge")
    assert code == 0 and result["action"] in {"wait", "fallback"}, result
    assert "lease" not in result and gh.mutations == []


@pytest.mark.parametrize("change", ["required-failure", "head-advanced"])
def test_final_read_detects_changes_before_put(reserved_bench, monkeypatch, change):
    gh, invoke, repo, contexts = reserved_bench
    code, result, _ = invoke(pp, "next", "merge")
    assert code == 0 and result["action"] == "merge", result
    original = gh.health
    health_reads = 0

    def health(*args, **kwargs):
        nonlocal health_reads
        health_reads += 1
        if health_reads == 2:  # The independent owner check immediately before PUT.
            if change == "head-advanced":
                repo["pullRequest"]["headRefOid"] = "d" * 40
            else:
                next(row for row in contexts["nodes"] if row["name"] == "lint")["conclusion"] = "FAILURE"
        return original(*args, **kwargs)

    monkeypatch.setattr(gh, "health", health)
    code, error, _ = invoke(pp, "complete", "merge", "--lease", result["lease"])
    assert code == 2, error
    assert health_reads == 2 and gh.mutations == []
