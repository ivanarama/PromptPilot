"""Confirm a single ordinary OneBase base-sync without changing its branch or merging.

The dispatcher supplies the authenticated, finite-budget transport, a fresh signed
gate, a local runtime fence and a persistent one-send claim. No provider controls
the command, endpoints, body, or target. A failed or ambiguous send never retries.
"""

import hashlib
from pathlib import Path
import re
import subprocess

from . import base_sync_carry, project_pipeline as pp
from .merge_ship_authorization import intent_line
from .review_routing_policy import require_legacy


REPOSITORY = "ivanarama/onebase"
TRUSTED = "ivanarama"
REMOTE = "https://github.com/" + REPOSITORY + ".git"
REQUIRED = frozenset(
    (
        "build",
        "lint",
        "postgres-integration",
        "vuln",
        "smoke",
        "e2e",
        "test-windows",
        "launcher-webview-build",
    )
)
ADDRESSED_QUERY = pp.TIMELINE_QUERY.replace("$cursor:String)", "$cursor:String,$ids:[ID!]!)").replace(
    " repository(owner:",
    """ nodes(ids:$ids){__typename
    ... on IssueComment{id fullDatabaseId createdAt lastEditedAt author{login} body}
    ... on LabeledEvent{id createdAt actor{login} label{name}}
    ... on PullRequestCommit{id commit{oid}}
  }
 repository(owner:""",
)


class OutsideScope(pp.PipelineError):
    """A more general, independently admitted recovery is required."""


def _need(ok, reason):
    if not ok:
        raise pp.PipelineError(reason)


def _git(config, working_dir, *args):
    try:
        result = subprocess.run(
            [
                base_sync_carry.git_bin(config),
                "--no-replace-objects",
                "-C",
                str(working_dir),
                "-c",
                "maintenance.auto=false",
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise pp.PipelineError("git could not verify the base-sync") from exc
    if result.returncode:
        if args[0] == "merge-tree" and result.returncode in (1, 129):
            raise OutsideScope("git cannot reproduce this conflict-free tree")
        raise pp.PipelineError(f"git {args[0]} could not verify the base-sync")
    return result.stdout.strip()


def _verify_remote(config, working_dir, branch, head):
    ref = "refs/heads/" + branch
    _git(config, working_dir, "check-ref-format", ref)
    _need(
        _git(config, working_dir, "ls-remote", "--refs", REMOTE, ref) == f"{head}\t{ref}",
        "remote branch identity changed",
    )


def _tree_proof(config, working_dir, branch, checkpoint):
    old, base, head, tree = (checkpoint[k] for k in ("from", "base", "to", "tree"))
    # Fetch exactly the checked same-repository ref, without changing a branch
    # or even the caller's FETCH_HEAD. All subsequent git input is fixed OIDs.
    _git(
        config,
        working_dir,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        REMOTE,
        "refs/heads/" + branch,
    )
    _need(
        _git(config, working_dir, "rev-list", "-n", "1", "--parents", head).split() == [head, old, base],
        "local commit parents differ",
    )
    _need(_git(config, working_dir, "rev-parse", head + "^{tree}") == tree, "local tree differs")
    if _git(config, working_dir, "merge-tree", "--write-tree", "--no-messages", old, base) != tree:
        raise OutsideScope("base-sync required substantive conflict resolution")


def _identity(pr, checkpoint):
    repo = checkpoint["repository"]
    if pr["head"]["repo"]["full_name"] != repo or pr["base"]["sha"] != checkpoint["base"]:
        raise OutsideScope("fork or subsequent base movement requires ordinary recovery")
    _need(
        pr["number"] == checkpoint["number"]
        and pr["state"] == "open"
        and pr["merged"] is False
        and pr["draft"] is False
        and pr["base"]["ref"] == "main"
        and pr["base"]["repo"]["full_name"] == repo
        and pr["head"]["sha"] == checkpoint["to"]
        and isinstance(pr["maintainer_can_modify"], bool),
        "PR identity/base changed",
    )
    branch = pr["head"]["ref"]
    _need(isinstance(branch, str) and branch and branch != "main", "unsupported PR branch")
    labels = sorted(label["name"] for label in pr["labels"])
    _need(
        "ship" in labels and not set(labels) & {"hold", "manual", "needs-decision", "changes-requested"},
        "ship or routing labels changed",
    )
    return branch, labels, pr["maintainer_can_modify"]


def _ci(gh, root, sha):
    result = gh.json("api", f"{root}/commits/{sha}/check-runs?per_page=100&page=1&filter=latest")
    rows = result["check_runs"]
    if not isinstance(rows, list) or result["total_count"] != len(rows) or len(rows) >= 100:
        raise OutsideScope("check-run inventory exceeds one complete page")
    found = {}
    seen_ids = set()
    for row in rows:
        name = row["name"]
        if name in REQUIRED:
            run_id = row["id"]
            _need(
                type(run_id) is int
                and run_id > 0
                and run_id not in seen_ids
                and row["head_sha"] == sha
                and row["app"]["slug"] == "github-actions"
                and row["app"]["id"] == 15368,
                "required check has an ambiguous identity",
            )
            seen_ids.add(run_id)
            if name not in found or run_id > found[name]["id"]:
                found[name] = row
    if set(found) != REQUIRED or any(
        run["status"] != "completed" or run["conclusion"] != "success" for run in found.values()
    ):
        raise OutsideScope("ordinary recovery must handle incomplete or failed CI")
    return {name: run["id"] for name, run in found.items()}


def _comment_readback(gh, number, node, body):
    comment = pp.comment_id(node)
    value = gh.json("api", f"repos/{REPOSITORY}/issues/comments/{comment}")
    _need(
        value["id"] == comment
        and value["node_id"] == node["id"]
        and value["issue_url"] == f"https://api.github.com/repos/{REPOSITORY}/issues/{number}"
        and value["user"]["login"] == TRUSTED
        and value["body"].encode("utf-8") == body.encode("utf-8"),
        "comment author, location, identity or exact UTF-8 readback differs",
    )
    return value


def _history(gh, config, checkpoint):
    number, old, base, head, intent = (checkpoint[k] for k in ("number", "from", "base", "to", "intent"))
    snap = pp.stable_timeline(gh, config, number)
    pp.validate_common(snap, config, {"head": head})
    require_legacy(gh, config, number, snapshot=snap)
    nodes = [edge["node"] for edge in snap["edges"]]
    comments = [node for node in nodes if node["__typename"] == "IssueComment"]
    _need(
        sum(
            node.get("commit", {}).get("oid") == old
            for node in nodes
            if node["__typename"] == "PullRequestCommit"
        )
        == 1,
        "reviewed source has no unique server commit anchor",
    )
    _need(
        len({n["id"] for n in nodes}) == len(nodes)
        and len({e["cursor"] for e in snap["edges"]}) == len(nodes)
        and len({pp.comment_id(n) for n in comments}) == len(comments)
        and all(isinstance(n["id"], str) and n["id"] for n in nodes)
        and all(
            "lastEditedAt" in n and isinstance(n["body"], str) and pp.comment_id(n) and pp.comment_id(n) > 0
            for n in comments
        ),
        "incomplete or ambiguous timeline identities",
    )
    try:
        facts = base_sync_carry.timeline_facts(snap, TRUSTED, old, head, base)
    except base_sync_carry._Refused as exc:
        raise pp.PipelineError(str(exc)) from exc
    proof, epoch, ship = facts["from_proof"], facts["from_info"], facts["ship_event"]
    _need(
        pp.trusted_ship_authorized(epoch, TRUSTED) and re.fullmatch("[A-Za-z0-9_=-]+", ship),
        "unproven continuous source ship",
    )
    fields = (
        f"from={old} base={base} review-comment={proof['review_id']} "
        f"claim={proof['claim_id']} completion={proof['completion_id']} ship-event={ship} previous=none"
    )
    intent_body = f"<!-- pp:base-sync-intent {fields} -->"
    done_body = (
        f"<!-- pp:base-sync-done intent={intent} from={old} to={head} "
        f"base={base} previous=none ship-event={ship} -->"
    )
    intents = [n for n in comments if "<!-- pp:base-sync-intent" in n["body"]]
    dones = [n for n in comments if "<!-- pp:base-sync-done" in n["body"]]
    if len(intents) != 1 or len(dones) > 1:
        raise OutsideScope("terminal route requires a unique first-hop intent and at most one done")
    saved = intents[0]
    marker = intent_line(saved["body"])
    _need(
        pp.comment_id(saved) == intent
        and marker and marker[0] == intent_body
        and saved["lastEditedAt"] is None
        and saved["author"]["login"] == TRUSTED,
        "checkpoint differs from canonical intent",
    )
    ids = [
        epoch["anchor_id"],
        proof["review_node"],
        proof["claim_node"],
        proof["completion_node"],
        ship,
        saved["id"],
    ]
    by_id = {n["id"]: n for n in nodes}
    order = [n["id"] for n in nodes]
    _need(
        by_id[ids[0]]["__typename"] == "PullRequestCommit"
        and order.index(saved["id"]) > max(order.index(ship), order.index(proof["completion_node"])),
        "intent outside reviewed epoch",
    )
    if dones:
        done = dones[0]
        _need(
            done["body"] == done_body
            and done["author"]["login"] == TRUSTED
            and done["lastEditedAt"] is None
            and order.index(done["id"]) > order.index(saved["id"]),
            "conflicting or edited confirmation",
        )
        ids.append(done["id"])

    _check_addressed(gh, config, number, snap, ids)
    return snap, saved, dones[0] if dones else None, done_body


def _check_addressed(gh, config, number, snap, ids):
    by_id = {edge["node"]["id"]: edge["node"] for edge in snap["edges"]}

    class Addressed:
        def graphql_page(self, owner, name, number, cursor):
            values = {"owner": owner, "name": name, "number": number, "cursor": cursor, "ids": ids}
            answer = gh.json(
                "api",
                "graphql",
                "--input",
                "-",
                input_value={"query": ADDRESSED_QUERY, "variables": values},
            )
            _need(
                not answer.get("errors") and answer["data"]["nodes"] == [by_id[i] for i in ids],
                "addressed proof nodes changed or disappeared",
            )
            return answer

    verified = pp.stable_timeline(Addressed(), config, number)
    _need(pp.canonical(verified) == pp.canonical(snap), "full timeline changed during addressed proof")


def observe_receipt(gh, record, *, config, expected_node_id=None):
    receipt = observe_delivery(gh, record, config=config, expected_node_id=expected_node_id)
    return receipt[0] if receipt else None


def observe_delivery(gh, record, *, config, expected_node_id=None):
    """Look up an uncertain send for just its recorded PR, including after REVIEW/merge.

    This only settles the local send journal. The original immutable intent and
    exact body hash remain required. No current-head or current-label authority
    is derived from historical events.
    """
    if (
        record.get("repository") != REPOSITORY
        or config.get("repository") != REPOSITORY
        or config.get("trusted_account") != TRUSTED
        or not all(type(record.get(k)) is int and 0 < record[k] < 2**63 for k in ("number", "intent"))
        or not all(
            isinstance(record.get(k), str) and re.fullmatch("[0-9a-f]{40}", record[k])
            for k in ("from", "base", "to", "tree")
        )
        or not isinstance(record.get("body_sha256"), str)
        or not re.fullmatch("[0-9a-f]{64}", record["body_sha256"])
    ):
        return None
    number, intent, old, base, head = (record[k] for k in ("number", "intent", "from", "base", "to"))
    _need(gh.json("api", "user")["login"] == TRUSTED, "unexpected receipt observation account")
    snap = pp.stable_timeline(gh, config, number)
    nodes = [e["node"] for e in snap["edges"]]
    comments = [n for n in nodes if n["__typename"] == "IssueComment"]
    intents = [n for n in comments if pp.comment_id(n) == intent]
    prefix = f"<!-- pp:base-sync-done intent={intent} "
    dones = [
        n
        for n in comments
        if (n.get("author") or {}).get("login") == TRUSTED
        and isinstance(n.get("body"), str)
        and n["body"].startswith(prefix)
    ]
    if len(intents) != 1 or len(dones) != 1:
        return None
    saved, done = intents[0], dones[0]
    if (
        any(
            n.get("lastEditedAt", True) is not None or (n.get("author") or {}).get("login") != TRUSTED
            for n in (saved, done)
        )
        or not isinstance(done.get("body"), str)
        or not isinstance(saved.get("body"), str)
        or hashlib.sha256(done["body"].encode("utf-8")).hexdigest() != record["body_sha256"]
    ):
        return None
    general = record.get("proof_kind") == "legacy-general-done-v1"
    marker = intent_line(saved["body"])
    if not marker or marker[1] != old or (not general and (marker[2] != base or marker[7] != "none")):
        return None
    ship, previous = marker[6], marker[7]
    expected = f"{prefix}from={old} to={head} base={base} previous={previous} ship-event={ship} -->"
    order = [n.get("id") for n in nodes]
    if (
        done["body"] != expected
        or expected_node_id is not None and done["id"] != expected_node_id
        or not all(isinstance(i, str) and i for i in order)
        or len(set(order)) != len(order)
        or not 0 < (pp.comment_id(done) or 0) < 2**63
        or len([n for n in comments if pp.comment_id(n) == pp.comment_id(done)]) != 1
        or ship not in order
        or not order.index(ship) < order.index(saved["id"]) < order.index(done["id"])
    ):
        return None
    ship_event = nodes[order.index(ship)]
    if (
        ship_event["__typename"] != "LabeledEvent"
        or (ship_event.get("actor") or {}).get("login") != TRUSTED
        or (ship_event.get("label") or {}).get("name") != "ship"
    ):
        return None
    _comment_readback(gh, number, saved, saved["body"])
    _comment_readback(gh, number, done, expected)
    _check_addressed(gh, config, number, snap, [ship, saved["id"], done["id"]])
    state, current_head = snap.get("state"), snap.get("headRefOid")
    _need(state in {"OPEN", "CLOSED", "MERGED"} and isinstance(current_head, str)
          and re.fullmatch("[0-9a-f]{40}", current_head), "incomplete receipt PR state")
    return pp.comment_id(done), state == "OPEN" and current_head == head


def _observe(gh, config, checkpoint, working_dir, *, verify_tree):
    root = f"repos/{REPOSITORY}"
    number, head, old, base = (checkpoint[k] for k in ("number", "to", "from", "base"))
    _need(gh.json("api", "user")["login"] == TRUSTED, "unexpected GitHub account")
    repository = gh.json("api", root)
    _need(
        repository["full_name"] == REPOSITORY and repository["permissions"]["push"] is True,
        "repository or write permission differs",
    )
    pr_path = f"{root}/pulls/{number}"
    identity = _identity(gh.json("api", pr_path), checkpoint)
    branch, labels, _permission = identity
    _verify_remote(config, working_dir, branch, head)
    _need(_identity(gh.json("api", pr_path), checkpoint) == identity, "PR branch readbacks differ")
    _verify_remote(config, working_dir, branch, head)
    if verify_tree:
        _tree_proof(config, working_dir, branch, checkpoint)
    main = gh.json("api", root + "/git/ref/heads/main")
    _need(
        main["ref"] == "refs/heads/main" and main["object"]["type"] == "commit",
        "protected main identity is unproved",
    )
    if main["object"]["sha"] != base:
        raise OutsideScope("protected main advanced; ordinary ancestry proof required")
    commit = gh.json("api", f"{root}/git/commits/{head}")
    _need(
        commit["sha"] == head
        and commit["tree"]["sha"] == checkpoint["tree"]
        and [p["sha"] for p in commit["parents"]] == [old, base],
        "server commit/tree/parents differ",
    )
    protection = gh.json("api", root + "/branches/main/protection")["required_status_checks"]
    _need(
        protection["strict"] is True and set(protection["contexts"]) == REQUIRED,
        "strict branch protection or CI8 contexts changed",
    )
    checks = tuple(_ci(gh, root, sha) for sha in (old, head))
    snapshot, saved, done, body = _history(gh, config, checkpoint)
    _need(snapshot["labels"] == labels and snapshot["baseRefOid"] == base, "GraphQL/REST PR state differs")
    _comment_readback(gh, number, saved, saved["body"])
    if done:
        _comment_readback(gh, number, done, body)
    _need(gh.json("api", root + "/git/ref/heads/main") == main, "protected main changed during proof")
    return snapshot, identity, checks, done, body


def confirm(gh, config, checkpoint, *, working_dir, prepare_source, gate, claim_send, validate_attempt):
    """Use one exact signed dispatch; no branch operation or general provider is invoked."""
    if (
        config.get("repository") != REPOSITORY
        or config.get("trusted_account") != TRUSTED
        or config.get("base_branch") != "main"
        or set(config.get("required_checks") or ()) != REQUIRED
        or checkpoint.get("repository") != REPOSITORY
        or checkpoint.get("next_step") != "confirm-base-sync-done"
        or not Path(working_dir).is_absolute()
    ):
        raise OutsideScope("terminal confirmation is limited to the trusted OneBase first-hop contract")
    number, head, intent = (checkpoint[k] for k in ("number", "to", "intent"))
    _need(
        all(type(key) is int and 0 < key < 2**63 for key in (number, intent))
        and all(
            isinstance(checkpoint.get(k), str) and re.fullmatch("[0-9a-f]{40}", checkpoint[k])
            for k in ("from", "base", "to", "tree")
        )
        and len({checkpoint["from"], checkpoint["base"], head}) == 3,
        "invalid checkpoint",
    )
    validate_attempt()
    prepared = _observe(gh, config, checkpoint, working_dir, verify_tree=True)
    # The expensive git and server proof is complete. Only now acquire the
    # final Source admission, then compare two full proof bundles across it.
    prepare_source()
    validate_attempt()
    try:
        final = _observe(gh, config, checkpoint, working_dir, verify_tree=False)
    except OutsideScope as exc:
        # A late Source may already have consumed its credit. Do not let a
        # general provider restart preparation in this same attempt.
        raise pp.PipelineError("confirmation scope changed after final Source admission") from exc
    _need(final == prepared, "full reviewed state changed across final Source admission")
    snap, identity, checks, done, body = final
    published = False
    if done is None:
        validate_attempt()
        result = gate()
        target = result.get("target") or {}
        _need(
            result.get("action") == "validated"
            and result.get("repository") == REPOSITORY
            and result.get("stage") == "merge"
            and target.get("stage") == "integration-merge-recovery"
            and target.get("number") == number
            and target.get("head") == head,
            "final scheduling gate refused",
        )
        validate_attempt()
        _need(
            claim_send(number, head, intent, hashlib.sha256(body.encode()).hexdigest()) is True,
            "previous confirmation send is uncertain; reconcile its existing receipt",
        )
        reply = pp.post_comment(gh, config, number, body)
        published = True
        validate_attempt()
        try:
            after, latest, new_checks, done, new_body = _observe(
                gh, config, checkpoint, working_dir, verify_tree=False
            )
        except OutsideScope as exc:
            raise pp.PipelineError("confirmation was sent; reconcile server readback") from exc
        _need(
            done
            and type(reply.get("id")) is int
            and reply["id"] == pp.comment_id(done)
            and reply["node_id"] == done["id"]
            and body == new_body
            and latest == identity
            and checks == new_checks,
            "confirmation receipt or PR state changed",
        )
        before_edges = snap["edges"]
        _need(
            [e for e in after["edges"] if e["node"]["id"] != done["id"]] == before_edges,
            "timeline changed alongside confirmation",
        )
    validate_attempt()
    return {
        "action": "completed",
        "stage": "merge-confirmation",
        "number": number,
        "head": head,
        "intent": intent,
        "done": pp.comment_id(done),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "published": published,
    }
