"""Restore local knowledge of an interrupted item using GitHub reads only."""
from datetime import datetime, timezone
import json
import re

from .pipeline_errors import PipelineError
from .tail_item import Item, ItemStop, _TTL, _check_item, _issue_rows, _match_issues


_REF_PREFIX = "refs/heads/pp-tail-dedupe/"


class RecoverySnapshot:
    def __init__(self, gh, config):
        self.gh = gh
        self.config = config
        self.rows = self.error = None
        self.refs = self.ref_error = None

    def has_ref(self, ref):
        if self.refs is None and self.ref_error is None:
            repo = self.config["repository"]
            prefix = _REF_PREFIX.removeprefix("refs/")
            try:
                timeout = min(self.config.get("github_timeout_seconds", 15), 15)
                response = self.gh.run("api", f"repos/{repo}/git/matching-refs/{prefix}", timeout_seconds=timeout)
                self.refs = _refs(response)
            except PipelineError as exc:
                self.ref_error = exc
        if self.ref_error is not None:
            raise self.ref_error
        return ref in self.refs

    def lookup(self, item, *, exact):
        if self.rows is None and self.error is None:
            path = f"repos/{self.config['repository']}/issues?state=all&per_page=100"
            try:
                timeout = min(self.config.get("github_timeout_seconds", 30), 30)
                raw = self.gh.run("api", "--paginate", path, "--jq", ".[]", timeout_seconds=timeout)
                self.rows = _issue_rows(raw)
            except PipelineError as exc:
                self.error = exc
        if self.error is not None:
            raise self.error
        matches = _match_issues(self.rows, self.config, item)
        if len(matches) > 1:
            raise ItemStop("ambiguous-create", "multiple issues carry the same canonical task identity")
        if exact and matches != _match_issues(self.rows, self.config, item, exact=True):
            return None
        return next(iter(matches), None)


def _refs(raw):
    try:
        rows = json.loads(raw)
    except ValueError as exc:
        raise PipelineError("invalid GitHub TAIL dedupe ref listing") from exc
    if not isinstance(rows, list):
        raise PipelineError("incomplete GitHub TAIL dedupe ref listing")
    result = set()
    for value in rows:
        name = value.get("ref") if isinstance(value, dict) else None
        obj = value.get("object") if isinstance(value, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if (not isinstance(name, str) or not re.fullmatch(_REF_PREFIX + "[0-9a-f]{64}", name)
                or name in result or not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{40}", sha)):
            raise PipelineError("could not verify a TAIL dedupe ref in the GitHub listing")
        result.add(name)
    return result


def inspect_item(config, row, source, index):
    item = Item.selected(row["pr_number"], source, index)
    try:
        records = _check_item(source, item, index, config["trusted_account"], for_recovery=True)
    except ItemStop as stop:
        return item, None, {"item": index, **stop.outcome}
    return item, records, None


def lease_active(lease):
    return lease is not None and datetime.now(timezone.utc) < lease[2] + _TTL


def reconcile_item(source, index, item, records, snapshot):
    lease, _, intents, dropped = records
    paused = source["state"] == "waiting-policy" or dropped
    active = lease_active(lease)
    ref = _REF_PREFIX + item.dedupe
    intent = intents[0][0] if intents else None
    evidence = {"item": index, "intent": intent, "ref": ref}
    try:
        if intent is not None or snapshot.has_ref(ref):
            existing = snapshot.lookup(item, exact=paused)
            if existing is not None:
                exact_source = paused or snapshot.lookup(item, exact=True) == existing
                return {**evidence, "state": "existing-issue", "issue": existing,
                        "exact_source": exact_source, "restore_ready": exact_source and not active,
                        "reason": "found the issue for the interrupted TAIL item"}, None
            if not active:
                reason = (f"no issue proved for create-intent {intent}" if intent is not None
                          else f"no issue proved for the existing dedupe ref {ref}")
                return {**evidence, "state": "ambiguous-create", "reason": reason}, None
    except ItemStop as stop:
        return {**evidence, **stop.outcome}, None
    except PipelineError as exc:
        return {**evidence, "state": "ambiguous-create", "reason": str(exc)}, str(exc)

    if dropped:
        return {"item": index, "state": "waiting-policy", "reason": "the maintainer dropped this item"}, None
    if active:
        return {**evidence, "state": "active-elsewhere",
                "reason": "an unexpired item lease belongs to another process"}, None
    return None, None
