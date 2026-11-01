"""The typed adapter must bind identity on every page of both stable reads."""
import copy
import json
from pathlib import Path

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot import review_routing_snapshot as snapshots


BASE = json.loads((Path(__file__).parent / "fixtures/pr-identity-v1.json").read_text(encoding="utf-8"))["cases"][0]["graphql"]
CONFIG = {"repository": "owner/repo", "base_branch": "main"}


def page(*, cursor="first", more=False):
    pr = copy.deepcopy(BASE)
    pr.update(author={"id":"U_AUTHOR","login":"contributor"},
              labels={"nodes":[],"pageInfo":{"hasNextPage":False}},
              timelineItems={"updatedAt":"2026-10-29T00:00:00Z",
                             "pageInfo":{"hasNextPage":more,"endCursor":cursor},
                             "edges":[{"cursor":cursor,"node":{"__typename":"PullRequestCommit","id":"COMMIT","commit":{"oid":BASE["headRefOid"]}}}]})
    return {"data":{"repository":{"id":"R_BASE","nameWithOwner":"owner/repo","pullRequest":pr}}}


class Reader:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def json(self, *args):
        self.calls.append(args)
        assert args[0] == "api" and "-X" not in args and "mutation" not in " ".join(args)
        if args == ("api", "repos/owner/repo"):
            return {"full_name":"owner/repo","node_id":"R_BASE"}
        assert args[1] == "graphql"
        assert "author{login ... on Node{id}}" in args[3]
        return next(self.pages)


def test_two_full_passes_use_the_same_initial_configured_repository_identity():
    reader = Reader([page(more=True),page(cursor="second"),page(more=True),page(cursor="second")])
    result = snapshots.stable_timeline(reader, CONFIG, 42)
    assert len(result["edges"]) == 2
    assert result["identity"]["pr_id"] == "PR_NODE_42"
    assert result["author"]["login"] == "contributor"
    assert len(reader.calls) == 5


@pytest.mark.parametrize("path,value", [
    (("headRepository","id"),"R_REPLACED"),
    (("headRefName",),"retargeted"),
    (("author","id"),"U_OTHER"),
    (("author","login"),"other"),
    (("id",),"PR_OTHER"),
    (("number",),43),
    (("baseRefOid",),"c"*40),
    (("isDraft",),None),
])
def test_identity_or_state_change_on_later_page_aborts_before_any_writer(path,value):
    later=page(cursor="second")
    data=later["data"]["repository"]["pullRequest"]
    for key in path[:-1]:
        data=data[key]
    data[path[-1]]=value
    with pytest.raises(PipelineError):
        snapshots.stable_timeline(Reader([page(more=True),later]), CONFIG, 42)


def test_partial_errors_deleted_fork_and_repeated_cursor_fail_closed():
    bad=page()
    bad["errors"]=[{"message":"partial"}]
    fork=page()
    fork["data"]["repository"]["pullRequest"]["headRepository"]=None
    for pages in ([bad],[fork],[page(more=True),page(more=True)]):
        with pytest.raises(PipelineError):
            snapshots.stable_timeline(Reader(pages), CONFIG, 42)


def test_same_head_different_identity_between_complete_passes_is_not_a_stable_proof():
    second=page()
    second["data"]["repository"]["pullRequest"]["headRefName"]="other"
    with pytest.raises(PipelineError,match="stable reads"):
        snapshots.stable_timeline(Reader([page(),second]), CONFIG, 42)


def test_nonfinal_page_watermark_cannot_change_during_a_complete_pass():
    first=page(more=True);last=page(cursor='second')
    last['data']['repository']['pullRequest']['timelineItems']['updatedAt']='2026-10-29T00:01:00Z'
    with pytest.raises(PipelineError,match='watermark changed'):
        snapshots.stable_timeline(Reader([first,last]),CONFIG,42)
