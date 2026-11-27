"""Durable observations for the ordinary, signed MERGE fallback.

A checkpoint records the GitHub identities the project procedure observed. It
never authorizes a GitHub write, consumes a gate, or declares a PR completed.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
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


def confirmation_source(data, profile_id, profile, admission):
    """A busy sampler can coexist with this election's still-current publication."""
    from . import db, pipeline_insights as pi

    cache = data.get("cache") or {}
    if cache.get("refresh_blocked") != "scan_in_progress":
        return data
    lease = admission.get("_lease")
    if (admission.get("allowed") is not True or lease is None
            or getattr(pi._scan_lease_context, "lease", None) is not lease
            or lease.scope != pi._GITHUB_SCAN_LEASE_SCOPE or lease.lost or lease.unavailable
            or type(lease.status_revision) is not int or lease.status_revision <= 0):
        return data
    guard = pi._wake_cache_guard(profile_id, profile, cache)
    if not guard or not 0 <= time.time() - guard["generated_at"] < pi._CACHE_TTL_SECONDS:
        return data
    try:
        with db._connect() as conn:
            conn.execute("BEGIN")
            if (not db._pipeline_scan_lease_guard_matches(conn, lease.guard)
                    or not db._pipeline_cache_guard_matches(conn, guard)):
                return data
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (db._pipeline_refresh_status_key(profile_id),)).fetchone()
            if not row:
                return data
            event = json.loads(row[0])
            status = event["status"]
            budget = status["github_budget"]
            # Successful acquisitions and busy observations receive unique,
            # ordered revisions in the same SQLite writer transaction. A busy
            # decision newer than this still-owned lease observed this owner.
            if (event["version"] != 1 or event["repository"] != profile["repository"]
                    or type(event["revision"]) is not int or event["revision"] <= lease.status_revision
                    or status["refresh_blocked"] != "scan_in_progress"
                    or status["refresh_deferred_until"] != cache.get("refresh_deferred_until")
                    or budget["state"] != "scan_in_progress" or budget["allowed"] is not False
                    or budget["budget_route"] != "insights" or budget["lease_scope"] != lease.scope
                    or pi._source_checker_failure_revision(event, profile) >
                       pi._source_checker_verified_revision(data, event)):
                return data
    except (OSError, sqlite3.Error, KeyError, AttributeError, TypeError, ValueError):
        return data
    # The caller still checks complete/current queue, checker, owner and graph.
    # Leave the shared cache and the dashboard's explanation unchanged.
    result_cache = dict(cache)
    result_cache.pop("refresh_blocked")
    return dict(data, cache=result_cache)


def election_source_state(data, profile_id, profile, lease):
    """Require a fresh local publication matching the ordinary signed election."""
    from . import pipeline_insights as pi

    state = pi._required_snapshot_state(data, profile, "merge")
    if state:
        return state
    token = data["cache"].get("token")
    if (data.get("profile_id") != profile_id or data.get("repository") != lease["repository"]
            or lease["repository"] != profile.get("repository") or not isinstance(token, dict)
            or token.get("profile_hash") != pi._profile_fingerprint(profile)
            or type(token.get("revision")) is not int or type(token.get("epoch")) is not int):
        return "MERGE Source has no matching publication token"
    health = data.get("diagnostics")
    if not isinstance(health, dict) or health.get("errors") or health.get("checker_failed"):
        return "MERGE Source checker unavailable"
    try:
        fallback_handoff.health_gate(health, "merge", lease["target"], election=True)
    except pp.PipelineError as exc:
        return str(exc)
    return None


def _wait_key(task):
    # One bounded observation per ordinary series, never a cross-task gate.
    return f"pipeline_merge_source_wait:v1:{task.series_id}"


def remember_source_wait(task, profile_hash, checkpoint, *, busy_until=None):
    from . import db
    from .task_identity import task_execution_fingerprint

    if not getattr(task, "series_id", None) or not getattr(task, "started_at", None):
        return
    marker = {"task": task.id, "execution": task_execution_fingerprint(task),
              "profile_hash": profile_hash, "checkpoint": checkpoint, "busy_until": busy_until}
    with db._connect(immediate=True) as conn:
        conn.execute("""INSERT OR REPLACE INTO settings (key,value)
                        SELECT ?,? FROM tasks WHERE id=? AND series_id=?
                        AND status='running' AND started_at=?""",
                     (_wait_key(task), json.dumps(marker), task.id, task.series_id,
                      task.started_at.isoformat()))


def _live_claim(task):
    from . import db

    with db._connect() as conn:
        return conn.execute("""SELECT 1 FROM tasks t JOIN task_series s ON s.id=t.series_id
            WHERE t.id=? AND s.id=? AND t.status='running' AND t.started_at=?
              AND s.paused=0 AND s.ended_at IS NULL AND NOT EXISTS (
                SELECT 1 FROM settings WHERE key IN ('worker_paused',?) AND value='1')""",
            (task.id, task.series_id, task.started_at.isoformat(), f"cancel_task:{task.id}")).fetchone() is not None


def _take_source_wait(task, profile_id, profile_hash):
    from . import db
    from .task_identity import task_execution_fingerprint

    if not getattr(task, "series_id", None) or not getattr(task, "started_at", None):
        return None
    key = _wait_key(task)
    raw = db.get_setting(key)
    if not raw:
        return None
    try:
        marker = json.loads(raw)
        if (marker["task"] != task.id or marker["profile_hash"] != profile_hash
                or marker["execution"] != task_execution_fingerprint(task)):
            return None
        hint = marker["checkpoint"]
        with db._connect(immediate=True) as conn:
            # Consume this exact marker before use. Only a contended Source scan
            # can put it back, with the same bounded deadline.
            taken = conn.execute("""DELETE FROM settings WHERE key=? AND value=? AND EXISTS (
                SELECT 1 FROM tasks WHERE id=? AND status='running' AND started_at=?)""",
                (key, raw, task.id, task.started_at.isoformat())).rowcount
            if taken and hint == _load(conn, profile_id, hint, hint["number"], hint["to"]):
                return marker
    except (KeyError, TypeError, ValueError):
        return None
    return None


def prepare_source(task, profile_id, profile):
    """Prepare the next signed election outside its account-wide preflight lease."""
    from . import db, pipeline_insights as pi
    from .pipeline_sampling import refresh_ahead_seconds

    fingerprint = pi._profile_fingerprint(profile)
    marker = _take_source_wait(task, profile_id, fingerprint)
    if marker is None:
        return None
    checkpoint = marker["checkpoint"]
    busy_until = marker.get("busy_until")
    if checkpoint["repository"] != profile.get("repository"):
        return None
    if busy_until is not None and (type(busy_until) not in (int, float) or busy_until <= time.time()):
        return None
    if not _live_claim(task):
        return pi._required_snapshot_defer(profile_id, "merge", "MERGE task is no longer active")
    series = db.list_series()
    current = pi.read_cached(profile_id, series)
    target = {"stage": RECOVERY, "number": checkpoint["number"], "head": checkpoint["to"]}
    lead = refresh_ahead_seconds(profile)
    state = pi._required_snapshot_state(current, profile, "merge")
    if state is None and (not agrees_with_source(current, {"target": target}, checkpoint)
                          or pi._snapshot_has_remaining_ttl(current, lead)):
        return None
    # An unavailable/moving scanner yields its real admission deadline; no
    # provider, final-Source credit, or second lease is held during this scan.
    try:
        data = pi.analyze(profile_id, series, use_cache=False, refresh_diagnostics=True,
                          reuse_fresh=True, snapshot_queue_id="merge", minimum_remaining_seconds=lead)
        state = pi._required_snapshot_state(data, profile, "merge")
    except (KeyError, OSError, ValueError, RuntimeError, sqlite3.Error):
        return pi._required_snapshot_defer(profile_id, "merge", "MERGE Source preparation unavailable")
    if not _live_claim(task) or pi._profile_fingerprint(pi._profiles().get(profile_id) or {}) != fingerprint:
        return pi._required_snapshot_defer(profile_id, "merge", "MERGE task or profile changed")
    if state is None:
        return None  # signed next owns the target choice, including a new HEAD or an empty queue
    cache = data.get("cache") or {}
    blocked = cache.get("refresh_blocked")
    if blocked == "scan_in_progress":
        # The sampler owns the shared scan lease. Let other work use the slot
        # and keep preparing this same election across short, genuine collisions.
        limit = busy_until if busy_until is not None else time.time() + pi._CACHE_TTL_SECONDS
        remember_source_wait(task, fingerprint, checkpoint, busy_until=limit)
    if blocked and cache.get("refresh_deferred_until"):
        refusal = dict(data.get("github_budget") or {}, state=blocked, allowed=False,
                       reason=cache.get("refresh_blocked_reason") or state,
                       defer_until=cache["refresh_deferred_until"])
        return pi._budget_defer_route(refusal, profile_id, profile, {"id": "merge"})
    return pi._required_snapshot_defer(profile_id, "merge", state)


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


def _passive_source_state(conn, lease, profile_id, profile, data):
    from . import pipeline_insights as pi, pipeline_item_holds as holds

    target = lease["target"]
    if not isinstance(data, dict):
        raise ValueError("passive Source read unavailable")
    health = data.get("diagnostics") or {}
    cache = data.get("cache") or {}
    token = cache.get("token") or {}
    if not isinstance(token, dict) or not isinstance(health, dict):
        raise ValueError("malformed cached MERGE observation")
    owner = health.get("integration_owner") or {}
    if (not isinstance(owner, dict) or target["stage"] not in {"merge", RECOVERY}
            or owner.get("base_sync_candidate") is not None
            or _load(conn, profile_id, lease, target["number"], target["head"]) is not None
            or conn.execute("SELECT 1 FROM settings WHERE key=?",
                            (pending_key(lease["repository"], target),)).fetchone()):
        raise ValueError("post-update or unresolved MERGE send requires canonical reconciliation")
    expected = dict(target)
    if owner.get("stage") == RECOVERY:
        expected["stage"] = RECOVERY
    fallback_handoff.health_gate(health, "merge", expected, election=True)
    queues = [q for q in data.get("queues", []) if q.get("id") == "merge"]
    if (data.get("profile_id") != profile_id or data.get("repository") != lease["repository"]
            or health.get("errors") or health.get("checker_failed")
            or cache.get("complete") is not True or cache.get("available") is not True
            or len(queues) != 1 or queues[0].get("membership_complete") is not True
            or token.get("profile_hash") != pi._profile_fingerprint(profile)
            or type(token.get("revision")) is not int or type(token.get("epoch")) is not int):
        raise ValueError("passive MERGE observation requires a complete same-profile Source")
    blocked = cache.get("refresh_blocked")
    if blocked and blocked not in holds._SOURCE_WAITS:
        raise ValueError("unrecognized Source refusal")
    if blocked:
        state = blocked
    elif cache.get("invalidated") is True:
        state = "invalidated"
    elif cache.get("stale") is True:
        state = "stale"
    elif pi._required_snapshot_state(data, profile, "merge") is None:
        state = None
    else:
        raise ValueError("unclear Source state")

    return state, token


def _pre_send_wait(conn, task, lease, gate, profile_id, profile, data):
    from . import pipeline_item_holds as holds

    target = lease["target"]
    # Collect this election's exact records, never a successor's baseline.
    records = {}
    for key in (holds._attempt_key(task.id), holds.BASELINE + str(task.series_id)):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            baseline = json.loads(row["value"])
            proof = baseline.get("merge_source_election") or {}
            if (baseline.get("task_id") == task.id and proof.get("started_at") == task.started_at.isoformat()
                    and proof.get("gate") == gate and proof.get("number") == target["number"]
                    and proof.get("head") == target["head"]):
                records[key] = baseline
    if not records:
        raise ValueError("ordinary MERGE election baseline is absent")
    state, error = None, None
    try:
        state, token = _passive_source_state(conn, lease, profile_id, profile, data)
        for baseline in records.values():
            initial = baseline["merge_source_election"].get("source_token") or {}
            if (initial.get("profile_hash") != token["profile_hash"]
                    or type(initial.get("revision")) is not int or initial["revision"] > token["revision"]
                    or type(initial.get("epoch")) is not int or initial["epoch"] > token["epoch"]):
                raise ValueError("Source is outside this ordinary election")
    except (pp.PipelineError, ValueError, TypeError, KeyError, AttributeError) as exc:
        state, error = None, str(exc)
    # An authenticated later refusal also invalidates an earlier wait receipt.
    for key, baseline in records.items():
        baseline["merge_source_election"]["wait"] = state
        conn.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(baseline), key))
    if error is not None:
        return {"state": "refused", "reason": error, "product_mutation_authorized": False}
    result = {"state": "wait" if state else "observed", "reason": state,
              "number": target["number"], "head": target["head"], "product_mutation_authorized": False}
    if state:
        result["result_line"] = f"ИТОГ: ПУСТО — нет допуска: {state}"
    return result


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
    passive = args.operation in {"source-wait", "before-intent-wait"}
    if (lease.get("target_reservation") or lease["repository"] != profile.get("repository")
            or target["number"] != args.number or not SHA.fullmatch(args.old if passive else args.to)):
        raise ValueError("MERGE checkpoint target differs from the signed ordinary election")
    data = None
    if passive:
        try:
            data = pi.read_cached(args.profile, db.list_series())
        except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, RuntimeError):
            # Still invalidate any previous observation after fencing the exact attempt.
            pass
    reading = args.operation == "read"
    recovery = not passive and target["stage"] == RECOVERY and target["head"] == args.to
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
        if args.operation == "before-intent-wait":
            from . import merge_pre_intent_wait
            if not merge_pre_intent_wait.enabled(matched[2], profile):
                raise ValueError("pre-intent Source waiting is not enabled for this queue")
            return merge_pre_intent_wait.observe(conn, task, lease, row["merge_checkpoint_gate"],
                                                 args.profile, profile, data, args.base)
        if passive:
            return _pre_send_wait(conn, task, lease, row["merge_checkpoint_gate"], args.profile, profile, data)
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
            transferred = _handoff(conn, task, lease, row["merge_checkpoint_gate"], facts)
    result = {"state": state, "checkpoint": saved, "product_mutation_authorized": False}
    if not reading and transferred:
        result.update(next_step="native-confirmation", result_line="ИТОГ: ПУСТО — ожидание подтверждения обновления ветки")
    return result


def _handoff(conn, task, lease, gate, facts):
    """Transfer only the managed ordinary attempt. A new native election re-proves every fact."""
    from . import pipeline_item_holds as holds
    target = lease["target"]
    transferred = False
    for key in (holds._attempt_key(task.id), holds.BASELINE + str(task.series_id)):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            continue
        measured = json.loads(row["value"])
        election = measured.get("merge_source_election") or {}
        if (measured.get("task_id") != task.id or election.get("native_done") is not True
                or election.get("started_at") != task.started_at.isoformat() or election.get("gate") != gate
                or (election.get("number"), election.get("head")) != (target["number"], target["head"])):
            continue
        election["handoff"] = facts
        conn.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(measured), key))
        transferred = True
    return transferred


def handed_off(conn, series_id, task_id, raw):
    """Only this attempt's recorded transfer may skip creation of a failure hold."""
    try:
        measured = json.loads(raw)
        election = measured["merge_source_election"]
        checkpoint = election["handoff"]
        target = measured["review_target"]
        task = conn.execute("SELECT series_id,started_at,merge_checkpoint_gate FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(task and task["series_id"] == series_id and measured["task_id"] == task_id
                    and election["native_done"] is True and task["started_at"] == election["started_at"]
                    and task["merge_checkpoint_gate"] and task["merge_checkpoint_gate"] == election["gate"]
                    and target == {"stage": "merge", "number": checkpoint["number"], "head": election["head"]}
                    and election["number"] == checkpoint["number"] and election["head"] in (checkpoint["from"], checkpoint["to"])
                    and _load(conn, checkpoint["profile"], checkpoint, checkpoint["number"], checkpoint["to"]) == checkpoint)
    except (ValueError, TypeError, KeyError):
        return False


def add_arguments(parser):
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("record", "read", "source-wait", "before-intent-wait"):
        op = operations.add_parser(name)
        op.add_argument("--profile", required=True)
        op.add_argument("--number", required=True, type=int)
        op.add_argument("--lease-file", required=True)
        if name in {"record", "read"}:
            op.add_argument("--to", required=True)
        if name != "read":
            op.add_argument("--from", dest="old", required=True)
        if name in {"record", "before-intent-wait"}:
            op.add_argument("--base", required=True)
        if name == "record":
            op.add_argument("--tree", required=True)
            op.add_argument("--intent", required=True, type=int)


def execute(args):
    try:
        result = _apply(args, os.environ)
        if args.operation == "before-intent-wait" and result["state"] == "wait":
            from . import merge_pre_intent_wait
            merge_pre_intent_wait.catch_up(args.profile)
    except (pp.PipelineError, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        result = {"state": "refused", "reason": str(exc), "product_mutation_authorized": False}
    print(json.dumps(result, ensure_ascii=False))
    return int(result["state"] == "refused")


def pending_key(repository, target=None):
    prefix = "pipeline_merge_confirmation:pending:v1:" + pp.digest(repository) + ":"
    return prefix + pp.digest([target["number"], target["head"]]) if target else prefix


def blocks_fallback(repository, target):
    from . import db

    if target:
        return db.get_setting(pending_key(repository, target)) is not None
    prefix = pending_key(repository)
    with db._connect() as conn:
        return conn.execute("SELECT 1 FROM settings WHERE key>? AND key<? LIMIT 1",
                            (prefix, prefix + "g")).fetchone() is not None
