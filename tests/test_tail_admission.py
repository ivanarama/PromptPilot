import copy
import hashlib
import json

import pytest

from promptpilot import project_pipeline as pp, tail_admission as tail
from tail_admission_fixture import add_comment, add_edge, graphql_response, source_fixture


CONFIG = {"repository": "owner/project", "trusted_account": "owner", "base_branch": "main"}


class Reader:
    def __init__(self, source):
        self.source = source
        self.timeline_reads = 0

    def graphql_page(self, owner, name, number, cursor):
        assert (owner, name, number, cursor) == ("owner", "project", 42, None)
        self.timeline_reads += 1
        return graphql_response(self.source)

    def json(self, *args):
        if args == ("api", "repos/owner/project/pulls/42"):
            return self.source["pull"]
        assert args == ("api", "repos/owner/project/pulls/1261")
        return self.source["cutover"]

    def run(self, *args):
        assert args == ("api", "--paginate", "repos/owner/project/issues/42/comments?per_page=100", "--jq", ".[]")
        return "\n".join(json.dumps(row, ensure_ascii=False) for row in self.source["comments"])


def admit(source):
    reader = Reader(source)
    result = tail.classify(reader, CONFIG, source["candidate"])
    assert reader.timeline_reads == 2
    return result


def test_verified_empty_and_full_legacy_completion():
    empty = source_fixture(items=[])
    result = admit(empty)
    assert result["state"] == "resolved" and result["completion_kind"] == "empty"
    assert result["review_body_digest"] == hashlib.sha256(empty["comments"][0]["body"].encode()).hexdigest()
    assert result["review_id"] == "101" and result["review_updated_at"] == empty["comments"][0]["updated_at"]
    legacy = source_fixture("legacy")
    add_comment(legacy, "Summary\n<!-- pp:tail-done -->")
    assert admit(legacy)["completion_kind"] == "legacy"
    # The old source need not have a claim-bound current-epoch anchor.
    legacy["snapshot"]["edges"].pop(0)
    assert admit(legacy)["completion_kind"] == "legacy"


def test_completion_versions_trusted_authors_and_reopening():
    source = source_fixture()
    version = source["comments"][0]["updated_at"]
    marker = f"<!-- pp:tail-done review-comment=101 review-updated={version} -->"
    add_comment(source, "<!-- pp:tail-done -->")
    add_comment(source, marker, login="stranger")
    add_comment(source, marker.replace("101", "102"))
    assert admit(source)["state"] == "ready"
    own = add_comment(source, "Summary\n" + marker)
    assert admit(source)["completion_kind"] == "versioned"
    source["comments"].remove(own)
    source["snapshot"]["edges"].pop()
    assert admit(source)["state"] == "ready"


@pytest.mark.parametrize("fault", ["cutover-edit", "override", "orphan", "deleted", "restore", "other-base",
                                   "deleted-review", "force-push", "after-merge", "edited-claim"])
def test_inadmissible_sources_cannot_complete_or_become_ready(fault):
    legacy = fault in {"cutover-edit", "override"}
    s = source_fixture("legacy" if legacy else "committed", items=[])
    edges = s["snapshot"]["edges"]
    if fault == "cutover-edit":
        s["comments"][0]["updated_at"] = s["cutover"]["merged_at"]
        edges[1]["node"]["lastEditedAt"] = s["cutover"]["merged_at"]
    elif fault == "override":
        add_comment(s, "pp:review-again")
    elif fault == "orphan":
        edges.pop(3)
        s["comments"].pop(2)
    elif fault == "deleted":
        add_edge(s, "CommentDeletedEvent", id="deleted", createdAt=s["pull"]["merged_at"])
    elif fault == "restore":
        add_edge(s, "HeadRefRestoredEvent", id="restore")
    elif fault == "other-base":
        s["pull"]["base"]["ref"] = "release"
    elif fault == "deleted-review":
        edges.pop(1)
        s["comments"].pop(0)
        s["candidate"]["review_id"] = "101"
    elif fault == "force-push":
        edges.insert(-1, {"cursor": "force", "node": {"id": "force", "__typename": "BaseRefForcePushedEvent"}})
    elif fault == "after-merge":
        edges[-1], edges[-2] = edges[-2], edges[-1]
    else:
        s["comments"][1]["updated_at"] = s["pull"]["merged_at"]
        edges[2]["node"]["lastEditedAt"] = s["pull"]["merged_at"]
    assert admit(s)["state"] == "invalid-source"


@pytest.mark.parametrize("label", ["hold", "no-tail"])
def test_policy_snapshots_disagree_or_wait_without_selecting_work(label):
    s = source_fixture()
    s["pull"]["labels"] = [{"name": label}]
    with pytest.raises(pp.PipelineError, match="labels"):
        admit(s)
    s["snapshot"]["labels"] = [label]
    assert admit(s)["state"] == "waiting-policy"


def test_missing_source_and_foreign_empty_marker():
    source = source_fixture("legacy", items=[])
    source["comments"][0]["user"]["login"] = "stranger"
    source["snapshot"]["edges"][1]["node"]["author"]["login"] = "stranger"
    assert admit(source)["state"] == "pending-proof"
    source["candidate"]["review_id"] = "101"
    assert admit(source)["state"] == "invalid-source"


def test_changed_selected_legacy_review_cannot_reveal_an_older_completion():
    source = source_fixture("legacy", items=[])
    # The earlier eligible empty review survives. The previously selected
    # newer comment lost its review marker in an edit.
    source["candidate"]["review_id"] = "200"
    add_comment(source, "Changed", ident=200, created=source["comments"][0]["created_at"],
                edited=source["pull"]["merged_at"])
    add_comment(source, "<!-- pp:tail-done -->")
    assert admit(source)["state"] == "invalid-source"


@pytest.mark.parametrize("fault", ["no-type", "partial-commit", "unstable-body", "cutover"])
def test_incomplete_source_is_reported_as_a_per_candidate_error(fault):
    source = source_fixture()
    if fault == "no-type":
        source["snapshot"]["edges"][0]["node"].pop("__typename")
    elif fault == "partial-commit":
        source["snapshot"]["edges"][0]["node"]["commit"] = None
    elif fault == "unstable-body":
        source["comments"][0]["body"] += "content changed"
    else:
        source["cutover"]["merged"] = False
    with pytest.raises(pp.PipelineError):
        admit(source)


def test_actual_source_text_and_original_bytes_across_formats():
    items = ["1. [выброс] Наблюдение", "2. [заявка] Суть\r\n    с деталями — заголовок: «Название»",
             "3. [заявка] Другая суть Заголовок: «Историческое имя»."]
    source = source_fixture(items=items)
    body = source["comments"][0]["body"].replace("\r\n", "\n").replace("\n", "\r\n")
    source["comments"][0]["body"] = source["snapshot"]["edges"][1]["node"]["body"] = body
    result = admit(source)
    assert result["state"] == "ready"
    assert [item["kind"] for item in result["items"]] == ["discard", "issue", "issue"]
    item = result["items"][1]
    assert (item["index"], item["text"], item["title"]) == (2, "Суть с деталями", "Название")
    expected = "2. [заявка] Суть с деталями — заголовок: «Название»"
    assert item["item_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert result["review_body_digest"] == hashlib.sha256(body.encode()).hexdigest()
    assert result["items"][2]["title"] == "Историческое имя"


@pytest.mark.parametrize("item", ["чужая строка", "1. [заявка] Только заголовок",
                                  "2. [заявка] x → заголовок: «y»",
                                  "1. [заявка] x → заголовок: «y» Заголовок: «z»"])
def test_damaged_section_refuses_entire_source(item):
    assert admit(source_fixture(items=[item]))["state"] == "invalid-source"


@pytest.mark.parametrize("fault", ["partial-errors", "repeated-cursor", "false-type", "page-watermark", "labels"])
def test_timeline_pagination_fails_closed(fault):
    first = graphql_response(source_fixture())
    page = first["data"]["repository"]["pullRequest"]
    info = page["timelineItems"]["pageInfo"]
    info.update(hasNextPage=True, endCursor="after:first")
    second = copy.deepcopy(first)
    second_pr = second["data"]["repository"]["pullRequest"]
    if fault == "partial-errors":
        second["errors"] = [{"message": "incomplete"}]
    elif fault == "false-type":
        second_pr["timelineItems"]["pageInfo"]["hasNextPage"] = None
    elif fault == "page-watermark":
        second_pr["timelineItems"]["updatedAt"] = "changed"
    elif fault == "labels":
        second_pr["labels"]["pageInfo"]["hasNextPage"] = None
    class Pages:
        def graphql_page(self, owner, name, number, cursor):
            return first if cursor is None else second
    with pytest.raises(pp.PipelineError):
        pp.timeline_pass(Pages(), CONFIG, 42)
