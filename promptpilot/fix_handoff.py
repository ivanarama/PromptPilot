"""Signed, exact-target scheduling handoff for an opt-in FIX lane.

This proves only election and reservation ownership. The repository's
``fix-approved`` skill remains responsible for its full per-mutation GitHub
and compare-and-swap checks. No FIX route calls this module in production yet.
"""

from __future__ import annotations

import re
import time


PROTOCOL = "promptpilot-fix-target-v1"
TTL = 7200


def identity(value: dict) -> dict:
    from . import project_pipeline as pp

    stage, number, revision = pp._fix_candidate_key(value)
    return {"stage": stage, "number": number, "head": revision}


def ordered_health_candidates(health: dict) -> list[dict]:
    from . import project_pipeline as pp

    if (not isinstance(health, dict)
            or health.get("state") not in {"green", "yellow"}
            or not isinstance(health.get("findings"), list)
            or any(not isinstance(item, dict)
                   or item.get("severity") == "red"
                   for item in health["findings"])):
        raise pp.PipelineError("FIX health is incomplete or red")
    ordered = pp._ordered_fix_candidates(health.get("fix_candidates"))
    keys = [identity(item) for item in ordered]
    if len({item["number"] for item in keys}) != len(keys):
        raise pp.PipelineError("FIX health contains duplicate target numbers")
    return ordered


def health_gate(health: dict, target: dict, config: dict,
                *, owner_task_id: int) -> None:
    """Target must still be eligible; earlier work must belong to other lanes."""
    from . import db, project_pipeline as pp

    expected = identity(target)
    ordered = [identity(item) for item in ordered_health_candidates(health)]
    if expected not in ordered:
        raise pp.PipelineError("FIX target is no longer in the executable queue")
    reservations = {
        int(item["number"]): item
        for item in db.list_pipeline_target_reservations(
            repository=config["repository"])
    }
    for earlier in ordered[:ordered.index(expected)]:
        held = reservations.get(earlier["number"])
        if (held is None or held["stage"] != earlier["stage"]
                or held["head"] != earlier["head"]
                or int(held["task_id"]) == owner_task_id):
            raise pp.PipelineError(
                "a higher-priority FIX target became unreserved; start a new task")


def create(config: dict, health: dict, candidate: dict,
           reservation: dict) -> dict:
    from . import project_pipeline as pp

    if config.get("parallel_fix_enabled") is not True:
        raise pp.PipelineError("parallel FIX handoff is disabled")
    target = identity(candidate)
    repo, stage, number, head, task_id, _token = pp._reservation_identity(
        reservation)
    if (repo != str(config["repository"]).lower()
            or (stage, number, head) != (
                target["stage"], target["number"], target["head"])
            or task_id != pp._pipeline_task_id(required=True)):
        raise pp.PipelineError("FIX reservation contradicts the elected target")
    health_gate(health, target, config, owner_task_id=task_id)
    replicas = pp._configured_replica_count()
    if replicas < 2:
        raise pp.PipelineError("parallel FIX handoff requires at least two replicas")
    issued = int(time.time())
    lease = {
        "version": 1, "purpose": PROTOCOL, "stage": "fix",
        "repository": config["repository"], "target": target,
        "config_sha256": pp.digest(config), "issued_at": issued,
        "expires_at": issued + TTL, "pipeline_replicas": replicas,
        "target_reservation": reservation,
    }
    return {
        "action": "fallback", "reason": "exact FIX target requires the full skill",
        "target": target,
        "handoff": {
            "protocol": PROTOCOL, "stage": "fix",
            "repository": config["repository"], "target": target,
            "lease": pp.encode_signed_lease(lease),
        },
    }


def validate_lease(lease: dict) -> None:
    from . import project_pipeline as pp

    if (not isinstance(lease, dict) or lease.get("purpose") != PROTOCOL
            or lease.get("stage") != "fix"
            or not isinstance(lease.get("repository"), str)
            or not re.fullmatch(r"[^/\s]+/[^/\s]+", lease["repository"])
            or not isinstance(lease.get("config_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", lease["config_sha256"])):
        raise pp.PipelineError("invalid FIX handoff lease")
    target = identity(lease.get("target"))
    issued, expires = lease.get("issued_at"), lease.get("expires_at")
    now = int(time.time())
    if (type(issued) is not int or type(expires) is not int
            or issued > now + 60 or expires <= now
            or not 0 < expires - issued <= TTL):
        raise pp.PipelineError("FIX handoff lease expired or has invalid lifetime")
    replicas = lease.get("pipeline_replicas")
    if type(replicas) is not int or not 2 <= replicas <= 16:
        raise pp.PipelineError("FIX handoff has invalid replica count")
    repo, stage, number, head, _task_id, _token = pp._reservation_identity(
        lease.get("target_reservation"))
    if (repo != lease["repository"].lower()
            or (stage, number, head) != (
                target["stage"], target["number"], target["head"])):
        raise pp.PipelineError("FIX handoff reservation contradicts its target")


def validate(preflight: dict) -> dict:
    from . import project_pipeline as pp

    handoff = preflight.get("handoff") if isinstance(preflight, dict) else None
    if (not isinstance(handoff, dict)
            or preflight.get("action") != "fallback"
            or handoff.get("protocol") != PROTOCOL
            or handoff.get("stage") != "fix"
            or not isinstance(handoff.get("lease"), str)):
        raise pp.PipelineError("invalid FIX handoff envelope")
    lease = pp.decode_signed_lease(handoff["lease"])
    validate_lease(lease)
    if (handoff.get("repository") != lease["repository"]
            or identity(handoff.get("target")) != lease["target"]
            or identity(preflight.get("target")) != lease["target"]):
        raise pp.PipelineError("FIX handoff envelope contradicts its signed target")
    return lease


def gate(gh, config: dict, lease_value: str, *, config_path=None) -> dict:
    """Fresh scheduling check before the first external FIX mutation."""
    from . import project_pipeline as pp

    lease = pp.decode_signed_lease(lease_value)
    validate_lease(lease)
    if (config.get("parallel_fix_enabled") is not True
            or config.get("repository") != lease["repository"]
            or pp.digest(config) != lease["config_sha256"]):
        raise pp.PipelineError("FIX handoff configuration changed")
    health = pp.run_health(config, config_path=config_path)
    if (config.get("parallel_fix_enabled") is not True
            or config.get("repository") != lease["repository"]
            or pp.digest(config) != lease["config_sha256"]):
        raise pp.PipelineError("FIX handoff configuration changed during health refresh")
    validate_lease(lease)
    pp.ensure_identity(gh, config)
    reservation = lease["target_reservation"]
    health_gate(health, lease["target"], config,
                owner_task_id=int(reservation["task_id"]))
    pp.renew_lease_target_reservation({
        "stage": "fix", "target_stage": lease["target"]["stage"],
        "repository": lease["repository"],
        "number": lease["target"]["number"],
        "head": lease["target"]["head"],
        "target_reservation": reservation,
        "pipeline_replicas": lease["pipeline_replicas"],
    }, config)
    return {
        "action": "validated", "stage": "fix",
        "repository": lease["repository"], "target": lease["target"],
        "mutation_authorized": False,
        "reason": "fresh election passed; repository FIX mutation gates still required",
    }
