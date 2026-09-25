"""Read-only, exact-target handoff from election to the full repository skill.

This is a scheduling proof, never authority to mutate GitHub. The repository
skill still owns every timeline, authorization, CI and compare-and-swap gate.
"""

from __future__ import annotations

import re
import time
from datetime import datetime


PROTOCOL = "promptpilot-fallback-target-v1"
PRE_REVIEW_VALIDATION_STAGE = "pre-review-validation"
PRE_REVIEW_SYNC_FIELDS = (
    "intent_comment_id", "done_comment_id", "from", "to", "base",
    "identity_sha256", "intent_created_at", "done_created_at",
)
CONTENT_REVIEW_STAGES = {"review", PRE_REVIEW_VALIDATION_STAGE}
INTEGRATION_REVIEW_STAGES = {"integration-review", "legacy-integration-review"}
REVIEW_STAGES = CONTENT_REVIEW_STAGES | INTEGRATION_REVIEW_STAGES
MERGE_STAGES = {"merge", "integration-merge-ready", "legacy-integration-merge-ready",
                "integration-merge-recovery"}
INTEGRATION_MERGE_STAGES = MERGE_STAGES - {"merge"}
INTEGRATION_STAGES = INTEGRATION_REVIEW_STAGES | INTEGRATION_MERGE_STAGES
TTL = 7200


def _rfc3339(value: object) -> datetime | None:
    if (not isinstance(value, str)
            or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                value,
            )):
        return None
    try:
        return datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None


def identity(value: dict) -> dict:
    from .project_pipeline import PipelineError

    if (not isinstance(value, dict) or type(value.get("number")) is not int
            or value["number"] <= 0 or not isinstance(value.get("head"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["head"])
            or not isinstance(value.get("stage"), str)
            or value.get("stage") not in REVIEW_STAGES | MERGE_STAGES):
        raise PipelineError("fallback target requires exact PR, HEAD and stage")
    target = {key: value[key] for key in ("number", "head", "stage")}
    metadata = value.get("pre_review_sync")
    if value["stage"] != PRE_REVIEW_VALIDATION_STAGE:
        if metadata is not None:
            raise PipelineError("fallback target has pre-review metadata on another stage")
        return target
    if not isinstance(metadata, dict) or set(metadata) != set(PRE_REVIEW_SYNC_FIELDS):
        raise PipelineError("pre-review validation requires exact sync metadata")
    if any(type(metadata[field]) is not int
           or not 0 < metadata[field] <= 2**63 - 1
           for field in ("intent_comment_id", "done_comment_id")):
        raise PipelineError("pre-review validation requires positive comment IDs")
    if any(not isinstance(metadata[field], str)
           or not re.fullmatch(r"[0-9a-f]{40}", metadata[field])
           for field in ("from", "to", "base")):
        raise PipelineError("pre-review validation requires exact commit identities")
    if (not isinstance(metadata["identity_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["identity_sha256"])):
        raise PipelineError("pre-review validation requires an exact identity digest")
    intent_created = _rfc3339(metadata["intent_created_at"])
    done_created = _rfc3339(metadata["done_created_at"])
    if intent_created is None or done_created is None:
        raise PipelineError("pre-review validation requires RFC3339 timestamps")
    if done_created <= intent_created:
        raise PipelineError("pre-review sync completion must follow its intent")
    if value["head"] != metadata["to"]:
        raise PipelineError("pre-review validation HEAD does not match sync destination")
    target["pre_review_sync"] = {field: metadata[field] for field in PRE_REVIEW_SYNC_FIELDS}
    return target


def validate_health(health: dict) -> None:
    """The opt-in requires the complete, internally consistent lane schema."""
    from .project_pipeline import PipelineError

    findings = health.get("findings")
    if (not isinstance(health.get("state"), str) or health.get("state") not in {"green", "yellow"}
            or not isinstance(findings, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("severity"), str)
                   or item.get("severity") not in {"green", "yellow"}
                   for item in findings)):
        raise PipelineError("fallback health is incomplete or red")
    owner = health.get("integration_owner")
    barriers = [item for item in findings if item.get("code") == "single_flight_barrier"]
    if owner is not None:
        owner = identity(owner)
        if (owner["stage"] not in INTEGRATION_STAGES or len(barriers) != 1
                or type(barriers[0].get("pr")) is not int
                or barriers[0]["pr"] != owner["number"]):
            raise PipelineError("fallback owner contradicts the single-flight barrier")
    elif barriers:
        raise PipelineError("fallback barrier has no exact owner")

    queues = {}
    for field, stages in (("review_candidates", REVIEW_STAGES),
                          ("content_review_candidates", CONTENT_REVIEW_STAGES),
                          ("merge_executable", MERGE_STAGES)):
        values = health.get(field)
        if not isinstance(values, list):
            raise PipelineError(f"fallback health is missing {field}")
        values = [identity(item) for item in values]
        if (len({item["number"] for item in values}) != len(values)
                or any(item["stage"] not in stages for item in values)):
            raise PipelineError(f"fallback health has invalid or duplicate {field}")
        queues[field] = values
    reviewing = queues["review_candidates"]
    content = queues["content_review_candidates"]
    merging = queues["merge_executable"]
    if owner is not None and owner["stage"] in INTEGRATION_REVIEW_STAGES:
        if reviewing != [owner] or merging:
            raise PipelineError("fallback integration REVIEW contradicts executable queues")
    else:
        if reviewing != content:
            raise PipelineError("fallback content and executable REVIEW queues disagree")
        if owner is not None:
            if merging != [owner]:
                raise PipelineError("fallback MERGE owner is not the sole executable target")
        elif any(item["stage"] != "merge" for item in merging):
            raise PipelineError("fallback integration MERGE target has no owner")
    if {item["number"] for item in reviewing + content} & {item["number"] for item in merging}:
        raise PipelineError("fallback PR appears in conflicting executable stages")


def rest_only_review_owner(health: dict) -> bool:
    """Recognize the one legacy REST snapshot that lacks the MERGE allowlist.

    Older pipelinehealth output can represent an integration-review owner while
    leaving ``merge_executable`` null. MERGE must keep routing that state to the
    full skill (which reconstructs GraphQL lineage), never turn it into a signed
    MERGE handoff and never defer it as a tool error. All other health fields
    still have to satisfy the target-v1 schema.
    """
    from .project_pipeline import PipelineError

    if health.get("merge_executable") is not None:
        return False
    compatible = dict(health, merge_executable=[])
    try:
        validate_health(compatible)
        owner = identity(compatible.get("integration_owner"))
    except PipelineError:
        return False
    return owner["stage"] in INTEGRATION_REVIEW_STAGES


def health_gate(health: dict, stage: str, target: dict, *, election: bool) -> None:
    from .project_pipeline import PipelineError

    validate_health(health)
    expected = identity(target)
    if stage not in {"review", "merge"} or expected["stage"] not in (
            REVIEW_STAGES if stage == "review" else MERGE_STAGES):
        raise PipelineError("fallback target belongs to another stage")
    owner = health.get("integration_owner")
    owner = identity(owner) if owner is not None else None

    field = "review_candidates" if stage == "review" else "merge_executable"
    if stage == "review" and expected["stage"] in CONTENT_REVIEW_STAGES and not election:
        field = "content_review_candidates"
    candidates = health.get(field)
    if not isinstance(candidates, list) or not candidates:
        raise PipelineError(f"fallback target absent from {field}")
    actual = [identity(item) for item in candidates]
    if len({item["number"] for item in actual}) != len(actual):
        raise PipelineError("fallback allowlist contains duplicate PRs")
    if expected not in actual or ((election or stage == "merge") and actual[0] != expected):
        raise PipelineError("fallback target no longer matches the exact executable candidate")
    if expected["stage"] in INTEGRATION_STAGES:
        if owner != expected or actual != [expected]:
            raise PipelineError("fallback integration target is not the sole executable owner")
        if stage == "review" and health.get("merge_executable") != []:
            raise PipelineError("fallback integration REVIEW contradicts merge_executable")
    elif stage == "merge" and owner is not None:
        raise PipelineError("fallback ordinary merge is blocked by an integration owner")
    if stage == "review" and expected["stage"] in CONTENT_REVIEW_STAGES:
        content = health.get("content_review_candidates")
        if not isinstance(content, list) or expected not in [identity(item) for item in content]:
            raise PipelineError("fallback content target was not proved by health")
        if election and owner is not None and owner["stage"] in INTEGRATION_REVIEW_STAGES:
            raise PipelineError("fallback content election bypasses integration REVIEW")


def config_digest(config: dict) -> str:
    from .project_pipeline import digest

    return digest(config)


def create(config: dict, health: dict, stage: str, target: dict, reason: str,
           *, target_reservation: dict | None = None) -> dict:
    from . import project_pipeline as pp

    health_gate(health, stage, target, election=True)
    target = identity(target)
    issued = int(time.time())
    replicas = pp._configured_replica_count()
    if replicas > 1 and target_reservation is None:
        raise pp.PipelineError(
            "replicated REVIEW fallback has no target reservation")
    lease = {"version": 1, "purpose": PROTOCOL, "stage": stage,
             "repository": config["repository"], "target": target,
             "config_sha256": config_digest(config), "issued_at": issued,
             "expires_at": issued + TTL}
    if target_reservation is not None:
        repository, target_stage, number, head, task_id, _token = \
            pp._reservation_identity(target_reservation)
        if (repository != str(config["repository"]).lower()
                or target_stage != target["stage"] or number != target["number"]
                or head != target["head"]
                or pp._pipeline_task_id(required=True) != task_id):
            raise pp.PipelineError(
                "pipeline target reservation contradicts fallback target")
        lease["target_reservation"] = target_reservation
        lease["pipeline_replicas"] = replicas
    return {"action": "fallback", "reason": reason, "target": target,
            "handoff": {"protocol": PROTOCOL, "stage": stage,
                        "repository": config["repository"], "target": target,
                        "lease": pp.encode_signed_lease(lease)}}


def validate(preflight: dict, stage: str) -> dict:
    """Validate the complete envelope before it can enter the provider prompt."""
    from .project_pipeline import PipelineError, decode_signed_lease

    handoff = preflight.get("handoff")
    if (preflight.get("action") != "fallback" or not isinstance(handoff, dict)
            or handoff.get("protocol") != PROTOCOL or handoff.get("stage") != stage
            or not isinstance(handoff.get("lease"), str)):
        raise PipelineError("invalid fallback handoff protocol or stage")
    lease = decode_signed_lease(handoff.get("lease"))
    validate_lease(lease, stage)
    if (handoff.get("repository") != lease["repository"]
            or identity(handoff.get("target")) != lease["target"]
            or identity(preflight.get("target")) != lease["target"]):
        raise PipelineError("fallback envelope contradicts its signed target")
    return lease


def validate_lease(lease: dict, stage: str) -> None:
    from . import project_pipeline as pp
    from .project_pipeline import PipelineError

    if (lease.get("purpose") != PROTOCOL or lease.get("stage") != stage
            or stage not in {"review", "merge"}
            or not isinstance(lease.get("repository"), str)
            or not re.fullmatch(r"[^/\s]+/[^/\s]+", lease["repository"])
            or not isinstance(lease.get("config_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", lease["config_sha256"])):
        raise PipelineError("invalid fallback lease")
    target = identity(lease.get("target"))
    if target["stage"] not in (REVIEW_STAGES if stage == "review" else MERGE_STAGES):
        raise PipelineError("fallback lease target belongs to another stage")
    issued, expires = lease.get("issued_at"), lease.get("expires_at")
    now = int(time.time())
    if (type(issued) is not int or type(expires) is not int or issued > now + 60
            or expires <= now or not 0 < expires - issued <= TTL):
        raise PipelineError("fallback lease is expired or has an invalid validity window")
    reservation = lease.get("target_reservation")
    replicas = lease.get("pipeline_replicas", 1)
    if type(replicas) is not int or not 1 <= replicas <= 16:
        raise PipelineError("fallback lease has an invalid replica count")
    if replicas > 1 and reservation is None:
        raise PipelineError(
            "replicated REVIEW fallback lease has no target reservation")
    if reservation is not None:
        repository, target_stage, number, head, _task_id, _token = \
            pp._reservation_identity(reservation)
        if (repository != str(lease["repository"]).lower()
                or target_stage != target["stage"] or number != target["number"]
                or head != target["head"]):
            raise PipelineError(
                "pipeline target reservation contradicts fallback lease")


def gate(gh, config: dict, stage: str, lease_value: str, *, config_path=None) -> dict:
    """Fresh global scheduling gate; zero GitHub writes and no re-election."""
    from . import project_pipeline as pp

    lease = pp.decode_signed_lease(lease_value)
    validate_lease(lease, stage)

    def check_config():
        if (config.get("fallback_handoff") != "target-v1"
                or config.get("repository") != lease["repository"]
                or config_digest(config) != lease["config_sha256"]):
            raise pp.PipelineError("fallback configuration changed; start a new task")

    check_config()
    health = pp.run_health(config, config_path=config_path)
    check_config()  # run_health may fast-forward and reload the project config
    validate_lease(lease, stage)  # scan time counts toward expiry
    pp.ensure_identity(gh, config)
    if stage == "merge" and pp.pending_merge_intents(gh, config):
        raise pp.PipelineError("pending merge cleanup takes precedence; start a new task")
    health_gate(health, stage, lease["target"], election=False)
    validate_lease(lease, stage)
    if lease.get("target_reservation") is not None:
        pp.renew_lease_target_reservation({
            "stage": stage,
            "target_stage": lease["target"]["stage"],
            "repository": lease["repository"],
            "number": lease["target"]["number"],
            "head": lease["target"]["head"],
            "target_reservation": lease["target_reservation"],
            "pipeline_replicas": lease.get("pipeline_replicas", 1),
        }, config)
    return {"action": "validated", "stage": stage, "repository": lease["repository"],
            "target": lease["target"], "mutation_authorized": False,
            "reason": "fresh scheduling gate passed; full repository mutation gates still required"}
