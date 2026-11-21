"""Publish the missing TAIL item-done for one independently proved issue."""

from .pipeline_errors import PipelineError
from .project_pipeline import ensure_identity, post_comment
from .tail_item import ItemStop
from .tail_recovery import RecoverySnapshot, _REF_PREFIX, inspect_item, lease_active


def _inspect(config, row, source, index, issue):
    item, records, outcome = inspect_item(config, row, source, index)
    if outcome is not None:
        if outcome["state"] != "item-done":
            raise PipelineError(outcome["reason"])
        if outcome["issue"] != issue:
            raise PipelineError("TAIL restore found an item-done for a different issue")
        outcome = {**outcome, "already_present": True}
    elif lease_active(records[0]):
        outcome = {"state": "active-elsewhere",
                   "reason": "an unexpired item lease belongs to another process"}
    return item, records, outcome


def _prove_issue(gh, config, item, records, issue):
    # Use a new snapshot for each proof so a cached absence or earlier exact
    # receipt cannot authorize a write after the issue has changed.
    snapshot = RecoverySnapshot(gh, config)
    if not records[2] and not snapshot.has_ref(_REF_PREFIX + item.dedupe):
        raise PipelineError("TAIL restore has no create intent or dedupe ref")
    try:
        actual = snapshot.lookup(item, exact=True)
    except ItemStop as exc:
        raise PipelineError(exc.outcome["reason"]) from exc
    if actual != issue:
        raise PipelineError("TAIL restore could not prove the requested exact-source issue")


def restore_item(gh, config, number, index, issue, recheck, before_publish):
    """Finalize an already-created issue while holding the shared PR guard.

    `recheck` must independently establish the exact requested review version.
    `before_publish` atomically journals a possibly accepted POST. The journal
    does not authorize retry if the first response cannot be confirmed.
    """
    if type(issue) is not int or issue <= 0:
        raise PipelineError("expected a positive TAIL issue number")

    row, source = recheck()
    item, records, outcome = _inspect(config, row, source, index, issue)
    if outcome is not None:
        return outcome

    _prove_issue(gh, config, item, records, issue)
    ensure_identity(gh, config)
    _prove_issue(gh, config, item, records, issue)
    current_row, current = recheck()
    current_item, current_records, outcome = _inspect(config, current_row, current, index, issue)
    if outcome is not None:
        return outcome
    if (item != current_item or records != current_records or source["state"] != current["state"]
            or source["comments"] != current["comments"]):
        raise PipelineError("TAIL selected review or controls changed during restore")

    body = f"<!-- pp:tail-item-done {item.key} issue={issue} -->"
    before_publish()
    try:
        posted = post_comment(gh, config, number, body)
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise PipelineError("restored TAIL item-done response could not be verified") from exc
    cid = posted.get("id")
    if type(cid) is not int or cid <= 0:
        raise PipelineError("no returned id for the restored TAIL item-done")

    published_row, published = recheck()
    _, _, confirmed = _inspect(config, published_row, published, index, issue)
    if confirmed is None or confirmed["state"] != "item-done":
        raise PipelineError("restored TAIL item-done is not confirmed by the current source")
    if confirmed["completion_id"] != cid:
        raise PipelineError("restored TAIL item-done receipt differs from the canonical marker")
    return {**confirmed, "already_present": False}
