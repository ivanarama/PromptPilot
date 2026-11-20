"""Resume an ordinary MERGE's external Actions wait without starting a provider."""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, merge_ci_wait as ci, pipeline_insights as pi, project_pipeline as pp
from .task_identity import task_execution_fingerprint


POLL_COST = {"core": 2, "search": 0, "graphql": 0}


def enabled(queue, profile):
    execution = queue.get("execution") or {}
    return (isinstance(execution, dict) and queue.get("id") == "merge" and queue.get("item_blockers") is True
            and str(execution.get("stage") or "merge").lower() == "merge"
            and str(execution.get("mode", "auto")).lower() == "auto"
            and execution.get("merge_confirmation") == "bounded-v1"
            and profile.get("github_budget", {}).get("source_cost_protocol") == "source-graphql-cost-v1")


def source_wait(task, profile, data):
    """A fresh executable queue may save the election only for a passive poll."""
    items = (data.get("diagnostics") or {}).get("merge_executable") or []
    if (not items or not isinstance(items[0], dict)
            or pi._required_snapshot_state(data, profile, "merge") is not None
            or data.get("repository") != profile["repository"] or items[0].get("stage") not in ci.ORDINARY_STAGES):
        return None
    record = ci.series_wait(task.series_id, profile["repository"])
    if (record and record["state"] in ci.WAITING
            and all(items[0].get(k) == record[k] for k in ("number", "head"))):
        return record
    return None


def elected_wait(task, repo, preflight, lease):
    action = preflight["action"]
    if action == "fallback" and lease and lease["target"]["stage"] in ci.ORDINARY_STAGES:
        return ci.active_wait(repo, lease["target"])
    target = preflight.get("target") or {}
    if action == "merge" and target.get("number") and target.get("head"):
        return ci.active_wait(repo, target)
    if action == "wait" and preflight.get("reason") == "required CI checks are still running":
        record = ci.series_wait(task.series_id, repo)
        if record and preflight.get("number") == record["number"]:
            return record
    return None


def diagnose(task, profile_id, profile, queue, admission, execution, command, working_dir, record, exclusions):
    """Buy a second project preflight only when an elected route contradicts the recorded run."""
    config_path = ci.adapter_config_path(command)
    native = pi._expanded_command(["{python}", "-m", "promptpilot.project_pipeline"], "merge")
    argv = [*native, "--config", config_path, "merge-ci-diagnose", "--number", str(record["number"]),
            "--head", record["head"]]
    decision = pi._reserve_execution_admission(task, profile_id, profile, queue, admission,
                                              "tool_preflight", retain_budget=True)
    if not decision.get("allowed"):
        return None, pi._budget_defer_route(decision, profile_id, profile, queue, mode="tool")
    try:
        response = pi._tool_preflight(execution, argv, working_dir,
                                      env_extra={"PP_PIPELINE_EXCLUDED_NUMBERS": json.dumps(exclusions),
                                                 "PP_DATA_DIR": str(Path(db.DB_PATH).resolve().parent)})
        if response.get("action") == "fallback":
            lease = pi._validate_targeted_handoff(response, "merge")
            if (lease["repository"] == record["repository"] and lease["config_sha256"] == record["config_sha256"]
                    and lease.get("target_reservation") is None and lease["target"].get("stage") in ci.ORDINARY_STAGES
                    and all(lease["target"].get(k) == record[k] for k in ("number", "head"))):
                return response, None
        if response.get("action") == "wait" and response.get("state") in {"success", "superseded"}:
            return None, {"action": "defer", "mode": "tool", "defer_until":
                          datetime.fromtimestamp(time.time() + 5, timezone.utc).isoformat(),
                          "reason": f"MERGE CI {response['state']}; re-elect for fresh checks"}
        reason = response.get("reason") or response.get("error") or "conflicting diagnostic target"
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as exc:
        reason = str(exc)
    finally:
        pi.release_execution_admission()
        admission["_budget_reservation_revision"] = db.pipeline_github_budget_reservations(
            pi._GITHUB_SCAN_LEASE_SCOPE)["revision"]
    return None, {"action": "defer", "mode": "tool", "defer_for": "2m", "reason":
                  f"MERGE CI diagnosis for #{record['number']}: {pi._bounded_defer_text(reason)}"}


def poll(task, profile_id, profile, queue, admission, record, preflight=None):
    admission = pi._reserve_execution_admission(
        task, profile_id, profile, queue, admission, "merge_ci_poll", retain_budget=True)
    if not admission.get("allowed"):
        return pi._budget_defer_route(admission, profile_id, profile, queue, mode="tool",
                                      preflight=preflight)
    account = admission["_lease"]
    reservation = pi._execution_budget_context.reservation
    identity = task_execution_fingerprint(task)

    def fence():
        account.ensure_owned()
        with db._connect() as conn:
            conn.execute("BEGIN")
            observed = db._pipeline_source_refresh_state(
                conn, pi._GITHUB_SCAN_LEASE_SCOPE, task.id, profile_id, "merge",
                {"merge_ci_poll": POLL_COST}, bounded_route="merge_ci_poll")["guard"]
            if (observed["token"] != reservation.token or observed["execution_sha256"] != identity
                    or observed["started_at"] != task.started_at.isoformat()
                    or not db._pipeline_scan_lease_guard_matches(conn, account.guard)):
                raise pp.PipelineError("MERGE CI readback lost its task or account reservation")

    route = {"action": "defer", "mode": "tool", "profile_id": profile_id, "queue_id": queue["id"]}
    def reselect_at():
        return datetime.fromtimestamp(time.time() + 5, timezone.utc).isoformat()
    label = f"PR #{record['number']} Actions run {record['run_id']} attempt {record['attempt']}"
    try:
        fence()
        try:
            observation = ci.observe_run(pp.GitHub(timeout_seconds=15), record)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            if time.time() < record["deadline"]:
                raise
            # This is a local deadline, not a failed CI conclusion. A fresh
            # signed diagnosis must still obtain the real run before any write.
            observation = {"state": "timeout", "run_id": record["run_id"], "attempt": record["attempt"],
                           "deadline": record["deadline"]}
        fence()
        if not ci.save_observation(record, observation):
            return dict(route, defer_until=reselect_at(), reason=f"{label}: CI journal changed, re-elect")
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        return dict(route, defer_for="2m", defer_policy="hard_not_before",
                    reason=f"{label}: CI readback unavailable: {pi._bounded_defer_text(exc)}")
    state = observation["state"]
    if state in ci.WAITING:
        until = min(time.time() + ci.POLL_SECONDS, observation["deadline"])
        return dict(route, reason=f"{label}: {state}", defer_policy="hard_not_before",
                    defer_until=datetime.fromtimestamp(until, timezone.utc).isoformat())
    if state == record["state"]:
        return dict(route, defer_for="2m", defer_policy="hard_not_before",
                    reason=f"{label}: {state}, awaiting a compatible fresh election")
    return dict(route, defer_until=reselect_at(), reason=f"{label}: {state}, re-elect for fresh checks")


def add_command(envelope, native, profile_id, target, gate_file, record):
    envelope["ci_wait_command"] = [*native, "merge-ci-wait", "<operation>", "--profile", profile_id,
        "--number", str(target["number"]), "--head", target["head"],
        "--run-id", "<run_id>", "--attempt", "<attempt>", "--lease-file", gate_file]
    if record:
        envelope["ci_wait_record"] = {key: record[key] for key in
                                      ("run_id", "attempt", "state", "deadline", "claimed_at")}
    return (
        "Для §4 канонического MERGE действует управляемое ожидание CI. Выбери точный CI run и attempt. "
        "При наличии ci_wait_record продолжай с его run_id и attempt. При выполняющемся CI вызови "
        "ci_wait_command с observe. При waiting или unknown заверши ИТОГ: ПУСТО и освободи воркер. "
        "Не запускай watch, sleep или повторный сбор истории. Для единственного разрешённого повтора "
        "подтверди временный сбой обязательного задания по логам и все штатные Source, HEAD, ship и "
        "gate-проверки. Вызови ci_wait_command с claim для первой упавшей попытки. Только свежий "
        "state=claimed с send_allowed=true допускает одну отправку gh run rerun <run_id> --failed "
        "в проверенном репозитории. При любом результате отправки выполни observe для возвращённой "
        "попытки. При неясном ответе, отказе или ранее занятом claim повторная отправка запрещена. "
        "Готовый CI требует всех штатных восьми проверок и новой проверки актуальности перед записью. "
        "При failed или timeout пройди штатную диагностику именно этой попытки. При смене HEAD, "
        "попытки, потере ship или отказе команды остановись для новой сверки.\n\n"
    )
