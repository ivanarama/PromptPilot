"""Exact search through the public Source tool, with raw GitHub JSON transport."""
import copy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from promptpilot import github_search, pipeline_insights as pi, pipeline_source_refresh as source
from promptpilot.models import TaskCreate
from tests.test_github_budget import _limits
from tests.test_pipeline_source_refresh import continuation as continuation, ledger

REAL_SEARCH = pi._github_search


def node(number, kind="Issue", labels=()):
    result = {"__typename": kind, "id": f"node-{number}", "number": number,
              "title": f"Item {number}", "createdAt": "2026-11-01T12:00:00Z",
              "updatedAt": "2026-11-02T12:00:00Z", "closedAt": None,
              "url": f"https://github.com/owner/example/issues/{number}",
              "repository": {"nameWithOwner": "owner/example"},
              "labels": {"totalCount": len(labels), "nodes": [{"name": label} for label in labels],
                         "pageInfo": {"hasNextPage": False, "endCursor": None}}}
    if kind == "PullRequest":
        result.update(mergedAt=None, url=f"https://github.com/owner/example/pull/{number}")
    return result


def page(nodes, count=None, more=False, cursor=None, cost=1):
    return {"data": {"rateLimit": {"cost": cost}, "search": {
        "issueCount": len(nodes) if count is None else count,
        "nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}


def stable_rates(monkeypatch):
    limits = _limits()
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: copy.deepcopy(limits))
    return limits


def gh_transport(monkeypatch, responses, callback=None, preview_cost=1, preview_errors="absent"):
    """Exercise actual _gh_api_json argv/stdin/JSON parsing, never real gh."""
    class Calls(list):
        previews = None
    calls = Calls(); calls.previews = []
    monkeypatch.setattr(pi, "_gh_executable", lambda: "gh")
    def run(command, **kwargs):
        assert command == ["gh", "api", "graphql", "--input", "-"]
        payload = json.loads(kwargs["input"])
        if "dryRun: true" in payload["query"]:
            calls.previews.append(payload)
            response = {"data": {"rateLimit": {"cost": preview_cost}}}
            if preview_errors != "absent": response["errors"] = preview_errors
            return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
        calls.append(payload)
        if callback:
            callback(len(calls), payload)
        response = responses.pop(0) if isinstance(responses, list) else responses(payload)
        return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
    monkeypatch.setattr(pi.subprocess, "run", run)
    return calls


def test_raw_rest_and_graphql_membership_metadata_digest_are_equivalent(isolated_db, monkeypatch):
    raw_rest = {"total_count": 2, "items": [
        {"number": 17, "title": "Item 17", "labels": [{"name": "z"}, {"name": "a"}],
         "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z",
         "closed_at": None, "html_url": "https://github.com/owner/example/issues/17"},
        {"number": 18, "title": "Item 18", "labels": [], "pull_request": {"merged_at": None},
         "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z",
         "closed_at": None, "html_url": "https://github.com/owner/example/pull/18"}]}
    # Preserve the previous public REST normalization contract, not its request
    # mechanism. Fixture covers the union, every metadata field and label order.
    expected = [{"key": "issue:17", "kind": "issue", "number": 17, "title": "Item 17", "labels": ["a", "z"],
                 "created_at": raw_rest["items"][0]["created_at"], "updated_at": raw_rest["items"][0]["updated_at"],
                 "closed_at": None, "merged_at": None, "url": raw_rest["items"][0]["html_url"]},
                {"key": "pr:18", "kind": "pr", "number": 18, "title": "Item 18", "labels": [],
                 "created_at": raw_rest["items"][1]["created_at"], "updated_at": raw_rest["items"][1]["updated_at"],
                 "closed_at": None, "merged_at": None, "url": raw_rest["items"][1]["html_url"]}]
    calls = gh_transport(monkeypatch, [page([node(17, labels=("z", "a")), node(18, "PullRequest")])])
    query = '(is:issue OR is:pr) label:"needs decision" -label:hold sort:updated-asc'
    result = pi._github_search("owner/example", query)
    assert calls[0]["variables"] == {"query": "repo:owner/example " + query, "after": None}
    assert result["count"] == 2 and result["membership_complete"] is True
    digest = lambda values: hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    assert result["items"] == expected and digest(result["items"]) == digest(expected)
    assert result["usage"] == {"transport": "graphql", "graphql_cost": 1, "requests": 1,
                               "nodes_observed": 6, "max_request_nodes": 6,
                               "cost_previews": 0,
                               "max_search_request_nodes": 10200}
    assert github_search.MAX_SEARCH_REQUEST_NODES < 500000


def test_multiple_search_pages_and_repository_case_preserve_every_item(isolated_db, monkeypatch):
    first = [node(i) for i in range(1, 101)]; first[0]["repository"]["nameWithOwner"] = "Owner/Example"
    calls = gh_transport(monkeypatch, [page(first, 102, True, "cursor-one"), page([node(101), node(102)], 102)])
    result = pi._github_search("owner/example", "is:issue")
    assert calls[1]["variables"]["after"] == "cursor-one"
    assert result["count"] == len(result["items"]) == 102 and result["membership_complete"]
    assert result["usage"]["graphql_cost"] == 2


def test_nested_label_connection_is_fully_paginated(isolated_db, monkeypatch):
    item = node(1, labels=tuple(f"label{i:03}" for i in range(100)))
    item["labels"].update(totalCount=102, pageInfo={"hasNextPage": True, "endCursor": "label-cursor"})
    followup = {"data": {"rateLimit": {"cost": 1}, "node": {
        "__typename": "Issue", "id": item["id"], "updatedAt": item["updatedAt"], "labels": {
            "totalCount": 102, "nodes": [{"name": "label100"}, {"name": "label101"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
    calls = gh_transport(monkeypatch, [page([item]), followup])
    result = pi._github_search("owner/example", "is:issue")
    assert calls[1]["variables"] == {"id": "node-1", "after": "label-cursor"}
    assert result["items"][0]["labels"] == [f"label{i:03}" for i in range(102)]
    assert result["usage"]["nodes_observed"] == 105 and result["usage"]["max_request_nodes"] == 102


@pytest.mark.parametrize("count", [1000, 1001])
def test_search_window_never_claims_unseen_membership(isolated_db, monkeypatch, count):
    pages = [page([node(i) for i in range(offset + 1, offset + 101)], count,
                  more=offset < 900 or count > 1000, cursor=str(offset + 100)) for offset in range(0, 1000, 100)]
    calls = gh_transport(monkeypatch, pages)
    result = pi._github_search("owner/example", "is:issue")
    assert len(calls) == 10 and len(result["items"]) == 1000
    assert result["membership_complete"] is (count == 1000)


@pytest.mark.parametrize("fault", ["partial-errors", "unknown-type", "foreign-repository", "missing-node",
    "missing-metadata", "missing-cost", "bad-count", "duplicate", "label-truncation", "missing-cursor",
    "count-truncation", "bad-errors-shape"])
def test_unknown_partial_and_mixed_error_response_refuses_without_rest(isolated_db, monkeypatch, fault):
    response = page([node(1)])
    if fault == "partial-errors": response["errors"] = [{"message": "resource exceeded"}]
    elif fault == "unknown-type": response["data"]["search"]["nodes"][0]["__typename"] = "Discussion"
    elif fault == "foreign-repository": response["data"]["search"]["nodes"][0]["repository"]["nameWithOwner"] = "foreign/repo"
    elif fault == "missing-node": response["data"]["search"]["nodes"] = [None]
    elif fault == "missing-metadata": del response["data"]["search"]["nodes"][0]["updatedAt"]
    elif fault == "missing-cost": del response["data"]["rateLimit"]
    elif fault == "bad-count": response["data"]["search"]["issueCount"] = True
    elif fault == "duplicate": response = page([node(1), node(1)])
    elif fault == "label-truncation": response["data"]["search"]["nodes"][0]["labels"]["totalCount"] = 2
    elif fault == "missing-cursor": response["data"]["search"]["pageInfo"]["hasNextPage"] = True
    elif fault == "count-truncation": response["data"]["search"]["issueCount"] = 2
    else: response["errors"] = {}
    calls = gh_transport(monkeypatch, [response])
    with pytest.raises(ValueError): pi._github_search("owner/example", "is:issue")
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["count-drift", "cursor-cycle", "label-cursor-cycle", "label-identity-drift", "label-updated-drift"])
def test_pagination_fences(isolated_db, monkeypatch, fault):
    if fault.startswith("label"):
        item = node(1, labels=("first",)); item["labels"].update(totalCount=3, pageInfo={"hasNextPage": True, "endCursor": "same"})
        followup = {"data": {"rateLimit": {"cost": 1}, "node": {"id": "other" if fault == "label-identity-drift" else "node-1",
            "__typename": "Issue", "updatedAt": "2026-11-03T12:00:00Z" if fault == "label-updated-drift" else item["updatedAt"], "labels": {"totalCount": 3, "nodes": [{"name": "second"}], "pageInfo": {"hasNextPage": True, "endCursor": "same"}}}}}
        responses = [page([item]), followup]
    else:
        responses = [page([node(1)], 3, True, "same"), page([node(2)], 4 if fault == "count-drift" else 3, True, "same")]
    calls = gh_transport(monkeypatch, responses)
    with pytest.raises(ValueError): pi._github_search("owner/example", "is:issue")
    assert len(calls) == 2


def test_public_task_bound_source_preserves_search_gap_and_every_foreign_reserve(continuation, monkeypatch):
    f = continuation
    stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_search", REAL_SEARCH)
    foreign = f.db.create_task(TaskCreate(prompt="foreign", priority=5))
    with f.db._connect() as c:
        c.execute("UPDATE tasks SET status='running',started_at=? WHERE id=?", (f.task.started_at.isoformat(), foreign.id))
    foreign_cost = {"core": 75, "search": 4, "graphql": 150}
    assert f.db.reserve_pipeline_github_budget("github-default", token="foreign", task_id=foreign.id,
        task_started_at=f.task.started_at, profile_id="foreign", queue_id="review", route="skill", cost=foreign_cost,
        limits=_limits(), minimum_remaining={"core": 250, "search": 2, "graphql": 100})["allowed"]
    before = ledger(f); costs = copy.deepcopy(f.profile["github_budget"])
    calls = gh_transport(monkeypatch, [page([node(1)])])
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] is True and result["budget"]["requested_cost"]["search"] == 10
    assert result["budget"]["reserved_other"] == {"core": 1275, "search": 16, "graphql": 1150}
    assert result["budget"]["effective_after"]["search"] == 4
    assert len(calls) == 1 and ledger(f) == before and f.profile["github_budget"] == costs
    policy = pi._with_shared_budget_floor(pi._github_budget_policy(f.profile))
    policy = pi._with_execution_snapshot_floor(pi._budget_policy_for_admission_priority(pi._budget_policy_for_route(policy, "skill"), 1))
    policy["requested_cost"] = {resource: 0 for resource in policy["requested_cost"]}
    decision = pi._evaluate_github_budget(policy, _limits(graphql=4999), now=time.time(), reserved_other={"core": 1275, "search": 16, "graphql": 1150})
    assert decision["minimum_remaining"]["search"] == 12 and decision["effective_after"]["search"] == 14 and decision["allowed"]
    rest_consumed = pi._evaluate_github_budget(policy, _limits(search=22), now=time.time(), reserved_other={"core": 1275, "search": 16, "graphql": 1150})
    assert rest_consumed["allowed"] is False  # Root's observed full-refresh failure remains a real denial.


@pytest.mark.parametrize("limits", [_limits(core=1499), _limits(graphql=1199), _limits(search=23)])
def test_public_source_keeps_all_core_graphql_and_search_admission_floors(continuation, monkeypatch, limits):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: limits)
    before = ledger(f)
    gh_transport(monkeypatch, lambda _: pytest.fail("denied budget made a GraphQL request"))
    assert source.refresh("example", f.env)["state"] == "wait"
    assert ledger(f) == before and f.db.list_pipeline_snapshots("example") == []


@pytest.mark.parametrize("fault", ["partial-errors", "pause", "session", "epoch"])
def test_public_source_publication_fences_survive_real_search_transport(continuation, monkeypatch, fault):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    before = ledger(f)
    response = page([node(1)])
    if fault == "partial-errors": response["errors"] = [{"message": "partial"}]
    def mutate(_number, _payload):
        if fault == "pause": f.db.set_setting("worker_paused", "1")
        elif fault == "session": f.db.set_session_id(f.task.id, "changed")
        elif fault == "epoch": f.db.increment_int_setting(pi._CACHE_EPOCH_KEY)
    gh_transport(monkeypatch, [response], mutate)
    if fault == "partial-errors":
        with pytest.raises(ValueError): source.refresh("example", f.env)
    else:
        assert source.refresh("example", f.env)["source_refreshed"] is False
    assert f.db.list_pipeline_snapshots("example") == [] and ledger(f) == before


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("fault", ["pause", "lease-loss", "session"])
def test_public_source_loses_no_fences_between_search_or_nested_label_pages(continuation, monkeypatch, nested, fault):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    before = ledger(f)
    if nested:
        item = node(1, labels=("first",)); item["labels"].update(totalCount=2, pageInfo={"hasNextPage": True, "endCursor": "labels"})
        responses = [page([item]), {"data": {"rateLimit": {"cost": 1}, "node": {
            "__typename": "Issue", "id": item["id"], "updatedAt": item["updatedAt"],
            "labels": {"totalCount": 2, "nodes": [{"name": "second"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}]
    else:
        responses = [page([node(1)], 2, True, "second-page"), page([node(2)], 2)]
    def mutate(number, _payload):
        if number != 2: return
        if fault == "pause": f.db.set_setting("worker_paused", "1")
        elif fault == "session": f.db.set_session_id(f.task.id, "changed-on-second-page")
        else:
            lease = pi._scan_lease_context.lease
            assert f.db.release_pipeline_scan_lease(lease.scope, lease.token)
    calls = gh_transport(monkeypatch, responses, mutate)
    result = source.refresh("example", f.env)
    assert len(calls) == 2 and result["source_refreshed"] is False
    assert f.db.list_pipeline_snapshots("example") == [] and ledger(f) == before


@pytest.mark.parametrize("resource,remaining", [("core", 1499), ("search", 23), ("graphql", 1199)])
def test_nested_labels_recheck_every_resource_before_another_query(continuation, monkeypatch, resource, remaining):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    current = _limits(); monkeypatch.setattr(pi, "_github_rate_limits", lambda: copy.deepcopy(current))
    item = node(1, labels=("first",)); item["labels"].update(totalCount=2, pageInfo={"hasNextPage": True, "endCursor": "labels"})
    before = ledger(f)
    def lower(_number, _payload):
        current[resource].update(remaining=remaining, used=current[resource]["limit"] - remaining)
    calls = gh_transport(monkeypatch, [page([item])], lower)
    result = source.refresh("example", f.env)
    assert result["state"] == "wait" and result["source_refreshed"] is False
    assert len(calls) == len(calls.previews) == 1  # next label preview never starts
    if resource != "graphql":
        assert result["budget"]["reserved_other"]["search"] == 12
    else:
        # External GraphQL spend can exhaust the whole-scan allowance first.
        assert result["budget"]["state"] == "budget_in_flight"
    assert ledger(f) == before and f.db.list_pipeline_snapshots("example") == []


def test_server_cost_preview_larger_than_unchanged_estimate_refuses_before_data(continuation, monkeypatch):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    before = ledger(f)
    calls = gh_transport(monkeypatch, [], preview_cost=101)
    result = source.refresh("example", f.env)
    assert result["state"] == "refused" and result["reason"] == "invalid_config"
    assert not calls and len(calls.previews) == 1
    assert ledger(f) == before and f.db.list_pipeline_snapshots("example") == []


def test_quota_drop_after_preview_is_wait_before_search_data(continuation, monkeypatch):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    snapshots = iter([_limits(), _limits(), _limits(graphql=1199)])
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: next(snapshots))
    before = ledger(f); calls = gh_transport(monkeypatch, [])
    result = source.refresh("example", f.env)
    assert result["state"] == "wait" and not calls and len(calls.previews) == 1
    assert ledger(f) == before and f.db.list_pipeline_snapshots("example") == []


def test_actual_cost_above_server_preview_never_publishes(continuation, monkeypatch):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    before = ledger(f); calls = gh_transport(monkeypatch, [page([node(1)], cost=2)], preview_cost=1)
    with pytest.raises(ValueError, match="cost changed after preview"):
        source.refresh("example", f.env)
    assert len(calls) == len(calls.previews) == 1
    assert ledger(f) == before and f.db.list_pipeline_snapshots("example") == []


@pytest.mark.parametrize("estimate,refreshed", [(5, False), (6, True)])
def test_whole_allowance_includes_initial_and_final_rate_observation(continuation, monkeypatch, estimate, refreshed):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    f.profile["github_budget"]["costs"]["insights"]["graphql"] = estimate
    before = ledger(f); calls = gh_transport(monkeypatch, [page([])])
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] is refreshed and len(calls) == 1
    assert ledger(f) == before
    if refreshed:
        assert result["source_search_usage"]["conservative_graphql_spend"] == 6
    else:
        assert result["state"] == "wait" and f.db.list_pipeline_snapshots("example") == []


def test_one_whole_allowance_covers_six_queries_pages_and_nested_labels(continuation, monkeypatch):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    f.profile["queues"][0]["query"] = "is:pr label:q0"
    f.profile["queues"].extend({"id": f"q{i}", "title": f"Q{i}", "capacity": 1,
        "series_contains": f"not-real-series-{i}", "query": f"is:pr label:q{i}"} for i in range(1, 6))
    def response(payload):
        if "PipelineSourceLabels" in payload["query"]:
            return {"data": {"rateLimit": {"cost": 1}, "node": {"__typename": "PullRequest",
                "id": "node-3", "updatedAt": "2026-11-02T12:00:00Z", "labels": {
                    "totalCount": 101, "nodes": [{"name": "last"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        variables = payload["variables"]; query = variables["query"]
        if "label:q0" in query:
            return page([node(1 if variables["after"] is None else 2, "PullRequest")], 2,
                        more=variables["after"] is None, cursor="second")
        if "label:q1" in query:
            item = node(3, "PullRequest", tuple(f"label{i:03}" for i in range(100)))
            item["labels"].update(totalCount=101, pageInfo={"hasNextPage": True, "endCursor": "label-end"})
            return page([item])
        return page([node(int(query[-1]) + 2, "PullRequest")])
    before = ledger(f); calls = gh_transport(monkeypatch, response)
    result = source.refresh("example", f.env)
    assert result["source_refreshed"] and len(calls) == len(calls.previews) == 8
    assert result["source_search_usage"]["conservative_graphql_spend"] == 34
    assert result["source_search_usage"]["graphql_allowance"] == 100
    assert result["budget"]["requested_cost"]["search"] == 10 and ledger(f) == before


@pytest.mark.parametrize("preview", [False, True])
def test_zero_cost_is_not_an_authoritative_cost_observation(continuation, monkeypatch, preview):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    calls = gh_transport(monkeypatch, [page([], cost=0)], preview_cost=0 if preview else 1)
    with pytest.raises(ValueError, match="must be positive"): source.refresh("example", f.env)
    assert len(calls) == (0 if preview else 1) and f.db.list_pipeline_snapshots("example") == []


@pytest.mark.parametrize("errors", [{}, None])
def test_malformed_preview_errors_never_start_data_request(continuation, monkeypatch, errors):
    f = continuation; monkeypatch.setattr(pi, "_github_search", REAL_SEARCH); stable_rates(monkeypatch)
    calls = gh_transport(monkeypatch, [], preview_errors=errors)
    with pytest.raises(ValueError, match="preview failed"): source.refresh("example", f.env)
    assert not calls and len(calls.previews) == 1 and f.db.list_pipeline_snapshots("example") == []
