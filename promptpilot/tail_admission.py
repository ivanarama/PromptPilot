"""Read-only TAIL source selection shared by queue inspection and the mutation gates."""

from __future__ import annotations

import hashlib
import json
import re

from . import project_pipeline as pp
from . import review_routing_positive as positive
from .pipeline_errors import PipelineError
from .pr_identity import SHA
from .review_routing_baseline import _edges
from .review_routing_lease import timestamp


class InvalidSource(PipelineError):
    """The current observation cannot prove that this particular source is admissible."""


class InvalidTailSection(InvalidSource):
    def __init__(self, review_id, reason, line=None, text=None):
        self.location = {"review_id": str(review_id)}
        where = f"review comment {review_id}"
        if line is not None:
            self.location.update(line=line, text=text)
            where += f", line {line}"
        super().__init__(f"{where}: {reason}")


_TAIL_MARKER = re.compile(r"^<!-- pp:review pp:tail=([0-9]+) -->$", re.M)
_ITEM = re.compile(r"^([1-9][0-9]*)\. \[(заявка|выброс)\] (.+)$", re.S)
_TITLE_SEPARATOR = re.compile(r"(?<=[\x09-\x0d ])(?:(?:→|—)[\x09-\x0d ]+заголовок:|Заголовок:)(?=[\x09-\x0d ])")
_AFTER_MERGE = positive.LIFECYCLE - {"MergedEvent"}


def normalize_text(value):
    """pp-text-v1 preserves Unicode scalars and folds only ASCII whitespace."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidSource("TAIL text contains an invalid Unicode scalar") from exc
    return re.sub(r"[\x09-\x0d\x20]+", " ", value).strip(" ")


def _sha(value):
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise InvalidSource("TAIL review is not valid UTF-8") from exc


def _items(review):
    """Parse exactly the ordinary REVIEW tail section and retain the original item hash."""
    lines = review["body"].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    markers = [ix for ix, line in enumerate(lines) if _TAIL_MARKER.fullmatch(line)]
    starts = [ix for ix, line in enumerate(lines) if line == "Хвост:"]
    ends = [ix for ix, line in enumerate(lines) if line.startswith("Вердикт:")]

    def invalid(reason, ix):
        return InvalidTailSection(review["id"], reason, ix + 1, lines[ix])

    for positions, label in ((starts, "Хвост:"), (ends, "Вердикт:"), (markers, "pp:review")):
        if not positions:
            raise InvalidTailSection(review["id"], f"TAIL review is missing {label}")
        if len(positions) > 1:
            raise invalid(f"TAIL review repeats {label}", positions[1])
    if starts[0] >= ends[0]:
        raise invalid("TAIL verdict precedes its tail section", ends[0])
    item_starts = []
    dash = False
    for ix in range(starts[0] + 1, ends[0]):
        line = lines[ix]
        if not line.strip(" \t"):
            continue
        if line.strip(" \t") == "—" and not item_starts and not dash:
            dash = True
        elif not dash and _ITEM.fullmatch(line):
            item_starts.append(ix)
        elif not item_starts or not line.startswith((" ", "\t")):
            raise invalid("TAIL review contains an unexpected tail section line", ix)
    items = []
    for index, (start, stop) in enumerate(zip(item_starts, item_starts[1:] + ends), 1):
        raw = "\n".join(lines[start:stop])
        match = _ITEM.fullmatch(raw)
        number, tag, content = match.groups()
        if number != str(index):
            raise invalid("TAIL item numbers are not consecutive", start)
        kind = "issue" if tag == "заявка" else "discard"
        title, text = None, normalize_text(content)
        if kind == "issue":
            # Locate separators before folding whitespace so an error points
            # to its physical line even when the item spans several lines.
            splits = list(_TITLE_SEPARATOR.finditer(content))
            last_line = start + content.rstrip(" \t\r\n\v\f").count("\n")
            if not splits:
                raise invalid("TAIL issue has no unique title separator", last_line)
            if len(splits) > 1:
                repeated = start + content.count("\n", 0, splits[1].start())
                raise invalid("TAIL issue has no unique title separator", repeated)
            sep = splits[0]
            title = normalize_text(content[sep.end():])
            text = normalize_text(content[:sep.start()])
            # format_review wraps the entire title, including quoted UI names.
            quote_pattern = r"«(.+)»\.?" if sep.group().startswith("→") else r"«([^«»]+)»\.?"
            quoted = re.fullmatch(quote_pattern, title)
            if quoted:
                title = quoted.group(1).strip(" ")
            elif sep.group() == "Заголовок:" or title.startswith("«") or title.endswith("»"):
                raise invalid("TAIL issue has malformed title quotes", last_line)
            if not title or not text:
                raise invalid("TAIL issue needs a title and task text", last_line if text else start)
        items.append(dict(index=index, kind=kind, title=title, text=text,
                          item_sha256=_sha(normalize_text(raw))))
    marker = markers[0]
    if _TAIL_MARKER.fullmatch(lines[marker])[1] != str(sum(item["kind"] == "issue" for item in items)):
        raise invalid("TAIL review count disagrees with its issue items", marker)
    return items


def _read_comments(gh, config, number, snapshot):
    path = f"repos/{config['repository']}/issues/{number}/comments?per_page=100"
    raw = gh.run("api", "--paginate", path, "--jq", ".[]")
    try:
        rows = [json.loads(line) for line in raw.split("\n") if line.strip()]
    except json.JSONDecodeError as exc:
        raise PipelineError("TAIL REST comments pagination is incomplete") from exc
    graph = {}
    for edge in _edges(snapshot):
        node = edge["node"]
        if node["__typename"] == "IssueComment":
            ident = pp.comment_id(node)
            if not ident or ident <= 0 or ident in graph:
                raise PipelineError("TAIL timeline comment identity is missing or repeated")
            graph[ident] = node
    comments = []
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or "user" not in row
                or row["user"] is not None and not isinstance(row["user"], dict)):
            raise PipelineError("TAIL REST comment identity is unavailable")
        node = graph.pop(row["id"], None)
        login = (row["user"] or {}).get("login")
        if (node is None or node.get("body") != row.get("body")
                or (node.get("author") or {}).get("login") != login
                or timestamp(node["createdAt"]) != timestamp(row.get("created_at"))
                or timestamp(node["lastEditedAt"] or node["createdAt"]) != timestamp(row.get("updated_at"))):
            raise PipelineError("TAIL REST comments and stable timeline disagree")
        if login == config["trusted_account"]:
            comments.append(dict(row, tail_edited=node["lastEditedAt"] is not None))
    if graph:
        raise PipelineError("TAIL REST comments omit timeline entries")
    return comments


def _merge(snapshot, pr, config, candidate):
    head, base = candidate["head"], candidate["base"]
    if (not isinstance(pr, dict) or not isinstance(pr.get("head"), dict)
            or not isinstance(pr.get("base"), dict) or not isinstance(pr["base"].get("repo"), dict)):
        raise PipelineError("TAIL merged PR projection is incomplete")
    labels = pr.get("labels")
    if (not isinstance(labels, list)
            or not all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels)
            or sorted(label["name"] for label in labels) != snapshot.get("labels")):
        raise PipelineError("TAIL REST and GraphQL labels disagree or are incomplete")
    if (type(pr.get("number")) is not int or pr["number"] != candidate["pr_number"]
            or pr.get("state") != "closed" or pr.get("merged") is not True
            or pr["head"].get("sha") != head or pr["base"].get("ref") != base
            or str(pr["base"]["repo"].get("full_name")).lower() != config["repository"].lower()
            or not isinstance(pr.get("merge_commit_sha"), str) or not SHA.fullmatch(pr["merge_commit_sha"])
            or snapshot.get("state") != "MERGED" or snapshot.get("isDraft") is not False
            or snapshot.get("baseRefName") != base
            or base != config["base_branch"] or base != "main"
            or snapshot.get("headRefOid") != head or snapshot.get("labelsComplete") is not True
            or timestamp(pr.get("merged_at")) != timestamp(candidate["merged_at"])):
        raise InvalidSource("TAIL merged PR identity differs from its durable observation")
    edges = _edges(snapshot)
    for edge in edges:
        node = edge["node"]
        if not isinstance(node.get("__typename"), str) or not node["__typename"]:
            raise PipelineError("TAIL timeline node type is incomplete")
        target = {"PullRequestCommit": "commit", "HeadRefForcePushedEvent": "afterCommit"}.get(node["__typename"])
        if target and (not isinstance(node.get(target), dict) or not isinstance(node[target].get("oid"), str)):
            raise PipelineError("TAIL timeline commit projection is incomplete")
    merges = [ix for ix, edge in enumerate(edges) if edge["node"]["__typename"] == "MergedEvent"]
    if len(merges) != 1:
        raise InvalidSource("TAIL requires exactly one observed merge")
    cut = merges[0]
    merge = edges[cut]["node"]
    if (not isinstance(merge.get("commit"), dict) or merge["commit"].get("oid") != pr["merge_commit_sha"]
            or timestamp(merge.get("createdAt")) != timestamp(pr["merged_at"])):
        raise InvalidSource("TAIL merge evidence disagrees between GitHub APIs")
    lifecycle = [edge["node"]["__typename"] for edge in edges[cut + 1:]
                 if edge["node"]["__typename"] in _AFTER_MERGE]
    if lifecycle not in ([], ["HeadRefDeletedEvent"]):
        raise InvalidSource("TAIL merged PR has an unsupported later lifecycle")
    return cut


def _ordinary(gh, config, snapshot, cut, comments, candidate):
    trusted = config["trusted_account"]
    edges = snapshot["edges"]
    cutover = gh.json("api", f"repos/{config['repository']}/pulls/1261")
    if (not isinstance(cutover, dict) or cutover.get("merged") is not True
            or cutover.get("number") != 1261 or cutover.get("state") != "closed"):
        raise PipelineError("TAIL legacy cutover is unavailable")
    boundary = timestamp(cutover.get("merged_at"))
    # The protocol's line grammar accepts CRLF. Keep the raw REST body for
    # hashing while the existing claim-bound proof reader sees its LF form.
    prefix_edges = []
    for edge in edges[:cut]:
        node = edge["node"]
        if node["__typename"] == "IssueComment" and "\r" in node["body"]:
            node = dict(node, body=node["body"].replace("\r\n", "\n").replace("\r", "\n"))
            edge = dict(edge, node=node)
        prefix_edges.append(edge)
    prefix = dict(snapshot, edges=prefix_edges)
    try:
        info = pp.epoch(prefix, trusted)
    except PipelineError:
        # An old legacy review may predate the current epoch protocol. Its
        # own selection and cutover are still checked independently below.
        info = None
    by_id = {row["id"]: row for row in comments}
    established = pp.proof(info, candidate["head"], trusted) if info is not None else None
    merge_id = edges[cut]["node"]["id"]
    if established:
        review = by_id[established["review_id"]]
        if any(edge["node"]["__typename"] in positive.LIFECYCLE | {"CommentDeletedEvent"}
               for edge in info["edges"]):
            raise InvalidSource("TAIL review epoch contains a lifecycle change or deleted comment")
        epoch_digest = pp.digest([info["hash"], established, merge_id])
        protocol = "committed"
    else:
        reviews = [row for row in comments if _TAIL_MARKER.search(row["body"].replace("\r\n", "\n"))]
        if not reviews:
            if candidate.get("review_id") or any("<!-- pp:review-claim " in r["body"]
                                                   or "<!-- pp:head-reviewed " in r["body"] for r in comments):
                raise InvalidSource("TAIL selected review or proof is missing")
            return None
        def review_order(row):
            return timestamp(row["created_at"]), row["id"]

        review = max(reviews, key=review_order)
        if max(timestamp(review["updated_at"]), timestamp(review["created_at"])) >= boundary:
            raise InvalidSource("TAIL review lacks current claim-bound proof and is outside the legacy cutover")
        previous_id = candidate.get("review_id")
        if previous_id and str(review["id"]) != str(previous_id):
            previous = next((row for row in comments if str(row["id"]) == str(previous_id)), None)
            if previous is None or review_order(previous) > review_order(review):
                raise InvalidSource("TAIL cannot fall back past a missing or changed selected review")
        epoch_digest = pp.digest([candidate["head"], merge_id, review["id"], cutover["merged_at"]])
        protocol = "legacy"
    review_index = next(ix for ix, edge in enumerate(edges) if edge["node"]["__typename"] == "IssueComment"
                        and pp.comment_id(edge["node"]) == review["id"])
    for edge in edges[review_index + 1:]:
        node = edge["node"]
        if node["__typename"] == "CommentDeletedEvent":
            raise InvalidSource("TAIL review history contains a deleted comment")
        if (node["__typename"] == "IssueComment" and (node.get("author") or {}).get("login") == trusted
                and pp.has_review_again(node["body"].replace("\r\n", "\n"))):
            raise InvalidSource("TAIL selected review was superseded by a review request")
    return protocol, review, _items(review), epoch_digest


def _legacy_done_ids(snapshot, review):
    comments = (edge["node"] for edge in snapshot["edges"] if edge["node"]["__typename"] == "IssueComment")
    selected = next(node for node in comments if pp.comment_id(node) == review["id"])
    version_time = timestamp(review["updated_at"])
    done = set()
    # The remaining edges follow the selected publication, including same-second
    # comments. Edits have no edge of their own, so a tie with an edit is ambiguous.
    for node in comments:
        if "<!-- pp:tail-done -->" in node["body"].replace("\r\n", "\n").split("\n"):
            posted = timestamp(node["createdAt"])
            if posted > version_time or (posted == version_time and selected["lastEditedAt"] is None):
                done.add(pp.comment_id(node))
    return done


def classify(gh, config, candidate):
    """Select and re-prove the exact current source. Never issue a GitHub write."""
    number = candidate["pr_number"]
    pr = gh.json("api", f"repos/{config['repository']}/pulls/{number}")
    snapshot = pp.stable_timeline(gh, config, number)
    try:
        cut = _merge(snapshot, pr, config, candidate)
        paused = bool({"hold", "no-tail"}.intersection(snapshot["labels"]))
        comments = _read_comments(gh, config, number, snapshot)
        if positive.has_history(snapshot, config["trusted_account"]):
            purpose = "tail-completion" if paused else "tail"
            source = positive.live(gh, config, number, purpose=purpose)
            if (source["snapshot"]["edges"] != snapshot["edges"] or source["head"] != candidate["head"]
                    or source["snapshot"]["labels"] != snapshot["labels"]):
                raise PipelineError("TAIL typed proof changed between independent reads")
            publication = source["source"]["source"]["publication_id"]
            review = next((row for row in comments if str(row["id"]) == str(publication)), None)
            if review is None or review["updated_at"] != source["publication_created_at"]:
                raise InvalidSource("TAIL typed review publication identity disagrees with REST")
            protocol, items, epoch_digest = "typed", source["tail"], source["source_sha256"]
        else:
            selected = _ordinary(gh, config, snapshot, cut, comments, candidate)
            if selected is None:
                return dict(state="pending-proof", reason="TAIL has no selected review")
            protocol, review, items, epoch_digest = selected
        version = dict(review_id=str(review["id"]), review_updated_at=review["updated_at"],
                       review_body_digest=_sha(review["body"]), epoch_digest=epoch_digest)
        marker = f"<!-- pp:tail-done review-comment={review['id']} review-updated={review['updated_at']} -->"
        legacy_done = _legacy_done_ids(snapshot, review) if protocol == "legacy" else set()
        for row in comments:
            lines = row["body"].replace("\r\n", "\n").split("\n")
            if marker in lines:
                kind = "versioned"
            elif row["id"] in legacy_done:
                kind = "legacy"
            else:
                continue
            # GitHub cannot establish when an edited comment gained the marker.
            completed_at = None if row["tail_edited"] else row["created_at"]
            return dict(state="resolved", completion_kind=kind,
                        completion_digest=pp.digest([kind, version, row["id"], row["updated_at"]]),
                        completed_at=completed_at, reason="TAIL completion verified", **version)
        if not any(item["kind"] == "issue" for item in items):
            return dict(state="resolved", completion_kind="empty", completion_digest=pp.digest(["empty", version]),
                        reason="TAIL selected review has zero issue items", **version)
        source = dict(protocol=protocol, review_body=review["body"], review_edited=review["tail_edited"], title=pr.get("title"),
                      items=items, comments=comments, **version)
        if paused:
            return dict(state="waiting-policy", reason="TAIL paused by PR policy", **source)
        return dict(state="ready", reason="TAIL selected review has open items", **source)
    except InvalidTailSection as exc:
        return dict(state="invalid-source", reason=str(exc), parse_error=exc.location)
    except InvalidSource as exc:
        return dict(state="invalid-source", reason=str(exc))
