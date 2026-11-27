"""Hand a fresh OneBase queue snapshot to one waiting FIX occurrence.

This only advances local scheduling. Dispatch, budget admission and target election
still run in the ordinary worker after the task is claimed.
"""
import json
import logging
import math
import sqlite3
import time

from .task_identity import task_execution_fingerprint


_LATCH = "pipeline_fix_source_wake:v1:onebase"
_SCAN = "refresh_blocked=scan_in_progress"


def _enabled(profile_id, profile, queue):
    if not isinstance(profile, dict) or not isinstance(queue, dict):
        return False
    budget = profile.get("github_budget")
    gate = queue.get("dispatch_gate")
    route = queue.get("execution", {})
    return (profile_id == "onebase" and profile.get("repository") == "ivanarama/onebase"
            and isinstance(budget, dict) and budget.get("source_cost_protocol") == "source-graphql-cost-v1"
            and queue.get("id") == "fix" and isinstance(gate, dict) and gate.get("require_fresh_snapshot") is True
            and isinstance(route, dict) and (not route or route.get("mode") == "skill")
            and queue.get("replicas", 1) == 1 and queue.get("capacity", 1) == 1)


def wait_hint(profile_id, profile, queue, data, state):
    from . import pipeline_insights as pi

    if state not in (_SCAN, "invalidated") or not _enabled(profile_id, profile, queue):
        return None
    cache = data.get("cache") if isinstance(data, dict) else None
    if not isinstance(cache, dict):
        return None
    profile_hash = pi._profile_fingerprint(profile)
    token = cache.get("token")
    if token is None and cache.get("available") is False:
        return {"profile_hash": profile_hash, "revision": 0, "epoch": 0, "create": state == _SCAN}
    if not isinstance(token, dict) or token.get("profile_hash") != profile_hash:
        return None
    if any(type(token.get(k)) is not int or not 0 <= token[k] < 2**63 for k in ("revision", "epoch")):
        return None
    return {"profile_hash": profile_hash, "revision": token["revision"], "epoch": token["epoch"],
            "create": state == _SCAN}


def _execution(row):
    return f"{task_execution_fingerprint(row)}:{row['priority']}"


def record(conn, task_id, hint, reason):
    """Called inside the successful running-attempt -> pending CAS transaction."""
    from . import db

    row = conn.execute("SELECT * FROM tasks WHERE id=? AND status='pending' AND series_id IS NOT NULL",
                       (task_id,)).fetchone()
    if not row or row["next_run_at"] or row["session_id"] or row["herdr_pane"] or row["retry_count"]:
        conn.execute("DELETE FROM pipeline_fix_source_waits WHERE task_id=?", (task_id,))
        return
    execution = _execution(row)
    prior = conn.execute("SELECT * FROM pipeline_fix_source_waits WHERE task_id=?", (task_id,)).fetchone()
    same = (prior is not None and prior["series_id"] == row["series_id"] and prior["execution"] == execution
            and prior["profile_hash"] == hint["profile_hash"])
    if not same and not hint["create"]:
        conn.execute("DELETE FROM pipeline_fix_source_waits WHERE task_id=?", (task_id,))
        return
    conn.execute("""INSERT OR REPLACE INTO pipeline_fix_source_waits
                    (task_id,series_id,profile_hash,execution,waiting_since,seen_revision,seen_epoch,reason,ready_guard)
                    VALUES (?,?,?,?,?,?,?,?,NULL)""",
                 (task_id, row["series_id"], hint["profile_hash"], execution,
                  prior["waiting_since"] if same else db._now(), hint["revision"], hint["epoch"], reason))


def _guard_current(conn, guard):
    from . import db, pipeline_insights as pi

    try:
        profile_hash = guard["profile_hash"]
        if (not isinstance(profile_hash, str)
                or guard["cache_key"] != pi._cache_key("onebase", profile_hash)
                or guard["revision_key"] != pi._published_revision_key("onebase", profile_hash)
                or guard["epoch_key"] != pi._CACHE_EPOCH_KEY or guard["ttl_seconds"] != pi._CACHE_TTL_SECONDS
                or type(guard["generated_at"]) not in (int, float)
                or not math.isfinite(guard["generated_at"]) or guard["generated_at"] > time.time()
                or not db._pipeline_cache_guard_matches(conn, guard)):
            return False
        latest = dict(conn.execute("SELECT key,value FROM settings WHERE key IN (?,?)",
                      (pi._published_profile_key("onebase"), pi._published_profile_revision_key("onebase"))))
        return (latest.get(pi._published_profile_key("onebase")) == profile_hash
                and latest.get(pi._published_profile_revision_key("onebase")) == str(guard["revision"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def preferred(conn, now):
    """Choose among due tasks only. The existing budget fairness waiter still runs first."""
    for row in conn.execute("""SELECT t.*,w.execution,w.ready_guard FROM pipeline_fix_source_waits w
                              JOIN tasks t ON t.id=w.task_id AND t.series_id=w.series_id
                              JOIN task_series s ON s.id=t.series_id
                              WHERE w.ready_guard IS NOT NULL AND t.status='pending' AND t.error=w.reason
                              AND NOT s.paused AND s.ended_at IS NULL AND t.budget_wait_scope IS NULL
                              AND (t.next_run_at IS NULL OR t.next_run_at<=?)
                              AND (t.scheduled_at IS NULL OR t.scheduled_at<=?)
                              ORDER BY w.waiting_since,t.id""", (now, now)).fetchall():
        try:
            guard = json.loads(row["ready_guard"])
        except (TypeError, ValueError):
            guard = None
        if _execution(row) == row["execution"] and _guard_current(conn, guard):
            return row["id"]
        conn.execute("UPDATE pipeline_fix_source_waits SET ready_guard=NULL WHERE task_id=?", (row["id"],))
    return None


def _wake(profile_id, profile, data, series):
    from . import db, pipeline_insights as pi

    queue = next((q for q in profile.get("queues", []) if _enabled(profile_id, profile, q)), None)
    if not queue or pi._required_snapshot_state(data, profile, "fix") is not None:
        return None
    series_ids = [s["id"] for s in pi._series_replicas_for_queue(queue, series) if not s.get("paused")]
    if not series_ids:
        return None
    guard = pi._wake_cache_guard(profile_id, profile, data["cache"])
    if guard is None:
        return None
    with db._connect(immediate=True) as conn:
        now = db._now()
        # Ordinary FIX uses one configured series, with possibly several pending occurrences.
        rows = conn.execute("""SELECT t.*,w.execution FROM pipeline_fix_source_waits w
                              JOIN tasks t ON t.id=w.task_id AND t.series_id=w.series_id
                              JOIN task_series s ON s.id=t.series_id
                              WHERE w.series_id=? AND w.profile_hash=? AND w.seen_revision<? AND w.seen_epoch<=?
                              AND t.status='pending' AND t.error=w.reason AND t.budget_wait_scope IS NULL
                              AND NOT s.paused AND s.ended_at IS NULL AND (t.next_run_at IS NULL OR t.next_run_at<=?)
                              ORDER BY w.waiting_since,t.priority,t.id""",
                            (series_ids[0], guard["profile_hash"], guard["revision"], guard["epoch"], now)).fetchall()
        if not rows:
            return None
        paused = conn.execute("SELECT value FROM settings WHERE key='worker_paused'").fetchone()
        if (paused and paused["value"] == "1") or not _guard_current(conn, guard):
            return None
        encoded = json.dumps(guard, sort_keys=True)
        latch = conn.execute("SELECT value FROM settings WHERE key=?", (_LATCH,)).fetchone()
        if latch and latch["value"] == encoded:
            return None
        for row in rows:
            if _execution(row) != row["execution"]:
                continue
            conn.execute("UPDATE pipeline_fix_source_waits SET ready_guard=? WHERE task_id=?", (encoded, row["id"]))
            conn.execute("UPDATE tasks SET scheduled_at=? WHERE id=?", (now, row["id"]))
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (_LATCH, encoded))
            return row["id"]
    return None


def wake(profile_id, profile, data, series):
    try:
        return _wake(profile_id, profile, data, series)
    except (sqlite3.Error, OSError, AttributeError, TypeError, ValueError, KeyError, OverflowError) as exc:
        logging.getLogger(__name__).warning("FIX snapshot wake unavailable: %s", exc)


def wake_cached(profile_id):
    from . import db, pipeline_insights as pi

    try:
        profile = pi._profiles().get(profile_id)
        if profile:
            series = db.list_series()
            return wake(profile_id, profile, pi.read_cached(profile_id, series), series)
    except (sqlite3.Error, OSError, AttributeError, TypeError, ValueError, KeyError, OverflowError) as exc:
        logging.getLogger(__name__).warning("FIX snapshot catch-up unavailable: %s", exc)
