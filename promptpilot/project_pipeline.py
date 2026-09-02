"""Deterministic, executor-neutral helper for GitHub maintenance pipelines.

The helper intentionally supports only the ordinary REVIEW transaction and an
already-clean ordinary MERGE. Complicated base-sync/carry/recovery states return
``fallback`` so the repository's full skill remains the authority for them.
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
from pathlib import Path


REVIEW = re.compile(r"(?m)^Reviewed-SHA: ([0-9a-f]{40})$.*^Outcome-Label: (reviewed|changes-requested|needs-decision)$.*^<!-- pp:review pp:tail=([0-9]+) -->$", re.S)
CLAIM = re.compile(r"^<!-- pp:review-claim ([0-9a-f]{40}) review-comment=([0-9]+) epoch-sha256=([0-9a-f]{64}) -->$")
COMPLETE = re.compile(r"^<!-- pp:head-reviewed ([0-9a-f]{40}) review-comment=([0-9]+) claim=([0-9]+) epoch-sha256=([0-9a-f]{64}) -->$")
OVERRIDE = re.compile(r"(?m)^pp:review-again$")
BASE_SYNC = re.compile(r"(?m)^<!-- pp:base-sync-(?:intent|done) ")
LINKED = re.compile(r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#([0-9]+)")

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


def run_health(config: dict) -> dict:
    command = [str(value) for value in config["health_command"]]
    if command and command[0] in {"go", "go.exe"} and shutil.which(command[0]) is None:
        standard = Path(r"C:\Program Files\Go\bin\go.exe")
        if standard.exists():
            command[0] = str(standard)
    env = os.environ.copy()
    if env.get("PP_GH_EXE"):
        env.setdefault("GH_EXE", env["PP_GH_EXE"])
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
    if labels & {"hold", "ship", "needs-decision"}:
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


def ensure_identity(gh: GitHub, config: dict) -> None:
    identity = gh.json("api", "user")
    if identity.get("login") != config["trusted_account"]:
        raise PipelineError(f"authenticated as {identity.get('login')}, expected {config['trusted_account']}")


def capabilities(config: dict) -> dict:
    return {"protocol": "promptpilot-pipelinectl-v1", "repository": config["repository"],
            "stages": {"review": "ordinary", "merge": "clean-ordinary"},
            "fallback": "repository skill"}


def next_review(gh: GitHub, config: dict) -> dict:
    health = run_health(config)
    if health.get("state") == "red":
        return {"action": "fallback", "reason": "health check is red"}
    candidates = health.get("review_candidates") or []
    if not candidates:
        return {"action": "empty", "verdict": "ПУСТО", "reason": "review queue is empty"}
    item = candidates[0]
    if item.get("stage") != "review" or any(f.get("code") == "single_flight_barrier" for f in health.get("findings", [])):
        return {"action": "fallback", "reason": "integration/base-sync state requires the full skill"}
    if int(item.get("review_depth", 0)) >= 2:
        return {"action": "fallback", "reason": "third review round requires human-escalation rules"}
    snapshot = stable_timeline(gh, config, int(item["number"]))
    validate_common(snapshot, config, item)
    labels = set(snapshot["labels"])
    if labels & {"hold", "ship", "changes-requested", "needs-decision"}:
        return {"action": "fallback", "reason": "routing labels require the full skill"}
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    for _index, _edge, node in comments(info, config["trusted_account"]):
        body = (node.get("body") or "").strip()
        if REVIEW.search(body) or CLAIM.fullmatch(body) or COMPLETE.fullmatch(body) or BASE_SYNC.search(body):
            return {"action": "fallback", "reason": "current epoch contains recovery/protocol state"}
    lease = {"version": 1, "stage": "review", "repository": config["repository"],
             "number": item["number"], "head": snapshot["headRefOid"],
             "snapshot": digest(snapshot), "epoch": info["hash"],
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
    candidates = health.get("review_candidates") or []
    if (not candidates or int(candidates[0].get("number", 0)) != int(lease["number"])
            or candidates[0].get("stage") != "review"
            or any(item.get("code") == "single_flight_barrier" for item in health.get("findings", []))):
        raise PipelineError("global REVIEW allowlist changed; rerun next review")
    snapshot = stable_timeline(gh, config, int(lease["number"]))
    validate_common(snapshot, config, lease)
    if digest(snapshot) != lease["snapshot"]:
        raise PipelineError("lease is stale; rerun next review")
    info = review_gate(snapshot, config, lease)
    review = post_comment(gh, config, lease["number"], body)

    snapshot = stable_timeline(gh, config, lease["number"])
    info = review_gate(snapshot, config, lease)
    if any(CLAIM.fullmatch((node.get("body") or "").strip())
           for _index, _edge, node in comments(info, config["trusted_account"])):
        raise PipelineError("another review claim appeared before claim publication")
    review_id = int(review["id"])
    claim_body = f"<!-- pp:review-claim {lease['head']} review-comment={review_id} epoch-sha256={lease['epoch']} -->"
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
    completion_body = (f"<!-- pp:head-reviewed {lease['head']} review-comment={review_id} "
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
    return sorted(result, key=lambda value: value["number"])


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


def next_merge(gh: GitHub, config: dict) -> dict:
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
    ship_events = []
    for index, edge in enumerate(info["edges"]):
        node = edge.get("node") or {}
        if node.get("__typename") in {"LabeledEvent", "UnlabeledEvent"} and (node.get("label") or {}).get("name") == "ship":
            ship_events.append((index, node))
    if not ship_events or ship_events[-1][1].get("__typename") != "LabeledEvent" or (ship_events[-1][1].get("actor") or {}).get("login") != config["trusted_account"] or ship_events[-1][0] <= established["completion_index"]:
        return {"action": "fallback", "reason": "ship authorization is not an ordinary trusted post-review event"}
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
    result = gh.json("api", f"repos/{config['repository']}/pulls/{lease['number']}/merge",
                     "--method", "PUT", "--input", "-",
                     input_value={"merge_method": config["merge_method"], "sha": lease["head"]})
    if result.get("merged") is not True:
        raise PipelineError(result.get("message") or "GitHub did not confirm merge")
    removed = []
    for issue in sorted({int(value) for value in LINKED.findall(status.get("body") or "")}):
        output = gh.run("api", "--method", "DELETE",
                        f"repos/{config['repository']}/issues/{issue}/labels/in-work", allow=(0, 1))
        if output is not None:
            removed.append(issue)
    return {"action": "completed", "stage": "merge", "number": lease["number"],
            "head": lease["head"], "merge_sha": result.get("sha"), "in_work_checked": removed}


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
    complete_parser.add_argument("stage", choices=("review", "merge"))
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
            else:
                value = complete_merge(gh, config, args.lease)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (PipelineError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"action": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
