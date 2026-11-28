"""Read the current budget for an existing priority-one FIX skill reservation."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
import secrets
import sqlite3
import time

class _Refused(ValueError):
    """Only fixed refusal codes are emitted from this exception."""


def _require(test, reason):
    if not test:
        raise _Refused(reason)


def _digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@contextmanager
def _passive_native():
    from .config import DB_OBSERVATION_ONLY

    marker = DB_OBSERVATION_ONLY.set(True)
    try:
        from . import db, pipeline_insights
        yield db, pipeline_insights
    finally:
        DB_OBSERVATION_ONLY.reset(marker)


def _policy(pi, profiles, profile_id):
    profile = profiles[profile_id]
    queues = [q for q in profile["queues"] if q["id"] == "fix"]
    _require(len(queues) == 1 and pi._queue_replica_count(queues[0]) == 1, "ordinary_fix_queue_required")
    policy = pi._github_budget_policy(profile)
    _require(policy and policy.get("costs"), "costed_profile_required")
    shared = pi._with_shared_budget_floor(policy, profiles)
    skill = pi._budget_policy_for_route(shared, "skill")
    return pi._with_execution_snapshot_floor(pi._budget_policy_for_admission_priority(skill, 1))


def policy_sha256(profiles, profile_id="onebase"):
    """Compute the installation pin including every profile's shared account floor."""
    with _passive_native() as (_, pi):
        return _digest(_policy(pi, profiles, profile_id))


def observe(profile_id, series_id, expected_policy, environ=None):
    env = os.environ if environ is None else environ
    result = dict(state="refused", ready=False, read_only=True, granted=False,
                  budget_observation_scope="running-budget-policy-v4", proof_kind="running",
                  source_snapshot_observed=False, requires_independent_native_source_gate=True,
                  native_source_admission_granted=False, native_admission_granted=False,
                  mutable_admission_granted=False, caller_process_ownership_proven=False,
                  profile_id=profile_id, queue_id="fix", series_id=series_id, priority=1)
    try:
        raw_id = env.get("PP_TASK_ID", "")
        _require(isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdecimal()
                 and len(raw_id) <= 19 and 0 < int(raw_id) < 2**63, "worker_task_environment_missing")
        _require(type(series_id) is int and 0 < series_id < 2**63, "resolver_series_priority_required")
        task_id = int(raw_id)
        with _passive_native() as (db, pi):
            profiles = pi._profiles()
            policy = _policy(pi, profiles, profile_id)
            _require(_digest(policy) == expected_policy, "effective_budget_policy_changed")
            scope = policy["lease_scope"]
            result.update(task_id=task_id, profile_hash=pi._profile_fingerprint(profiles[profile_id]),
                          effective_policy_sha256=expected_policy)
            # The rate, all live promises (including our own) and their revision
            # come from the same database snapshot. No scan or reservation occurs.
            uri = db.DB_PATH.resolve(strict=True).as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                ledger = db._pipeline_source_refresh_state(conn, scope, task_id, profile_id, "fix", policy["costs"])
                guard = ledger["guard"]
                _require(guard["series_id"] == series_id and guard["priority"] == 1, "running_task_identity_changed")
                _require("PP_TASK_STARTED_AT" not in env or guard["started_at"] == env["PP_TASK_STARTED_AT"],
                         "worker_start_environment_changed")
                _require(guard["route"] == "skill" and guard["cost"] == policy["requested_cost"]
                         and ledger["final_source_cost"] is None, "own_reservation_binding_changed")
                now = time.time()
                result.update(started_at=guard["started_at"], generated_at=now,
                              task_identity_source="current-running-task-row")
                rate = db._read_pipeline_github_rate_snapshot(conn, scope)
                _require(rate and 0 <= now - rate["observed_at"] <= 60, "rate_snapshot_stale")
                # Apply the same accounting invariant as both live GitHub response
                # readers before trusting an existing SQLite publication.
                _require(all(item["used"] + item["remaining"] <= item["limit"]
                             for item in rate["limits"].values()), "rate_snapshot_invalid")
                _require(all(item["reset"] > now for item in rate["limits"].values()), "rate_window_expired")
                policy["requested_cost"] = dict.fromkeys(policy["minimum_remaining"], 0)
                decision = pi._evaluate_github_budget(policy, rate["limits"], now=now,
                    status_revision=ledger["revision"], reserved_other=ledger["totals"],
                    active_reservations=ledger["count"])
                reservations = db._load_pipeline_budget_reservations(conn, db._pipeline_github_budget_reservation_key(scope))
                result.update(state="ready" if decision["allowed"] else "wait", ready=decision["allowed"],
                              reason=decision["reason"], decision=decision, defer_until=decision.get("defer_until"),
                              reset_at=decision.get("defer_until"), ledger_revision=ledger["revision"],
                              ledger_sha256=_digest(reservations), rate_sha256=_digest(rate),
                              rate_observed_at=rate["observed_at"], receipt_id=secrets.token_hex(16))
    except _Refused as exc:
        result["reason"] = str(exc)
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, OverflowError, RuntimeError):
        result["reason"] = "observation_unavailable"
    return result


def add_arguments(parser):
    parser.add_argument("--profile", required=True)
    parser.add_argument("--series-id", type=int, required=True)
    parser.add_argument("--policy-sha256", required=True)


def execute(args):
    receipt = observe(args.profile, args.series_id, args.policy_sha256)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return {"ready": 0, "wait": 3, "refused": 2}[receipt["state"]]
