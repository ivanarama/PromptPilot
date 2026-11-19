"""GitHub REST and GraphQL wire data for a single merged TAIL source."""

from datetime import datetime, timedelta, timezone
import hashlib


def add_edge(source, kind, **node):
    edges = source["snapshot"]["edges"]
    edges.append({"cursor": f"edge:{node['id']}", "node": {"__typename": kind, **node}})


def add_comment(source, body, *, ident=None, login=None, created=None, edited=None):
    comments = source["comments"]
    ident = ident if ident is not None else max([row["id"] for row in comments] + [100]) + 1
    created = created or source["pull"]["merged_at"]
    login = login or source["trusted"]
    row = {"id": ident, "node_id": f"comment-{ident}", "created_at": created,
           "updated_at": edited or created, "body": body, "user": {"login": login}}
    comments.append(row)
    add_edge(source, "IssueComment", id=row["node_id"], fullDatabaseId=str(ident), body=body,
             createdAt=created, lastEditedAt=edited, author={"login": login})
    return row


def graphql_response(source):
    snapshot = source["snapshot"]
    pr = {field: snapshot[field] for field in ("headRefOid", "baseRefOid", "baseRefName", "state", "isDraft")}
    pr.update(labels={"nodes": [{"name": label} for label in snapshot["labels"]],
                      "pageInfo": {"hasNextPage": False}},
              timelineItems={"updatedAt": snapshot["updatedAt"], "edges": snapshot["edges"],
                             "pageInfo": {"hasNextPage": False, "endCursor": None}})
    return {"data": {"repository": {"pullRequest": pr}}}


def source_fixture(protocol="committed", number=42, merged_at="2026-03-01T00:00:00Z",
                   repository="owner/project", trusted="owner", items=None):
    if items is None:
        items = ["1. [заявка] Проверить сохранение записи → заголовок: «Сохранение записи»"]
    count = sum("[заявка]" in item for item in items)
    head, merge_sha = "a" * 40, "b" * 40
    merge_date = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    review_date = merge_date - timedelta(days=1)
    stamp = lambda date: date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    review_at = stamp(review_date)
    cutover_at = stamp(review_date + (timedelta(hours=1) if protocol == "legacy" else -timedelta(hours=1)))
    anchor = "anchor"
    epoch = hashlib.sha256(f"pp-review-epoch-v1\nhead={head}\nanchor-node={anchor}\n".encode()).hexdigest()
    source = {
        "trusted": trusted,
        "candidate": {"pr_number": number, "head": head, "base": "main", "merged_at": merged_at},
        "pull": {"number": number, "title": "Test source", "state": "closed", "merged": True, "labels": [],
                 "head": {"sha": head}, "base": {"ref": "main", "repo": {"full_name": repository}},
                 "merged_at": merged_at, "merge_commit_sha": merge_sha},
        "snapshot": {"headRefOid": head, "baseRefOid": "c" * 40, "baseRefName": "main",
                     "isDraft": False, "state": "MERGED", "labels": [], "labelsComplete": True,
                     "edges": [], "updatedAt": merged_at},
        "cutover": {"number": 1261, "state": "closed", "merged": True, "merged_at": cutover_at},
        "comments": [],
    }
    add_edge(source, "PullRequestCommit", id=anchor, commit={"oid": head})
    review = "\n".join([f"Reviewed-SHA: {head}", "Outcome-Label: reviewed", "Хвост:", *(items or ["—"]),
                        "Вердикт: годится к мержу.", f"<!-- pp:review pp:tail={count} -->"])
    add_comment(source, review, ident=101, created=review_at)
    if protocol == "committed":
        add_comment(source, f"<!-- pp:review-claim {head} review-comment=101 epoch-sha256={epoch} -->",
                    ident=102, created=review_at)
        add_comment(source, f"<!-- pp:head-reviewed {head} review-comment=101 claim=102 epoch-sha256={epoch} -->",
                    ident=103, created=review_at)
    add_edge(source, "MergedEvent", id="merged", commit={"oid": merge_sha}, createdAt=merged_at)
    return source
