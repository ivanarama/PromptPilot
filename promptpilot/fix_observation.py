"""Explain a saved FIX candidate list without running the scheduler or checker."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from . import fix_handoff
from .pipeline_errors import PipelineError
from .review_routing_policy import settings as handoff_settings


def _reservations(path: str, repository: str, now: str) -> set[int]:
    # Do not import db.py here. Importing it opens and initializes the scheduler DB.
    uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as conn:
            conn.execute("PRAGMA query_only=ON")
            return {number for (number,) in conn.execute(
                """SELECT r.number FROM pipeline_target_reservations r
                   WHERE r.repository = ? AND (r.expires_at > ? OR EXISTS (
                       SELECT 1 FROM tasks t WHERE t.id = r.task_id
                         AND t.status = 'running'
                         AND (r.task_started_at = '' OR t.started_at = r.task_started_at)
                   ))""", (repository, now)
            )}
    except sqlite3.Error as exc:
        raise PipelineError(f"unable to read FIX reservations: {exc}") from exc


def _identities(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        target = fix_handoff.identity(item)
        revision = target.pop("head")
        target["revision"] = revision
        result.append(target)
    return result


def _ordinary_observations(health: dict) -> list[dict]:
    """Old checkers can report issues without an election revision."""
    items = health.get("fix_candidates")
    if not isinstance(items, list):
        raise PipelineError("checker lacks the ordinary FIX candidate list")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise PipelineError("checker returned an invalid ordinary FIX candidate")
        number, stage = item.get("number"), item.get("stage")
        if type(number) is not int or number <= 0 or number in seen:
            raise PipelineError("checker returned an invalid or duplicate ordinary FIX number")
        if stage in ("review", "fix-pr"):
            stage = "fix-pr"
        elif stage in ("fix", "fix-issue"):
            stage = "fix-issue"
        else:
            raise PipelineError("checker returned an unsupported ordinary FIX candidate kind")
        seen.add(number)
        result.append({"number": number, "stage": stage})
    return result


def observe(config: dict, health_path: str, reservations_db: str | None = None) -> dict:
    health = json.loads(Path(health_path).read_text(encoding="utf-8-sig"))
    fix_handoff.require_usable_health_shape(health)
    repository = config["repository"].strip().lower()
    snapshot_repo = health.get("repository")
    if snapshot_repo is not None and (
        not isinstance(snapshot_repo, str) or snapshot_repo.lower() != repository
    ):
        raise PipelineError("FIX snapshot belongs to a different repository")

    handoff = handoff_settings(config)
    if handoff and handoff["enabled"]:
        route = "source_bound"
        candidates = _identities(fix_handoff.technical_health_candidates(health))
    elif config.get("parallel_fix_enabled") is True:
        route = "parallel"
        candidates = _identities(fix_handoff.ordered_health_candidates(health))
    else:
        route = "legacy"
        candidates = []

    now = datetime.now(timezone.utc).isoformat()
    reserved = _reservations(reservations_db, repository, now) if reservations_db else None
    for candidate in candidates:
        if reserved is None:
            reason = "local_reservations_not_checked"
        elif candidate["number"] in reserved:
            reason = "locally_reserved"
        else:
            reason = "no_local_reservation_seen"
        candidate["reason"] = reason

    if route != "parallel" and (route == "legacy" or "fix_candidates" in health):
        seen = {item["number"] for item in candidates}
        candidates.extend(
            {**item, "reason": "legacy_skill_selects" if route == "legacy" else "outside_source_bound_route"}
            for item in _ordinary_observations(health)
            if item["number"] not in seen
        )
    for candidate in candidates:
        candidate["locally_reserved"] = candidate["number"] in reserved if reserved is not None else None

    return {
        "action": "observation",
        "stage": "fix",
        "repository": repository,
        "route": route,
        "health_state": health["state"],
        "snapshot_repository_verified": snapshot_repo is not None,
        "local_reservations_checked_at": now if reserved is not None else None,
        "candidates": candidates,
        "mutation_authorized": False,
    }
