"""Durable observations for the ordinary, signed MERGE fallback.

A checkpoint records the GitHub identities the project procedure observed. It
never authorizes a GitHub write, consumes a gate, or declares a PR completed.
"""
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from . import fallback_handoff, project_pipeline as pp


RECOVERY = "integration-merge-recovery"
NEXT_STEP = "confirm-base-sync-done"
SHA = re.compile(r"[0-9a-f]{40}")


def _key(profile, repository, number, head, config):
    return "pipeline_merge_checkpoint:v1:" + pp.digest([profile, repository, number, head, config])


def _facts(profile, lease, number, old, base, head, tree, intent):
    if (not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in (old, base, head, tree))
            or len({old, base, head}) != 3 or type(number) is not int or not 0 < number < 2**63
            or type(intent) is not int or not 0 < intent < 2**63):
        raise ValueError("checkpoint requires exact commit/tree identities and positive PR and comment IDs")
    return {"protocol": "merge-checkpoint-v1", "profile": profile,
            "repository": lease["repository"], "config_sha256": lease["config_sha256"],
            "number": number, "from": old, "base": base, "to": head, "tree": tree,
            "intent": intent, "next_step": NEXT_STEP}


def _load(conn, profile, lease, number, head):
    key = _key(profile, lease["repository"], number, head, lease["config_sha256"])
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    saved = json.loads(row["value"])
    if not isinstance(saved, dict) or saved != _facts(
            profile, lease, number, saved.get("from"), saved.get("base"), head,
            saved.get("tree"), saved.get("intent")):
        raise ValueError("saved MERGE checkpoint is inconsistent")
    return saved


def recovery_hint(profile, lease):
    """Called only after verifying the newly elected project fallback lease."""
    from . import db

    target = lease["target"]
    if target["stage"] != RECOVERY or lease.get("target_reservation"):
        return None
    with db._connect() as conn:
        try:
            return _load(conn, profile, lease, target["number"], target["head"])
        except (ValueError, TypeError):
            # An invalid local hint cannot hold the queue or skip live recovery.
            return None


def agrees_with_source(data, lease, checkpoint):
    """Compare the published checker graph; all project proofs remain live."""
    health = data.get("diagnostics") or {}
    try:
        fallback_handoff.health_gate(health, "merge", lease["target"], election=True)
    except (pp.PipelineError, ValueError, TypeError):
        return False
    candidate = health["integration_owner"].get("base_sync_candidate")
    return (data.get("repository") == checkpoint["repository"]
            and isinstance(candidate, dict) and candidate.get("source") == "head_parents"
            and all(candidate.get(k) == checkpoint[k] for k in ("from", "base", "to")))


def bind_election(task, token):
    """Remember which ordinary signed gate Native issued for this exact attempt."""
    from . import db

    started = getattr(task, "started_at", None)
    if not hasattr(started, "isoformat") or type(getattr(task, "id", None)) is not int:
        return
    with db._connect(immediate=True) as conn:
        conn.execute("UPDATE tasks SET merge_checkpoint_gate=? WHERE id=? AND status='running' AND started_at=?",
                     (pp.digest([started.isoformat(), token]), task.id, started.isoformat()))


def _gate_file(task_id, path):
    from . import db

    directory = Path(db.DB_PATH).resolve().parent / "pipelinectl-state" / "leases"
    path = Path(path)
    if (path.parent != directory or path.is_symlink() or not path.is_file()
            or not re.fullmatch(rf"task-{task_id}-gate-[0-9a-f]{{20}}\.lease", path.name)
            or path.stat().st_size > 131072):
        raise ValueError("expected the active task's scheduler-issued gate lease file")
    token = path.read_text(encoding="ascii")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    if path.name != f"task-{task_id}-gate-{digest[:20]}.lease":
        raise ValueError("gate lease file identity differs")
    lease = pp.decode_signed_lease(token)
    fallback_handoff.validate_lease(lease, "merge")
    return lease, token


def _apply(args, env):
    raw = env.get("PP_TASK_ID", "")
    marker = env.get("PP_TASK_STARTED_AT")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", raw) or int(raw) >= 2**63 or not marker:
        raise ValueError("MERGE checkpoint requires worker-issued task and start markers")
    from . import db, pipeline_insights as pi

    task_id = int(raw)
    task = db.get_task(task_id)
    matched = pi._matching_queue(task) if task is not None else None
    if matched is None or matched[0] != args.profile or matched[2]["id"] != "merge":
        raise ValueError("profile or queue does not own this MERGE task")
    profile = matched[1]
    policy = pi._github_budget_policy(profile)
    if not policy or not policy.get("costs"):
        raise ValueError("MERGE checkpoint requires costed Native admission")
    lease, token = _gate_file(task_id, args.lease_file)
    target = lease["target"]
    if (lease.get("target_reservation") or lease["repository"] != profile.get("repository")
            or target["number"] != args.number or not SHA.fullmatch(args.to)):
        raise ValueError("MERGE checkpoint target differs from the signed ordinary election")
    reading = args.operation == "read"
    recovery = target["stage"] == RECOVERY and target["head"] == args.to
    # Any validated MERGE stage can update its elected old HEAD if the base advances.
    # Only recovery elected at the new HEAD can read or reconstruct that transition.
    if not recovery and (reading or target["head"] != args.old):
        raise ValueError("operation is outside the signed MERGE phase or HEAD")
    with db._connect(immediate=not reading) as conn:
        if reading:
            conn.execute("BEGIN")
        guard = db._pipeline_source_refresh_state(
            conn, policy["lease_scope"], task_id, args.profile, "merge", policy["costs"])["guard"]
        row = conn.execute("SELECT merge_checkpoint_gate FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (guard["route"] != "fallback_targeted" or guard["started_at"] != marker
                or row["merge_checkpoint_gate"] != pp.digest([marker, token])):
            raise ValueError("checkpoint requires this attempt's admitted gate lease")
        saved = _load(conn, args.profile, lease, args.number, args.to)
        if reading:
            state = "found" if saved else "missing"
        else:
            facts = _facts(args.profile, lease, args.number, args.old, args.base, args.to,
                           args.tree, args.intent)
            if saved is not None and saved != facts:
                raise ValueError("conflicting checkpoint for the same elected HEAD")
            if saved is None:
                key = _key(args.profile, lease["repository"], args.number, args.to, lease["config_sha256"])
                conn.execute("INSERT INTO settings (key,value) VALUES (?,?)", (key, json.dumps(facts)))
            saved, state = facts, "recorded"
    return {"state": state, "checkpoint": saved, "product_mutation_authorized": False}


def add_arguments(parser):
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("record", "read"):
        op = operations.add_parser(name)
        op.add_argument("--profile", required=True)
        op.add_argument("--number", required=True, type=int)
        op.add_argument("--to", required=True)
        op.add_argument("--lease-file", required=True)
        if name == "record":
            op.add_argument("--from", dest="old", required=True)
            op.add_argument("--base", required=True)
            op.add_argument("--tree", required=True)
            op.add_argument("--intent", required=True, type=int)


def execute(args):
    try:
        result = _apply(args, os.environ)
    except (pp.PipelineError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        result = {"state": "refused", "reason": str(exc), "product_mutation_authorized": False}
    print(json.dumps(result, ensure_ascii=False))
    return int(result["state"] == "refused")
