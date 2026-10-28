"""Offline report schema for plan 195 A-PP, preparatory slice A1.

Validation establishes structure, never authenticity, successful reproduction,
or permission to publish a review or assign FIX. No handoff reader is enabled.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .pipeline_errors import PipelineError


VERSION = "review-routing-v1"
MAX_REPORT_BYTES = 262144


def _keys(value, required: set[str], optional: set[str] | None = None):
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - (optional or set())):
        raise PipelineError("invalid routing report fields")


def _text(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise PipelineError("routing report requires nonempty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PipelineError("routing report contains invalid UTF-8") from exc


def _technical(evidence):
    _keys(evidence, {"entrypoint", "environment", "command", "expected", "actual",
                     "result", "invariant"})
    for value in evidence.values():
        _text(value)


def _product(evidence):
    _keys(evidence, {"options", "recommendation"})
    options = evidence["options"]
    if not isinstance(options, list) or len(options) < 2:
        raise PipelineError("product blocker requires at least two options")
    numbers = set()
    for option in options:
        _keys(option, {"number", "behavior", "consequences"})
        number = option["number"]
        if type(number) is not int or not 1 <= number <= 2147483647 or number in numbers:
            raise PipelineError("product option numbers must be unique positive integers")
        numbers.add(number)
        _text(option["behavior"])
        _text(option["consequences"])
    recommendation = evidence["recommendation"]
    _keys(recommendation, {"number", "reason"})
    if (type(recommendation["number"]) is not int
            or recommendation["number"] not in numbers):
        raise PipelineError("product recommendation must select a listed option")
    _text(recommendation["reason"])


def _simple(evidence, fields):
    _keys(evidence, fields)
    for value in evidence.values():
        _text(value)


VALIDATORS = {
    "technical": _technical,
    "product": _product,
    "safety_exception": lambda value: _simple(value, {"boundary", "requested_exception"}),
    "insufficient_data": lambda value: _simple(value, {"missing", "verification"}),
}


def inspect_report(report: dict, expected_head: str) -> dict:
    """Validate explicit categories and commit the complete report's exact text.

    The expected HEAD is caller-supplied data, not an authenticated GitHub read.
    The returned classification must not be used as an execution allowlist.
    """
    _keys(report, {"routing_version", "head", "change", "checks", "blocking", "tail"})
    if report["routing_version"] != VERSION:
        raise PipelineError("unsupported routing report version")
    if (not isinstance(expected_head, str)
            or not re.fullmatch(r"[0-9a-f]{40}", expected_head)
            or report["head"] != expected_head):
        raise PipelineError("routing report HEAD differs from expected exact SHA")
    _text(report["change"])
    checks = report["checks"]
    if not isinstance(checks, list) or not checks:
        raise PipelineError("routing report must list performed checks")
    for check in checks:
        _text(check)
    if not isinstance(report["blocking"], list) or not isinstance(report["tail"], list):
        raise PipelineError("routing blockers and tail must be arrays")
    ids, kinds = set(), set()
    for blocker in report["blocking"]:
        _keys(blocker, {"id", "kind", "summary", "evidence"})
        blocker_id = blocker["id"]
        if (not isinstance(blocker_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", blocker_id)
                or blocker_id in ids):
            raise PipelineError("blocker IDs must be unique invariant identifiers")
        ids.add(blocker_id)
        kind = blocker["kind"]
        if not isinstance(kind, str) or kind not in VALIDATORS:
            raise PipelineError("unknown blocker category; no technical inference")
        kinds.add(kind)
        _text(blocker["summary"])
        VALIDATORS[kind](blocker["evidence"])
    for item in report["tail"]:
        _keys(item, {"kind", "text"}, {"title"})
        if item["kind"] not in ("issue", "discard"):
            raise PipelineError("unknown routing tail kind")
        _text(item["text"])
        if item["kind"] == "issue" or "title" in item:
            _text(item.get("title"))
    canonical = (json.dumps(report, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    classification = ("no_blockers" if not kinds else next(iter(kinds)) if len(kinds) == 1
                      else "mixed_blockers")
    return {"schema": "pp-review-routing-inspection-v1", "routing_version": VERSION,
            "head": expected_head, "classification": classification,
            "blocking_count": len(report["blocking"]),
            "report_sha256": hashlib.sha256(canonical).hexdigest(),
            "canonical_record": canonical.decode("utf-8"),
            "authority": "unverified-input", "mutation_authorized": False,
            "handoff_enabled": False}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PipelineError("duplicate routing JSON field")
        value[key] = item
    return value


def inspect_file(path: str, expected_head: str) -> dict:
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise PipelineError("routing report exceeds 256 KiB")
    try:
        report = json.loads(data.decode("utf-8", errors="strict"),
                            object_pairs_hook=_unique_object,
                            parse_constant=lambda value: _reject_constant(value))
    except UnicodeError as exc:
        raise PipelineError("routing report is not strict UTF-8") from exc
    return inspect_report(report, expected_head)


def _reject_constant(value):
    raise PipelineError(f"non-finite routing JSON value: {value}")
