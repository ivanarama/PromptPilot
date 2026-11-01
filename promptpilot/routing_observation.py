"""Read-only presentation of a paired checker's authenticated observations.

This module grants no dispatch, mutation or budget authority. Unknown, absent
and stale observations stay unknown; labels and report prose cannot classify.
"""
import copy
import json
import re

from .pipeline_errors import PipelineError
from .review_routing import VALIDATORS


GROUPS = {
    "technical": "Техническая доработка",
    "engineering": "Инженерное восстановление",
    "product": "Продуктовое решение",
    "safety_exception": "Исключение из правил безопасности",
    "insufficient_data": "Недостаточно данных",
    "tool_error": "Ошибка инструмента",
    "unknown": "Причина не подтверждена",
}


def _displayable(item):
    """Reject malformed display payloads without promoting them to zero rows."""
    if not isinstance(item, dict):
        return False
    try:
        if len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 300 * 1024:
            return False
    except (TypeError, ValueError, UnicodeError):
        return False
    if (type(item.get("number")) is not int or item["number"] < 1
            or not isinstance(item.get("head"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", item["head"])
            or not isinstance(item.get("route"), str)
            or item.get("route") not in {"unknown", "technical", "engineering", "decision",
                                         "waiting_ship", "merge", "review", "paused"}
            or not isinstance(item.get("evidence_status"), str)
            or item.get("evidence_status") not in {"unknown", "older_head_or_epoch", "verified_current"}):
        return False
    for key in ("categories", "blockers", "reasons"):
        if not isinstance(item.get(key), list):
            return False
    if (any(not isinstance(value, str) for value in item["categories"] + item["reasons"])
            or len(item["blockers"]) > 64 or len(item["reasons"]) > 32
            or any(value not in {"technical", "product", "safety_exception", "insufficient_data"}
                   for value in item["categories"])
            or len(set(item["categories"])) != len(item["categories"])):
        return False
    if (type(item.get("budget_limit")) is not int or not 1 <= item["budget_limit"] <= 16
            or (item.get("budget_used") is not None
                and (type(item["budget_used"]) is not int or not 0 <= item["budget_used"] <= 16))):
        return False
    source = item.get("source")
    if item["evidence_status"] == "verified_current" and (source is None or item.get("budget_used") is None):
        return False
    if source is not None:
        fields = {"publication_id", "claim_id", "completion_id", "head", "identity_sha256", "epoch", "report_sha256"}
        if (not isinstance(source, dict) or set(source) != fields
                or any(not isinstance(value, str) for value in source.values())):
            return False
        if (any(not re.fullmatch(r"[1-9][0-9]{0,127}", source[key])
                for key in ("publication_id", "claim_id", "completion_id"))
                or not re.fullmatch(r"[0-9a-f]{40}", source["head"])
                or any(not re.fullmatch(r"[0-9a-f]{64}", source[key])
                       for key in ("identity_sha256", "epoch", "report_sha256"))
                or item["evidence_status"] == "verified_current" and source["head"] != item["head"]):
            return False
    recipient = item.get("recipient")
    if not isinstance(recipient, str):
        if (not isinstance(recipient, dict) or set(recipient) != {"kind", "login"}
                or recipient.get("kind") not in ("author", "pipeline")
                or not isinstance(recipient.get("login"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", recipient["login"])):
            return False
    if item["evidence_status"] == "verified_current" or "transition_at" in item or "transition_kind" in item:
        if (not isinstance(item.get("transition_at"), str)
                or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z", item["transition_at"])
                or not isinstance(item.get("transition_kind"), str)
                or not re.fullmatch(r"[a-z][a-z-]{0,63}", item["transition_kind"])):
            return False
    if "transition_id" in item and (not isinstance(item["transition_id"], str)
                                    or not re.fullmatch(r"[1-9][0-9]{0,127}", item["transition_id"])):
        return False
    blocker_ids = set()
    for blocker in item["blockers"]:
        if (not isinstance(blocker, dict) or not isinstance(blocker.get("summary"), str)
                or not isinstance(blocker.get("evidence"), dict)
                or set(blocker) != {"id", "kind", "summary", "evidence"}
                or not isinstance(blocker.get("kind"), str)
                or blocker["kind"] not in item["categories"]
                or not blocker["summary"].strip() or "\0" in blocker["summary"]
                or not isinstance(blocker["id"], str)
                or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", blocker["id"])
                or blocker["id"] in blocker_ids):
            return False
        blocker_ids.add(blocker["id"])
        try:
            # Share the existing schema validators; display validation does
            # not create a report, authenticity proof or execution permission.
            VALIDATORS[blocker["kind"]](blocker["evidence"])
        except PipelineError:
            return False
    return True


def build_routing_observation(diagnostics, *, available, tool_errors=None):
    raw = diagnostics.get("routing_observation")
    valid = (available and isinstance(raw, dict)
             and raw.get("version") == "routing-observation-v1"
             and isinstance(raw.get("items"), list))
    groups = {key: [] for key in GROUPS}
    items = []
    classification_complete = True
    if valid:
        for original in raw["items"][:128]:
            if not _displayable(original):
                classification_complete = False
                continue
            item = copy.deepcopy(original)
            items.append(item)
            keys = set()
            if item.get("route") in {"technical", "engineering"}:
                keys.add(item["route"])
            if item.get("evidence_status") == "verified_current":
                categories = item.get("categories")
                categories = categories if isinstance(categories, list) else []
                keys.update(category for category in categories
                            if isinstance(category, str)
                            if category in GROUPS and category != "tool_error")
            if item.get("evidence_status") != "verified_current":
                classification_complete = False
            if ((item.get("route") == "decision" and not keys)
                    or (item.get("evidence_status") != "verified_current"
                        and item.get("route") in {"engineering", "technical", "decision"})):
                keys.add("unknown")
            for key in keys:
                groups[key].append(item)
    errors = tool_errors if isinstance(tool_errors, dict) else {}
    groups["tool_error"] = copy.deepcopy(errors.get("items") or [])
    classification_complete = bool(valid and classification_complete and raw.get("complete") is True
                    and not raw.get("truncated") and len(raw["items"]) <= 128)
    error_complete = errors.get("complete") is True
    truncated = bool(valid and (raw.get("truncated") or len(raw["items"]) > 128))
    errors_truncated = bool(errors.get("truncated"))
    return {
        "available": bool(valid),
        "complete": classification_complete and error_complete and not errors_truncated,
        "truncated": truncated or errors_truncated,
        "mutation_authorized": False, "items": items,
        "groups": [{"id": key, "title": title, "items": groups[key],
                    "complete": error_complete if key == "tool_error" else classification_complete,
                    "truncated": errors_truncated if key == "tool_error" else truncated,
                    "count": (len(groups[key]) if classification_complete else None)
                    if key != "tool_error" else (
                        len(groups[key]) if errors.get("complete") else None)}
                   for key, title in GROUPS.items()],
    }
