"""Carry a trusted ``ship`` through one proven mechanical base-sync (issue #42).

After a base-sync — GitHub ``update-branch`` or the repository skill's
intent/done — the PR head is a two-parent merge ``to = merge(from, base)``.
That is a new commit: by the ordinary exact-HEAD rule neither the committed
review of ``from`` nor the human ``ship`` cover it, and the merge queue waited
for a full integration REVIEW.

The owner's decision on #42 (PromptPilot#42, 2026-09-28): an already granted
trusted ``ship`` may carry over ONE mechanical base-sync, in an opt-in path
only (``base_sync_carry``), when all of this is proven right before the
mutation:

1. ``from`` has the canonical, unedited committed review proof with
   ``Outcome-Label: reviewed``; a trusted ``ship`` was set after that review
   and is still the latest ``ship`` transition; the PR still targets the
   configured base branch; there is no later override (``pp:review-again``)
   or hold.
2. ``to`` is exactly the two-parent merge ``[from, base]``; ``base`` is an
   ancestor of the current base branch; no extra commit, force-push or base
   change happened around it.
3. The tree of ``to`` is byte-identical to a conflict-free reproduction of the
   merge of ``base`` into ``from``. Conflict resolution is never carried.
4. All required CI succeed on exact ``to``.

If anything is not proven, nothing is carried and nothing is merged: the
owner stays on the ordinary integration REVIEW route.

The project health snapshot's ``base_sync_candidate`` (onebase#1776) is a
descriptive data source, never a proof: every fact is established here from
the stable GraphQL timeline, local git and the live PR state, and the
snapshot is only cross-checked against them. The position of the base-sync
commit in the timeline proves nothing (GitHub orders commits by their date,
onebase#1561): the transition is proven by the graph. This module publishes
nothing.
The MERGE transaction re-proves the carry after reserving its intent and
merges with GitHub's exact ``sha`` compare-and-swap.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from . import project_pipeline as pp
from .pipeline_errors import PipelineError

PROTOCOL = "base-sync-carry-v1"
CLOCK_MARGIN_SECONDS = 60
STAGES = frozenset({"integration-review", "legacy-integration-review"})
CONSUMER_CHECKS = frozenset({"base_ancestry", "merge_tree", "required_checks",
                             "timeline_epoch"})
BLOCKING_LABELS = frozenset({"hold", "needs-decision", "changes-requested"})
SHA = re.compile(r"[0-9a-f]{40}")
BASE_SYNC_DONE = re.compile(
    r"(?m)^<!-- pp:base-sync-done intent=([0-9]+) from=([0-9a-f]{40}) "
    r"to=([0-9a-f]{40}) base=([0-9a-f]{40}) previous=([0-9]+|none) "
    r"ship-event=([A-Za-z0-9_=-]+) -->$")


class _Refused(Exception):
    """A carry condition is not proven: take the ordinary REVIEW route."""


class _Pending(Exception):
    """Not proven yet, but may become proven without anyone acting (CI runs)."""


def _node(edge: dict) -> dict:
    return edge.get("node") or {}


def candidate_of(owner: dict) -> dict:
    """The snapshot's candidate, if its shape allows a carry at all."""
    value = owner.get("base_sync_candidate")
    if not isinstance(value, dict):
        raise _Refused("health snapshot gives no base_sync_candidate for the owner")
    shas = [value.get(field) for field in ("from", "base", "to")]
    if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in shas):
        raise _Refused("base_sync_candidate has malformed commit identities")
    if value["to"] != owner.get("head"):
        raise _Refused("base_sync_candidate describes another HEAD")
    if value.get("source") != "head_parents":
        raise _Refused("base_sync_candidate is not read from the commit graph")
    checks = value.get("consumer_must_verify")
    if not isinstance(checks, list) or not set(checks) <= CONSUMER_CHECKS:
        # A check we do not know is a contract change: fail closed.
        raise _Refused(f"base_sync_candidate asks for unknown consumer checks: {checks}")
    if value.get("current_head_reviewed") is not False:
        raise _Refused("the current HEAD has its own review: this is not a carry")
    review = value.get("from_review")
    if not isinstance(review, dict) or review.get("state") != "consistent":
        state = review.get("state") if isinstance(review, dict) else "missing"
        raise _Refused(f"review pair of the reviewed version is {state}")
    if review.get("outcome_label") != "reviewed":
        raise _Refused("review of the reviewed version is not reviewed")
    return value


def timeline_facts(snapshot: dict, trusted: str, from_sha: str, to_sha: str,
                   base_sha: str, *, cut: int | None = None) -> dict:
    """Conditions 1 and the timeline half of 2, from the server timeline only.

    Where the base-sync commit sits in the timeline is not evidence: GitHub
    orders a ``PullRequestCommit`` by the commit date, not by the push
    (onebase#1561, #1824), so a merge made locally before the review or the
    ship lands among them. The transition is proven by the graph instead: the
    timeline lists the commit of ``to`` exactly once, and with that edge set
    aside the epoch of ``from`` runs to the end with no other commit,
    force-push, restore, base change, deleted or edited trusted comment and no
    later ``pp:review-again``. Git proves the parents (:func:`git_facts`).

    ``cut`` limits the history to the edges before a merge intent marker: after
    the merge the cleanup removes ``ship``, and the recovery must still derive
    the same evidence. The commit edge is looked up in the whole timeline.
    """
    everything = snapshot["edges"]
    listed = [index for index, edge in enumerate(everything)
              if _node(edge).get("__typename") == "PullRequestCommit"
              and (_node(edge).get("commit") or {}).get("oid") == to_sha]
    if not listed:
        raise _Refused("the base-sync commit is not in the timeline")
    if len(listed) > 1:
        raise _Refused("the base-sync commit is listed in the timeline more than once")
    history = everything if cut is None else everything[:cut]
    edges = [edge for index, edge in enumerate(history) if index != listed[0]]

    # The reviewed version's epoch, the base-sync commit set aside: nothing
    # may have interrupted it since.
    try:
        from_info = pp.epoch(dict(snapshot, headRefOid=from_sha, edges=edges), trusted)
        pp.validate_epoch_safety(from_info, trusted)
    except PipelineError as exc:
        raise _Refused(f"after the reviewed version: {exc}") from exc
    established = pp.proof(from_info, from_sha, trusted)
    if not established:
        anchor = _node(edges[from_info["anchor_index"]]).get("__typename")
        if anchor == "IssueComment":
            raise _Refused("a later pp:review-again restarted the review epoch")
        if anchor == "HeadRefRestoredEvent":
            raise _Refused("the head branch was restored after the reviewed version")
        raise _Refused("the reviewed version has no canonical committed review proof")
    if established.get("outcome") != "reviewed":
        raise _Refused(f"the review of the reviewed version is {established.get('outcome')}")
    review_at = next((index for index, edge in enumerate(edges)
                      if _node(edge).get("id") == established.get("review_node")), None)
    if review_at is None:
        raise _Refused("the review conclusion is not in the timeline")

    # A review of ``to`` itself, wherever it sits, belongs to the ordinary path.
    for _index, _edge, node in pp.comments(from_info, trusted):
        body = (node.get("body") or "").strip()
        matches = (pp.REVIEW.search(body), pp.CLAIM.fullmatch(body), pp.COMPLETE.fullmatch(body))
        if any(match and match.group(1) == to_sha for match in matches):
            raise _Refused("the base-sync commit has its own review transaction: this is not a carry")

    # Markers are optional, but a marker that contradicts the graph is not.
    for edge in edges:
        node = _node(edge)
        if (node.get("__typename") != "IssueComment"
                or (node.get("author") or {}).get("login") != trusted):
            continue
        for match in BASE_SYNC_DONE.finditer(node.get("body") or ""):
            if match.group(3) == to_sha and (match.group(2), match.group(4)) != (from_sha, base_sha):
                raise _Refused("a pp:base-sync-done marker contradicts the commit graph")

    # Trusted ship: the latest ship transition, set after the review verdict
    # was published. The ordinary rule accepts a ship anywhere in the HEAD epoch
    # (it is sticky intent for a review still to come); a carried ship must be
    # a decision taken with the review in front of the human.
    transitions = [(index, _node(edge)) for index, edge in enumerate(edges)
                   if _node(edge).get("__typename") in {"LabeledEvent", "UnlabeledEvent"}
                   and (_node(edge).get("label") or {}).get("name") == "ship"]
    if not transitions:
        raise _Refused("no ship label transition in the timeline")
    ship_at, latest = transitions[-1]
    if (latest.get("__typename") != "LabeledEvent"
            or (latest.get("actor") or {}).get("login") != trusted):
        raise _Refused("the latest ship transition is not a trusted ship")
    if ship_at <= review_at:
        raise _Refused("trusted ship was set before the review of the reviewed version")
    if not latest.get("id"):
        raise _Refused("the ship event has no node id")
    return {"from_info": from_info, "from_proof": established, "ship_event": latest["id"]}


MERGE_TREE_GIT = (2, 38)  # `merge-tree --write-tree` prints the merged tree's OID


def git_bin(config: dict) -> str:
    """The git the carry proof runs on.

    A Mac's system git can be too old to prove a tree at all — Apple Git
    2.37.1 ships only the old `merge-tree <base-tree> <branch1> <branch2>`,
    which prints a human-readable diff and no OID. So the binary is a setting:
    point `git_bin` (or PP_GIT_BIN) at a pinned newer git instead of replacing
    the system one.
    """
    return str(config.get("git_bin") or os.environ.get("PP_GIT_BIN") or "git")


def _git(config: dict, *args: str, allow=(0,)) -> tuple[int, str]:
    timeout = int(config.get("base_sync_timeout_seconds", 60))
    binary = git_bin(config)
    try:
        result = subprocess.run(
            [binary, "-c", "maintenance.auto=false", *args], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"git {args[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise PipelineError(f"git is not available at {binary!r}: {exc}") from exc
    if result.returncode not in allow:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise PipelineError(f"git {args[0]}: {detail}")
    return result.returncode, result.stdout.strip()


def ensure_objects(config: dict, number: int, *shas: str) -> None:
    """Have these commits locally; fetch the PR head and base branch if not.

    Which commits matter comes from GitHub (the stable GraphQL snapshot and
    the live PR state), not from local refs: git only supplies the objects.
    """
    def missing():
        return [sha for sha in shas
                if _git(config, "cat-file", "-e", f"{sha}^{{commit}}", allow=(0, 1, 128))[0]]
    if not missing():
        return
    base = str(config.get("base_branch") or "main")
    _git(config, "fetch", "--no-tags", "origin",
         f"+refs/pull/{number}/head:refs/promptpilot/carry/pr-{number}",
         f"+refs/heads/{base}:refs/remotes/origin/{base}")
    if missing():
        raise _Refused("commits of the base-sync are not reachable from origin")


def parents_of(config: dict, sha: str) -> list[str]:
    return _git(config, "rev-list", "--parents", "-n", "1", sha)[1].split()[1:]


def commit_date(config: dict, sha: str) -> int:
    """The later of the author and committer dates, in seconds since the epoch."""
    return max(int(value) for value in _git(config, "show", "-s", "--format=%at %ct", sha)[1].split())


def git_version(config: dict) -> tuple[int, ...]:
    """Numeric version of the git in use.

    "git version 2.37.1 (Apple Git-137.1)" → (2, 37, 1); the vendor suffix
    carries no ordering and is left out.
    """
    _, output = _git(config, "--version")
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", output)
    if not match:
        raise PipelineError(f"git --version is unreadable: {output!r}")
    return tuple(int(part) for part in match.groups() if part is not None)


def _dotted(version) -> str:
    return ".".join(str(part) for part in version)


def ensure_tree_proof_possible(config: dict) -> dict:
    """Capability preflight: can this git prove a tree at all?

    Condition 3 is byte equality of the merged tree, and the only thing that
    produces it is `merge-tree --write-tree`, which arrived in git 2.38. There
    is no substitute: a file list, a diffstat or GitHub's mergeable flag say
    nothing about content. So when the available git is older we refuse here —
    before the carry is attempted — and the PR takes the ordinary REVIEW
    route. A refusal, not a wait: no git gets newer on its own, and a carry
    pending forever would hold the lane with no way out.

    Checked in the only place that produces the proof, so every route — the
    live verification and the post-merge recomputation — is covered.
    """
    binary = git_bin(config)
    version = git_version(config)
    _, usage = _git(config, "merge-tree", "-h", allow=(0, 1, 129))
    if version < MERGE_TREE_GIT or "--write-tree" not in usage:
        raise _Refused(
            f"git {_dotted(version)} at {binary!r} cannot prove the merged tree: "
            f"'merge-tree --write-tree' needs git {_dotted(MERGE_TREE_GIT)} or newer. "
            "Point 'git_bin' (or PP_GIT_BIN) at a pinned newer git; until then the "
            "carry is off and the ordinary REVIEW route applies")
    return {"git": binary, "version": _dotted(version)}


def git_facts(config: dict, number: int, from_sha: str, base_sha: str,
              to_sha: str, base_tip: str) -> dict:
    """Conditions 2 (commit shape, ancestry) and 3 (reproduced tree).

    ``base_tip`` is the base branch tip GitHub reports for this PR.
    """
    ensure_tree_proof_possible(config)
    ensure_objects(config, number, to_sha, base_tip)
    if parents_of(config, to_sha) != [from_sha, base_sha]:
        raise _Refused("HEAD is not exactly the two-parent merge [from, base]")
    code, _ = _git(config, "merge-base", "--is-ancestor", base_sha, base_tip, allow=(0, 1))
    if code:
        raise _Refused("base is not an ancestor of the current base branch")
    code, output = _git(config, "merge-tree", "--write-tree", "--no-messages",
                        from_sha, base_sha, allow=(0, 1))
    if code:
        raise _Refused("the reproduced merge of base into from has conflicts; "
                       "conflict resolution is not carried")
    reproduced = output.splitlines()[0].strip() if output else ""
    actual = _git(config, "rev-parse", f"{to_sha}^{{tree}}")[1]
    if not reproduced or reproduced != actual:
        raise _Refused("the tree of HEAD differs from the reproduced merge of base into from")
    return {"tree": actual}


def live_state(gh, config: dict, number: int, to_sha: str) -> dict:
    """Condition 4 and the merge state of the exact HEAD."""
    value = gh.json("pr", "view", str(number), "--repo", config["repository"], "--json",
                    "headRefOid,mergeStateStatus,mergeable,statusCheckRollup,body")
    if value.get("headRefOid") != to_sha:
        raise _Refused("the PR head moved while it was being verified")
    checks = value.get("statusCheckRollup") or []
    if pp.checks_in_progress(config, checks):
        raise _Pending("required CI checks are still running on HEAD")
    ready, reason = pp.checks_ready(config, checks)
    if not ready:
        raise _Refused(f"required CI on HEAD: {reason}")
    state, mergeable = value.get("mergeStateStatus"), value.get("mergeable")
    if state == "UNKNOWN" or mergeable == "UNKNOWN":
        raise _Pending("GitHub is still computing mergeability")
    if state == "BEHIND":
        raise _Refused("the base branch moved again: only one mechanical hop is carried")
    if state != "CLEAN" or mergeable != "MERGEABLE":
        raise _Refused(f"merge state {state}/{mergeable}")
    return value


def evidence_of(from_sha: str, base_sha: str, to_sha: str, facts: dict) -> dict:
    """What the merge intent binds: immutable, recomputable after the merge."""
    return {"protocol": PROTOCOL, "from": from_sha, "base": base_sha, "to": to_sha,
            "from_proof": facts["from_proof"], "ship_event": facts["ship_event"]}


def verify(gh, config: dict, number: int, to_sha: str, *,
           candidate: dict | None = None, snapshot: dict | None = None) -> dict:
    """Prove the carry for PR ``number`` at HEAD ``to_sha`` — or say why not.

    ``candidate`` is the health snapshot's ``base_sync_candidate`` (cross-
    checked when given); without it the parents come from git alone. Returns
    ``{"verdict": "proven", "evidence", "snapshot", "status"}``, or
    ``{"verdict": "pending" | "refused", "reason"}``.
    """
    try:
        if not config.get("base_sync_carry"):
            raise _Refused("base_sync_carry is off")
        if not config.get("required_checks"):
            raise _Refused("base_sync_carry needs an explicit required_checks list")
        trusted = config["trusted_account"]
        snapshot = snapshot or pp.stable_timeline(gh, config, number)
        try:
            pp.validate_common(snapshot, config, {"head": to_sha})
        except PipelineError as exc:
            raise _Refused(str(exc)) from exc
        labels = set(snapshot["labels"])
        if "ship" not in labels:
            raise _Refused("ship label is absent")
        if labels & BLOCKING_LABELS:
            raise _Refused(f"routing label {sorted(labels & BLOCKING_LABELS)[0]} is set")
        base_tip = snapshot.get("baseRefOid")
        if not isinstance(base_tip, str) or not SHA.fullmatch(base_tip):
            raise _Refused("GitHub reports no base branch tip for the PR")
        if candidate is not None:
            from_sha, base_sha = candidate["from"], candidate["base"]
        else:
            ensure_objects(config, number, to_sha)
            parents = parents_of(config, to_sha)
            if len(parents) != 2:
                raise _Refused("HEAD is not a two-parent merge")
            from_sha, base_sha = parents
        facts = timeline_facts(snapshot, trusted, from_sha, to_sha, base_sha)
        if candidate is not None:
            review = candidate["from_review"]
            established = facts["from_proof"]
            if (review.get("sha") != from_sha
                    or review.get("review_comment") != established["review_id"]
                    or review.get("claim") != established["claim_id"]
                    or review.get("epoch_sha256") != facts["from_info"]["hash"]):
                raise _Refused("the health snapshot and the server timeline disagree "
                               "on the review of the reviewed version")
        git_facts(config, number, from_sha, base_sha, to_sha, base_tip)
        status = live_state(gh, config, number, to_sha)
        if commit_date(config, to_sha) > time.time() - CLOCK_MARGIN_SECONDS:
            # GitHub lists a commit by its date (onebase#1561): one dated ahead
            # of the clock would land after the merge intent, where the cleanup
            # accepts no commit. Wait until the date has passed.
            raise _Pending("the base-sync commit is dated ahead of the clock; "
                           "waiting until its date passes")
        return {"verdict": "proven", "evidence": evidence_of(from_sha, base_sha, to_sha, facts),
                "snapshot": pp.digest(snapshot), "status": status}
    except _Pending as exc:
        return {"verdict": "pending", "reason": str(exc)}
    except _Refused as exc:
        return {"verdict": "refused", "reason": str(exc)}
    except PipelineError as exc:
        return {"verdict": "refused", "reason": f"carry could not be verified: {exc}"}


def evidence_after_merge(gh, config: dict, snapshot: dict, intent: dict,
                         intent_index: int) -> dict:
    """Recompute the evidence a merged carry intent bound (post-merge recovery)."""
    to_sha = intent["head"]
    commit = gh.json("api", f"repos/{config['repository']}/commits/{to_sha}")
    parents = [item.get("sha") for item in commit.get("parents") or []]
    if len(parents) != 2 or not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in parents):
        raise PipelineError("merged carry target is not a two-parent merge")
    try:
        facts = timeline_facts(snapshot, config["trusted_account"], parents[0], to_sha,
                               parents[1], cut=intent_index)
    except _Refused as exc:
        raise PipelineError(f"carry evidence no longer holds: {exc}") from exc
    return evidence_of(parents[0], parents[1], to_sha, facts)


def owner_carry(gh, config: dict, health: dict) -> dict | None:
    """The integration owner's carry verdict, when the opt-in applies at all."""
    owner = health.get("integration_owner") or {}
    if not config.get("base_sync_carry") or owner.get("stage") not in STAGES:
        return None
    try:
        candidate = candidate_of(owner)
    except _Refused as exc:
        return {"verdict": "refused", "reason": str(exc)}
    return verify(gh, config, int(owner["number"]), candidate["to"], candidate=candidate)
