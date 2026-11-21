"""Connect the repository's TAIL procedure to discovered PRs and their current review."""
import hashlib
import json
import re

from . import tail_admission, tail_recovery, tail_store
from .pipeline_errors import PipelineError


_PROOF = ("review_id", "review_updated_at", "review_body_digest", "epoch_digest")
_RESULT = ("state", "reason", *_PROOF, "completion_kind", "completion_digest", "next_check_at",
           "recovery", "proof_error")


def _version(row, result):
    if not all(result.get(key) for key in _PROOF):
        return None
    identity = [row[key] for key in ("repository", "base", "pr_number", "head", "merged_at")]
    data = json.dumps([*identity, *(result[key] for key in _PROOF)], separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def _observation(row, result):
    summary = {"number": row["pr_number"], "state": result["state"],
               "version": _version(row, result)}
    for key in ("reason", "completion_kind", "review_id", "review_updated_at", "protocol", "parse_error", "recovery"):
        if result.get(key) is not None:
            summary[key] = result[key]
    if result["state"] == "ready":
        summary["items"] = result["items"]
    return summary


def _record(row, result):
    prior_error = row.get("proof_error") or {}
    if "proof_error" not in result and prior_error.get("kind") == "source":
        result = dict(result, proof_error=None)
    error = result.get("proof_error")
    if error:
        result = dict(result, proof_error={**error, "reason": error["reason"][:1000]})
    try:
        return tail_store.record_result(row, **{k: result[k] for k in _RESULT if k in result})
    except tail_store.TailCandidateConflict:
        # Both readers may hand off the same remote proof. Neither owns the
        # item until the GitHub lease elects it. A different completion (or
        # a re-opened candidate) is a real conflict even within one review.
        if _version(row, result) is None:
            raise
        current = tail_store.get_candidate(row["repository"], row["base"], row["pr_number"])
        if (not current or current["state"] != result["state"]
                or _version(current, current) != _version(row, result)
                or current["completion_kind"] != result.get("completion_kind")
                or current["completion_digest"] != result.get("completion_digest")
                or "recovery" in result
                and (current.get("recovery") or []) != (result["recovery"] or [])
                or "proof_error" in result and current.get("proof_error") != result["proof_error"]):
            raise
        return current["revision"]


def _row(config, number):
    if number <= 0:
        raise PipelineError("expected a positive TAIL PR number")
    row = tail_store.get_candidate(config["repository"].lower(), config["base_branch"], number)
    if row is None:
        raise PipelineError("PR is not in the discovered queue for this repository and base")
    return row


def _read(gh, config, row):
    try:
        result = tail_admission.classify(gh, config, row)
        if result["state"] in ("ready", "resolved") and _version(row, result) is None:
            raise PipelineError("TAIL admission returned incomplete review evidence")
        return result
    except PipelineError as exc:
        # Every entry point must surface a failed recheck of an old completion.
        # Preserve the known source and let a concurrent observation stand.
        proof_error = row.get("proof_error") or {}
        if proof_error.get("kind") != "issue":
            proof_error = {"kind": "source", "reason": str(exc)}
        try:
            _record(row, {"state": "invalid-source", "reason": str(exc),
                          "proof_error": proof_error})
        except tail_store.TailCandidateConflict:
            pass
        raise


def _project_terminal_items(row, source, outcomes):
    invalid = next((item for item in outcomes if item["state"] == "invalid-source"), None)
    if invalid:
        return dict(source, state="invalid-source", reason=invalid["reason"], recovery=outcomes)
    issue_count = sum(item["kind"] == "issue" for item in source.get("items", ()))
    if issue_count and len(outcomes) == issue_count and all(item["state"] == "item-done" for item in outcomes):
        proof = [_version(row, source), [[item["item"], item["issue"], item["completion_id"]] for item in outcomes]]
        digest = hashlib.sha256(json.dumps(proof, separators=(",", ":")).encode("ascii")).hexdigest()
        return dict(source, state="resolved", completion_kind="versioned", completion_digest=digest,
                    reason="TAIL item completions verified", recovery=outcomes)
    return source


def _inspect_items(config, row, source):
    for item in source.get("items", ()):
        if item["kind"] == "issue":
            index = item["index"]
            yield index, *tail_recovery.inspect_item(config, row, source, index)


def _selection(config, row, result):
    result = _project_terminal_items(
        row, result, [outcome for _, _, _, outcome in _inspect_items(config, row, result) if outcome is not None])
    if (result["state"] in ("ready", "waiting-policy") and row["state"] == "ambiguous-create"
            and _version(row, row) == _version(row, result)):
        # A fresh admission check does not establish whether an interrupted
        # create succeeded. Preserve its wait until a reconciliation can look.
        return dict(result, state="ambiguous-create", reason=row["last_reason"],
                    next_check_at=row["next_check_at"], recovery=row.get("recovery") or [])
    return result


def _recovery_source_unchanged(gh, config, row, source):
    # Issue listing may take several pages. Its result is local evidence only
    # for the same freshly checked review and controls.
    current = _read(gh, config, row)
    return (_version(row, current) == _version(row, source)
            and current["state"] == source["state"]
            and current.get("comments") == source.get("comments"))


def _recorded_ambiguity(row, version, index):
    if row["state"] != "ambiguous-create" or _version(row, row) != version:
        return None
    recovery = row.get("recovery")
    if recovery:
        return next((item for item in recovery if item["item"] == index and item["state"] == "ambiguous-create"), None)
    return {"item": index, "state": "ambiguous-create", "reason": row["last_reason"] or "previous TAIL create is unresolved"}


def next_candidates(gh, config):
    rows = tail_store.due_candidates(config["repository"].lower(), config["base_branch"], limit=5)
    observations, ready, failures = [], [], []
    for row in rows:
        try:
            result = _selection(config, row, _read(gh, config, row))
            _record(row, result)
        except tail_store.TailCandidateConflict:
            observations.append({"number": row["pr_number"], "state": "changed-concurrently"})
            continue
        except PipelineError as exc:
            result = {"state": "invalid-source", "reason": str(exc)}
            failures.append({"number": row["pr_number"], "reason": str(exc)})
        observation = _observation(row, result)
        observations.append(observation)
        if result["state"] == "ready":
            ready.append(observation)
    if failures and len(failures) == len(rows):
        raise PipelineError("TAIL source checks failed: " + "; ".join(
            f"#{failure['number']} {failure['reason']}" for failure in failures))
    return {"action": "tail-next", "ready": ready, "observations": observations, "errors": failures}


def _require_same(row, result, expected):
    if re.fullmatch("[0-9a-f]{64}", expected) is None or _version(row, result) != expected:
        raise PipelineError("TAIL review version changed or its proof is no longer available")


def gate(gh, config, number, version):
    row = _row(config, number)
    result = _selection(config, row, _read(gh, config, row))
    _record(row, result)
    _require_same(row, result, version)
    if result["state"] not in ("ready", "resolved"):
        raise PipelineError(result.get("reason") or result["state"])
    return {"action": "tail-gate", **_observation(row, result)}


def reconcile(gh, config, number=None):
    """Recheck up to five due candidates and persist recovery evidence locally."""
    rows = ([_row(config, number)] if number is not None else
            tail_store.due_candidates(config["repository"].lower(), config["base_branch"], limit=5))
    snapshot = tail_recovery.RecoverySnapshot(gh, config)
    observations, errors = [], []
    for row in rows:
        pr_number = row["pr_number"]
        try:
            result = _read(gh, config, row)
            if result["state"] in ("ready", "waiting-policy"):
                outcomes = []
                issue_error = None
                for index, item, records, outcome in _inspect_items(config, row, result):
                    if outcome is None:
                        outcome, error = tail_recovery.reconcile_item(result, index, item, records, snapshot)
                        if (outcome and outcome.get("restore_ready")
                                and tail_store.pending_restore(row["repository"], pr_number,
                                                               _version(row, result), index)):
                            outcome = dict(outcome, restore_ready=False,
                                           reason="previous TAIL marker write is unconfirmed")
                        if error is not None:
                            errors.append({"number": pr_number, "item": index, "reason": error})
                            issue_error = issue_error or error
                        previous = _recorded_ambiguity(row, _version(row, result), index)
                        if previous is not None and (outcome is None
                                or outcome["state"] not in ("existing-issue", "ambiguous-create", "active-elsewhere")):
                            outcome = previous
                    if outcome is not None:
                        outcomes.append(outcome)
                needs_recheck = any(item["state"] == "existing-issue"
                                    or issue_error is None and item["state"] == "ambiguous-create" for item in outcomes)
                if needs_recheck and not _recovery_source_unchanged(gh, config, row, result):
                    reason = "TAIL selected review or controls changed during recovery lookup"
                    result = {"state": "invalid-source", "reason": reason}
                    errors.append({"number": pr_number, "reason": reason})
                else:
                    result = _project_recovery(row, result, outcomes)
                result["proof_error"] = {"kind": "issue", "reason": issue_error} if issue_error else None
            else:
                result["proof_error"] = None
            _record(row, result)
        except tail_store.TailCandidateConflict:
            observations.append({"number": pr_number, "state": "changed-concurrently"})
            continue
        except PipelineError as exc:
            result = {"state": "invalid-source", "reason": str(exc)}
            errors.append({"number": pr_number, "reason": str(exc)})
        observations.append(_observation(row, result))
    return {"action": "tail-reconcile", "observations": observations, "errors": errors}


def _project_recovery(row, source, outcomes):
    result = _project_terminal_items(row, source, outcomes)
    ambiguous = next((item for item in outcomes if item["state"] == "ambiguous-create"), None)
    if ambiguous and result["state"] in ("ready", "waiting-policy"):
        result = dict(result, state="ambiguous-create", reason=ambiguous["reason"])
    active = next((item for item in outcomes if item["state"] == "active-elsewhere"), None)
    if (active and result["state"] == "ready"
            and len(outcomes) == sum(item["kind"] == "issue" for item in result["items"])
            and not any(item["state"] == "existing-issue" for item in outcomes)):
        result = dict(result, state="active-elsewhere", reason=active["reason"])
    return dict(result, recovery=outcomes)


def create(gh, config, number, version, item, context_file=None):
    row = _row(config, number)
    with tail_store.item_writer(row["repository"], number) as acquired:
        if not acquired:
            return _busy_writer("tail-create", number, item)
        return _create(gh, config, number, version, item, context_file)


def _busy_writer(action, number, item):
    return {"action": action, "number": number, "item": item, "state": "active-elsewhere",
            "reason": "another process holds the local TAIL writer lock for this PR"}


def _create(gh, config, number, version, item, context_file):
    from .tail_item import apply_item, read_issue_context

    context = read_issue_context(context_file)
    def recheck():
        row = _row(config, number)
        result = _read(gh, config, row)
        if result["state"] != "ready" or _version(row, result) != version:
            _record(row, result)
        _require_same(row, result, version)
        if result["state"] != "ready":
            raise PipelineError(result.get("reason") or result["state"])
        return row, result

    row = _row(config, number)
    source = _read(gh, config, row)
    if source["state"] not in ("ready", "waiting-policy") or _version(row, source) != version:
        _record(row, source)
        _require_same(row, source, version)
        raise PipelineError(source.get("reason") or source["state"])
    _require_same(row, source, version)
    identity, records, outcome = tail_recovery.inspect_item(config, row, source, item)
    if outcome is None and tail_store.pending_restore(row["repository"], number, version, item):
        raise PipelineError("previous TAIL marker write is unconfirmed; verify it through tail-restore")
    paused = source["state"] == "waiting-policy"
    same_version = _version(row, row) == version
    prior_recovery = (row.get("recovery") or []) if same_version else []
    other_outcomes = [value for value in prior_recovery if value["item"] != item]
    previous = _recorded_ambiguity(row, version, item)
    recovery_error = None
    if outcome is None and (paused or previous is not None or records[2]):
        snapshot = tail_recovery.RecoverySnapshot(gh, config)
        outcome, recovery_error = tail_recovery.reconcile_item(source, item, identity, records, snapshot)
        if previous is not None and (outcome is None or outcome["state"] not in (
                "existing-issue", "ambiguous-create", "active-elsewhere")):
            outcome = {key: value for key, value in previous.items() if key != "item"}
        if (outcome is not None and outcome["state"] in ("existing-issue", "ambiguous-create")
                and recovery_error is None and not _recovery_source_unchanged(gh, config, row, source)):
            reason = "TAIL selected review or controls changed during recovery lookup"
            _record(row, {"state": "invalid-source", "reason": reason})
            raise PipelineError(reason)
    if outcome is None:
        if paused:
            _record(row, _selection(config, row, source))
            raise PipelineError(source.get("reason") or source["state"])
        outcome = apply_item(gh, config, row, source, item, recheck, context)
    recovery = sorted([*other_outcomes, {"item": item, **outcome}], key=lambda value: value["item"])
    projection = source
    for state in ("invalid-source", "ambiguous-create", "active-elsewhere"):
        failure = next((value for value in recovery if value["state"] == state), None)
        if failure is not None:
            projection = dict(source, state=state, reason=failure["reason"])
            break
    if recovery_error:
        projection = dict(projection, proof_error={"kind": "issue", "reason": recovery_error})
    # Only update the observation actually examined by this process. A
    # concurrent reclassification must not be undone by a late result.
    try:
        _record(row, dict(projection, recovery=recovery))
    except tail_store.TailCandidateConflict:
        pass
    return {"action": "tail-create", "number": number, "item": item, **outcome}


def restore(gh, config, number, version, item, issue):
    """Finish a previously created issue through the shared local writer."""
    from .tail_restore import restore_item

    row = _row(config, number)
    repository = row["repository"]
    with tail_store.item_writer(repository, number) as acquired:
        if not acquired:
            return _busy_writer("tail-restore", number, item)
        return _restore(gh, config, number, version, item, issue, restore_item, repository)


def _restore(gh, config, number, version, item, issue, restore_item, repository):
    latest = None

    def recheck():
        nonlocal latest
        row = _row(config, number)
        source = _read(gh, config, row)
        _require_same(row, source, version)
        if source["state"] not in ("ready", "waiting-policy"):
            raise PipelineError(source.get("reason") or source["state"])
        latest = row, source
        return latest

    def before_publish():
        tail_store.restore_attempt(repository, number, version, item, issue)

    outcome = restore_item(gh, config, number, item, issue, recheck, before_publish)
    if outcome["state"] == "item-done":
        row, source = latest
        tail_store.restore_attempt(repository, number, version, item, issue, confirmed=True)
        prior = (row.get("recovery") or []) if _version(row, row) == version else []
        known = {entry["item"]: entry for entry in prior if entry["state"] != "item-done"}
        for index, _, _, observed in _inspect_items(config, row, source):
            if observed is not None:
                known[index] = observed
        result = _project_recovery(row, source, [known[index] for index in sorted(known)])
        if result["state"] == "resolved":
            result["proof_error"] = None
        try:
            _record(row, result)
        except tail_store.TailCandidateConflict:
            pass  # A later index observation cannot undo the confirmed GitHub marker.
    return {"action": "tail-restore", "number": number, "item": item, **outcome}
