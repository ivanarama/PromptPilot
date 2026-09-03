"""Deterministic, executor-neutral helper for GitHub maintenance pipelines.

The helper supports the ordinary REVIEW transaction and an already-clean
ordinary MERGE, including durable post-merge cleanup recovery. Complicated
base-sync/carry states return ``fallback`` so the repository's full skill
remains the authority for them.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REVIEW = re.compile(r"(?m)^Reviewed-SHA: ([0-9a-f]{40})$.*^Outcome-Label: (reviewed|changes-requested|needs-decision)$.*^<!-- pp:review pp:tail=([0-9]+) -->$", re.S)
CLAIM_MESSAGE = "PromptPilot service marker: REVIEW result publication claimed."
COMPLETE_MESSAGE = "PromptPilot service marker: REVIEW result committed."
MERGE_INTENT_MESSAGE = "PromptPilot service marker: MERGE transaction reserved."
MERGE_DONE_MESSAGE = "PromptPilot service marker: MERGE cleanup completed."
CLAIM = re.compile(r"^(?:PromptPilot service marker: REVIEW result publication claimed\.\n)?<!-- pp:review-claim ([0-9a-f]{40}) review-comment=([0-9]+) epoch-sha256=([0-9a-f]{64}) -->$")
COMPLETE = re.compile(r"^(?:PromptPilot service marker: REVIEW result committed\.\n)?<!-- pp:head-reviewed ([0-9a-f]{40}) review-comment=([0-9]+) claim=([0-9]+) epoch-sha256=([0-9a-f]{64}) -->$")
OVERRIDE = re.compile(r"(?m)^pp:review-again$")
BASE_SYNC = re.compile(r"(?m)^<!-- pp:base-sync-(?:intent|done) ")
LINKED = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"\s*:?\s+(?:([a-z0-9_.-]+)/([a-z0-9_.-]+))?#([1-9][0-9]*)\b"
)
PLAN_LINK = re.compile(r"(?m)^Plan-Issue: #([0-9]+)\r?\nPlan-Path: (Plans/[^/\r\n]+\.md)$")
MERGE_CLEANUP_INTENT = re.compile(
    r"^(?:PromptPilot service marker: MERGE transaction reserved\.\n)?"
    r"<!-- pp:merge-cleanup-intent head=([0-9a-f]{40}) "
    r"proof-sha256=([0-9a-f]{64}) body-sha256=([0-9a-f]{64}) "
    r"issues=(none|[1-9][0-9]*(?:,[1-9][0-9]*)*) -->$"
)
MERGE_CLEANUP_DONE = re.compile(
    r"^(?:PromptPilot service marker: MERGE cleanup completed\.\n)?"
    r"<!-- pp:merge-cleanup-done intent=([0-9]+) head=([0-9a-f]{40}) "
    r"merge=([0-9a-f]{40}) -->$"
)
ISSUE_URL_NUMBER = re.compile(r"/issues/([1-9][0-9]*)$")

TIMELINE_QUERY = r"""
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  headRefOid baseRefOid baseRefName state
  labels(first:100){nodes{name} pageInfo{hasNextPage}}
  timelineItems(first:100,after:$cursor,itemTypes:[PULL_REQUEST_COMMIT,HEAD_REF_FORCE_PUSHED_EVENT,HEAD_REF_DELETED_EVENT,HEAD_REF_RESTORED_EVENT,BASE_REF_CHANGED_EVENT,BASE_REF_FORCE_PUSHED_EVENT,BASE_REF_DELETED_EVENT,MERGED_EVENT,ISSUE_COMMENT,COMMENT_DELETED_EVENT,LABELED_EVENT,UNLABELED_EVENT]){
   updatedAt pageInfo{hasNextPage endCursor}
   edges{cursor node{__typename
    ... on PullRequestCommit{id commit{oid}}
    ... on HeadRefForcePushedEvent{id createdAt afterCommit{oid}}
    ... on HeadRefDeletedEvent{id createdAt}
    ... on HeadRefRestoredEvent{id createdAt}
    ... on BaseRefChangedEvent{id createdAt previousRefName currentRefName}
    ... on BaseRefForcePushedEvent{id createdAt beforeCommit{oid} afterCommit{oid}}
    ... on BaseRefDeletedEvent{id createdAt baseRefName}
    ... on MergedEvent{id createdAt commit{oid}}
    ... on IssueComment{id fullDatabaseId createdAt lastEditedAt author{login} body}
    ... on CommentDeletedEvent{id createdAt}
    ... on LabeledEvent{id createdAt actor{login} label{name}}
    ... on UnlabeledEvent{id createdAt actor{login} label{name}}
   }}
  }
 }}}
"""


class PipelineError(RuntimeError):
    pass


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def content_review_digest(snapshot: dict) -> str:
    """Bind an audit to PR content without invalidating it on a normal base advance."""
    stable = dict(snapshot)
    stable.pop("baseRefOid", None)
    return digest(stable)


def encode_lease(value: dict) -> str:
    return base64.urlsafe_b64encode(canonical(value)).decode("ascii").rstrip("=")


def decode_lease(value: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        result = json.loads(raw)
    except Exception as exc:
        raise PipelineError(f"invalid lease: {exc}") from exc
    if not isinstance(result, dict) or result.get("version") != 1:
        raise PipelineError("unsupported lease")
    return result


class GitHub:
    def __init__(self, executable: str | None = None):
        self.executable = executable or os.environ.get("GH_EXE") or os.environ.get("PP_GH_EXE")
        self.executable = self.executable or shutil.which("gh") or shutil.which("gh.exe")
        if not self.executable:
            standard = Path(r"C:\Program Files\GitHub CLI\gh.exe")
            if standard.exists():
                self.executable = str(standard)
        if not self.executable:
            raise PipelineError("GitHub CLI not found")

    def run(self, *args: str, input_value=None, allow=(0,)) -> str:
        data = None
        if input_value is not None:
            data = json.dumps(input_value, ensure_ascii=False)
        result = subprocess.run(
            [self.executable, *args], input=data, capture_output=True, text=True,
            encoding="utf-8", errors="strict",
        )
        if result.returncode not in allow:
            message = (result.stderr or result.stdout or f"gh exited {result.returncode}").strip()
            raise PipelineError(message)
        return result.stdout

    def json(self, *args: str, input_value=None):
        raw = self.run(*args, input_value=input_value)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PipelineError(f"gh returned invalid JSON: {exc}") from exc

    def graphql_page(self, owner: str, name: str, number: int, cursor: str | None):
        args = ["api", "graphql", "-f", f"query={TIMELINE_QUERY}", "-F", f"owner={owner}",
                "-F", f"name={name}", "-F", f"number={number}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        return self.json(*args)


def load_config(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = ("repository", "trusted_account", "health_command")
    if not isinstance(data, dict) or any(not data.get(key) for key in required):
        raise PipelineError(f"config must define {', '.join(required)}")
    if "/" not in data["repository"] or not isinstance(data["health_command"], list):
        raise PipelineError("invalid repository or health_command")
    data.setdefault("base_branch", "main")
    data.setdefault("merge_method", "merge")
    return data


def queue_priority(item: dict, config: dict, now: datetime | None = None) -> int:
    """Return P0..P3 as 0..3. Manual labels beat auto labels and classification."""
    settings = config.get("priority") or {}
    manual = settings.get("manual_labels") or {
        "p0": "queue:p0", "p1": "queue:p1", "p2": "queue:p2", "p3": "queue:p3",
    }
    automatic = settings.get("auto_labels") or {
        "p0": "queue:auto:p0", "p1": "queue:auto:p1",
        "p2": "queue:auto:p2", "p3": "queue:auto:p3",
    }
    labels = {value.get("name", "") if isinstance(value, dict) else str(value)
              for value in item.get("labels", [])}
    base = next((level for level in range(4) if manual.get(f"p{level}") in labels), None)
    if base is None:
        base = next((level for level in range(4)
                     if automatic.get(f"p{level}") in labels), None)
    if base is None:
        if labels & {"security", "severity:critical", "blocker", "data-loss"}:
            base = 0
        elif "bug" in labels:
            base = 1
        elif labels & {"enhancement", "documentation"}:
            base = 2
        elif "question" in labels:
            base = 3
        else:
            value = str(settings.get("default_level", "p2")).lower()
            base = int(value[1]) if re.fullmatch(r"p[0-3]", value) else 2
    created_raw = item.get("created_at") or item.get("createdAt")
    try:
        created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        created = None
    aging_hours = max(1, int(settings.get("aging_hours", 168)))
    current = now or datetime.now(timezone.utc)
    boost = min(max(0, base - 1), int(max(0, (current - created).total_seconds()) // (aging_hours * 3600))) if created else 0
    return base - boost


def run_health(config: dict) -> dict:
    command = [str(value) for value in config["health_command"]]
    if command and command[0] in {"go", "go.exe"} and shutil.which(command[0]) is None:
        standard = Path(r"C:\Program Files\Go\bin\go.exe")
        if standard.exists():
            command[0] = str(standard)
    env = os.environ.copy()
    if env.get("PP_GH_EXE"):
        env.setdefault("GH_EXE", env["PP_GH_EXE"])
        gh_dir = str(Path(env["PP_GH_EXE"]).parent)
        path_parts = env.get("PATH", "").split(os.pathsep)
        if gh_dir and gh_dir not in path_parts:
            env["PATH"] = gh_dir + os.pathsep + env.get("PATH", "")
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="strict", env=env)
    if result.returncode not in (0, 1):
        raise PipelineError((result.stderr or result.stdout or "health command failed").strip())
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"health command returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("health command must return a JSON object")
    return value


def timeline_pass(gh: GitHub, config: dict, number: int) -> dict:
    owner, name = config["repository"].split("/", 1)
    cursor = None
    edges = []
    header = None
    while True:
        payload = gh.graphql_page(owner, name, number, cursor)
        pr = payload.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr:
            raise PipelineError(f"PR #{number} not found")
        current = {key: pr.get(key) for key in ("headRefOid", "baseRefOid", "baseRefName", "state")}
        current["labels"] = sorted(node["name"] for node in pr["labels"]["nodes"])
        current["labelsComplete"] = not pr["labels"]["pageInfo"]["hasNextPage"]
        if header is None:
            header = current
        elif header != current:
            raise PipelineError("PR changed while timeline was paginated")
        connection = pr["timelineItems"]
        edges.extend(connection["edges"])
        if not connection["pageInfo"]["hasNextPage"]:
            return {**header, "updatedAt": connection.get("updatedAt"), "edges": edges}
        cursor = connection["pageInfo"].get("endCursor")
        if not cursor:
            raise PipelineError("timeline pagination did not return endCursor")


def stable_timeline(gh: GitHub, config: dict, number: int) -> dict:
    first = timeline_pass(gh, config, number)
    second = timeline_pass(gh, config, number)
    if canonical(first) != canonical(second):
        raise PipelineError("timeline changed between stable reads")
    return first


def comment_id(node: dict) -> int | None:
    value = node.get("fullDatabaseId")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def epoch(snapshot: dict, trusted: str) -> dict:
    head = snapshot.get("headRefOid")
    candidates = []
    for index, edge in enumerate(snapshot["edges"]):
        node = edge.get("node") or {}
        kind = node.get("__typename")
        if kind == "PullRequestCommit" and (node.get("commit") or {}).get("oid") == head:
            candidates.append((index, edge))
        elif kind == "HeadRefForcePushedEvent" and (node.get("afterCommit") or {}).get("oid") == head:
            candidates.append((index, edge))
        elif kind == "HeadRefRestoredEvent" and head:
            candidates.append((index, edge))
    if not candidates:
        raise PipelineError("current HEAD has no server timeline anchor")
    anchor_index, anchor = candidates[-1]
    for index, edge in enumerate(snapshot["edges"][anchor_index + 1:], anchor_index + 1):
        node = edge.get("node") or {}
        if (node.get("__typename") == "IssueComment" and
                (node.get("author") or {}).get("login") == trusted and
                node.get("lastEditedAt") is None and OVERRIDE.search(node.get("body") or "")):
            anchor_index, anchor = index, edge
    node = anchor["node"]
    anchor_id = node.get("id")
    if not anchor_id:
        raise PipelineError("epoch anchor has no node id")
    epoch_hash = hashlib.sha256(
        f"pp-review-epoch-v1\nhead={head}\nanchor-node={anchor_id}\n".encode("ascii")
    ).hexdigest()
    return {"anchor_index": anchor_index, "anchor_id": anchor_id,
            "anchor_cursor": anchor["cursor"], "hash": epoch_hash,
            "edges": snapshot["edges"][anchor_index + 1:]}


def validate_common(snapshot: dict, config: dict, data: dict) -> None:
    if snapshot.get("state") != "OPEN" or snapshot.get("baseRefName") != config["base_branch"]:
        raise PipelineError("PR is not open against the configured base branch")
    if not snapshot.get("labelsComplete"):
        raise PipelineError("more than 100 labels; cannot prove gate")
    if data and snapshot.get("headRefOid") != data.get("head"):
        raise PipelineError("PR HEAD changed")


def validate_epoch_safety(info: dict, trusted: str) -> None:
    dangerous = {"HeadRefForcePushedEvent", "HeadRefDeletedEvent", "HeadRefRestoredEvent",
                 "BaseRefChangedEvent", "BaseRefForcePushedEvent", "BaseRefDeletedEvent",
                 "CommentDeletedEvent", "MergedEvent", "PullRequestCommit"}
    for edge in info["edges"]:
        node = edge.get("node") or {}
        if node.get("__typename") in dangerous:
            raise PipelineError(f"unsupported epoch event: {node.get('__typename')}")
        if (node.get("__typename") == "IssueComment" and
                (node.get("author") or {}).get("login") == trusted and
                node.get("lastEditedAt") is not None):
            raise PipelineError("trusted comment was edited in current epoch")


def review_gate(snapshot: dict, config: dict, lease: dict, *, require_outcome: str | None = None) -> dict:
    validate_common(snapshot, config, lease)
    labels = set(snapshot["labels"])
    # ``ship`` is a sticky human intent: merge this exact HEAD if its review
    # succeeds.  It may be applied while REVIEW is still publishing its
    # claim/completion transaction, so it must not hide the PR from REVIEW.
    if labels & {"hold", "needs-decision"}:
        raise PipelineError("review routing gate closed")
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    if info["hash"] != lease["epoch"] or info["anchor_id"] != lease["anchor"]:
        raise PipelineError("review epoch changed")
    if require_outcome and require_outcome not in labels:
        raise PipelineError(f"outcome label {require_outcome} disappeared")
    return info


def comments(info: dict, trusted: str):
    return [(index, edge, edge["node"]) for index, edge in enumerate(info["edges"])
            if (edge.get("node") or {}).get("__typename") == "IssueComment"
            and ((edge["node"].get("author") or {}).get("login") == trusted)
            and edge["node"].get("lastEditedAt") is None]


def proof(info: dict, head: str, trusted: str) -> dict | None:
    by_id = {comment_id(node): (index, node) for index, _edge, node in comments(info, trusted)}
    claims = []
    completions = []
    for index, _edge, node in comments(info, trusted):
        body = (node.get("body") or "").strip()
        match = CLAIM.fullmatch(body)
        if match and match.group(1) == head and match.group(3) == info["hash"]:
            claims.append((index, comment_id(node), int(match.group(2)), node))
        match = COMPLETE.fullmatch(body)
        if match and match.group(1) == head and match.group(4) == info["hash"]:
            completions.append((index, comment_id(node), int(match.group(2)), int(match.group(3)), node))
    if not claims:
        return None
    winner = min(claims, key=lambda item: (item[0], item[1] or 0))
    for item in completions:
        index, completion_id, review_id, claim_id, completion_node = item
        if claim_id != winner[1] or review_id != winner[2] or index <= winner[0]:
            continue
        review_item = by_id.get(review_id)
        if not review_item or review_item[0] >= winner[0]:
            continue
        match = REVIEW.search(review_item[1].get("body") or "")
        if not match or match.group(1) != head:
            continue
        return {"review_id": review_id, "review_node": review_item[1].get("id"),
                "claim_id": claim_id, "claim_node": winner[3].get("id"),
                "completion_id": completion_id, "completion_node": completion_node.get("id"),
                "outcome": match.group(2), "completion_index": index}
    return None


def trusted_ship_authorized(info: dict, trusted: str) -> bool:
    """Accept the latest trusted ship transition anywhere in this HEAD epoch.

    The canonical review proof is checked separately.  Requiring the label
    event to occur after the final completion comment created a UI race: a
    human could set ship after seeing the reviewed result but before the worker
    finished its bookkeeping.  The epoch anchor still binds this permission to
    the current HEAD, and a later unlabel or foreign label remains invalid.
    """
    transitions = []
    for index, edge in enumerate(info["edges"]):
        node = edge.get("node") or {}
        if (node.get("__typename") in {"LabeledEvent", "UnlabeledEvent"} and
                (node.get("label") or {}).get("name") == "ship"):
            transitions.append((index, node))
    if not transitions:
        return False
    latest = transitions[-1][1]
    return (latest.get("__typename") == "LabeledEvent" and
            (latest.get("actor") or {}).get("login") == trusted)


def post_comment(gh: GitHub, config: dict, number: int, body: str) -> dict:
    value = gh.json("api", f"repos/{config['repository']}/issues/{number}/comments",
                    "--method", "POST", "--input", "-", input_value={"body": body})
    if value.get("body") != body or (value.get("user") or {}).get("login") != config["trusted_account"]:
        raise PipelineError("posted comment failed exact UTF-8/author verification")
    return value


def add_label(gh: GitHub, config: dict, number: int, label: str) -> None:
    value = gh.json("api", f"repos/{config['repository']}/issues/{number}/labels",
                    "--method", "POST", "--input", "-", input_value={"labels": [label]})
    if label not in [item.get("name") for item in value]:
        raise PipelineError(f"label {label} was not confirmed")


def remove_label(gh: GitHub, config: dict, number: int, label: str) -> None:
    gh.run("api", "--method", "DELETE",
           f"repos/{config['repository']}/issues/{number}/labels/{label}", allow=(0, 1))


def same_repo_closing_issues(body: str, repository: str) -> list[int]:
    owner, name = repository.split("/", 1)
    result = set()
    for match in LINKED.finditer(body or ""):
        ref_owner, ref_name, number = match.groups()
        if ref_owner and (ref_owner.lower() != owner.lower() or ref_name.lower() != name.lower()):
            continue
        result.add(int(number))
    return sorted(result)


def repository_comments(gh: GitHub, config: dict) -> list[dict]:
    raw = gh.run(
        "api", "--paginate",
        f"repos/{config['repository']}/issues/comments?per_page=100&sort=created&direction=asc",
        "--jq", ".[]",
    )
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def parse_merge_intent(comment: dict, config: dict) -> dict | None:
    if (comment.get("user") or {}).get("login") != config["trusted_account"]:
        return None
    if comment.get("created_at") != comment.get("updated_at"):
        return None
    match = MERGE_CLEANUP_INTENT.fullmatch((comment.get("body") or "").strip())
    issue_match = ISSUE_URL_NUMBER.search(comment.get("issue_url") or "")
    if not match or not issue_match:
        return None
    issues = [] if match.group(4) == "none" else [int(value) for value in match.group(4).split(",")]
    return {
        "id": int(comment["id"]),
        "number": int(issue_match.group(1)),
        "head": match.group(1),
        "proof_sha256": match.group(2),
        "body_sha256": match.group(3),
        "issues": issues,
        "body": (comment.get("body") or "").strip(),
    }


def parse_merge_done(comment: dict, config: dict) -> dict | None:
    if (comment.get("user") or {}).get("login") != config["trusted_account"]:
        return None
    if comment.get("created_at") != comment.get("updated_at"):
        return None
    match = MERGE_CLEANUP_DONE.fullmatch((comment.get("body") or "").strip())
    issue_match = ISSUE_URL_NUMBER.search(comment.get("issue_url") or "")
    if not match or not issue_match:
        return None
    return {"intent": int(match.group(1)), "head": match.group(2),
            "merge": match.group(3), "number": int(issue_match.group(1))}


def pending_merge_intents(gh: GitHub, config: dict) -> list[dict]:
    comments_value = repository_comments(gh, config)
    done_values = [done for comment in comments_value
                   if (done := parse_merge_done(comment, config)) is not None]
    intents = []
    for comment in comments_value:
        intent = parse_merge_intent(comment, config)
        if intent is None:
            continue
        completed = any(done["intent"] == intent["id"] and
                        done["number"] == intent["number"] and
                        done["head"] == intent["head"] for done in done_values)
        if not completed:
            intents.append(intent)
    return sorted(intents, key=lambda item: item["id"])


def remove_in_work_from_closed_issue(gh: GitHub, config: dict, number: int) -> bool:
    path = f"repos/{config['repository']}/issues/{number}"
    issue = gh.json("api", path)
    labels = {item.get("name") for item in issue.get("labels", [])}
    if issue.get("state") != "closed":
        raise PipelineError(f"closing issue #{number} is not closed after merge")
    if "in-work" not in labels:
        return False
    remove_label(gh, config, number, "in-work")
    confirmed = gh.json("api", path)
    if "in-work" in {item.get("name") for item in confirmed.get("labels", [])}:
        raise PipelineError(f"in-work removal was not confirmed for issue #{number}")
    return True


def finish_plan_handoff(gh: GitHub, config: dict, pr_number: int, body: str) -> dict | None:
    """Return an issue to FIX after its reviewed plan PR was merged."""
    match = PLAN_LINK.search(body or "")
    if not match:
        return None
    issue_number, plan_path = int(match.group(1)), match.group(2)
    issue = gh.json("api", f"repos/{config['repository']}/issues/{issue_number}")
    labels = {item.get("name") for item in issue.get("labels", [])}
    if issue.get("state") != "open" or "approved" not in labels:
        raise PipelineError(f"plan issue #{issue_number} is not open and approved")
    marker = (f"План `{plan_path}` влит через PR #{pr_number}; заявка возвращена в FIX.\n"
              f"<!-- pp:plan-ready issue={issue_number} pr={pr_number} path={plan_path} -->")
    raw = gh.run("api", "--paginate",
                 f"repos/{config['repository']}/issues/{issue_number}/comments?per_page=100",
                 "--jq", ".[]")
    issue_comments = [json.loads(line) for line in raw.splitlines() if line.strip()]
    marker_exists = any(
        (item.get("user") or {}).get("login") == config["trusted_account"]
        and item.get("created_at") == item.get("updated_at")
        and item.get("body") == marker
        for item in issue_comments
    )
    if not marker_exists:
        if "plan-in-review" not in labels:
            raise PipelineError(f"plan issue #{issue_number} has no recoverable plan-in-review marker")
        post_comment(gh, config, issue_number, marker)
    if "ready-fix" not in labels:
        add_label(gh, config, issue_number, "ready-fix")
    if "plan-in-review" in labels:
        remove_label(gh, config, issue_number, "plan-in-review")
    if "needs-decision" in labels:
        remove_label(gh, config, issue_number, "needs-decision")
    return {"issue": issue_number, "path": plan_path}


def validate_merged_intent(gh: GitHub, config: dict, intent: dict) -> tuple[dict, str]:
    pr = gh.json("api", f"repos/{config['repository']}/pulls/{intent['number']}")
    head = (pr.get("head") or {}).get("sha")
    base = (pr.get("base") or {}).get("ref")
    merge_sha = pr.get("merge_commit_sha")
    body = pr.get("body") or ""
    if (pr.get("state") != "closed" or pr.get("merged") is not True or
            head != intent["head"] or base != config["base_branch"] or
            not re.fullmatch(r"[0-9a-f]{40}", merge_sha or "")):
        raise PipelineError(f"PR #{intent['number']} is not the exact merged cleanup target")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != intent["body_sha256"]:
        raise PipelineError("merged PR body changed after cleanup intent")
    if same_repo_closing_issues(body, config["repository"]) != intent["issues"]:
        raise PipelineError("closing issue set no longer matches cleanup intent")

    snapshot = stable_timeline(gh, config, intent["number"])
    if (snapshot.get("state") != "MERGED" or snapshot.get("baseRefName") != config["base_branch"]
            or not snapshot.get("labelsComplete")):
        raise PipelineError("merged GraphQL snapshot does not match cleanup target")
    intent_index = None
    merged_events = []
    forbidden = {"PullRequestCommit", "HeadRefForcePushedEvent", "HeadRefRestoredEvent",
                 "BaseRefChangedEvent", "BaseRefForcePushedEvent", "BaseRefDeletedEvent",
                 "CommentDeletedEvent"}
    for index, edge in enumerate(snapshot["edges"]):
        node = edge.get("node") or {}
        if (node.get("__typename") == "IssueComment" and
                comment_id(node) == intent["id"] and node.get("body") == intent["body"] and
                (node.get("author") or {}).get("login") == config["trusted_account"] and
                node.get("lastEditedAt") is None):
            intent_index = index
        if intent_index is not None and index > intent_index:
            if node.get("__typename") in forbidden:
                raise PipelineError(f"unsupported event after merge cleanup intent: {node.get('__typename')}")
            if node.get("__typename") == "MergedEvent":
                merged_events.append((index, (node.get("commit") or {}).get("oid")))
    if intent_index is None:
        raise PipelineError("merge cleanup intent is missing or edited in GraphQL timeline")
    matching = [value for index, value in merged_events if index > intent_index and value == merge_sha]
    if len(matching) != 1:
        raise PipelineError("cleanup intent is not followed by one matching merged event")

    proof_snapshot = dict(snapshot)
    proof_snapshot["headRefOid"] = intent["head"]
    established = proof(epoch(proof_snapshot, config["trusted_account"]),
                        intent["head"], config["trusted_account"])
    if not established or digest(established) != intent["proof_sha256"]:
        raise PipelineError("review proof no longer matches merge cleanup intent")
    return pr, merge_sha


def recover_merge_cleanup(gh: GitHub, config: dict, intent: dict) -> dict:
    ensure_identity(gh, config)
    # REST can report a successful merge a fraction earlier than the GraphQL
    # timeline exposes its MergedEvent. Retry only that transient observation;
    # every actual invariant violation remains fail-closed on the first read.
    for attempt in range(3):
        try:
            pr, merge_sha = validate_merged_intent(gh, config, intent)
            break
        except PipelineError as exc:
            transient = (
                "matching merged event" in str(exc)
                or "merged GraphQL snapshot" in str(exc)
            )
            if not transient or attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    removed = []
    for issue in intent["issues"]:
        if remove_in_work_from_closed_issue(gh, config, issue):
            removed.append(issue)
    plan_ready = finish_plan_handoff(gh, config, intent["number"], pr.get("body") or "")

    pr_issue_path = f"repos/{config['repository']}/issues/{intent['number']}"
    pr_issue = gh.json("api", pr_issue_path)
    if "ship" in {item.get("name") for item in pr_issue.get("labels", [])}:
        remove_label(gh, config, intent["number"], "ship")
        confirmed = gh.json("api", pr_issue_path)
        if "ship" in {item.get("name") for item in confirmed.get("labels", [])}:
            raise PipelineError("ship removal was not confirmed after merge")

    done_body = (f"{MERGE_DONE_MESSAGE}\n"
                 f"<!-- pp:merge-cleanup-done intent={intent['id']} "
                 f"head={intent['head']} merge={merge_sha} -->")
    raw = gh.run("api", "--paginate",
                 f"repos/{config['repository']}/issues/{intent['number']}/comments?per_page=100",
                 "--jq", ".[]")
    pr_comments = [json.loads(line) for line in raw.splitlines() if line.strip()]
    done_exists = any(
        (item.get("user") or {}).get("login") == config["trusted_account"]
        and item.get("created_at") == item.get("updated_at")
        and (item.get("body") or "").strip() == done_body
        for item in pr_comments
    )
    if not done_exists:
        post_comment(gh, config, intent["number"], done_body)
    return {"action": "completed", "stage": "merge-cleanup", "number": intent["number"],
            "head": intent["head"], "merge_sha": merge_sha,
            "in_work_removed": removed, "plan_ready": plan_ready}


def ensure_identity(gh: GitHub, config: dict) -> None:
    identity = gh.json("api", "user")
    if identity.get("login") != config["trusted_account"]:
        raise PipelineError(f"authenticated as {identity.get('login')}, expected {config['trusted_account']}")


def capabilities(config: dict) -> dict:
    return {"protocol": "promptpilot-pipelinectl-v1", "repository": config["repository"],
            "stages": {"review": "content-or-integration",
                       "merge": "clean-ordinary-with-cleanup-recovery"},
            "fallback": "repository skill"}


def review_empty_reason(health: dict) -> str:
    owner = health.get("integration_owner")
    if isinstance(owner, dict) and owner.get("number"):
        return (f"содержательная очередь пуста; интеграционный владелец "
                f"#{owner['number']} находится на этапе {owner.get('stage', 'unknown')}")
    waiting = health.get("reviewed_waiting_ship") or []
    if waiting:
        numbers = ", ".join(f"#{item.get('number')}" for item in waiting[:5])
        return f"содержательная очередь пуста; ждут решения ship: {numbers}"
    return str(health.get("summary") or "содержательная очередь ревью пуста")


def next_review(gh: GitHub, config: dict) -> dict:
    health = run_health(config)
    if health.get("state") == "red":
        return {"action": "fallback", "reason": "health check is red"}
    candidates = health.get("review_candidates") or []
    if not candidates:
        return {"action": "empty", "verdict": "ПУСТО", "reason": review_empty_reason(health)}
    item = candidates[0]
    if item.get("stage") != "review":
        return {"action": "fallback", "reason": "integration/base-sync state requires the full skill"}
    if int(item.get("review_depth", 0)) >= 2:
        return {"action": "fallback", "reason": "third review round requires human-escalation rules"}
    snapshot = stable_timeline(gh, config, int(item["number"]))
    validate_common(snapshot, config, item)
    labels = set(snapshot["labels"])
    if labels & {"hold", "changes-requested", "needs-decision"}:
        return {"action": "fallback", "reason": "routing labels require the full skill"}
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    for _index, _edge, node in comments(info, config["trusted_account"]):
        body = (node.get("body") or "").strip()
        if REVIEW.search(body) or CLAIM.fullmatch(body) or COMPLETE.fullmatch(body) or BASE_SYNC.search(body):
            return {"action": "fallback", "reason": "current epoch contains recovery/protocol state"}
    lease = {"version": 1, "stage": "review", "repository": config["repository"],
             "number": item["number"], "head": snapshot["headRefOid"],
             "snapshot": content_review_digest(snapshot), "epoch": info["hash"],
             "anchor": info["anchor_id"], "depth": int(item.get("review_depth", 0))}
    return {"action": "audit", "target": item, "lease": encode_lease(lease),
            "inspect": [f"gh pr view {item['number']} --repo {config['repository']} --json title,body,headRefName,files,statusCheckRollup",
                        f"gh pr diff {item['number']} --repo {config['repository']}"],
            "complete": "write report JSON, then run the same command with: complete review --lease <lease> --report <file>",
            "report_schema": {"change": "string", "checks": ["string"], "blocking": ["string"],
                              "tail": [{"kind": "issue|discard", "text": "string", "title": "required for issue"}],
                              "human": "optional string"}}


def format_review(lease: dict, report: dict) -> tuple[str, str]:
    for key in ("change", "checks", "blocking", "tail"):
        if key not in report:
            raise PipelineError(f"report missing {key}")
    if not isinstance(report["change"], str) or not isinstance(report["checks"], list) or not isinstance(report["blocking"], list) or not isinstance(report["tail"], list):
        raise PipelineError("invalid report field types")
    if not report["checks"] or not all(isinstance(value, str) and value.strip() for value in report["checks"]):
        raise PipelineError("report must list the checks actually performed")
    clean = lambda value: str(value).replace("<!--", "< !--").replace("pp:", "pp :").strip()
    blocking = [clean(value) for value in report["blocking"] if clean(value)]
    outcome = "changes-requested" if blocking else "reviewed"
    tail_lines = []
    issue_count = 0
    for item in report["tail"][:10]:
        kind, value = item.get("kind"), clean(item.get("text", ""))
        if not value or kind not in ("issue", "discard"):
            raise PipelineError("tail entries require kind issue|discard and text")
        if kind == "issue":
            title = clean(item.get("title", ""))
            if not title or issue_count >= 3:
                raise PipelineError("issue tail requires title and at most three issue entries")
            issue_count += 1
            tail_lines.append(f"{len(tail_lines)+1}. [заявка] {value} → заголовок: «{title}»")
        else:
            tail_lines.append(f"{len(tail_lines)+1}. [выброс] {value}")
    body = [f"**Ревью.** (круг {lease['depth'] + 1})", f"Reviewed-SHA: {lease['head']}",
            f"Outcome-Label: {outcome}", f"Что меняется: {clean(report['change'])}.",
            "Проверено: " + ("; ".join(clean(value) for value in report["checks"]) or "проверки не запускались") + ".",
            "Блокирующее: " + ("; ".join(f"{i+1}) {value}" for i, value in enumerate(blocking)) or "нет") + ".",
            "Хвост:", *(tail_lines or ["—"]),
            "Вердикт: " + ("есть замечания." if blocking else "годится к мержу.")]
    if report.get("human"):
        body.append(f"Человеку: {clean(report['human'])}.")
    body.append(f"<!-- pp:review pp:tail={issue_count} -->")
    rendered = "\n".join(body)
    try:
        repaired = rendered.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        repaired = rendered
    if repaired != rendered and ("вЂ" in rendered or "В«" in rendered or "В»" in rendered
                                 or rendered.count("Р") + rendered.count("С") >= 3):
        raise PipelineError("review report appears to contain UTF-8/Windows-1251 mojibake")
    return rendered, outcome


def content_review_allowed(health: dict, number: int) -> bool:
    """Keep an ordinary lease valid across unrelated integration-lane moves."""
    candidates = health.get("content_review_candidates")
    if not isinstance(candidates, list):
        candidates = health.get("review_candidates") or []
    return any(int(item.get("number", 0)) == int(number)
               and item.get("stage") == "review" for item in candidates)


def complete_review(gh: GitHub, config: dict, lease_value: str, report_path: str) -> dict:
    lease = decode_lease(lease_value)
    if lease.get("stage") != "review" or lease.get("repository") != config["repository"]:
        raise PipelineError("lease belongs to another stage or repository")
    report = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
    body, outcome = format_review(lease, report)
    ensure_identity(gh, config)
    health = run_health(config)
    if health.get("state") == "red":
        raise PipelineError("health check became red")
    # Integration work may become the global single-flight owner while an
    # ordinary content audit is running. That unrelated lane transition must
    # not invalidate a lease whose own HEAD/timeline is still unchanged.
    if not content_review_allowed(health, int(lease["number"])):
        raise PipelineError("content REVIEW target left the allowlist; rerun next review")
    snapshot = stable_timeline(gh, config, int(lease["number"]))
    validate_common(snapshot, config, lease)
    if content_review_digest(snapshot) != lease["snapshot"]:
        raise PipelineError("lease is stale; rerun next review")
    info = review_gate(snapshot, config, lease)
    review = post_comment(gh, config, lease["number"], body)

    snapshot = stable_timeline(gh, config, lease["number"])
    info = review_gate(snapshot, config, lease)
    if any(CLAIM.fullmatch((node.get("body") or "").strip())
           for _index, _edge, node in comments(info, config["trusted_account"])):
        raise PipelineError("another review claim appeared before claim publication")
    review_id = int(review["id"])
    claim_body = (f"{CLAIM_MESSAGE}\n<!-- pp:review-claim {lease['head']} "
                  f"review-comment={review_id} epoch-sha256={lease['epoch']} -->")
    claim = post_comment(gh, config, lease["number"], claim_body)

    snapshot = stable_timeline(gh, config, lease["number"])
    info = review_gate(snapshot, config, lease)
    claims = []
    for index, _edge, node in comments(info, config["trusted_account"]):
        match = CLAIM.fullmatch((node.get("body") or "").strip())
        if match and match.group(1) == lease["head"] and match.group(3) == lease["epoch"]:
            claims.append((index, comment_id(node), int(match.group(2))))
    winner = min(claims, key=lambda item: (item[0], item[1] or 0)) if claims else None
    if not winner or winner[1] != int(claim["id"]) or winner[2] != review_id:
        raise PipelineError("another review claim won; diagnostic comment was left without labels")
    add_label(gh, config, lease["number"], outcome)

    snapshot = stable_timeline(gh, config, lease["number"])
    info = review_gate(snapshot, config, lease, require_outcome=outcome)
    completion_body = (f"{COMPLETE_MESSAGE}\n"
                       f"<!-- pp:head-reviewed {lease['head']} review-comment={review_id} "
                       f"claim={int(claim['id'])} epoch-sha256={lease['epoch']} -->")
    completion = post_comment(gh, config, lease["number"], completion_body)
    final = stable_timeline(gh, config, lease["number"])
    final_info = epoch(final, config["trusted_account"])
    established = proof(final_info, lease["head"], config["trusted_account"])
    if not established or established["completion_id"] != int(completion["id"]):
        raise PipelineError("completion was posted but canonical proof was not established")
    return {"action": "completed", "stage": "review", "number": lease["number"],
            "head": lease["head"], "outcome": outcome, "review_comment": review_id,
            "claim": int(claim["id"]), "completion": int(completion["id"])}


def list_ship(gh: GitHub, config: dict) -> list[dict]:
    raw = gh.run("api", "--paginate", f"repos/{config['repository']}/pulls?state=open&per_page=100", "--jq", ".[]")
    values = [json.loads(line) for line in raw.splitlines() if line.strip()]
    result = []
    for item in values:
        labels = {label["name"] for label in item.get("labels", [])}
        if "ship" in labels and not labels & {"hold", "needs-decision"} and item.get("base", {}).get("ref") == config["base_branch"]:
            result.append(item)
    return sorted(result, key=lambda value: (queue_priority(value, config), value["number"]))


def pr_checks(gh: GitHub, config: dict, number: int) -> tuple[dict, list[dict]]:
    value = gh.json("pr", "view", str(number), "--repo", config["repository"], "--json",
                    "mergeStateStatus,mergeable,statusCheckRollup,body")
    checks = value.get("statusCheckRollup") or []
    return value, checks


def checks_ready(config: dict, checks: list[dict]) -> tuple[bool, str]:
    required = set(config.get("required_checks") or [])
    states = {}
    for item in checks:
        name = item.get("name") or item.get("context") or ""
        state = (item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
        states[name] = state
    if required:
        missing = sorted(required - states.keys())
        if missing:
            return False, "required checks missing: " + ", ".join(missing)
        relevant = {name: states[name] for name in required}
    else:
        if not states and not config.get("allow_no_checks", False):
            return False, "no checks reported"
        relevant = states
    bad = {name: state for name, state in relevant.items() if state not in {"SUCCESS", "NEUTRAL", "SKIPPED"}}
    return (not bad, "checks are green" if not bad else "checks not green: " + ", ".join(f"{k}={v}" for k, v in bad.items()))


def intent_body(head: str, established: dict, body: str, issues: list[int]) -> str:
    issues_value = ",".join(str(value) for value in issues) or "none"
    return (f"{MERGE_INTENT_MESSAGE}\n"
            f"<!-- pp:merge-cleanup-intent head={head} proof-sha256={digest(established)} "
            f"body-sha256={hashlib.sha256(body.encode('utf-8')).hexdigest()} "
            f"issues={issues_value} -->")


def pending_merge_action(gh: GitHub, config: dict, intent: dict) -> dict:
    pr = gh.json("api", f"repos/{config['repository']}/pulls/{intent['number']}")
    if pr.get("merged") is True:
        lease = {"version": 1, "stage": "merge-cleanup", "repository": config["repository"],
                 "intent": intent}
        return {"action": "cleanup", "target": {"number": intent["number"], "head": intent["head"]},
                "lease": encode_lease(lease),
                "complete": "run the same command with: complete merge-cleanup --lease <lease>"}
    if (pr.get("state") != "open" or (pr.get("head") or {}).get("sha") != intent["head"] or
            (pr.get("base") or {}).get("ref") != config["base_branch"]):
        return {"action": "fallback", "reason": "merge cleanup intent target changed ambiguously"}

    snapshot = stable_timeline(gh, config, intent["number"])
    validate_common(snapshot, config, intent)
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    established = proof(info, intent["head"], config["trusted_account"])
    if not established or digest(established) != intent["proof_sha256"]:
        return {"action": "fallback", "reason": "merge cleanup intent review proof is stale"}
    if not trusted_ship_authorized(info, config["trusted_account"]):
        return {"action": "fallback", "reason": "merge cleanup intent lost trusted ship"}
    status, checks = pr_checks(gh, config, intent["number"])
    body = status.get("body") or ""
    if (hashlib.sha256(body.encode("utf-8")).hexdigest() != intent["body_sha256"] or
            same_repo_closing_issues(body, config["repository"]) != intent["issues"]):
        return {"action": "fallback", "reason": "merge cleanup payload changed"}
    ready, reason = checks_ready(config, checks)
    if status.get("mergeStateStatus") != "CLEAN" or status.get("mergeable") != "MERGEABLE" or not ready:
        return {"action": "wait", "reason": reason, "number": intent["number"]}
    lease = {"version": 1, "stage": "merge", "repository": config["repository"],
             "number": intent["number"], "head": intent["head"],
             "snapshot": digest(snapshot), "proof": established, "intent": intent}
    return {"action": "merge", "target": {"number": intent["number"], "head": intent["head"]},
            "lease": encode_lease(lease),
            "complete": "run the same command with: complete merge --lease <lease>"}


def next_merge(gh: GitHub, config: dict) -> dict:
    pending = pending_merge_intents(gh, config)
    if pending:
        return pending_merge_action(gh, config, pending[0])
    health = run_health(config)
    if health.get("state") == "red":
        return {"action": "fallback", "reason": "health check is red"}
    if any(item.get("code") == "single_flight_barrier" for item in health.get("findings", [])):
        return {"action": "fallback", "reason": "single-flight/base-sync owner requires the full skill"}
    queue = list_ship(gh, config)
    if not queue:
        return {"action": "empty", "verdict": "ПУСТО", "reason": "merge queue is empty"}
    item = queue[0]
    snapshot = stable_timeline(gh, config, item["number"])
    validate_common(snapshot, config, {"head": item["head"]["sha"]})
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    if any(BASE_SYNC.search(node.get("body") or "") for _index, _edge, node in comments(info, config["trusted_account"])):
        return {"action": "fallback", "reason": "base-sync lineage requires the full skill"}
    established = proof(info, snapshot["headRefOid"], config["trusted_account"])
    if not established:
        return {"action": "fallback", "reason": "ordinary canonical review proof not found"}
    if not trusted_ship_authorized(info, config["trusted_account"]):
        return {"action": "fallback", "reason": "ship authorization is not a trusted current-HEAD event"}
    status, checks = pr_checks(gh, config, item["number"])
    if status.get("mergeStateStatus") != "CLEAN" or status.get("mergeable") != "MERGEABLE":
        return {"action": "fallback", "reason": f"merge state {status.get('mergeStateStatus')}/{status.get('mergeable')} requires the full skill"}
    ready, reason = checks_ready(config, checks)
    if not ready:
        return {"action": "wait", "reason": reason, "number": item["number"]}
    lease = {"version": 1, "stage": "merge", "repository": config["repository"],
             "number": item["number"], "head": snapshot["headRefOid"],
             "snapshot": digest(snapshot), "proof": established}
    return {"action": "merge", "target": {"number": item["number"], "title": item["title"], "head": snapshot["headRefOid"]},
            "lease": encode_lease(lease), "complete": "run the same command with: complete merge --lease <lease>"}


def complete_merge(gh: GitHub, config: dict, lease_value: str) -> dict:
    lease = decode_lease(lease_value)
    if lease.get("stage") != "merge" or lease.get("repository") != config["repository"]:
        raise PipelineError("lease belongs to another stage or repository")
    ensure_identity(gh, config)
    health = run_health(config)
    if health.get("state") == "red":
        raise PipelineError("health check became red")
    if any(item.get("code") == "single_flight_barrier" for item in health.get("findings", [])):
        raise PipelineError("single-flight owner appeared; rerun next merge")
    snapshot = stable_timeline(gh, config, lease["number"])
    validate_common(snapshot, config, lease)
    if digest(snapshot) != lease["snapshot"]:
        raise PipelineError("merge lease is stale; rerun next merge")
    labels = set(snapshot["labels"])
    if "ship" not in labels or labels & {"hold", "needs-decision"}:
        raise PipelineError("merge label gate closed")
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    established = proof(info, lease["head"], config["trusted_account"])
    if not established or established != lease["proof"]:
        raise PipelineError("review proof changed")
    status, checks = pr_checks(gh, config, lease["number"])
    ready, reason = checks_ready(config, checks)
    if status.get("mergeStateStatus") != "CLEAN" or status.get("mergeable") != "MERGEABLE" or not ready:
        raise PipelineError(f"merge is no longer ready: {reason}")
    body = status.get("body") or ""
    intent = lease.get("intent")
    if intent is None:
        issues = same_repo_closing_issues(body, config["repository"])
        marker = intent_body(lease["head"], established, body, issues)
        posted = post_comment(gh, config, lease["number"], marker)
        all_pending = pending_merge_intents(gh, config)
        candidates = [item for item in all_pending
                      if item["number"] == lease["number"] and item["head"] == lease["head"]]
        if (not candidates or candidates[0]["id"] != int(posted["id"]) or
                not all_pending or all_pending[0]["id"] != int(posted["id"])):
            return {"action": "wait", "stage": "merge", "number": lease["number"],
                    "reason": "another merge cleanup intent won"}
        intent = candidates[0]
    elif int(intent.get("number", 0)) != int(lease["number"]):
        raise PipelineError("merge intent belongs to another PR")

    snapshot = stable_timeline(gh, config, lease["number"])
    validate_common(snapshot, config, lease)
    labels = set(snapshot["labels"])
    if "ship" not in labels or labels & {"hold", "needs-decision"}:
        raise PipelineError("merge label gate closed after cleanup intent")
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    current_proof = proof(info, lease["head"], config["trusted_account"])
    if not current_proof or current_proof != lease["proof"] or digest(current_proof) != intent["proof_sha256"]:
        raise PipelineError("review proof changed after cleanup intent")
    if not trusted_ship_authorized(info, config["trusted_account"]):
        raise PipelineError("trusted ship changed after cleanup intent")
    status, checks = pr_checks(gh, config, lease["number"])
    ready, reason = checks_ready(config, checks)
    if status.get("mergeStateStatus") != "CLEAN" or status.get("mergeable") != "MERGEABLE" or not ready:
        raise PipelineError(f"merge is no longer ready after cleanup intent: {reason}")
    current_body = status.get("body") or ""
    if (hashlib.sha256(current_body.encode("utf-8")).hexdigest() != intent["body_sha256"] or
            same_repo_closing_issues(current_body, config["repository"]) != intent["issues"]):
        raise PipelineError("cleanup payload changed after intent")

    result = gh.json("api", f"repos/{config['repository']}/pulls/{lease['number']}/merge",
                     "--method", "PUT", "--input", "-",
                     input_value={"merge_method": config["merge_method"], "sha": lease["head"]})
    if result.get("merged") is not True:
        raise PipelineError(result.get("message") or "GitHub did not confirm merge")
    return recover_merge_cleanup(gh, config, intent)


def run(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(prog="pipelinectl")
    parser.add_argument("--config", default="pipelinectl.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    next_parser = sub.add_parser("next")
    next_parser.add_argument("stage", choices=("review", "merge"))
    complete_parser = sub.add_parser("complete")
    complete_parser.add_argument("stage", choices=("review", "merge", "merge-cleanup"))
    complete_parser.add_argument("--lease", required=True)
    complete_parser.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "capabilities":
            value = capabilities(config)
        else:
            gh = GitHub()
            if args.command == "next":
                value = next_review(gh, config) if args.stage == "review" else next_merge(gh, config)
            elif args.stage == "review":
                if not args.report:
                    raise PipelineError("complete review requires --report")
                value = complete_review(gh, config, args.lease, args.report)
            elif args.stage == "merge":
                value = complete_merge(gh, config, args.lease)
            else:
                lease = decode_lease(args.lease)
                if (lease.get("stage") != "merge-cleanup" or
                        lease.get("repository") != config["repository"] or
                        not isinstance(lease.get("intent"), dict)):
                    raise PipelineError("lease belongs to another stage or repository")
                value = recover_merge_cleanup(gh, config, lease["intent"])
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (PipelineError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"action": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
