"""Observe a published full Source for PR rework in an admitted ordinary FIX."""
import hashlib
import json
import time

from . import db, pipeline_insights as pi, project_pipeline as pp
from .source_graphql_cost import digest, health_contract, validate_receipt


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


def _validate_checker(profile, report, observed_at, ttl):
    diagnostics = report["diagnostics"]
    stamp = report["diagnostics_generated_at"]
    if (type(stamp) not in (int, float) or not 0 <= observed_at - stamp < ttl
            or type(diagnostics.get("exit_code")) is not int or diagnostics["exit_code"] != 0):
        raise ValueError("fresh successful FIX checker required")
    usage = report["source_search_usage"]
    if usage["successful_observation_cost_proved"] is not True or usage["uncertain_cost"] is not False:
        raise ValueError("unproved Source publication")
    contract = health_contract(profile)
    receipt = diagnostics["source_graphql_usage"]
    validate_receipt(receipt, contract, nonce=receipt["nonce"], allowance=receipt["allowance"])
    proof = {"executable_sha256": contract["executable_sha256"], "receipt_sha256": digest(receipt)}
    if (diagnostics["source_graphql_cost_proof"] != proof
            or any(usage["health_cost_provenance"][key] != value for key, value in proof.items())):
        raise ValueError("checker proof differs from its published provenance")


def observe(continuation, profile):
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
    wait = {"state": "wait", "reason": "fresh FIX cache unavailable", "source_reused": False,
            "source_refreshed": False, "mutable_admission_granted": False, "product_mutation_authorized": False}
    try:
        payload = json.loads(initial[cache_key])
        stamp, epoch, revision = (payload[k] for k in ("generated_at", "epoch", "revision"))
        report = payload["result"]
        if (type(payload["version"]) is not int or payload["version"] != pi._CACHE_SCHEMA_VERSION
                or payload["profile_id"] != profile_id or payload["profile_hash"] != profile_hash
                or payload["repository"] != profile["repository"] or report["generated_at"] != stamp
                or initial[profile_key] != profile_hash or initial[revision_key] != str(revision)
                or type(epoch) is not int or epoch < 0 or type(revision) is not int or revision <= 0
                or type(stamp) not in (int, float) or not 0 <= time.time() - stamp < pi._CACHE_TTL_SECONDS
                or not pi._cache_matches_profile(report, profile_id, profile)
                or not all(q["membership_complete"] is True for q in report["queues"])):
            return wait
        cache = pi._cache_metadata("durable", stamp, epoch, revision, epoch, profile_hash)
        report = pi._with_persisted_refresh_status(dict(report, cache=cache), profile_id, profile)
        if report.get("cache", {}).get("refresh_blocked") == "checker_state_unavailable":
            raise RuntimeError("cannot verify the durable FIX checker status")
        if pi._required_snapshot_state(report, profile, "fix") is not None:
            return wait
        checker_ttl = min(pi._CACHE_TTL_SECONDS, int(profile["health_check"].get("cache_seconds", 1800)))
        _validate_checker(profile, report, time.time(), checker_ttl)
        heads = _pr_heads(report)
        if not heads:
            return wait
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, pp.PipelineError):
        return wait
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
                                    (cache_key, initial[cache_key])).fetchone()
                or not 0 <= observed_at - stamp < pi._CACHE_TTL_SECONDS
                or not 0 <= observed_at - report["diagnostics_generated_at"] < checker_ttl
                or not db._pipeline_cache_guard_matches(conn, pi._wake_cache_guard(profile_id, profile, cache))):
            return wait
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
