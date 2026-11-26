"""Suspend an ordinary MERGE before its first write until a new Source is published.

The saved base is a scheduling hint. A new signed election and ordinary live
product checks still authorize every publication.
"""
import json
import logging
import sqlite3
import time

from . import db, merge_checkpoint, project_pipeline as pp
from .task_identity import task_execution_fingerprint


def enabled(queue, profile):
    from .config import PIPELINE_SNAPSHOT_INTERVAL
    route = queue.get("execution")
    budget = profile.get("github_budget")
    return (isinstance(route, dict) and isinstance(budget, dict) and PIPELINE_SNAPSHOT_INTERVAL > 0 and queue.get("id") == "merge"
            and queue.get("item_blockers") is True and route.get("mode", "auto") == "auto"
            and route.get("merge_confirmation") == "bounded-v1"
            and budget.get("source_cost_protocol") == "source-graphql-cost-v1")


def _key(profile_id):
    return "pipeline_merge_before_intent_wait:v1:" + profile_id


def _result_line(reason):
    return f"ИТОГ: ПУСТО — нет допуска: {reason}"


def observe(conn, task, lease, gate, profile_id, profile, data, base):
    from . import config, pipeline_insights as pi

    target = lease["target"]
    if (target["stage"] != "merge" or not isinstance(base, str)
            or not merge_checkpoint.SHA.fullmatch(base) or base == target["head"]
            or not config.PIPELINE_SNAPSHOT_INTERVAL):
        raise ValueError("pre-intent observation requires an ordinary MERGE, active sampler and planned base")
    key = _key(profile_id)
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row:
        try:
            old_task = json.loads(row["value"])["task_id"]
        except (ValueError, TypeError, KeyError):
            old_task = task.id  # replace a malformed optional hint, never use it
        if old_task == task.id:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
    try:
        state, token = merge_checkpoint._passive_source_state(conn, lease, profile_id, profile, data)
        if data["diagnostics"].get("integration_owner") is not None:
            raise ValueError("integration recovery has its own Source protocol")
        if state in (None, "stale"):
            # A task-bound final refresh can return busy before the sampler
            # publishes a refresh status. Read the active lease in this transaction.
            scope = pi._github_budget_policy(profile)["lease_scope"]
            scan = conn.execute("SELECT value FROM settings WHERE key=?", (db._pipeline_scan_lease_key(scope),)).fetchone()
            if scan and db._pipeline_scan_lease_guard_matches(
                    conn, {"scope": scope, "token": json.loads(scan["value"])["token"]}):
                state = "scan_in_progress"
        if state is None:
            return {"state": "observed", "reason": None, "product_mutation_authorized": False}
        if state not in {"scan_in_progress", "invalidated"}:
            raise ValueError("no pre-intent Source wait for this election")
    except (pp.PipelineError, KeyError, ValueError, TypeError) as exc:
        return {"state": "refused", "reason": str(exc), "product_mutation_authorized": False}
    receipt = {
        "series_id": task.series_id, "task_id": task.id, "started_at": task.started_at.isoformat(),
        "gate": gate, "execution": task_execution_fingerprint(task), "repository": lease["repository"],
        "config_sha256": lease["config_sha256"], "target": target, "base": base, "reason": state,
        "source": token, "expires_at": min(lease["expires_at"], time.time() + 2 * pi._CACHE_TTL_SECONDS),
    }
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, json.dumps(receipt)))
    return {"state": "wait", "reason": state, "number": target["number"], "head": target["head"],
            "result_line": _result_line(state), "product_mutation_authorized": False}


def _receipt(profile_id, profile):
    from . import config, pipeline_insights as pi

    if not config.PIPELINE_SNAPSHOT_INTERVAL:
        return None
    raw = db.get_setting(_key(profile_id))
    if not raw:
        return None
    try:
        saved = json.loads(raw)
        source = saved["source"]
        target = saved["target"]
        if (type(saved["expires_at"]) in (int, float) and time.time() < saved["expires_at"]
                and saved["repository"] == profile.get("repository")
                and source["profile_hash"] == pi._profile_fingerprint(profile)
                and all(type(source[k]) is int for k in ("revision", "epoch"))
                and all(type(saved[k]) is int for k in ("series_id", "task_id"))
                and all(isinstance(saved[k], str) for k in ("started_at", "gate", "execution", "config_sha256"))
                and target["stage"] == "merge" and type(target["number"]) is int
                and merge_checkpoint.SHA.fullmatch(target["head"]) and merge_checkpoint.SHA.fullmatch(saved["base"])
                and saved["reason"] in {"scan_in_progress", "invalidated"}):
            return saved
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _clean_exit(row, saved):
    if row["started_at"] != saved["started_at"] or row["merge_checkpoint_gate"] != saved["gate"]:
        return False
    lines = (row["result"] or "").partition("--- Meta ---")[0].rstrip().splitlines()
    return (row["status"] == "completed" and row["verdict"] == "ПУСТО"
            and bool(lines) and lines[-1].strip() == _result_line(saved["reason"]))


def _origin_and_successor(saved):
    with db._connect() as conn:
        origin = conn.execute("""SELECT status,started_at,merge_checkpoint_gate,verdict,result FROM tasks
                                 WHERE id=? AND series_id=?""", (saved["task_id"], saved["series_id"])).fetchone()
        successor = conn.execute("""SELECT id,status FROM tasks WHERE series_id=? AND id>?
                                    ORDER BY id LIMIT 1""", (saved["series_id"], saved["task_id"])).fetchone()
        return origin, successor


def _fresh(profile, data, saved):
    from . import pipeline_insights as pi

    if pi._required_snapshot_state(data, profile, "merge") is not None:
        return False
    token = data["cache"].get("token") or {}
    previous = saved["source"]
    return (token.get("profile_hash") == previous["profile_hash"]
            and type(token.get("revision")) is int and token["revision"] > previous["revision"]
            and type(token.get("epoch")) is int and token["epoch"] >= previous["epoch"])


def catch_up(profile_id):
    from . import pipeline_insights as pi

    try:
        profile = pi._profiles().get(profile_id)
        if profile:
            wake(profile_id, profile, pi.read_cached(profile_id, db.list_series()))
    except (OSError, sqlite3.Error, ValueError) as exc:
        logging.getLogger(__name__).warning("MERGE wait catch-up unavailable: %s", exc)


def wake(profile_id, profile, data):
    """Wake once on publication, including a scan racing with provider completion."""
    try:
        _wake_published(profile_id, profile, data)
    except sqlite3.Error as exc:
        # The snapshot already won its publish CAS; recurrence still polls it.
        logging.getLogger(__name__).warning("MERGE Source wake unavailable: %s", exc)


def _wake_published(profile_id, profile, data):
    from . import pipeline_insights as pi

    saved = _receipt(profile_id, profile)
    if saved is None or not _fresh(profile, data, saved):
        return
    origin, successor = _origin_and_successor(saved)
    if (origin is None or (successor and successor["status"] not in {"pending", "rate_limited"})):
        return
    active = (origin["status"] == "running" and origin["started_at"] == saved["started_at"]
              and origin["merge_checkpoint_gate"] == saved["gate"])
    if not active and not _clean_exit(origin, saved):
        return
    guard = pi._wake_cache_guard(profile_id, profile, data["cache"])
    if guard:
        db.wake_series_group_once([saved["series_id"]], _key(profile_id) + ":wake",
                                  saved["gate"], cache_guard=guard,
                                  expected_setting=(_key(profile_id), json.dumps(saved)))


def dispatch(task, profile_id, profile, data):
    """Only the immediate, identical successor can resume; ordinary next owns election."""
    from . import pipeline_insights as pi

    saved = _receipt(profile_id, profile)
    if (saved is None or saved["series_id"] != task.series_id
            or saved["execution"] != task_execution_fingerprint(task)):
        return None, None
    origin, successor = _origin_and_successor(saved)
    if (origin is None or not _clean_exit(origin, saved) or successor is None
            or successor["id"] != task.id or successor["status"] != "running"):
        return None, None
    if not _fresh(profile, data, saved):
        return None, pi._required_snapshot_defer(profile_id, "merge", "pre-intent MERGE awaits a new Source")
    # Identity inventory is optional on old checkers. A changed base invalidates
    # the resume hint; on both paths the project re-reads the protected ref.
    candidates = (data.get("diagnostics") or {}).get("merge_executable") or []
    if not candidates or any(candidates[0].get(k) != v for k, v in saved["target"].items()):
        return None, None
    if candidates[0].get("base_ref_oid") not in (None, saved["base"]):
        return None, None
    return saved, None
