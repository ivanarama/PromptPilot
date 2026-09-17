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
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .pipeline_errors import PipelineError

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
MERGE_COMMENT_INDEX_VERSION = 1
MERGE_COMMENT_SCAN_OVERLAP_SECONDS = 300

TIMELINE_QUERY = r"""
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  headRefOid baseRefOid baseRefName state isDraft
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


def pipeline_lease_key(*, create: bool) -> bytes:
    configured = os.environ.get("PP_PIPELINE_LEASE_KEY_FILE")
    data_dir = Path(os.environ.get("PP_DATA_DIR", Path.home() / ".promptpilot"))
    path = Path(configured) if configured else data_dir / "pipeline-lease.key"
    try:
        value = path.read_bytes()
    except FileNotFoundError:
        value = None
    if value is not None:
        if len(value) != 32:
            raise PipelineError("pipeline lease signing key is invalid")
        return value
    if not create:
        raise PipelineError("pipeline lease signing key is missing")

    # Publish only a fully written key.  O_EXCL on the final path exposes a
    # zero-length file between create and write; a same-time reader can then
    # fail, and a crash leaves that invalid file permanently.  A hard link from
    # a flushed sibling temp file is an atomic create-if-absent on NTFS and
    # normal POSIX filesystems, so concurrent first use converges on one key.
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_bytes(32)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(candidate)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    try:
        value = path.read_bytes()
    except FileNotFoundError as exc:
        raise PipelineError("pipeline lease signing key was not published") from exc
    if len(value) != 32:
        raise PipelineError("pipeline lease signing key is invalid")
    return value


def encode_signed_lease(value: dict) -> str:
    payload = encode_lease(value)
    signature = hmac.new(
        pipeline_lease_key(create=True), payload.encode("ascii"), hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload}.{encoded_signature}"


def decode_signed_lease(value: str) -> dict:
    try:
        payload, encoded_signature = value.split(".")
        signature = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4)
        )
    except (ValueError, TypeError) as exc:
        raise PipelineError(f"invalid signed lease: {exc}") from exc
    expected = hmac.new(
        pipeline_lease_key(create=False), payload.encode("ascii"), hashlib.sha256,
    ).digest()
    if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(signature, expected):
        raise PipelineError("invalid pipeline lease signature")
    return decode_lease(payload)


def validate_review_lease(lease: dict, config: dict) -> None:
    if lease.get("stage") != "review" or lease.get("repository") != config["repository"]:
        raise PipelineError("lease belongs to another stage or repository")
    if (not isinstance(lease.get("number"), int) or isinstance(lease.get("number"), bool)
            or lease["number"] <= 0):
        raise PipelineError("review lease has an invalid PR number")
    if not isinstance(lease.get("head"), str) or not re.fullmatch(r"[0-9a-f]{40}", lease["head"]):
        raise PipelineError("review lease has an invalid HEAD")
    for field in ("snapshot", "epoch"):
        if not isinstance(lease.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", lease[field]):
            raise PipelineError(f"review lease has an invalid {field}")
    if not isinstance(lease.get("anchor"), str) or not lease["anchor"]:
        raise PipelineError("review lease has an invalid epoch anchor")
    if (not isinstance(lease.get("depth"), int) or isinstance(lease.get("depth"), bool)
            or lease["depth"] not in (0, 1)):
        raise PipelineError("review lease depth requires human escalation")
    completion_gate = lease.get("completion_gate", "health")
    if completion_gate not in {"health", "target-v1"}:
        raise PipelineError("review lease has an unsupported completion gate")
    if completion_gate == "target-v1":
        issued_at, expires_at = lease.get("issued_at"), lease.get("expires_at")
        nonce = lease.get("nonce")
        if (not isinstance(issued_at, int) or isinstance(issued_at, bool) or
                not isinstance(expires_at, int) or isinstance(expires_at, bool)):
            raise PipelineError("signed review lease has an invalid validity window")
        now = int(time.time())
        ttl = int(config.get("review_lease_seconds", 7200))
        if issued_at > now + 60 or expires_at <= now or expires_at <= issued_at:
            raise PipelineError("signed review lease is expired or not yet valid")
        if expires_at - issued_at > ttl:
            raise PipelineError("signed review lease validity exceeds configured limit")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise PipelineError("signed review lease has an invalid nonce")
    _validate_lease_reservation(lease, config)


class GitHub:
    def __init__(self, executable: str | None = None, *, timeout_seconds: int = 120):
        self.executable = executable or os.environ.get("GH_EXE") or os.environ.get("PP_GH_EXE")
        self.executable = self.executable or shutil.which("gh") or shutil.which("gh.exe")
        if not self.executable:
            standard = Path(r"C:\Program Files\GitHub CLI\gh.exe")
            if standard.exists():
                self.executable = str(standard)
        if not self.executable:
            raise PipelineError("GitHub CLI not found")
        self.timeout_seconds = timeout_seconds

    def run(self, *args: str, input_value=None, allow=(0,),
            timeout_seconds: int | None = None) -> str:
        data = None
        if input_value is not None:
            data = json.dumps(input_value, ensure_ascii=False)
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            result = subprocess.run(
                [self.executable, *args], input=data, capture_output=True, text=True,
                encoding="utf-8", errors="strict", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            operation = args[0] if args else "command"
            raise PipelineError(
                f"GitHub CLI {operation} timed out after {timeout}s") from exc
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
    data.setdefault("review_completion_gate", "health")
    data.setdefault("review_lease_seconds", 7200)
    data.setdefault("target_reservation_ttl_seconds", data["review_lease_seconds"])
    data.setdefault("fallback_handoff", "legacy")
    if not isinstance(data["fallback_handoff"], str) or data["fallback_handoff"] not in {"legacy", "target-v1"}:
        raise PipelineError("fallback_handoff must be legacy or target-v1")
    if not isinstance(data.get("sync_base_before_health", False), bool):
        raise PipelineError("sync_base_before_health must be a boolean")
    data.setdefault("base_sync_timeout_seconds", 60)
    if (not isinstance(data["base_sync_timeout_seconds"], int)
            or isinstance(data["base_sync_timeout_seconds"], bool)
            or not 5 <= data["base_sync_timeout_seconds"] <= 300):
        raise PipelineError(
            "base_sync_timeout_seconds must be an integer from 5 to 300")
    timeout_limits = {
        "github_timeout_seconds": (120, 5, 600),
        "health_timeout_seconds": (300, 5, 1800),
        "merge_comment_backfill_timeout_seconds": (900, 60, 3600),
    }
    for key, (default, minimum, maximum) in timeout_limits.items():
        data.setdefault(key, default)
        if (not isinstance(data[key], int) or isinstance(data[key], bool)
                or not minimum <= data[key] <= maximum):
            raise PipelineError(
                f"{key} must be an integer from {minimum} to {maximum}")
    if data["review_completion_gate"] not in {"health", "target-v1"}:
        raise PipelineError("review_completion_gate must be health or target-v1")
    if (not isinstance(data["review_lease_seconds"], int) or
            isinstance(data["review_lease_seconds"], bool) or
            not 300 <= data["review_lease_seconds"] <= 28800):
        raise PipelineError("review_lease_seconds must be an integer from 300 to 28800")
    if (not isinstance(data["target_reservation_ttl_seconds"], int) or
            isinstance(data["target_reservation_ttl_seconds"], bool) or
            not 300 <= data["target_reservation_ttl_seconds"] <= 28800):
        raise PipelineError(
            "target_reservation_ttl_seconds must be an integer from 300 to 28800")
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


def sync_base_before_health(config: dict) -> bool:
    """Fast-forward a clean base checkout before running repository health.

    Repository-owned health tools are versioned with the project. Running one
    from a stale automation checkout can make decisions using an obsolete queue
    contract, so opt-in profiles refresh that checkout and fail closed when it
    cannot be updated safely. Untracked review artifacts are intentionally
    allowed; tracked changes are not. Return whether the opt-in sync ran.
    """
    if not config.get("sync_base_before_health", False):
        return False

    base = str(config.get("base_branch") or "main")
    timeout = int(config.get("base_sync_timeout_seconds", 60))

    def git(*args: str):
        command = ["git", "-c", "maintenance.auto=false", *args]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(
                f"cannot synchronize {base} before health: "
                f"git {args[0]} timed out after {timeout}s") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "git command failed").strip()
            raise PipelineError(f"cannot synchronize {base} before health: {detail}")
        return result.stdout.strip()

    branch = git("branch", "--show-current")
    if branch != base:
        raise PipelineError(
            f"cannot synchronize {base} before health: current branch is {branch or 'detached HEAD'}"
        )
    if git("status", "--porcelain", "--untracked-files=no"):
        raise PipelineError(
            f"cannot synchronize {base} before health: checkout has tracked changes"
        )
    # The health contract only consumes the authoritative base branch. Fetching
    # and pruning every remote branch turns each gate into a repository-wide
    # ref scan, which is needlessly expensive on large/slow worktrees and can
    # make an otherwise healthy queue time out before the checker starts.
    git(
        "fetch", "--no-tags", "origin",
        f"+refs/heads/{base}:refs/remotes/origin/{base}",
    )
    git("merge", "--ff-only", f"origin/{base}")
    return True


def run_health(config: dict, *, config_path: str | None = None) -> dict:
    synced_base = str(config.get("base_branch") or "main")
    synchronized = sync_base_before_health(config)
    if synchronized and config_path is not None:
        refreshed = load_config(config_path)
        refreshed_base = str(refreshed.get("base_branch") or "main")
        if refreshed_base != synced_base:
            raise PipelineError(
                "base branch changed while synchronizing pipeline config; rerun the command"
            )
        # The fast-forward can update pipelinectl.json itself. Keep the same
        # dictionary object so every decision after health (including lease
        # capabilities) observes the just-checked-out project contract.
        config.clear()
        config.update(refreshed)
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
    timeout = int(config.get("health_timeout_seconds", 300))
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="strict", env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(
            f"health command timed out after {timeout}s") from exc
    if result.returncode not in (0, 1):
        raise PipelineError((result.stderr or result.stdout or "health command failed").strip())
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        # ``go run`` returns 1 when the wrapped health binary exits non-zero.
        # That overlaps with the checker's documented health-status exit code,
        # so JSON is still authoritative when present. If the wrapper instead
        # produced no usable JSON, preserve its diagnostic (notably GitHub 403
        # and rate-limit details) rather than hiding it behind a parser error.
        detail = (result.stderr or "").strip()
        if result.returncode and detail:
            raise PipelineError(detail) from exc
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
        current = {key: pr.get(key) for key in (
            "headRefOid", "baseRefOid", "baseRefName", "state", "isDraft",
        )}
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
    if snapshot.get("isDraft") is not False:
        raise PipelineError("draft state is unknown or PR is still a draft")
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


def committed_review_depth(snapshot: dict, trusted: str) -> int:
    """Count unique immutable claim-bound review completions in the full timeline."""
    review_ids = set()
    for edge in snapshot.get("edges", []):
        node = edge.get("node") or {}
        if (node.get("__typename") != "IssueComment" or
                (node.get("author") or {}).get("login") != trusted or
                node.get("lastEditedAt") is not None):
            continue
        match = COMPLETE.fullmatch((node.get("body") or "").strip())
        if match:
            review_ids.add(int(match.group(2)))
    return len(review_ids)


def content_review_target_gate(snapshot: dict, config: dict, lease: dict) -> dict:
    """Prove a leased ordinary REVIEW locally, without rereading other PRs."""
    info = review_gate(snapshot, config, lease)
    labels = set(snapshot["labels"])
    if "changes-requested" in labels:
        raise PipelineError("content REVIEW target entered the FIX route")
    depth = committed_review_depth(snapshot, config["trusted_account"])
    if depth != int(lease.get("depth", -1)) or depth >= 2:
        raise PipelineError("review depth changed or requires human escalation")
    for _index, _edge, node in comments(info, config["trusted_account"]):
        body = (node.get("body") or "").strip()
        if (REVIEW.search(body) or CLAIM.fullmatch(body) or
                COMPLETE.fullmatch(body) or BASE_SYNC.search(body)):
            raise PipelineError("content REVIEW target contains recovery/protocol state")
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


def repository_comments(
        gh: GitHub, config: dict, *, since: str | None = None) -> list[dict]:
    order = "created" if since is None else "updated"
    query = (
        f"repos/{config['repository']}/issues/comments"
        f"?per_page=100&sort={order}&direction=asc"
    )
    if since is not None:
        query += f"&since={quote(since, safe=':-TZ')}"
    arguments = ("api", "--paginate", query, "--jq", ".[]")
    if since is None:
        raw = gh.run(
            *arguments,
            timeout_seconds=int(config.get(
                "merge_comment_backfill_timeout_seconds", 900)),
        )
    else:
        raw = gh.run(*arguments)
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
    return {"id": int(comment["id"]), "intent": int(match.group(1)),
            "head": match.group(2), "merge": match.group(3),
            "number": int(issue_match.group(1))}


def _merge_comment_index_path(config: dict) -> Path:
    configured = os.environ.get("PP_PIPELINE_STATE_DIR")
    if configured:
        directory = Path(configured)
    else:
        data_dir = Path(os.environ.get("PP_DATA_DIR", Path.home() / ".promptpilot"))
        directory = data_dir / "pipelinectl-state"
    identity = canonical({
        "repository": config["repository"],
        "trusted_account": config["trusted_account"],
    })
    name = hashlib.sha256(identity).hexdigest()
    return directory / f"merge-comments-{name}.json"


@contextmanager
def _locked_merge_comment_index(config: dict):
    """Serialize the repository comment mirror without touching scheduler DB.

    The lock deliberately covers the authoritative GitHub read.  That makes
    two local pipelinectl processes converge on one ordered marker stream and,
    more importantly, prevents either one from publishing a second merge
    intent while the other is refreshing the single-flight barrier.
    """
    state_path = _merge_comment_index_path(config)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield state_path
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield state_path
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse_github_timestamp(value, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PipelineError(f"repository comment has invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineError(f"repository comment has invalid {field}") from exc
    if parsed.tzinfo is None:
        raise PipelineError(f"repository comment has invalid {field}")
    return parsed.astimezone(timezone.utc)


def _format_github_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_merge_comment_index(config: dict) -> dict:
    return {
        "version": MERGE_COMMENT_INDEX_VERSION,
        "repository": config["repository"],
        "trusted_account": config["trusted_account"],
        "initialized": False,
        "checkpoint": None,
        "intents": {},
        "done": {},
    }


def _validate_cached_intent(value: dict, key: str) -> None:
    if not isinstance(value, dict) or str(value.get("id")) != key:
        raise PipelineError("merge comment index contains an invalid intent")
    if (type(value.get("id")) is not int or type(value.get("number")) is not int
            or value["id"] <= 0 or value["number"] <= 0
            or not isinstance(value.get("body"), str)):
        raise PipelineError("merge comment index contains an invalid intent")
    match = MERGE_CLEANUP_INTENT.fullmatch(value["body"])
    issues = value.get("issues")
    if (not match or not isinstance(issues, list)
            or any(type(item) is not int or item <= 0 for item in issues)):
        raise PipelineError("merge comment index contains an invalid intent")
    parsed_issues = ([] if match.group(4) == "none"
                     else [int(item) for item in match.group(4).split(",")])
    if (value.get("head") != match.group(1)
            or value.get("proof_sha256") != match.group(2)
            or value.get("body_sha256") != match.group(3)
            or issues != parsed_issues):
        raise PipelineError("merge comment index contains an invalid intent")


def _validate_cached_done(value: dict, key: str) -> None:
    if (not isinstance(value, dict) or str(value.get("id")) != key
            or type(value.get("id")) is not int
            or type(value.get("intent")) is not int
            or type(value.get("number")) is not int
            or value["id"] <= 0 or value["intent"] <= 0 or value["number"] <= 0
            or not isinstance(value.get("head"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["head"])
            or not isinstance(value.get("merge"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["merge"])):
        raise PipelineError("merge comment index contains an invalid completion")


def _load_merge_comment_index(path: Path, config: dict) -> dict:
    if not path.exists():
        return _new_merge_comment_index(config)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"merge comment index is unreadable: {exc}") from exc
    if (not isinstance(value, dict)
            or value.get("version") != MERGE_COMMENT_INDEX_VERSION
            or value.get("repository") != config["repository"]
            or value.get("trusted_account") != config["trusted_account"]
            or type(value.get("initialized")) is not bool
            or not isinstance(value.get("intents"), dict)
            or not isinstance(value.get("done"), dict)):
        raise PipelineError("merge comment index has an invalid identity or schema")
    checkpoint = value.get("checkpoint")
    if checkpoint is not None:
        _parse_github_timestamp(checkpoint, field="checkpoint")
    for key, intent in value["intents"].items():
        _validate_cached_intent(intent, key)
    for key, done in value["done"].items():
        _validate_cached_done(done, key)
    return value


def _write_merge_comment_index(path: Path, value: dict) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _merge_comment_scan_since(state: dict) -> str | None:
    checkpoint = state.get("checkpoint")
    if not state.get("initialized") or checkpoint is None:
        return None
    parsed = _parse_github_timestamp(checkpoint, field="checkpoint")
    return _format_github_timestamp(
        parsed - timedelta(seconds=MERGE_COMMENT_SCAN_OVERLAP_SECONDS))


def _apply_merge_comments(
        state: dict, comments_value: list[dict], config: dict) -> None:
    normalized = []
    for comment in comments_value:
        if not isinstance(comment, dict) or type(comment.get("id")) is not int:
            raise PipelineError("repository comment response is malformed")
        updated = _parse_github_timestamp(comment.get("updated_at"), field="updated_at")
        _parse_github_timestamp(comment.get("created_at"), field="created_at")
        normalized.append((updated, int(comment["id"]), comment))
    # A comment can move between REST pages when it is edited during a scan.
    # Sorting with an explicit scalar key keeps such duplicate IDs harmless;
    # comparing the response dictionaries themselves would raise TypeError
    # when both copies share the same second-resolution timestamp.
    for updated, comment_id, comment in sorted(
            normalized, key=lambda item: (item[0], item[1])):
        key = str(comment_id)
        # An edit is delivered again by the updated-time delta.  Remove the
        # previous interpretation first so an edited service marker can never
        # remain authoritative in the durable mirror.
        state["intents"].pop(key, None)
        state["done"].pop(key, None)
        intent = parse_merge_intent(comment, config)
        done = parse_merge_done(comment, config)
        if intent is not None:
            state["intents"][key] = intent
        if done is not None:
            state["done"][key] = done


def _pending_from_merge_comment_index(state: dict) -> list[dict]:
    completed = {
        (done["intent"], done["number"], done["head"])
        for done in state["done"].values()
    }
    return sorted(
        (intent for intent in state["intents"].values()
         if (intent["id"], intent["number"], intent["head"]) not in completed),
        key=lambda item: item["id"],
    )


def _sync_merge_comment_index(gh: GitHub, config: dict, state: dict) -> None:
    # Capture the watermark before issuing the first page.  A full backfill can
    # take many minutes; advancing to max(updated_at) from its eventual result
    # could skip a marker that appeared early in pagination but was not part of
    # that page chain.  The next scan starts from this time minus the clock-skew
    # overlap, independently of how long the completed scan took.
    scan_started_at = _utc_now()
    since = _merge_comment_scan_since(state)
    comments_value = repository_comments(gh, config, since=since)
    _apply_merge_comments(state, comments_value, config)
    state["checkpoint"] = _format_github_timestamp(scan_started_at)
    state["initialized"] = True


def _record_merge_comment(gh: GitHub, config: dict, comment: dict) -> None:
    """Record a directly returned GitHub marker without advancing scan truth.

    A POST response is canonical for that one comment, but it does not prove
    that the repository listing has exposed every concurrent comment yet.  The
    checkpoint therefore advances only from a completed listing scan.
    """
    with _locked_merge_comment_index(config) as path:
        state = _load_merge_comment_index(path, config)
        _sync_merge_comment_index(gh, config, state)
        _apply_merge_comments(state, [comment], config)
        _write_merge_comment_index(path, state)


def pending_merge_intents(gh: GitHub, config: dict) -> list[dict]:
    with _locked_merge_comment_index(config) as path:
        state = _load_merge_comment_index(path, config)
        _sync_merge_comment_index(gh, config, state)
        _write_merge_comment_index(path, state)
        return _pending_from_merge_comment_index(state)


def reserve_merge_intent(gh: GitHub, config: dict, number: int,
                         marker: str) -> tuple[dict | None, list[dict]]:
    """Publish one intent under the same lock as the canonical delta scan.

    Returning ``None`` means an earlier intent already owns the integration
    lane.  Marker ordering is always the GitHub comment database id, matching
    the former full-history implementation.
    """
    with _locked_merge_comment_index(config) as path:
        state = _load_merge_comment_index(path, config)
        _sync_merge_comment_index(gh, config, state)
        pending = _pending_from_merge_comment_index(state)
        if pending:
            _write_merge_comment_index(path, state)
            return None, pending

        posted = post_comment(gh, config, number, marker)
        intent = parse_merge_intent(posted, config)
        if intent is None or intent["number"] != number:
            raise PipelineError("posted merge cleanup intent is not canonical")
        _apply_merge_comments(state, [posted], config)

        # Refresh once more while still holding the local publisher lock.  The
        # POST itself is inserted directly, while the unchanged checkpoint
        # keeps any not-yet-visible concurrent GitHub marker discoverable on a
        # later invocation instead of assuming eventual listing visibility.
        _sync_merge_comment_index(gh, config, state)
        pending = _pending_from_merge_comment_index(state)
        _write_merge_comment_index(path, state)
        if not pending or pending[0]["id"] != intent["id"]:
            return None, pending
        return intent, pending


def exact_merge_intent_index(snapshot: dict, config: dict, intent: dict) -> int:
    """Locate the immutable GitHub marker in a stable PR timeline."""
    matches = []
    for index, edge in enumerate(snapshot.get("edges") or []):
        node = edge.get("node") or {}
        if (node.get("__typename") == "IssueComment"
                and comment_id(node) == intent.get("id")
                and node.get("body") == intent.get("body")
                and (node.get("author") or {}).get("login") == config["trusted_account"]
                and node.get("lastEditedAt") is None):
            matches.append(index)
    if len(matches) != 1:
        raise PipelineError(
            "merge cleanup intent is missing or edited in GraphQL timeline")
    return matches[0]


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
    intent_index = exact_merge_intent_index(snapshot, config, intent)
    merged_events = []
    forbidden = {"PullRequestCommit", "HeadRefForcePushedEvent", "HeadRefRestoredEvent",
                 "BaseRefChangedEvent", "BaseRefForcePushedEvent", "BaseRefDeletedEvent",
                 "CommentDeletedEvent"}
    for index, edge in enumerate(snapshot["edges"]):
        node = edge.get("node") or {}
        if index > intent_index:
            if node.get("__typename") in forbidden:
                raise PipelineError(f"unsupported event after merge cleanup intent: {node.get('__typename')}")
            if node.get("__typename") == "MergedEvent":
                merged_events.append((index, (node.get("commit") or {}).get("oid")))
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
    done_comment = next((
        item for item in pr_comments
        if (item.get("user") or {}).get("login") == config["trusted_account"]
        and item.get("created_at") == item.get("updated_at")
        and (item.get("body") or "").strip() == done_body
    ), None)
    if done_comment is None:
        done_comment = post_comment(gh, config, intent["number"], done_body)
    _record_merge_comment(gh, config, done_comment)
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
            "review_completion_gate": config.get("review_completion_gate", "health"),
            "fallback_handoff": config.get("fallback_handoff", "legacy"),
            "target_reservations": "sqlite-task-lease-v1",
            "fallback": "repository skill"}


def _configured_replica_count() -> int:
    raw = os.environ.get("PP_PIPELINE_REPLICAS", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise PipelineError("PP_PIPELINE_REPLICAS must be an integer") from exc
    if str(value) != str(raw).strip() or not 1 <= value <= 16:
        raise PipelineError("PP_PIPELINE_REPLICAS must be an integer from 1 to 16")
    return value


def _pipeline_task_id(*, required: bool) -> int | None:
    raw = os.environ.get("PP_TASK_ID")
    if raw is None and not required:
        return None
    try:
        value = int(raw or "")
    except (TypeError, ValueError) as exc:
        raise PipelineError("replicated pipeline election requires PP_TASK_ID") from exc
    if value <= 0 or str(value) != str(raw).strip():
        raise PipelineError("replicated pipeline election requires a positive PP_TASK_ID")
    return value


def _pipeline_task_started_at(*, required: bool) -> str | None:
    raw = os.environ.get("PP_TASK_STARTED_AT")
    if raw is None and not required:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise PipelineError(
            "replicated pipeline election requires PP_TASK_STARTED_AT") from exc
    if parsed.tzinfo is None:
        raise PipelineError(
            "replicated pipeline election requires an aware PP_TASK_STARTED_AT")
    return str(raw).strip()


def _provider_ownership_kind(*, required: bool) -> str | None:
    value = str(os.environ.get("PP_PROVIDER_OWNERSHIP_KIND") or "").strip().lower()
    if not value and not required:
        return None
    if value not in {"headless", "herdr"}:
        raise PipelineError(
            "replicated pipeline election requires "
            "PP_PROVIDER_OWNERSHIP_KIND=headless|herdr")
    return value


def _reservation_identity(value: dict) -> tuple[str, str, int, str, int, str]:
    if not isinstance(value, dict):
        raise PipelineError("pipeline target reservation is missing")
    repository = str(value.get("repository") or "").strip().lower()
    stage = str(value.get("stage") or "").strip().lower()
    number = value.get("number")
    head = str(value.get("head") or "").strip().lower()
    task_id = value.get("task_id")
    token = str(value.get("token") or "")
    if (not repository or "/" not in repository or not stage
            or type(number) is not int or number <= 0
            or not re.fullmatch(r"[0-9a-f]{40}", head)
            or type(task_id) is not int or task_id <= 0
            or not re.fullmatch(r"[0-9a-f]{32}", token)):
        raise PipelineError("pipeline target reservation is invalid")
    return repository, stage, number, head, task_id, token


def _validate_lease_reservation(lease: dict, config: dict) -> dict | None:
    lease_replicas = lease.get("pipeline_replicas", 1)
    if (type(lease_replicas) is not int or not 1 <= lease_replicas <= 16):
        raise PipelineError("pipeline lease has an invalid replica count")
    configured_replicas = _configured_replica_count()
    if configured_replicas > 1 and lease_replicas != configured_replicas:
        raise PipelineError("pipeline lease replica count changed; rerun next review")
    reservation = lease.get("target_reservation")
    if reservation is None:
        if lease_replicas > 1 or configured_replicas > 1:
            raise PipelineError(
                "replicated REVIEW lease has no target reservation")
        return None
    repository, stage, number, head, task_id, _token = _reservation_identity(
        reservation)
    if (repository != str(config["repository"]).lower()
            or stage != str(lease.get("target_stage") or lease.get("stage") or "").lower()
            or number != lease.get("number") or head != lease.get("head")):
        raise PipelineError("pipeline target reservation contradicts its lease")
    current_task_id = _pipeline_task_id(required=True)
    if current_task_id != task_id:
        raise PipelineError("pipeline target reservation belongs to another task")
    current_attempt = _pipeline_task_started_at(required=True)
    if reservation.get("task_started_at") != current_attempt:
        raise PipelineError("pipeline target reservation belongs to another task attempt")
    if reservation.get("ownership_kind") != _provider_ownership_kind(required=True):
        raise PipelineError("pipeline target reservation ownership kind changed")
    return reservation


def renew_lease_target_reservation(lease: dict, config: dict) -> dict | None:
    """Fence the first mutation with the still-live exact target lease."""
    reservation = _validate_lease_reservation(lease, config)
    if reservation is None:
        return None
    # Keep the scheduler DB a lazy dependency. Plain pipelinectl capabilities,
    # health and merge operations historically work without opening it.
    from . import db as scheduler_db

    renewed = scheduler_db.renew_pipeline_target_reservation(
        reservation, int(config.get(
            "target_reservation_ttl_seconds",
            config.get("review_lease_seconds", 7200),
        )))
    if renewed is None:
        raise PipelineError(
            "pipeline target reservation expired or was released; rerun next review")
    return renewed


def _target_key(value: dict) -> tuple[str, int, str]:
    try:
        stage = str(value["stage"]).lower()
        raw_number = value["number"]
        if type(raw_number) is not int:
            raise ValueError("number must be an integer")
        number = raw_number
        head = str(value["head"]).lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("pipeline health returned an invalid review candidate") from exc
    if (not stage or number <= 0 or not re.fullmatch(r"[0-9a-f]{40}", head)):
        raise PipelineError("pipeline health returned an invalid review candidate")
    return stage, number, head


def _review_health_without_targets(health: dict, unavailable: set[tuple]) -> dict:
    filtered = dict(health)
    for field in ("review_candidates", "content_review_candidates"):
        values = health.get(field)
        if isinstance(values, list):
            filtered[field] = [value for value in values
                               if _target_key(value) not in unavailable]
    return filtered


def _elect_review_candidate(config: dict, health: dict,
                            candidates: list[dict]) -> tuple[dict | None, dict | None, dict]:
    """Reserve the first free candidate and preserve queue order atomically."""
    replicas = _configured_replica_count()
    if replicas == 1:
        return candidates[0], None, health
    if (config.get("review_completion_gate") != "target-v1"
            or config.get("fallback_handoff") != "target-v1"):
        raise PipelineError(
            "replicated REVIEW requires review_completion_gate and "
            "fallback_handoff to be target-v1")
    task_id = _pipeline_task_id(required=True)
    task_started_at = _pipeline_task_started_at(required=True)
    ownership_kind = _provider_ownership_kind(required=True)
    from . import db as scheduler_db

    unavailable = set()
    selected = None
    reservation = None
    candidate_keys = [_target_key(candidate) for candidate in candidates]
    own = next((item for item in scheduler_db.list_pipeline_target_reservations(
        repository=config["repository"])
                if int(item["task_id"]) == task_id), None)
    if own is not None:
        own_key = (own["stage"], int(own["number"]), own["head"])
        if own_key not in candidate_keys:
            # One task means one immutable election envelope. A repeated next
            # after HEAD/eligibility changed must not delete the old fence and
            # silently start reviewing a different PR.
            raise PipelineError(
                "existing pipeline target reservation is no longer eligible; "
                "start a new task")
        selected_index = candidate_keys.index(own_key)
        selected = candidates[selected_index]
        unavailable.update(candidate_keys[:selected_index])
        reservation = scheduler_db.reserve_pipeline_target(
            config["repository"], own["stage"], int(own["number"]), own["head"],
            task_id, int(config.get(
                "target_reservation_ttl_seconds",
                config.get("review_lease_seconds", 7200),
            )),
            task_started_at=task_started_at,
            ownership_kind=ownership_kind,
        )
        if reservation is not None:
            return selected, reservation, _review_health_without_targets(
                health, unavailable)
        # The listed lease may have expired and been taken between the read
        # and the renewal transaction. This task already observed an exact
        # target, so it must not silently retarget within the same envelope.
        raise PipelineError(
            "existing pipeline target reservation was lost; start a new task")
    for candidate in candidates:
        target_stage, number, head = _target_key(candidate)
        reservation = scheduler_db.reserve_pipeline_target(
            config["repository"], target_stage, number, head, task_id,
            int(config.get(
                "target_reservation_ttl_seconds",
                config.get("review_lease_seconds", 7200),
            )),
            task_started_at=task_started_at,
            ownership_kind=ownership_kind,
        )
        if reservation is not None:
            selected = candidate
            break
        unavailable.add((target_stage, number, head))
    if selected is None:
        return None, None, health

    # fallback_target's election proof expects its target at the head of the
    # executable queue. Targets skipped solely because another local replica
    # owns them are removed from this immutable health view. The later fallback
    # gate uses the fresh global allowlist in membership mode, not this filter.
    return selected, reservation, _review_health_without_targets(
        health, unavailable)


def fallback_target(config: dict, health: dict, stage: str, target: dict, reason: str,
                    *, reservation: dict | None = None) -> dict:
    if config.get("fallback_handoff") != "target-v1":
        return {"action": "fallback", "reason": reason}
    from .fallback_handoff import MERGE_STAGES, REVIEW_STAGES, create

    allowed = REVIEW_STAGES if stage == "review" else MERGE_STAGES
    if not isinstance(target, dict) or target.get("stage") not in allowed:
        return {"action": "fallback", "reason": reason}

    return create(config, health, stage, target, reason,
                  target_reservation=reservation)


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


def next_review(gh: GitHub, config: dict, *, config_path: str | None = None) -> dict:
    health = run_health(config, config_path=config_path)
    if config.get("fallback_handoff") == "target-v1":
        from .fallback_handoff import validate_health

        validate_health(health)
    if health.get("state") == "red":
        return {"action": "fallback", "reason": "health check is red"}
    candidates = health.get("review_candidates") or []
    if not candidates:
        return {"action": "empty", "verdict": "ПУСТО", "reason": review_empty_reason(health)}
    item, reservation, election_health = _elect_review_candidate(
        config, health, candidates)
    if item is None:
        return {
            "action": "wait", "verdict": "ПУСТО",
            "reason": "all current REVIEW targets are reserved by other replicas",
        }
    replica_count = _configured_replica_count()
    if replica_count > 1 and reservation is None:
        raise PipelineError("replicated REVIEW election did not reserve its target")
    if item.get("stage") == "pre-review-validation":
        if config.get("fallback_handoff") != "target-v1":
            raise PipelineError(
                "pre-review validation requires the exact-target fallback protocol")
        return fallback_target(
            config, election_health, "review", item,
            "pre-review sync provenance requires validation and a full content review",
            reservation=reservation,
        )
    if item.get("stage") != "review":
        return fallback_target(
            config, election_health, "review", item,
            "integration/base-sync state requires the full skill",
            reservation=reservation,
        )
    completion_gate = config.get("review_completion_gate", "health")
    if completion_gate == "target-v1" and not content_review_elected(
            election_health, item):
        if config.get("fallback_handoff") == "target-v1":
            raise PipelineError("health election did not prove the exact content target")
        return {"action": "fallback", "reason": "health election did not prove the exact content target"}
    if int(item.get("review_depth", 0)) >= 2:
        return fallback_target(
            config, election_health, "review", item,
            "third review round requires human-escalation rules",
            reservation=reservation,
        )
    snapshot = stable_timeline(gh, config, int(item["number"]))
    validate_common(snapshot, config, item)
    info = epoch(snapshot, config["trusted_account"])
    depth = committed_review_depth(snapshot, config["trusted_account"])
    if completion_gate == "target-v1" and depth != int(item.get("review_depth", 0)):
        if config.get("fallback_handoff") == "target-v1":
            raise PipelineError("review depth changed after health election")
        return {"action": "fallback", "reason": "review depth changed after health election", "target": item}
    lease = {"version": 1, "stage": "review", "repository": config["repository"],
             "number": item["number"], "head": snapshot["headRefOid"],
             "snapshot": content_review_digest(snapshot), "epoch": info["hash"],
             "anchor": info["anchor_id"], "depth": depth,
             "completion_gate": completion_gate}
    if reservation is not None:
        lease["target_stage"] = item["stage"]
        lease["target_reservation"] = reservation
        lease["pipeline_replicas"] = replica_count
    if completion_gate == "target-v1":
        issued_at = int(time.time())
        lease.update({
            "issued_at": issued_at,
            "expires_at": issued_at + int(config.get("review_lease_seconds", 7200)),
            "nonce": secrets.token_hex(16),
        })
    try:
        content_review_target_gate(snapshot, config, lease)
    except PipelineError as exc:
        if config.get("fallback_handoff") == "target-v1":
            return fallback_target(
                config, election_health, "review", item, str(exc),
                reservation=reservation,
            )
        return {"action": "fallback", "reason": str(exc), "target": item}
    lease_value = (encode_signed_lease(lease) if completion_gate == "target-v1"
                   else encode_lease(lease))
    return {"action": "audit", "target": item, "lease": lease_value,
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


def content_review_elected(health: dict, selected: dict) -> bool:
    """Require the versioned target-gate election field, including exact state."""
    candidates = health.get("content_review_candidates")
    if not isinstance(candidates, list):
        return False
    expected_number = int(selected.get("number", 0))
    expected_depth = int(selected.get("review_depth", 0))
    return any(
        int(item.get("number", 0)) == expected_number
        and item.get("stage") == "review"
        and item.get("head") == selected.get("head")
        and int(item.get("review_depth", 0)) == expected_depth
        for item in candidates
    )


def complete_review(gh: GitHub, config: dict, lease_value: str, report_path: str,
                    *, config_path: str | None = None) -> dict:
    expected_gate = config.get("review_completion_gate", "health")
    lease = (decode_signed_lease(lease_value) if expected_gate == "target-v1"
             else decode_lease(lease_value))
    validate_review_lease(lease, config)
    report = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
    body, outcome = format_review(lease, report)
    identity_contract = (config["repository"], config["trusted_account"])
    ensure_identity(gh, config)
    completion_gate = lease.get("completion_gate", "health")
    if completion_gate != expected_gate:
        raise PipelineError("review completion gate changed; rerun next review")
    if completion_gate == "health":
        health = run_health(config, config_path=config_path)
        expected_gate = config.get("review_completion_gate", "health")
        if completion_gate != expected_gate:
            raise PipelineError("review completion gate changed; rerun next review")
        validate_review_lease(lease, config)
        if health.get("state") == "red":
            raise PipelineError("health check became red")
        if not content_review_allowed(health, int(lease["number"])):
            raise PipelineError("content REVIEW target left the allowlist; rerun next review")
        if (config["repository"], config["trusted_account"]) != identity_contract:
            ensure_identity(gh, config)
    snapshot = stable_timeline(gh, config, int(lease["number"]))
    validate_common(snapshot, config, lease)
    if content_review_digest(snapshot) != lease["snapshot"]:
        raise PipelineError("lease is stale; rerun next review")
    info = (content_review_target_gate(snapshot, config, lease)
            if completion_gate == "target-v1" else review_gate(snapshot, config, lease))
    if completion_gate == "target-v1":
        # Stable GraphQL reads can be slow.  A lease that expired during them
        # must not authorize the first externally visible mutation.
        validate_review_lease(lease, config)
    # The local election lease is independent from GitHub's state proof. It is
    # renewed only after all read-only gates and immediately before the first
    # comment/label mutation, preventing an expired replica from publishing a
    # second review after another task took over the same HEAD.
    renew_lease_target_reservation(lease, config)
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
    try:
        exact_merge_intent_index(snapshot, config, intent)
    except PipelineError as exc:
        return {"action": "fallback", "reason": str(exc)}
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


def next_merge(gh: GitHub, config: dict, *, config_path: str | None = None) -> dict:
    pending = pending_merge_intents(gh, config)
    if pending:
        return pending_merge_action(gh, config, pending[0])
    health = run_health(config, config_path=config_path)
    if config.get("fallback_handoff") == "target-v1":
        from .fallback_handoff import rest_only_review_owner, validate_health

        if rest_only_review_owner(health):
            return {"action": "fallback",
                    "reason": "single-flight/base-sync owner requires the full skill"}
        validate_health(health)
    if health.get("state") == "red":
        return {"action": "fallback", "reason": "health check is red"}
    if any(item.get("code") == "single_flight_barrier" for item in health.get("findings", [])):
        return fallback_target(config, health, "merge", health.get("integration_owner"),
                               "single-flight/base-sync owner requires the full skill")
    queue = list_ship(gh, config)
    if not queue:
        return {"action": "empty", "verdict": "ПУСТО", "reason": "merge queue is empty"}
    item = queue[0]
    target = {"number": item["number"], "head": item["head"]["sha"], "stage": "merge"}
    snapshot = stable_timeline(gh, config, item["number"])
    validate_common(snapshot, config, {"head": item["head"]["sha"]})
    info = epoch(snapshot, config["trusted_account"])
    validate_epoch_safety(info, config["trusted_account"])
    if any(BASE_SYNC.search(node.get("body") or "") for _index, _edge, node in comments(info, config["trusted_account"])):
        return fallback_target(config, health, "merge", target, "base-sync lineage requires the full skill")
    established = proof(info, snapshot["headRefOid"], config["trusted_account"])
    if not established:
        return fallback_target(config, health, "merge", target, "ordinary canonical review proof not found")
    if not trusted_ship_authorized(info, config["trusted_account"]):
        return fallback_target(config, health, "merge", target,
                               "ship authorization is not a trusted current-HEAD event")
    status, checks = pr_checks(gh, config, item["number"])
    if status.get("mergeStateStatus") != "CLEAN" or status.get("mergeable") != "MERGEABLE":
        return fallback_target(config, health, "merge", target,
                               f"merge state {status.get('mergeStateStatus')}/{status.get('mergeable')} requires the full skill")
    ready, reason = checks_ready(config, checks)
    if not ready:
        return {"action": "wait", "reason": reason, "number": item["number"]}
    lease = {"version": 1, "stage": "merge", "repository": config["repository"],
             "number": item["number"], "head": snapshot["headRefOid"],
             "snapshot": digest(snapshot), "proof": established}
    return {"action": "merge", "target": {"number": item["number"], "title": item["title"], "head": snapshot["headRefOid"]},
            "lease": encode_lease(lease), "complete": "run the same command with: complete merge --lease <lease>"}


def complete_merge(gh: GitHub, config: dict, lease_value: str,
                   *, config_path: str | None = None) -> dict:
    lease = decode_lease(lease_value)
    if lease.get("stage") != "merge" or lease.get("repository") != config["repository"]:
        raise PipelineError("lease belongs to another stage or repository")
    identity_contract = (config["repository"], config["trusted_account"])
    ensure_identity(gh, config)
    health = run_health(config, config_path=config_path)
    if lease.get("stage") != "merge" or lease.get("repository") != config["repository"]:
        raise PipelineError("lease belongs to another stage or repository")
    if (config["repository"], config["trusted_account"]) != identity_contract:
        ensure_identity(gh, config)
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
        intent, _ = reserve_merge_intent(
            gh, config, lease["number"], marker)
        if intent is None:
            return {"action": "wait", "stage": "merge", "number": lease["number"],
                    "reason": "another merge cleanup intent won"}
    elif int(intent.get("number", 0)) != int(lease["number"]):
        raise PipelineError("merge intent belongs to another PR")

    snapshot = stable_timeline(gh, config, lease["number"])
    validate_common(snapshot, config, lease)
    exact_merge_intent_index(snapshot, config, intent)
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
    gate_parser = sub.add_parser("gate-fallback")
    gate_parser.add_argument("stage", choices=("review", "merge"))
    gate_parser.add_argument("--lease", required=True)
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
            if hasattr(gh, "timeout_seconds"):
                gh.timeout_seconds = int(config.get("github_timeout_seconds", 120))
            if args.command == "next":
                value = (next_review(gh, config, config_path=args.config)
                         if args.stage == "review"
                         else next_merge(gh, config, config_path=args.config))
            elif args.command == "gate-fallback":
                from .fallback_handoff import gate

                value = gate(gh, config, args.stage, args.lease, config_path=args.config)
            elif args.stage == "review":
                if not args.report:
                    raise PipelineError("complete review requires --report")
                value = complete_review(
                    gh, config, args.lease, args.report, config_path=args.config,
                )
            elif args.stage == "merge":
                value = complete_merge(gh, config, args.lease, config_path=args.config)
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
        error = str(exc)
        print(json.dumps({"action": "error", "error": error}, ensure_ascii=False, indent=2))
        # Keep stdout machine-readable, but do not let a shell assignment hide
        # the only copy of the diagnostic when it branches on the exit code.
        print(f"pipelinectl {args.command}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
