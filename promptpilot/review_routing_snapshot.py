"""Coherent typed timeline reads; the legacy snapshot/digest is unchanged."""
from __future__ import annotations

import re

from .pipeline_errors import PipelineError
from .pr_identity import identity_from_graphql, REPOSITORY, SHA
from .review_routing_wire import canonical, pr_key


def _query():
    from .project_pipeline import TIMELINE_QUERY

    needle = "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    replacement = """repository(owner:$owner,name:$name){id nameWithOwner
 pullRequest(number:$number){id number
  baseRepository{id nameWithOwner} headRepository{id nameWithOwner}
  headRefName isCrossRepository author{login ... on Node{id}}"""
    if TIMELINE_QUERY.count(needle) != 1:
        raise PipelineError("typed timeline query adapter no longer matches legacy query")
    return TIMELINE_QUERY.replace(needle, replacement)


def configured_repository(gh, config):
    repository = config.get("repository")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise PipelineError("invalid routing configured repository")
    root = gh.json("api", f"repos/{repository}")
    if (not isinstance(root, dict) or root.get("full_name") != repository
            or not isinstance(root.get("node_id"), str) or not root["node_id"]):
        raise PipelineError("routing repository identity is unavailable")
    repo_id = root["node_id"]
    if config.get("base_repository_id", repo_id) != repo_id:
        raise PipelineError("routing configured repository ID changed")
    return repo_id


def _pass(gh, config, number, repo_id):
    owner, name = config["repository"].split("/")
    edges, seen_cursors, seen_edges = [], set(), set()
    cursor, header = None, None
    for _page in range(1000):
        args = ["api", "graphql", "-f", f"query={_query()}", "-F", f"owner={owner}",
                "-F", f"name={name}", "-F", f"number={number}"]
        if cursor is not None:
            args += ["-F", f"cursor={cursor}"]
        payload = gh.json(*args)
        try:
            if not isinstance(payload, dict) or payload.get("errors"):
                raise PipelineError("typed timeline query returned errors or partial data")
            repo = payload["data"]["repository"]
            if repo["id"] != repo_id or repo["nameWithOwner"] != config["repository"]:
                raise PipelineError("typed timeline repository identity changed")
            pr = repo["pullRequest"]
            identity = identity_from_graphql(pr, repository=config["repository"], repository_id=repo_id)
            if identity.pr_number != number:
                raise PipelineError("typed timeline PR number changed")
            author = pr["author"]
            if (not isinstance(author, dict) or not isinstance(author.get("id"), str)
                    or not author["id"] or not isinstance(author.get("login"), str)
                    or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", author["login"])):
                raise PipelineError("typed timeline PR author identity unavailable")
            if not isinstance(pr["baseRefOid"], str) or not SHA.fullmatch(pr["baseRefOid"]):
                raise PipelineError("typed timeline integration base SHA is invalid")
            labels = pr["labels"]
            if type(labels["pageInfo"]["hasNextPage"]) is not bool:
                raise PipelineError("typed timeline label completeness is unknown")
            label_names = [item["name"] for item in labels["nodes"]]
            if any(not isinstance(value, str) or not value for value in label_names):
                raise PipelineError("typed timeline labels are invalid")
            current = {key: pr[key] for key in (
                "headRefOid", "baseRefOid", "baseRefName", "state", "isDraft")}
            if current["state"] not in {"OPEN", "CLOSED", "MERGED"} or type(current["isDraft"]) is not bool:
                raise PipelineError("typed timeline lifecycle is unknown")
            current.update(identity=identity.to_dict(), identity_sha256=identity.sha256,
                           pr_key=pr_key(identity), author=dict(author), labels=sorted(label_names),
                           labelsComplete=not labels["pageInfo"]["hasNextPage"])
            if header is not None and current != header:
                raise PipelineError("typed PR identity/state changed during pagination")
            header = current
            connection = pr["timelineItems"]
            page_info = connection["pageInfo"]
            if type(page_info["hasNextPage"]) is not bool or not isinstance(connection["edges"], list):
                raise PipelineError("typed timeline pagination is incomplete")
            for edge in connection["edges"]:
                edge_cursor = edge["cursor"]
                if not isinstance(edge_cursor, str) or not edge_cursor or edge_cursor in seen_edges:
                    raise PipelineError("typed timeline edge cursor is missing or duplicated")
                if not isinstance(edge["node"], dict) or not edge["node"].get("__typename"):
                    raise PipelineError("typed timeline edge is partial")
                seen_edges.add(edge_cursor)
                edges.append(edge)
            if not page_info["hasNextPage"]:
                return {**header, "updatedAt": connection.get("updatedAt"), "edges": edges}
            cursor = page_info["endCursor"]
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise PipelineError("typed timeline pagination did not advance")
            seen_cursors.add(cursor)
        except (KeyError, TypeError, AttributeError) as exc:
            raise PipelineError("typed timeline metadata is partial or unavailable") from exc
    raise PipelineError("typed timeline exceeds bounded complete history")


def stable_timeline(gh, config, number):
    if type(number) is not int or not 1 <= number <= 2147483647:
        raise PipelineError("invalid typed timeline PR number")
    repo_id = configured_repository(gh, config)
    first = _pass(gh, config, number, repo_id)
    second = _pass(gh, config, number, repo_id)
    if canonical(first) != canonical(second):
        raise PipelineError("typed timeline changed between complete stable reads")
    return first
