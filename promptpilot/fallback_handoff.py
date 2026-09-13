"""Read-only, exact-target handoff from election to the full repository skill.

This is a scheduling proof, never authority to mutate GitHub. The repository
skill still owns every timeline, authorization, CI and compare-and-swap gate.
"""

from __future__ import annotations

import re
import time


PROTOCOL = "promptpilot-fallback-target-v1"
REVIEW_STAGES = {"review", "integration-review", "legacy-integration-review"}
MERGE_STAGES = {"merge", "integration-merge-ready", "legacy-integration-merge-ready",
                "integration-merge-recovery"}
TTL = 7200


def identity(value: dict) -> dict:
    from .project_pipeline import PipelineError

    if (not isinstance(value, dict) or type(value.get("number")) is not int
            or value["number"] <= 0 or not isinstance(value.get("head"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["head"])
            or not isinstance(value.get("stage"), str)
            or value.get("stage") not in REVIEW_STAGES | MERGE_STAGES):
        raise PipelineError("fallback target requires exact PR, HEAD and stage")
    return {key: value[key] for key in ("number", "head", "stage")}


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
        if (owner["stage"] in {"review", "merge"} or len(barriers) != 1
                or type(barriers[0].get("pr")) is not int
                or barriers[0]["pr"] != owner["number"]):
            raise PipelineError("fallback owner contradicts the single-flight barrier")
    elif barriers:
        raise PipelineError("fallback barrier has no exact owner")

    queues = {}
    for field, stages in (("review_candidates", REVIEW_STAGES),
                          ("content_review_candidates", {"review"}),
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
    if owner is not None and owner["stage"] in REVIEW_STAGES:
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
    return owner["stage"] in {"integration-review", "legacy-integration-review"}


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
    if stage == "review" and expected["stage"] == "review" and not election:
        field = "content_review_candidates"
    candidates = health.get(field)
    if not isinstance(candidates, list) or not candidates:
        raise PipelineError(f"fallback target absent from {field}")
    actual = [identity(item) for item in candidates]
    if len({item["number"] for item in actual}) != len(actual):
        raise PipelineError("fallback allowlist contains duplicate PRs")
    if expected not in actual or ((election or stage == "merge") and actual[0] != expected):
        raise PipelineError("fallback target no longer matches the exact executable candidate")
    if expected["stage"] not in {"review", "merge"}:
        if owner != expected or actual != [expected]:
            raise PipelineError("fallback integration target is not the sole executable owner")
        if stage == "review" and health.get("merge_executable") != []:
            raise PipelineError("fallback integration REVIEW contradicts merge_executable")
    elif stage == "merge" and owner is not None:
        raise PipelineError("fallback ordinary merge is blocked by an integration owner")
    if stage == "review" and expected["stage"] == "review":
        content = health.get("content_review_candidates")
        if not isinstance(content, list) or expected not in [identity(item) for item in content]:
            raise PipelineError("fallback content target was not proved by health")
        if election and owner is not None and owner["stage"] in REVIEW_STAGES:
            raise PipelineError("fallback content election bypasses integration REVIEW")


def config_digest(config: dict) -> str:
    from .project_pipeline import digest

    return digest(config)


def create(config: dict, health: dict, stage: str, target: dict, reason: str) -> dict:
    from .project_pipeline import encode_signed_lease

    health_gate(health, stage, target, election=True)
    target = identity(target)
    issued = int(time.time())
    lease = {"version": 1, "purpose": PROTOCOL, "stage": stage,
             "repository": config["repository"], "target": target,
             "config_sha256": config_digest(config), "issued_at": issued,
             "expires_at": issued + TTL}
    return {"action": "fallback", "reason": reason, "target": target,
            "handoff": {"protocol": PROTOCOL, "stage": stage,
                        "repository": config["repository"], "target": target,
                        "lease": encode_signed_lease(lease)}}


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
    return {"action": "validated", "stage": stage, "repository": lease["repository"],
            "target": lease["target"], "mutation_authorized": False,
            "reason": "fresh scheduling gate passed; full repository mutation gates still required"}
