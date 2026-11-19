"""Connect the repository's TAIL procedure to discovered PRs and their current review."""
import hashlib
import json
import re

from . import tail_admission, tail_store
from .pipeline_errors import PipelineError


_PROOF = ("review_id", "review_updated_at", "review_body_digest", "epoch_digest")
_RESULT = ("state", "reason", *_PROOF, "completion_kind", "completion_digest", "next_check_at")


def _version(row, result):
    if not all(result.get(key) for key in _PROOF):
        return None
    identity = [row[key] for key in ("repository", "base", "pr_number", "head", "merged_at")]
    data = json.dumps([*identity, *(result[key] for key in _PROOF)], separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def _observation(row, result):
    summary = {"number": row["pr_number"], "state": result["state"],
               "version": _version(row, result)}
    for key in ("reason", "completion_kind", "review_id", "review_updated_at", "protocol"):
        if result.get(key) is not None:
            summary[key] = result[key]
    if result["state"] == "ready":
        summary["items"] = result["items"]
    return summary


def _record(row, result):
    return tail_store.record_result(row, **{k: result[k] for k in _RESULT if k in result})


def _row(config, number):
    if number <= 0:
        raise PipelineError("expected a positive TAIL PR number")
    row = tail_store.get_candidate(config["repository"].lower(), config["base_branch"], number)
    if row is None:
        raise PipelineError("PR is not in the discovered queue for this repository and base")
    return row


def _read(gh, config, row):
    result = tail_admission.classify(gh, config, row)
    if result["state"] in ("ready", "resolved") and _version(row, result) is None:
        raise PipelineError("TAIL admission returned incomplete review evidence")
    return result


def next_candidates(gh, config):
    rows = tail_store.due_candidates(config["repository"].lower(), config["base_branch"], limit=5)
    observations, ready, failures = [], [], []
    for row in rows:
        try:
            result = _read(gh, config, row)
            _record(row, result)
        except tail_store.TailCandidateConflict:
            # Another reader may have classified the very same remote proof.
            # Both entry points can hand it off; only the GitHub lease elects
            # the process that may create. Changed observations must be reread.
            current = tail_store.get_candidate(row["repository"], row["base"], row["pr_number"])
            if (current and current["state"] == result["state"] == "ready"
                    and _version(row, result) == _version(current, current)):
                observation = _observation(row, result)
                ready.append(observation)
                observations.append(observation)
                continue
            observations.append({"number": row["pr_number"], "state": "changed-concurrently"})
            continue
        except PipelineError as exc:
            # Preserve the previous proof, leave the candidate due for a retry,
            # and allow other PRs in the same bounded batch to proceed.
            result = {"state": "invalid-source", "reason": str(exc)}
            try:
                _record(row, result)
            except tail_store.TailCandidateConflict:
                pass
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
    result = _read(gh, config, row)
    _record(row, result)
    _require_same(row, result, version)
    if result["state"] not in ("ready", "resolved"):
        raise PipelineError(result.get("reason") or result["state"])
    return {"action": "tail-gate", **_observation(row, result)}


def create(gh, config, number, version, item, context_file=None):
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

    row, source = recheck()
    outcome = apply_item(gh, config, row, source, item, recheck, context)
    if outcome["state"] in ("ambiguous-create", "active-elsewhere", "invalid-source"):
        # Only update the observation actually examined by this process. A
        # concurrent reclassification must not be undone by a late result.
        try:
            _record(row, dict(source, state=outcome["state"], reason=outcome["reason"]))
        except tail_store.TailCandidateConflict:
            pass
    return {"action": "tail-create", "number": number, "item": item, **outcome}
