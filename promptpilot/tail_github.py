"""Read one page of merged PR observations for the durable TAIL index."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from .pipeline_errors import PipelineError
from .pr_identity import REPOSITORY, SHA


_FIELDS = "number mergedAt headRefOid baseRefName repository { nameWithOwner }"
_SEARCH = """query TailSearch($owner: String!, $name: String!, $query: String!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) { nameWithOwner pullRequests(first: 1) { totalCount } }
  search(type: ISSUE, query: $query, first: 100, after: $after) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { __typename ... on PullRequest { %s } }
  }
}""" % _FIELDS
_ENUMERATE = """query TailEnumerate($owner: String!, $name: String!, $before: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(orderBy: {field: CREATED_AT, direction: ASC}, last: 100, before: $before) {
      totalCount pageInfo { hasPreviousPage startCursor }
      nodes { createdAt state %s }
    }
  }
}""" % _FIELDS


class SourceError(PipelineError):
    """A failed GitHub read may still carry an authoritative rate-limit receipt."""

    receipt: tuple[int, str] | None = None


class InvalidCursor(SourceError):
    """The saved cursor can no longer be used for this interval."""


class SearchUnsupported(SourceError):
    """The search service cannot enforce the requested qualifiers."""

    def __init__(self, message, *, requested=True):
        super().__init__(message)
        self.requested = requested


@dataclass(frozen=True)
class Page:
    items: list[dict]
    total_count: int
    raw_count: int
    next_cursor: str | None
    remaining: int
    reset_at: str
    enumerated: list[tuple[str, int]]


def _utc(value: str) -> str:
    if not isinstance(value, str):
        raise SourceError("missing TAIL timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.microsecond:
            raise ValueError
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise SourceError("invalid TAIL timestamp") from exc


def _source_error(message: str, mode: str, cursor: str | None) -> SourceError:
    detail = message.lower()
    if cursor and re.search(r"invalid[_ ]cursor|cursor (?:is |has )?(?:invalid|expired)|"
                            r"does not appear to be a valid cursor", detail):
        return InvalidCursor("GitHub rejected the saved TAIL cursor")
    if mode == "search" and re.search(r"(?:invalid|unsupported|unrecognized) (?:search |date )?"
                                      r"(?:qualifier|precision|search query)|"
                                      r"(?:qualifier|precision) (?:is )?(?:invalid|unsupported)", detail):
        return SearchUnsupported("GitHub rejected the TAIL search qualifiers")
    return SourceError("GitHub TAIL query failed: " + message)


def _natural(value) -> bool:
    return type(value) is int and value >= 0


def _rate_receipt(rate) -> tuple[int, str]:
    if (not isinstance(rate, dict) or not _natural(rate.get("remaining"))
            or not _natural(rate.get("cost")) or rate["cost"] == 0):
        raise PipelineError("invalid TAIL GraphQL rate limit receipt")
    return rate["remaining"], _utc(rate.get("resetAt"))


def read_page(gh, repository: str, mode: str, lower: str, upper: str, cursor: str | None,
              *, base: str = "main") -> Page:
    """Search an inclusive merge-time interval or enumerate the repository and filter locally."""
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise PipelineError("invalid TAIL repository")
    if not isinstance(base, str) or not base or mode not in {"search", "enumerate"}:
        raise PipelineError("invalid TAIL base or enumeration mode")
    lower, upper = _utc(lower), _utc(upper)
    if lower > upper:
        raise PipelineError("inverted TAIL discovery interval")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise InvalidCursor("invalid saved TAIL cursor")
    owner, name = repository.split("/")
    if mode == "search":
        # Qualifiers support second precision and explicit UTC offsets. Unusual
        # branch names can still be matched exactly by the enumeration query.
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", base):
            raise SearchUnsupported("base branch requires exact enumeration", requested=False)
        bounds = lower[:-1] + "+00:00.." + upper[:-1] + "+00:00"
        search = f"repo:{repository} is:pr is:merged base:{base} merged:{bounds} sort:created-asc"
        document, variables = _SEARCH, {"owner": owner, "name": name, "query": search, "after": cursor}
    else:
        document, variables = _ENUMERATE, {"owner": owner, "name": name, "before": cursor}
    try:
        response = gh.json("api", "graphql", "--input", "-",
                           input_value={"query": document, "variables": variables}, graphql_errors=True)
    except PipelineError as exc:
        raise _source_error(str(exc), mode, cursor) from exc
    except (OSError, UnicodeError) as exc:
        raise PipelineError("could not read the GitHub TAIL response") from exc
    if not isinstance(response, dict) or not isinstance(response.get("errors", []), list):
        raise PipelineError("invalid GitHub TAIL response")
    data = response.get("data")
    rate = data.get("rateLimit") if isinstance(data, dict) else None
    receipt = _rate_receipt(rate) if rate is not None else None
    try:
        return _parse_page(data, response.get("errors"), receipt, repository, base, mode, lower, upper, cursor)
    except SourceError as exc:
        exc.receipt = receipt
        raise


def _parse_page(data, errors, receipt, repository, base, mode, lower, upper, cursor) -> Page:
    if errors:
        failures = []
        for error in errors:
            if not isinstance(error, dict):
                raise SourceError("invalid GitHub TAIL error response")
            failures.append(_source_error(f"{error.get('type', '')}: {error.get('message', '')}",
                                          mode, cursor))
        if any(type(failure) is not type(failures[0]) for failure in failures):
            raise SourceError("GitHub TAIL returned multiple kinds of query errors")
        raise failures[0]
    if not isinstance(data, dict):
        raise SourceError("missing GitHub TAIL data")
    if receipt is None:
        raise SourceError("missing TAIL GraphQL rate limit receipt")
    repo = data.get("repository")
    if (not isinstance(repo, dict) or not isinstance(repo.get("nameWithOwner"), str)
            or repo["nameWithOwner"].lower() != repository.lower()):
        raise SourceError("missing or mismatched TAIL repository")
    pulls = repo.get("pullRequests")
    if not isinstance(pulls, dict) or not _natural(pulls.get("totalCount")):
        raise SourceError("cannot read the TAIL repository pull requests")
    if mode == "search":
        connection, count_field = data.get("search"), "issueCount"
        more_field, cursor_field = "hasNextPage", "endCursor"
    else:
        connection, count_field = pulls, "totalCount"
        more_field, cursor_field = "hasPreviousPage", "startCursor"
    if not isinstance(connection, dict) or not _natural(connection.get(count_field)):
        raise SourceError("invalid TAIL connection or count")
    nodes, info = connection.get("nodes"), connection.get("pageInfo")
    if (not isinstance(nodes, list) or len(nodes) > min(100, connection[count_field])
            or not isinstance(info, dict) or type(info.get(more_field)) is not bool):
        raise SourceError("invalid TAIL page or page metadata")
    next_cursor = info.get(cursor_field) if info[more_field] else None
    if info[more_field] and cursor and next_cursor == cursor:
        raise InvalidCursor("GitHub returned the same TAIL cursor")
    if info[more_field] and (not nodes or not isinstance(next_cursor, str) or not next_cursor):
        raise SourceError("TAIL page cursor did not advance")

    items = []
    enumerated = []
    seen = set()
    for node in nodes:
        if not isinstance(node, dict) or mode == "search" and node.get("__typename") != "PullRequest":
            raise SourceError("invalid GitHub TAIL pull request")
        number, head, pr_base = node.get("number"), node.get("headRefOid"), node.get("baseRefName")
        owner = node.get("repository")
        if (not _natural(number) or not number or number in seen
                or not isinstance(head, str) or not SHA.fullmatch(head)
                or not isinstance(pr_base, str) or not pr_base or not isinstance(owner, dict)
                or not isinstance(owner.get("nameWithOwner"), str)
                or owner["nameWithOwner"].lower() != repository.lower()):
            raise SourceError("invalid or duplicate TAIL pull request identity")
        seen.add(number)
        if mode == "enumerate":
            enumerated.append((_utc(node.get("createdAt")), number))
            if node.get("state") in ("OPEN", "CLOSED") and "mergedAt" in node and node["mergedAt"] is None:
                continue
            if node.get("state") != "MERGED":
                raise SourceError("invalid TAIL pull request merge state")
        merged = _utc(node.get("mergedAt"))
        if pr_base == base and lower <= merged <= upper:
            items.append({"number": number, "merged_at": merged, "head": head, "base": pr_base})
        elif mode == "search":
            raise SearchUnsupported("GitHub search returned a PR outside the TAIL qualifiers")
    remaining, reset_at = receipt
    return Page(items, connection[count_field], len(nodes), next_cursor, remaining, reset_at, enumerated)
