"""Read back one already attempted publication after the owner's stage changes."""

import json
import sqlite3

from . import (
    db,
    merge_checkpoint,
    merge_finish_proof,
    pipeline_insights as pi,
    project_pipeline as pp,
)
from .merge_finish_transport import RECEIPT_COST, Transport
from .task_identity import task_execution_fingerprint


def reconcile(task, profile_id, profile, queue, admission, preflight, target):
    """Unrelated signed work never enters here. One idle turn checks one receipt."""
    idle = preflight["action"] in ("wait", "empty")
    repo = profile["repository"]
    prefix = merge_checkpoint.pending_key(repo)
    scope = pi._GITHUB_SCAN_LEASE_SCOPE
    fingerprint = pp.digest(pi._profiles())
    execution = task_execution_fingerprint(task)
    # Rotate across uncertain records so a permanently absent first receipt
    # cannot starve an independently landed comment on a later target.
    cursor_key = "pipeline_merge_receipt_cursor:v1:" + pp.digest(repo)
    with db._connect() as conn:
        if target:
            row = conn.execute(
                "SELECT key,value FROM settings WHERE key=?",
                (merge_checkpoint.pending_key(repo, target),),
            ).fetchone()
        else:
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key=?", (cursor_key,)
            ).fetchone()
            # The exact suffix is a lowercase SHA-256. Both the forward
            # lookup and wraparound use the settings primary key.
            seek = "SELECT key,value FROM settings WHERE key>? AND key<? ORDER BY key LIMIT 1"
            row = conn.execute(seek, (cursor[0] if cursor else prefix, prefix + "g")).fetchone()
            if row is None and cursor:
                row = conn.execute(seek, (prefix, prefix + "g")).fetchone()
    if not row:
        return None
    key, raw = tuple(row)
    budget = pi._github_budget_policy(profile)
    if not budget or not budget.get("costs"):
        return {
            "action": "defer",
            "mode": "tool",
            "defer_for": "5m",
            "reason": "receipt readback requires costed Native admission",
        }
    admission = pi._reserve_execution_admission(
        task, profile_id, profile, queue, admission, "merge_receipt", retain_budget=True
    )
    if not admission["allowed"]:
        if idle:
            # The normal election already reported no executable work. This
            # optional housekeeping cannot put a future productive MERGE behind
            # a quota reset or consume a durable priority baton.
            pi._clear_budget_waiter_after_admission(task)
            return {
                "action": "complete_empty",
                "mode": "tool",
                "verdict": "ПУСТО",
                "profile_id": profile_id,
                "queue_id": "merge",
                "reason": "MERGE idle; receipt readback awaits " + str(admission.get("state")),
            }
        return pi._budget_defer_route(
            admission,
            profile_id,
            profile,
            queue,
            mode="tool",
            phase="post_preflight",
            preflight=preflight,
        )
    account = admission["_lease"]
    reservation = pi._execution_budget_context.reservation

    def guard(conn):
        current = db._pipeline_source_refresh_state(
            conn,
            scope,
            task.id,
            profile_id,
            "merge",
            {"merge_receipt": RECEIPT_COST},
            bounded_route="merge_receipt",
        )["guard"]
        if (
            current["token"] != reservation.token
            or current["execution_sha256"] != execution
            or current["started_at"] != task.started_at.isoformat()
            or current["priority"] != task.priority
            or current["session_id"] != task.session_id
            or current["herdr_pane"] != task.herdr_pane
            or not db._pipeline_scan_lease_guard_matches(conn, account.guard)
        ):
            raise pp.PipelineError("confirmation receipt readback lost its running claim or lease")
        found = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not found or found[0] != raw:
            raise pp.PipelineError("confirmation pending record changed")

    def fence():
        account.ensure_owned()
        if pp.digest(pi._profiles()) != fingerprint:
            raise pp.PipelineError("confirmation receipt readback profile changed")
        with db._connect() as conn:
            conn.execute("BEGIN")
            guard(conn)

    try:
        fence()
        record = json.loads(raw)
        if record["repository"] != repo or key != merge_checkpoint.pending_key(
            repo, {"number": record["number"], "head": record["to"]}
        ):
            raise pp.PipelineError("pending receipt target differs")
        with db._connect(immediate=True) as conn:
            guard(conn)
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (cursor_key, key)
            )
        gh = Transport(profile, pp.GitHub(), fence, receipt=True)
        receipt = merge_finish_proof.observe_receipt(
            gh, record, config={"repository": repo, "trusted_account": gh.actor}
        )
        if type(receipt) is not int or receipt <= 0:
            raise pp.PipelineError("recorded confirmation is not yet visible")
        fence()
        with db._connect(immediate=True) as conn:
            guard(conn)
            conn.execute("DELETE FROM settings WHERE key=? AND value=?", (key, raw))
        reason = f"confirmed existing base-sync comment {receipt} on PR #{record['number']}"
        if not idle:
            # A new election at full price can now proceed. Never reuse a
            # preflight result which predates this historical reconciliation.
            return {"action": "defer", "mode": "tool", "defer_for": "30s", "reason": reason}
    except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, TypeError) as exc:
        reason = "MERGE receipt readback: " + pi._bounded_defer_text(exc)
        if not idle:
            return {"action": "defer", "mode": "tool", "defer_for": "2m", "reason": reason}
    return {
        "action": "complete_empty",
        "mode": "tool",
        "verdict": "ПУСТО",
        "reason": reason,
        "profile_id": profile_id,
        "queue_id": "merge",
    }
