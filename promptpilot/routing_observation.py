"""Read-only presentation of a paired checker's authenticated observations.

This module grants no dispatch, mutation or budget authority. Unknown, absent
and stale observations stay unknown; labels and report prose cannot classify.
"""
import copy
import re


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
            or len(item["blockers"]) > 64):
        return False
    for blocker in item["blockers"]:
        if (not isinstance(blocker, dict) or not isinstance(blocker.get("summary"), str)
                or not isinstance(blocker.get("evidence"), dict)):
            return False
        evidence = blocker["evidence"]
        if "options" in evidence and (not isinstance(evidence["options"], list)
                                     or any(not isinstance(option, dict) for option in evidence["options"])):
            return False
        if "recommendation" in evidence and not isinstance(evidence["recommendation"], dict):
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
    complete = bool(valid and classification_complete and raw.get("complete") is True
                    and not raw.get("truncated") and len(raw["items"]) <= 128)
    return {
        "available": bool(valid), "complete": complete,
        "truncated": bool(valid and (raw.get("truncated") or len(raw["items"]) > 128)),
        "mutation_authorized": False, "items": items,
        "groups": [{"id": key, "title": title, "items": groups[key],
                    "count": (len(groups[key]) if complete else None)
                    if key != "tool_error" else (
                        len(groups[key]) if errors.get("complete") else None)}
                   for key, title in GROUPS.items()],
    }
