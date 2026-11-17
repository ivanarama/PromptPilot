"""Exact GitHub issue/PR search using the separate GraphQL quota.

The caller owns admission, pause, account lease and publication fences. There is
no REST fallback: a partial GraphQL response is never a complete Source proof.
"""
from datetime import datetime

_ITEM_FIELDS = """
  id number title createdAt updatedAt closedAt url
  repository { nameWithOwner }
  labels(first: 100) { totalCount nodes { name } pageInfo { hasNextPage endCursor } }
"""
SEARCH_QUERY = """query PipelineSourceSearch($query: String!, $after: String) {
  rateLimit { cost }
  search(type: ISSUE, query: $query, first: 100, after: $after) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on Issue { %s }
      ... on PullRequest { %s mergedAt }
    }
  }
}""" % (_ITEM_FIELDS, _ITEM_FIELDS)
LABEL_QUERY = """query PipelineSourceLabels($id: ID!, $after: String!) {
  rateLimit { cost }
  node(id: $id) {
    __typename
    ... on Issue {
      id updatedAt labels(first: 100, after: $after) {
        totalCount nodes { name } pageInfo { hasNextPage endCursor }
      }
    }
    ... on PullRequest {
      id updatedAt labels(first: 100, after: $after) {
        totalCount nodes { name } pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""
# Each search request returns at most 100 issue/PR nodes, their repositories,
# and 100 labels per item, below GitHub's 500,000-node per-request limit. Label
# requests return at most one item and 100 labels.
MAX_SEARCH_REQUEST_NODES = 10200


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid GraphQL search {name}")
    return value


def _cost(value):
    cost = _integer(value, "query cost")
    if cost == 0:
        raise ValueError("GraphQL search cost must be positive")
    return cost


def _connection(value):
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        raise ValueError("invalid GraphQL search connection")
    info = value.get("pageInfo")
    if (not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool
            or len(value["nodes"]) > 100):
        raise ValueError("invalid GraphQL search pagination")
    cursor = info.get("endCursor")
    if info["hasNextPage"] and (not isinstance(cursor, str) or not cursor):
        raise ValueError("missing GraphQL search cursor")
    return value["nodes"], info["hasNextPage"], cursor


def _item_metadata(item):
    if (not isinstance(item.get("title"), str) or not isinstance(item.get("url"), str)
            or not item["url"]):
        raise ValueError("missing GraphQL search item metadata")
    for field in ("createdAt", "updatedAt", "closedAt") + (("mergedAt",) if item["__typename"] == "PullRequest" else ()):
        value = item.get(field)
        if value is None and field in ("closedAt", "mergedAt") and field in item:
            continue
        try:
            if not isinstance(value, str) or datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
                raise ValueError("missing timezone")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid GraphQL search item timestamp") from exc


def search(repository, query, request, check, budget=None):
    """Return the existing normalized search contract plus measured API usage.

    Search syntax is passed intact, including quotes, qualifiers and boolean
    expressions. Server counts and every cursor page are validated; the shared
    1,000-result search window never becomes a false full-membership claim.
    """
    cost = requests = nodes_observed = max_nodes = previews = 0

    def fetch(document, variables):
        nonlocal cost, requests, previews
        check()
        predicted = None
        if budget is not None:
            # The server evaluates the exact query cost without evaluating its
            # data fields. The ordinary costed scan policy is rechecked before
            # both the preview and actual request; neither creates admission.
            budget.before_request(1)
            check()
            preview = request(["graphql"], {"query": document.replace(
                "rateLimit { cost }", "rateLimit(dryRun: true) { cost }"), "variables": variables})
            check()
            if (not isinstance(preview, dict) or preview.get("errors")
                    or "errors" in preview and not isinstance(preview["errors"], list)
                    or not isinstance(preview.get("data"), dict)
                    or not isinstance(preview["data"].get("rateLimit"), dict)):
                raise ValueError("GraphQL search cost preview failed")
            predicted = _cost(preview["data"]["rateLimit"].get("cost"))
            previews += 1
            budget.before_request(predicted)
            check()
        payload = request(["graphql"], {"query": document, "variables": variables})
        check()
        if (not isinstance(payload, dict) or payload.get("errors")
                or "errors" in payload and not isinstance(payload["errors"], list)
                or not isinstance(payload.get("data"), dict)):
            raise ValueError("GraphQL search returned errors or partial data")
        data = payload["data"]
        rate = data.get("rateLimit")
        if not isinstance(rate, dict):
            raise ValueError("GraphQL search cost observation is missing")
        actual = _cost(rate.get("cost"))
        if predicted is not None and actual > predicted:
            raise ValueError("GraphQL search cost changed after preview")
        cost += actual
        requests += 1
        return data

    def labels(item):
        nonlocal nodes_observed, max_nodes
        connection = item.get("labels")
        seen_cursors = set()
        result = []
        total = None
        while True:
            if not isinstance(connection, dict):
                raise ValueError("GraphQL search labels are missing")
            observed_total = _integer(connection.get("totalCount"), "label count")
            if total is None:
                total = observed_total
            if total != observed_total:
                raise ValueError("GraphQL search labels changed during pagination")
            page, more, cursor = _connection(connection)
            for label in page:
                if (not isinstance(label, dict) or not isinstance(label.get("name"), str)
                        or not label["name"] or label["name"] in result):
                    raise ValueError("invalid or duplicate GraphQL search label")
                result.append(label["name"])
            nodes_observed += len(page)
            if len(result) > total or not more and len(result) != total:
                raise ValueError("incomplete GraphQL search labels")
            if not more:
                return sorted(result)
            if not page or cursor in seen_cursors or len(result) >= total:
                raise ValueError("GraphQL search label cursor did not advance")
            seen_cursors.add(cursor)
            data = fetch(LABEL_QUERY, {"id": item["id"], "after": cursor})
            node = data.get("node")
            if (not isinstance(node, dict) or node.get("id") != item["id"]
                    or node.get("__typename") != item["__typename"]
                    or node.get("updatedAt") != item["updatedAt"]):
                raise ValueError("GraphQL search label item changed")
            connection = node.get("labels")
            nodes_observed += 1  # the owner node returned by node(id: ...)
            if isinstance(connection, dict) and isinstance(connection.get("nodes"), list):
                max_nodes = max(max_nodes, 1 + len(connection["nodes"]))

    items, seen, seen_ids, cursors = [], set(), set(), set()
    after = None
    total = None
    complete = True
    while True:
        data = fetch(SEARCH_QUERY, {"query": f"repo:{repository} {query}", "after": after})
        connection = data.get("search")
        page, more, cursor = _connection(connection)
        count = _integer(connection.get("issueCount"), "issue count")
        if total is None:
            total = count
        if count != total:
            raise ValueError("GraphQL search count changed during pagination")
        page_nodes = 2 * len(page)  # item plus its repository node
        for item in page:
            if (not isinstance(item, dict) or item.get("__typename") not in ("Issue", "PullRequest")
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or _integer(item.get("number"), "item number") == 0
                    or not isinstance(item.get("repository"), dict)
                    or not isinstance(item["repository"].get("nameWithOwner"), str)
                    or item["repository"]["nameWithOwner"].lower() != repository.lower()):
                raise ValueError("unknown or foreign GraphQL search item")
            _item_metadata(item)
            kind = "pr" if item["__typename"] == "PullRequest" else "issue"
            key = f"{kind}:{item['number']}"
            if item["number"] in seen or item["id"] in seen_ids:
                raise ValueError("duplicate GraphQL search membership")
            seen.add(item["number"])
            seen_ids.add(item["id"])
            item_labels = labels(item)
            page_nodes += len(item["labels"]["nodes"])
            items.append({"key": key, "kind": kind, "number": item["number"],
                "title": item.get("title") or f"#{item['number']}", "labels": item_labels,
                "created_at": item.get("createdAt"), "updated_at": item.get("updatedAt"),
                "closed_at": item.get("closedAt"), "merged_at": item.get("mergedAt"),
                "url": item.get("url")})
        nodes_observed += 2 * len(page)
        max_nodes = max(max_nodes, page_nodes)
        if len(items) > total:
            raise ValueError("GraphQL search count is smaller than membership")
        if not more:
            complete = len(items) == total
            if not complete and total <= 1000:
                raise ValueError("incomplete GraphQL search membership")
            break
        if len(items) >= 1000:
            complete = False
            break
        if not page or cursor in cursors or len(items) >= total:
            raise ValueError("GraphQL search cursor did not advance")
        cursors.add(cursor)
        after = cursor
    return {"count": total, "items": items, "membership_complete": complete,
            "usage": {"transport": "graphql", "graphql_cost": cost,
                      "requests": requests, "nodes_observed": nodes_observed,
                      "cost_previews": previews,
                      "max_request_nodes": max_nodes,
                      "max_search_request_nodes": MAX_SEARCH_REQUEST_NODES}}
