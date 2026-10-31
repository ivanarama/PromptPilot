"""Signed, exact-target scheduling handoff for an opt-in FIX lane.

This proves only election and reservation ownership. The repository's
``fix-approved`` skill remains responsible for its full per-mutation GitHub
and compare-and-swap checks. The route is disabled by default.
"""

from __future__ import annotations

import re
import time
from pathlib import Path


PROTOCOL = "promptpilot-fix-target-v1"
OWNER_PROTOCOL = "promptpilot-fix-owner-v1"
TTL = 7200
MAX_TTL = 14400
CONSUMER_MARKER = "<!-- pp:fix-target-consumer-v1 -->"


def lease_seconds(config: dict) -> int:
    """Bound an explicit operator timebox; keep old configurations unchanged."""
    from . import project_pipeline as pp

    value = config.get("fix_lease_seconds", TTL)
    if type(value) is not int or not 300 <= value <= MAX_TTL:
        raise pp.PipelineError("fix_lease_seconds must be an integer from 300 to 14400")
    return value


def consumer_digest(config: dict, checkout: str) -> str:
    """Require the paired, trusted local repository procedure before dispatch."""
    import hashlib
    from . import project_pipeline as pp

    name = config.get("fix_consumer_skill", ".claude/skills/fix-approved/SKILL.md")
    if not isinstance(name, str) or not name:
        raise pp.PipelineError("FIX consumer skill path is invalid")
    path = Path(name)
    if not path.is_absolute():
        path = Path(checkout) / path
    try:
        data = path.read_bytes()
        text = data.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise pp.PipelineError("paired FIX consumer skill is unavailable") from exc
    if CONSUMER_MARKER not in text.splitlines():
        raise pp.PipelineError("paired FIX consumer protocol is unavailable")
    return hashlib.sha256(data).hexdigest()


def identity(value: dict) -> dict:
    from . import project_pipeline as pp

    # Health publishes eligibility_digest; the signed normalized target uses
    # head for the same election revision. It is never a Git commit for issues.
    if (isinstance(value, dict) and value.get("stage") == "fix-issue"
            and "eligibility_digest" not in value):
        value = {**value, "eligibility_digest": value.get("head")}
    stage, number, revision = pp._fix_candidate_key(value)
    return {"stage": stage, "number": number, "head": revision}


def ordered_health_candidates(health: dict, *, excluded=()) -> list[dict]:
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
    return [item for item in ordered if item["number"] not in excluded]


def health_gate(health: dict, target: dict, config: dict,
                *, owner_task_id: int, excluded=()) -> None:
    """Target must still be eligible; earlier work must belong to other lanes."""
    from . import db, project_pipeline as pp

    expected = identity(target)
    ordered = [identity(item) for item in ordered_health_candidates(
        health, excluded=excluded)]
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
    lifetime = lease_seconds(config)
    target = identity(candidate)
    checkout = str(Path.cwd().resolve())
    consumer = consumer_digest(config, checkout)
    excluded = sorted(pp.admission_exclusions())
    repo, stage, number, head, task_id, _token = pp._reservation_identity(
        reservation)
    if (repo != str(config["repository"]).lower()
            or (stage, number, head) != (
                target["stage"], target["number"], target["head"])
            or task_id != pp._pipeline_task_id(required=True)):
        raise pp.PipelineError("FIX reservation contradicts the elected target")
    health_gate(health, target, config, owner_task_id=task_id, excluded=excluded)
    replicas = pp._configured_replica_count()
    if replicas < 2:
        raise pp.PipelineError("parallel FIX handoff requires at least two replicas")
    issued = int(time.time())
    lease = {
        "version": 1, "purpose": PROTOCOL, "stage": "fix",
        "repository": config["repository"], "target": target,
        "config_sha256": pp.digest(config), "issued_at": issued,
        "expires_at": issued + lifetime, "pipeline_replicas": replicas,
        "target_reservation": reservation,
        "health_checkout": checkout, "consumer_sha256": consumer,
        "admission_exclusions": excluded,
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


def validate_lease(lease: dict, *, purpose: str = PROTOCOL) -> None:
    from . import project_pipeline as pp

    if (not isinstance(lease, dict) or lease.get("purpose") != purpose
            or lease.get("stage") != "fix"
            or not isinstance(lease.get("repository"), str)
            or not re.fullmatch(r"[^/\s]+/[^/\s]+", lease["repository"])
            or not isinstance(lease.get("config_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", lease["config_sha256"])):
        raise pp.PipelineError("invalid FIX handoff lease")
    checkout = lease.get("health_checkout")
    if not isinstance(checkout, str) or not Path(checkout).is_absolute():
        raise pp.PipelineError("FIX health checkout must be an absolute path")
    if (not isinstance(lease.get("consumer_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", lease["consumer_sha256"])):
        raise pp.PipelineError("invalid paired FIX consumer digest")
    target = identity(lease.get("target"))
    excluded = lease.get("admission_exclusions")
    if (not isinstance(excluded, list)
            or any(type(n) is not int or n <= 0 for n in excluded)
            or excluded != sorted(set(excluded))
            or target["number"] in excluded):
        raise pp.PipelineError("invalid FIX admission exclusions")
    issued, expires = lease.get("issued_at"), lease.get("expires_at")
    now = int(time.time())
    if (type(issued) is not int or type(expires) is not int
            or issued > now + 60 or expires <= now
            or not 0 < expires - issued <= MAX_TTL):
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


def gate(gh, config: dict, lease_value: str, *, config_path=None,
         ownership_only: bool = False) -> dict:
    """Admission first, then a distinct ownership check for every mutation.

    An admitted FIX changes its own GitHub revision/queue membership when it
    pushes or creates a PR. Reusing the initial queue predicate after that
    would strand its canonical post-push transaction. The progress token only
    proves prior admission and exact task ownership: the repository must still
    check its live state, labels, epoch and delivery proof before each write.
    """
    from . import project_pipeline as pp

    lease = pp.decode_signed_lease(lease_value)
    purpose = OWNER_PROTOCOL if ownership_only else PROTOCOL
    validate_lease(lease, purpose=purpose)
    if pp._configured_replica_count() != lease["pipeline_replicas"]:
        raise pp.PipelineError("FIX handoff replica count changed")
    if (config.get("parallel_fix_enabled") is not True
            or config.get("repository") != lease["repository"]
            or pp.digest(config) != lease["config_sha256"]):
        raise pp.PipelineError("FIX handoff configuration changed")
    if lease["expires_at"] - lease["issued_at"] != lease_seconds(config):
        raise pp.PipelineError("FIX handoff lifetime contradicts configuration")
    if consumer_digest(config, lease["health_checkout"]) != lease["consumer_sha256"]:
        raise pp.PipelineError("paired FIX consumer changed")
    health = pp.run_health(config, config_path=config_path,
                           working_dir=lease["health_checkout"])
    if (config.get("parallel_fix_enabled") is not True
            or config.get("repository") != lease["repository"]
            or pp.digest(config) != lease["config_sha256"]):
        raise pp.PipelineError("FIX handoff configuration changed during health refresh")
    if consumer_digest(config, lease["health_checkout"]) != lease["consumer_sha256"]:
        raise pp.PipelineError("paired FIX consumer changed during health refresh")
    validate_lease(lease, purpose=purpose)
    pp.ensure_identity(gh, config)
    reservation = lease["target_reservation"]
    if ownership_only:
        # Still run the trusted full checker and reject incomplete/red state.
        # Target-specific authorization belongs to the canonical repository
        # procedure, including its narrowly allowed post-push/PR cleanup.
        ordered_health_candidates(health)
    else:
        health_gate(health, lease["target"], config,
                    owner_task_id=int(reservation["task_id"]),
                    excluded=lease["admission_exclusions"])
    pp.renew_lease_target_reservation({
        "stage": "fix", "target_stage": lease["target"]["stage"],
        "repository": lease["repository"],
        "number": lease["target"]["number"],
        "head": lease["target"]["head"],
        "target_reservation": reservation,
        "pipeline_replicas": lease["pipeline_replicas"],
    }, config)
    if lease['target']['stage']=='fix-pr':
        from .review_routing_policy import require_legacy
        require_legacy(gh,config,int(lease['target']['number']))
    result = {
        "action": "validated", "stage": "fix",
        "repository": lease["repository"], "target": lease["target"],
        "mutation_authorized": False,
        "phase": "ownership" if ownership_only else "admitted",
        "reason": "repository FIX mutation gates still required before every write",
    }
    if not ownership_only:
        result["ownership_lease"] = pp.encode_signed_lease({
            **lease, "purpose": OWNER_PROTOCOL,
        })
    # Actor I/O, DB renewal and progress-token key reads all consume lifetime.
    # Do not return admission (or a newly minted token) after signed expiry.
    validate_lease(lease, purpose=purpose)
    return result
