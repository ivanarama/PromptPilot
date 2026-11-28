"""Observe a published full Source for PR rework in an admitted ordinary FIX."""
import hashlib
import json
import time

from . import db, pipeline_insights as pi, project_pipeline as pp
from .source_graphql_cost import checker_miss as _checker_miss


def _pr_heads(report):
    """Both the queue membership and the checker must identify each reusable PR."""
    queue, = (q for q in report["queues"] if q["id"] == "fix")
    members = {}
    for item in queue["admission_items"]:
        if item["kind"] != "pr":
            continue
        number = item["number"]
        if type(number) is not int or number in members:
            raise ValueError("ambiguous FIX membership")
        members[number] = item
    heads = {}
    seen = set()
    for candidate in report["diagnostics"]["fix_candidates"]:
        stage, number, head = pp._fix_candidate_key(candidate)
        if stage != "fix-pr":
            continue
        if number in seen:
            raise ValueError("ambiguous FIX candidate")
        seen.add(number)
        member = members.get(number)
        if member is None:
            continue
        url = f"https://github.com/{report['repository']}/pull/{number}"
        labels = member["labels"]
        if (member["key"] == f"pr:{number}" and member["url"] == candidate["url"] == url
                and isinstance(labels, list) and "changes-requested" in labels
                and not {"ship", "hold", "manual"}.intersection(labels)):
            heads[str(number)] = head
    return heads


def _miss(code):
    # Existing launchers match the human-readable reason exactly before scanning.
    return {"state": "wait", "reason": "fresh FIX cache unavailable", "cache_miss_reason": code,
            "source_reused": False, "source_refreshed": False,
            "mutable_admission_granted": False, "product_mutation_authorized": False}


def _denied_priority_scan(raw_status, repository):
    """Only the persisted insights decision can distinguish a scan denial from a FIX gate."""
    if raw_status is None:
        return False
    event = json.loads(raw_status)
    status = event["status"]
    if (event["repository"] != repository or not isinstance(status, dict)
            or status.get("refresh_blocked") != "priority_waiter"):
        return False
    budget = status.get("github_budget")
    return (isinstance(budget, dict) and budget.get("budget_route") == "insights"
            and budget.get("state") == "priority_waiter" and budget.get("allowed") is False)


def _snapshot_time_miss(stamp, now):
    if type(stamp) not in (int, float) or not stamp >= 0:
        return "snapshot_invalid"
    if stamp > now:
        return "snapshot_from_future"
    if now - stamp >= pi._CACHE_TTL_SECONDS:
        return "snapshot_expired"
    return None


def observe(continuation, profile, *, allow_deferred_insights_read=False):
    guard = continuation.guard
    if continuation.queue_id != "fix" or guard["route"] != "skill":
        raise ValueError("cached FIX Source requires an ordinary FIX skill reservation")
    target_query = "SELECT 1 FROM pipeline_target_reservations WHERE task_id=?"
    with db._connect() as conn:
        if conn.execute(target_query, (guard["task_id"],)).fetchone():
            raise ValueError("cached FIX Source requires an ordinary initial election")
    profile_id = continuation.profile_id
    profile_hash = pi._profile_fingerprint(profile)
    cache_key = pi._cache_key(profile_id, profile_hash)
    status_key = db._pipeline_refresh_status_key(profile_id)
    profile_key = pi._published_profile_key(profile_id)
    revision_key = pi._published_profile_revision_key(profile_id)
    keys = (status_key, profile_key, revision_key)
    initial = db.get_settings_snapshot([cache_key, *keys])
    if cache_key not in initial:
        if initial.get(profile_key) not in (None, profile_hash):
            return _miss("profile_changed")
        return _miss("snapshot_missing")
    try:
        payload = json.loads(initial[cache_key])
        stamp, epoch, revision = (payload[k] for k in ("generated_at", "epoch", "revision"))
        report = payload["result"]
        if (type(payload["version"]) is not int or payload["version"] != pi._CACHE_SCHEMA_VERSION
                or report["generated_at"] != stamp or type(epoch) is not int or epoch < 0
                or type(revision) is not int or revision <= 0):
            return _miss("snapshot_invalid")
        if (payload["profile_id"] != profile_id or payload["profile_hash"] != profile_hash
                or payload["repository"] != profile["repository"] or initial[profile_key] != profile_hash
                or report["profile_id"] != profile_id or report["repository"] != profile["repository"]):
            return _miss("profile_changed")
        if initial[revision_key] != str(revision):
            return _miss("publication_changed")
        time_miss = _snapshot_time_miss(stamp, time.time())
        if time_miss:
            return _miss(time_miss)
        if (not pi._cache_matches_profile(report, profile_id, profile)
                or not all(q["membership_complete"] is True for q in report["queues"])):
            return _miss("queue_incomplete")
        cache = pi._cache_metadata("durable", stamp, epoch, revision, epoch, profile_hash)
        report = pi._with_persisted_refresh_status(dict(report, cache=cache), profile_id, profile)
        blocked = report["cache"].get("refresh_blocked")
        if blocked == "checker_state_unavailable":
            raise RuntimeError("cannot verify the durable FIX checker status")
        if (allow_deferred_insights_read and blocked == "priority_waiter"
                and _denied_priority_scan(initial.get(status_key), profile["repository"])):
            # This attempt already owns its skill reservation. Check its existing
            # publication without asking for the separately deferred scan.
            snapshot = dict(report, cache=cache)
            cache = dict(cache, refresh_blocked=blocked, refresh_budget={
                "budget_route": "insights", "state": blocked, "allowed": False})
        elif blocked:
            # Never pass arbitrary contents of persisted status into the journal.
            if blocked == "low":
                return _miss("budget_low")
            if blocked in ("checker_failed", "scan_in_progress", "budget_in_flight", "priority_waiter",
                           "fairness_waiter", "rate_snapshot_stale"):
                return _miss(blocked)
            return _miss("refresh_blocked")
        else:
            snapshot = report
        state = pi._required_snapshot_state(snapshot, profile, "fix")
        if state is not None:
            return _miss("checker_failed" if state in ("checker failed", "checker unavailable") else "snapshot_invalid")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return _miss("snapshot_invalid")
    try:
        checker_ttl = min(pi._CACHE_TTL_SECONDS, int(profile["health_check"].get("cache_seconds", 1800)))
        checker_miss = _checker_miss(profile, report, time.time(), checker_ttl)
        if checker_miss:
            return _miss(checker_miss)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return _miss("checker_proof_invalid")
    try:
        heads = _pr_heads(report)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, pp.PipelineError):
        return _miss("candidate_invalid")
    if not heads:
        return _miss("no_eligible_pr")
    # A newer checker refusal, publication, session or reservation can arrive
    # while we verify the durable data and the pinned checker on disk.
    continuation.ledger()
    with db._connect() as conn:
        conn.execute("BEGIN")
        if (not db._pipeline_source_refresh_guard_matches(conn, guard)
                or conn.execute(target_query, (guard["task_id"],)).fetchone()):
            raise ValueError("Native FIX attempt or admission changed during cache observation")
        current = dict(conn.execute("SELECT key,value FROM settings WHERE key IN (?,?,?)", keys))
        observed_at = time.time()
        if (any(current.get(k) != initial.get(k) for k in keys)
                or not conn.execute("SELECT 1 FROM settings WHERE key=? AND value=?",
                                    (cache_key, initial[cache_key])).fetchone()):
            return _miss("publication_changed")
        time_miss = _snapshot_time_miss(stamp, observed_at)
        if time_miss:
            return _miss(time_miss)
        if not 0 <= observed_at - report["diagnostics_generated_at"] < checker_ttl:
            return _miss("checker_expired")
        if not db._pipeline_cache_guard_matches(conn, pi._wake_cache_guard(profile_id, profile, cache)):
            # A complete cached publication can remain visible after invalidation.
            row = conn.execute("SELECT value FROM settings WHERE key=?", (pi._CACHE_EPOCH_KEY,)).fetchone()
            if (row[0] if row else "0") != str(epoch):
                return _miss("snapshot_invalidated")
            return _miss("publication_changed")
    binding = [guard[k] for k in ("task_id", "started_at", "session_id", "herdr_pane", "token",
                                  "execution_sha256", "series_id", "priority")]
    return {"state": "reused", "source_refreshed": False, "source_reused": True,
            "profile_id": profile_id, "queue_id": "fix", "admitted_route": "skill",
            "task_id": guard["task_id"], "task_started_at": guard["started_at"], "native_session_bound": True,
            "existing_execution_reservation_reused": True, "existing_skill_reservation_reused": True,
            "attempt_binding_sha256": hashlib.sha256(json.dumps(binding, separators=(",", ":")).encode()).hexdigest(),
            "observed_at": observed_at, "cache": cache, "snapshot_token": cache["token"], "fix_pr_heads": heads,
            "scope": "Native-source-observation-only", "mutable_admission_granted": False,
            "product_mutation_authorized": False}
