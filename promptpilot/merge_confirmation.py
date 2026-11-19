"""Execute a deterministic, pre-reserved post-update confirmation under Native's lease."""

import json
from pathlib import Path
import re
import sqlite3
import time

from . import (
    db,
    fallback_handoff,
    merge_checkpoint,
    merge_finish_proof,
    pipeline_insights as pi,
)
from . import project_pipeline as pp
from .merge_finish_transport import PHASES, Transport, reservation_cost
from .task_identity import task_execution_fingerprint


def execute(
    task,
    profile_id,
    profile,
    queue,
    admission,
    command,
    working_dir,
    handoff,
    checkpoint,
):
    scope = pi._GITHUB_SCAN_LEASE_SCOPE
    budget = pi._github_budget_policy(profile)
    reserved = reservation_cost(budget)
    account = admission["_lease"]
    reservation = getattr(pi._execution_budget_context, "reservation", None)
    target = handoff["target"]
    token = handoff["handoff"]["lease"]
    portfolio = pp.digest(pi._profiles())
    expected_execution = task_execution_fingerprint(task)
    expected_marker = pp.digest([task.started_at.isoformat(), token])
    key = merge_checkpoint.pending_key(checkpoint["repository"], target)
    pending_id = {
        k: checkpoint[k] for k in ("repository", "number", "to", "from", "base", "tree", "intent")
    }
    sent_already = False
    denied = None
    accepted_token = None
    prepared = False
    source_called = False

    def ledger(conn):
        observed = db._pipeline_source_refresh_state(
            conn,
            scope,
            task.id,
            profile_id,
            queue["id"],
            {"merge_confirmation": reserved},
            confirmation=True,
        )
        guard = observed["guard"]
        row = conn.execute(
            "SELECT merge_checkpoint_gate FROM tasks WHERE id=?", (task.id,)
        ).fetchone()
        if (
            not db._pipeline_scan_lease_guard_matches(conn, account.guard)
            or reservation is None
            or guard["token"] != reservation.token
            or guard["started_at"] != task.started_at.isoformat()
            or guard["execution_sha256"] != expected_execution
            or guard["series_id"] != task.series_id
            or guard["priority"] != task.priority
            or guard["session_id"] != task.session_id
            or guard["herdr_pane"] != task.herdr_pane
            or row["merge_checkpoint_gate"] != expected_marker
            or merge_checkpoint._load(conn, profile_id, lease, target["number"], target["head"])
            != checkpoint
        ):
            raise pp.PipelineError(
                "MERGE confirmation lost its running attempt, admission or checkpoint"
            )
        if accepted_token is not None:
            # Publication CAS and invalidations are durable and may race a
            # fresh in-memory dashboard. Check inside the one-send transaction.
            epoch = conn.execute(
                "SELECT value FROM settings WHERE key=?", (pi._CACHE_EPOCH_KEY,)
            ).fetchone()
            revision = conn.execute(
                "SELECT value FROM settings WHERE key=?",
                (pi._published_revision_key(profile_id, accepted_token["profile_hash"]),),
            ).fetchone()
            if (
                int(epoch[0] if epoch else 0) != accepted_token["epoch"]
                or not revision
                or int(revision[0]) != accepted_token["revision"]
                or not 0 <= time.time() - accepted_token["generated_at"] < pi._CACHE_TTL_SECONDS
            ):
                raise pp.PipelineError("MERGE confirmation lost its published Source token")
        return observed

    def native_fence():
        account.ensure_owned()
        fallback_handoff.validate_lease(lease, "merge")
        if (
            pp.digest(pi._profiles()) != portfolio
            or fallback_handoff.config_digest(pp.load_config(path)) != lease["config_sha256"]
            or fallback_handoff.config_digest(config) != lease["config_sha256"]
        ):
            raise pp.PipelineError("MERGE confirmation configuration changed")
        with db._connect() as conn:
            conn.execute("BEGIN")
            ledger(conn)

    def require_source():
        current = pi.read_cached(profile_id, db.list_series())
        if pi._required_snapshot_state(
            current, profile, "merge"
        ) or not merge_checkpoint.agrees_with_source(current, lease, checkpoint):
            raise pp.PipelineError("MERGE confirmation Source expired or owner changed")
        member = next(q for q in current["queues"] if q["id"] == "merge")
        runtime = current.get("runtime") or {}
        if (
            current.get("profile_id") != profile_id
            or runtime.get("state") != "online"
            or runtime.get("paused") is not False
            or member.get("task_id") != task.id
            or member.get("task_status") != "running"
            or (member.get("replica_status") or {}).get("valid") is not True
            or not any(
                item.get("number") == target["number"] for item in member.get("admission_items", [])
            )
            or current["diagnostics"].get("exit_code") != 0
        ):
            raise pp.PipelineError(
                "MERGE confirmation Source has no live task, queue or checker proof"
            )
        token = current["cache"].get("token")
        if (
            not isinstance(token, dict)
            or token.get("profile_hash") != pi._profile_fingerprint(profile)
            or type(token.get("epoch")) is not int
            or type(token.get("revision")) is not int
            or type(token.get("generated_at")) not in (float, int)
            or not 0 <= time.time() - token["generated_at"] < pi._CACHE_TTL_SECONDS
            or accepted_token is not None
            and token != accepted_token
        ):
            raise pp.PipelineError("MERGE confirmation Source token changed")
        return token

    def fence():
        native_fence()

    def prepare_source():
        nonlocal prepared, source_called, denied, accepted_token
        if source_called:
            raise pp.PipelineError("repeated final Source preparation")
        source_called = True
        native_fence()
        latest = pi.read_cached(profile_id, db.list_series())
        if pi._required_snapshot_state(latest, profile, "merge"):
            scan = pi._budget_policy_for_admission_priority(
                pi._budget_policy_for_route(pi._with_shared_budget_floor(budget), "insights"),
                task.priority,
            )

            class Continuation:
                published = None

                def __init__(self):
                    self.final_source_spend = {}
                    self.guard = self.ledger()["guard"]

                def ledger(self, policy=None):
                    native_fence()
                    with db._connect() as conn:
                        conn.execute("BEGIN")
                        return ledger(conn)

                def check(self):
                    native_fence()

            context = pi._scan_lease_context
            if (
                context.lease is not account
                or getattr(context, "continuation", None)
                or account.continuation
            ):
                raise pp.PipelineError("nested MERGE Source observation")
            previous_meter = account.search_budget
            continuation = Continuation()
            context.continuation = account.continuation = continuation
            try:
                raw = pi._github_rate_limits()
                reservations = continuation.ledger()
                if reservations["final_source_cost"] != scan["requested_cost"]:
                    raise pp.PipelineError("final MERGE Source credit unavailable")
                decision = pi._admit_scan_budget(
                    scan, raw, reservations, account, task.priority, task.id, True
                )
                if not decision.get("allowed"):
                    denied = decision
                    raise pp.PipelineError("final MERGE Source budget unavailable")
                decision["_lease"] = account
                account.search_budget = pi._GraphQLSearchBudget(scan, decision)
                fresh = pi._analyze_without_budget(
                    profile_id,
                    db.list_series(),
                    use_cache=False,
                    refresh_diagnostics=True,
                )
                if (
                    not continuation.published
                    or pi._required_snapshot_state(fresh, profile, "merge")
                    or fresh["cache"].get("source") != "live"
                ):
                    raise pp.PipelineError("final MERGE Source observation did not publish")
            finally:
                context.continuation = account.continuation = None
                account.search_budget = previous_meter
        accepted_token = require_source()
        native_fence()
        prepared = True

    def gate():
        nonlocal denied
        if not prepared:
            raise pp.PipelineError("late Source verification is absent")
        require_source()
        fence()
        # We already paid for proof. Only the independently capped gate and
        # publication/readback remain. Every foreign promise is still counted.
        raw = pi._github_rate_limits()
        if (
            type(account.last_graphql_metadata_cost) is not int
            or not 0 < account.last_graphql_metadata_cost <= 4
        ):
            raise pp.PipelineError("MERGE confirmation account metadata cost is unproved")
        with db._connect() as conn:
            conn.execute("BEGIN")
            observed = ledger(conn)
        remaining = {
            "core": PHASES["gate"][0] + PHASES["publication"][0],
            "search": 0,
            "graphql": PHASES["gate"][1] + PHASES["publication"][1] + transport.health_allowance,
        }
        policy = pi._budget_policy_for_route(
            pi._with_shared_budget_floor(budget), "merge_confirmation"
        )
        policy = pi._with_execution_snapshot_floor(
            pi._budget_policy_for_admission_priority(policy, task.priority)
        )
        decision = pi._evaluate_github_budget(
            dict(policy, requested_cost=remaining),
            raw,
            now=time.time(),
            reserved_other={
                k: observed["totals"][k]
                - reserved[k]
                - (observed["final_source_cost"] or {}).get(k, 0)
                for k in reserved
            },
        )
        if not decision["allowed"]:
            denied = decision
            raise pp.PipelineError(decision["state"])
        transport.phase = "gate"
        result = fallback_handoff.gate(
            transport, config, "merge", token, config_path=path, meter=transport
        )
        if result.get("target") != target or result.get("mutation_authorized") is not False:
            raise pp.PipelineError("MERGE confirmation scheduling gate returned another target")
        transport.phase = "publication"
        return result

    def claim(number, head, intent, body_sha256):
        nonlocal sent_already
        fence()
        if (
            (number, head, intent) != (target["number"], target["head"], checkpoint["intent"])
            or transport.phase != "publication"
            or not re.fullmatch("[a-f0-9]{64}", body_sha256)
        ):
            raise pp.PipelineError("MERGE confirmation send identity differs")
        with db._connect(immediate=True) as conn:
            ledger(conn)
            created = conn.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",
                (key, json.dumps(dict(pending_id, body_sha256=body_sha256))),
            ).rowcount
        sent_already = True
        if created:
            transport.authorized_body = body_sha256
        return created == 1

    try:
        lease = fallback_handoff.validate(handoff, "merge")
        if lease.get("target_reservation") or target["stage"] != merge_checkpoint.RECOVERY:
            raise pp.PipelineError("MERGE confirmation requires a signed ordinary recovery")
        previous = db.get_setting(key)
        if previous is not None:
            previous = json.loads(previous)
        if previous is not None:
            sent_already = True
            if any(previous[k] != v for k, v in pending_id.items()):
                raise pp.PipelineError("another MERGE confirmation send awaits readback")
        # The operator's election config is rebound by its signed content hash.
        # No path or executable from the PR, provider or checkpoint is accepted.
        if (
            command.count("--config") != 1
            or command[-2:] != ["next", "merge"]
            or not lease.get("health_checkout")
        ):
            raise merge_finish_proof.OutsideScope("signed project config or checkout unavailable")
        path = str(
            (Path(working_dir or Path.cwd()) / command[command.index("--config") + 1]).resolve(
                strict=True
            )
        )
        config = pp.load_config(path)
        merge_checkpoint.bind_election(task, token)
        fence()
        transport = Transport(
            profile, pp.GitHub(timeout_seconds=config["github_timeout_seconds"]), fence
        )
        transport.number = checkpoint["number"]
        result = merge_finish_proof.confirm(
            transport,
            config,
            checkpoint,
            working_dir=lease["health_checkout"],
            gate=gate,
            claim_send=claim,
            validate_attempt=fence,
            prepare_source=prepare_source,
        )
        if (
            result.get("action") != "completed"
            or result.get("stage") != "merge-confirmation"
            or (result["number"], result["head"], result["intent"])
            != (target["number"], target["head"], checkpoint["intent"])
            or type(result.get("done")) is not int
            or result["done"] <= 0
        ):
            raise pp.PipelineError("MERGE confirmation did not return a canonical readback")
        with db._connect(immediate=True) as conn:
            ledger(conn)
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                record = json.loads(row["value"])
                if (
                    any(record[k] != v for k, v in pending_id.items())
                    or record["body_sha256"] != result["body_sha256"]
                ):
                    raise pp.PipelineError("MERGE confirmation pending send changed")
                conn.execute("DELETE FROM settings WHERE key=? AND value=?", (key, row["value"]))
        return {
            "action": "complete_empty",
            "mode": "tool",
            "verdict": "ГОТОВО" if result["published"] else "ПУСТО",
            "profile_id": profile_id,
            "queue_id": "merge",
            "completion": result,
            "reason": f"confirmed base-sync for PR #{target['number']}; integration REVIEW required",
        }
    except merge_finish_proof.OutsideScope as exc:
        if not sent_already:
            return None
        reason = str(exc)
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        IndexError,
    ) as exc:
        reason = str(exc)
    if denied:
        return pi._budget_defer_route(
            denied,
            profile_id,
            profile,
            queue,
            mode="tool",
            phase="post_preflight",
            preflight=handoff,
        )
    return {
        "action": "defer",
        "mode": "tool",
        "defer_for": "2m",
        "reason": "MERGE confirmation stopped: " + pi._bounded_defer_text(reason),
    }
