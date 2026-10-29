"""Wire changes must not create permission or silently discard evidence."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.pr_identity import identity_from_graphql
from promptpilot.review_routing import inspect_report
from promptpilot import review_routing_wire as wire


FIXTURES = Path(__file__).parent / "fixtures"
REPORTS = json.loads((FIXTURES / "review-routing-v1.json").read_text(encoding="utf-8"))["cases"]
IDENTITY = identity_from_graphql(
    json.loads((FIXTURES / "pr-identity-v1.json").read_text(encoding="utf-8"))["cases"][0]["graphql"],
    repository="owner/repo", repository_id="R_BASE")
OWNER = "00000000-0000-4000-8000-000000000001"


def publication(*, case=0, depth=2, budget=0):
    report = copy.deepcopy(REPORTS[case]["report"])
    report["head"] = IDENTITY.head_sha
    return {"protocol": "review-routing-v1", "kind": "publication",
            "pr": wire.pr_key(IDENTITY), "owner": OWNER, "identity": IDENTITY.to_dict(),
            "author": {"id": "U_AUTHOR", "login": "contributor"}, "epoch": "e" * 64,
            "anchor": "COMMIT_NODE", "depth": depth, "budget_used": budget,
            "audit":{'snapshot_sha256':'a'*64,'last_edge_cursor':'C_HEAD',
                     'last_edge_node_id':'COMMIT_NODE','base_ref_oid':'b'*40},
            "max_attempts": 2, "report": report, "resume_id": None,
            "report_sha256": inspect_report(report, IDENTITY.head_sha)["report_sha256"],
            "outcome": wire.outcome(report, depth=depth, budget_used=budget)}


@pytest.mark.parametrize("depth,budget,expected", [
    (0, 0, "changes-requested"), (1, 0, "changes-requested"),
    (2, 0, "technical-handoff"), (3, 1, "technical-handoff"),
    (4, 2, "engineering-handoff"),
])
def test_third_round_and_lifetime_budget_have_explicit_outcomes(depth, budget, expected):
    record = publication(depth=depth, budget=budget)
    assert record["outcome"] == expected
    assert wire.parse(wire.render(record)) == record


def test_reducing_current_limit_preserves_spent_slots_and_parks_instead_of_resetting():
    record=publication(depth=4,budget=2)
    record['max_attempts']=1
    record['outcome']=wire.outcome(record['report'],depth=4,budget_used=2,max_attempts=1)
    assert record['outcome']=='engineering-handoff'
    assert wire.parse(wire.render(record))['budget_used']==2


def test_whole_report_not_only_categories_is_bound_in_wire_and_context():
    record = publication()
    before = wire.context(record)
    body = wire.render(record)
    assert "<!-- pp:head-reviewed" not in body
    for field in ("actual", "command", "result", "invariant"):
        changed = copy.deepcopy(record)
        changed["report"]["blocking"][0]["evidence"][field] += " changed"
        with pytest.raises(PipelineError, match="report hash"):
            wire.render(changed)
        changed["report_sha256"] = inspect_report(changed["report"], IDENTITY.head_sha)["report_sha256"]
        assert wire.context(changed) != before
    changed = copy.deepcopy(record)
    changed["owner"] = "00000000-0000-4000-8000-000000000002"
    assert wire.context(changed) == before  # Equivalent roots may race.
    assert wire.render(changed) != body


@pytest.mark.parametrize("edit", [
    lambda value: value.replace("\n", "\r\n"),
    lambda value: value + " ",
    lambda value: value.replace('"kind":"publication"', '"kind":"publication", "kind":"publication"'),
    lambda value: value.replace("sha256=", "sha256=0", 1),
    lambda value: value.replace("review-routing-v1", "review-routing-v2", 1),
])
def test_noncanonical_edited_or_future_wire_is_not_absent_proof(edit):
    with pytest.raises(PipelineError):
        wire.parse(edit(wire.render(publication())))


def test_publication_size_bound_does_not_truncate_reproduction():
    record = publication()
    record["report"]["change"] = "evidence" * 10000
    record["report_sha256"] = inspect_report(record["report"], IDENTITY.head_sha)["report_sha256"]
    with pytest.raises(PipelineError, match="not truncated"):
        wire.render(record)


def test_record_hash_is_exact_full_utf8_lf_without_normalizing_unicode():
    payload = publication()
    body = wire.render(payload)
    marker, data = body.split("\n", 1)
    assert marker.endswith("sha256=" + hashlib.sha256(data.encode("utf-8")).hexdigest() + " -->")
    assert data.endswith("\n") and "\r" not in data
    assert wire.parse(body) == payload


def test_complete_lifetime_reader_rejects_edited_or_transplanted_records():
    payload = publication()
    node = {"__typename": "IssueComment", "fullDatabaseId": 9007199254740993,
            "createdAt": "2026-10-29T00:00:00Z", "lastEditedAt": None,
            "author": {"login": "owner"}, "body": wire.render(payload)}
    snapshot = {"edges": [{"cursor": "1", "node": node}]}
    records, deleted = wire.records(snapshot, trusted="owner", expected_pr=wire.pr_key(IDENTITY))
    assert records[0].id == "9007199254740993" and not deleted
    node["lastEditedAt"] = "2026-10-29T00:01:00Z"
    with pytest.raises(PipelineError, match="edited"):
        wire.records(snapshot, trusted="owner", expected_pr=wire.pr_key(IDENTITY))
    node["lastEditedAt"] = None
    wrong = dict(wire.pr_key(IDENTITY), pr_id="PR_OTHER")
    with pytest.raises(PipelineError, match="another lifetime"):
        wire.records(snapshot, trusted="owner", expected_pr=wrong)
    node["author"]["login"] = "contributor"
    assert wire.records(snapshot, trusted="owner", expected_pr=wrong) == ([], ())
