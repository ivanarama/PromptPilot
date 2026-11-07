"""Bound one read-only proof probe to a configured MERGE task and scan budget."""
import json
import re
import sys
import time
from pathlib import Path

from . import db, project_pipeline as pp
from .merge_admission_progress import MAX_READS, PROTOCOL, VerifiedProgress, validate_facts

CACHE = "pipeline_merge_progress:v1:"


def _target(queue, data, holds, task):
    cache = data.get("cache") or {}
    age, ttl = cache.get("age_seconds"), cache.get("ttl_seconds")
    if (cache.get("complete") is not True or cache.get("stale") or cache.get("invalidated")
            or cache.get("refresh_blocked") or type(age) not in {int, float}
            or type(ttl) not in {int, float} or not 0 <= age < ttl <= 3600
            or not isinstance(cache.get("profile_hash"), str)
            or not re.fullmatch("[0-9a-f]{64}", cache["profile_hash"])):
        return None
    health = data.get("diagnostics") or {}
    from .fallback_handoff import validate_health, identity
    validate_health(health)
    for item in health["merge_executable"]:
        number, head, stage = item["number"], item["head"], item["stage"]
        hold = holds.get(str(number))
        live = (isinstance(hold, dict) and type(hold.get("at")) in {int, float}
                and 0 <= time.time() - hold["at"] < 24 * 3600 and hold.get("head") == head
                and hold.get("stage", "review") == "merge")
        if live:
            # Never migrate a legacy/HUMAN hold using a new probe.
            try:
                old = validate_facts(hold.get("canonical_progress"))
            except (pp.PipelineError, TypeError, ValueError):
                continue
            consumed = hold.get("progress_attempt")
            started = getattr(task, "started_at", None)
            if consumed is not None and (not isinstance(consumed, dict)
                    or consumed.get("task_id") != task.id or not hasattr(started, "isoformat")
                    or consumed.get("started_at") != started.isoformat()):
                continue
            if (hold.get("verdict") != "НЕ СМОГ"
                    or hold.get("last_progress_verdict", "НЕ СМОГ") != "НЕ СМОГ"
                    or old["phase"] != "reviewed-source"
                    or stage != "integration-merge-recovery"):
                continue
        elif stage != "merge":
            continue
        if stage == "integration-merge-recovery":
            owner = health.get("integration_owner")
            if identity(owner) != identity(item) or len(health["merge_executable"]) != 1:
                continue
        return item
    return None


def measurements(pi, task, profile_id, profile, queue, data, working_dir=None):
    """Return at most one authenticated hint; never elect, claim or run next."""
    from .pipeline_item_holds import _task_identity
    if not _task_identity(task):
        return {}
    try:
        execution = queue.get("execution") or {}
        if (queue.get("item_blockers") is not True
                or str(execution.get("stage") or queue.get("id")) != "merge"
                or execution.get("mode", "auto") not in {"auto", "tool"}
                or pi._queue_replica_count(queue) != 1):
            return {}
        holds = json.loads(db.get_setting("pipeline_item_holds:v1:" + str(task.series_id)) or "{}")
        if not isinstance(holds, dict):
            return {}
        target = _target(queue, data, holds, task)
        if target is None:
            return {}
        command = pi._tool_command(execution, "merge")
        if not command or command[-2:] != ["next", "merge"] or command.count("--config") > 1:
            return {}
        # Only the built-in adapter has the read-only inspection contract.
        # Do not append a new verb to an arbitrary wrapper or configured shell.
        prefix = command[:-2]
        if prefix[:3] == [sys.executable, "-m", "promptpilot.project_pipeline"]:
            options = prefix[3:]
        elif (prefix[:2] == [sys.executable, "pipelinectl"]
              or len(prefix) >= 2 and Path(prefix[0]).name.lower() in
              {"pp", "pp.exe", "promptpilot", "promptpilot.exe"} and prefix[1] == "pipelinectl"):
            options = prefix[2:]
        else:
            return {}
        if options and (len(options) != 2 or options[0] != "--config"):
            return {}
        configured_probe = execution.get("probe_command")
        if (configured_probe is not None and pi._expanded_command(configured_probe, "merge")
                != prefix + ["capabilities"]):
            return {}
        series = db.get_series(task.series_id) or {}
        root = Path(working_dir or getattr(task, "working_dir", None)
                    or series.get("working_dir") or "").resolve()
        if not root.is_dir():
            return {}
        if "--config" in command:
            index = command.index("--config")
            if index >= len(command) - 3:
                return {}
            path = Path(command[index + 1])
        else:
            path = Path("pipelinectl.json")
        path = (path if path.is_absolute() else root / path).resolve()
        config = pp.load_config(str(path))
        if (config.get("fallback_handoff") != "target-v1"
                or config["repository"] != profile.get("repository")
                or data.get("repository") != config["repository"]):
            return {}
        key = CACHE + str(task.id)
        saved = db.get_setting(key)
        if saved:
            cached = json.loads(saved)
            if cached.get("expires_at", 0) >= time.time():
                observed = VerifiedProgress(cached["attestation"], config=config,
                    config_path=path, checkout=root, number=target["number"], head=target["head"])
                return {target["number"]: observed}
        probe = command[:-2] + ["inspect-merge-progress", "--number", str(target["number"]),
                                "--head", target["head"]]
        with pi._github_scan_admission(profile, "canonical MERGE admission proof",
                profile_id=profile_id, budget_route="tool_preflight", task=task) as admission:
            cost = admission.get("requested_cost") or {}
            if (admission.get("enabled") is not True or admission.get("allowed") is not True
                    or admission.get("budget_route") != "tool_preflight"
                    or any(type(cost.get(resource)) is not int or cost[resource] < MAX_READS
                           for resource in ("core", "graphql"))):
                return {}
            available, _reason = pi._tool_available(execution, command, str(root), "merge")
            if not available:
                return {}
            value = pi._tool_preflight(execution, probe, str(root))
            if (value.get("action") != "observed" or value.get("protocol") != PROTOCOL
                    or value.get("mutation_authorized") is not False
                    or value.get("number") != target["number"] or value.get("head") != target["head"]):
                return {}
            observed = VerifiedProgress(value.get("attestation"), config=config, config_path=path,
                checkout=root, number=target["number"], head=target["head"])
            if ((target["stage"] == "merge" and observed.facts["phase"] != "reviewed-source")
                    or (target["stage"] == "integration-merge-recovery"
                        and observed.facts["phase"] != "pending-base-sync")):
                return {}
            expiry = pp.decode_signed_lease(value["attestation"])["expires_at"]
            db.set_setting(key, json.dumps({"attestation": value["attestation"], "expires_at": expiry}))
            return {target["number"]: observed}
    except (pp.PipelineError, OSError, RuntimeError, TypeError, ValueError, KeyError, IndexError):
        # Unknown/old CLI, absent key, unavailable proof/budget and malformed
        # inputs retain existing exclusions. Never grant a generic fallback.
        return {}
