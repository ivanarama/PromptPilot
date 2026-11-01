"""Read-only presentation of a paired checker's authenticated observations.

This module grants no dispatch, mutation or budget authority. Unknown, absent
and stale observations stay unknown; labels and report prose cannot classify.
"""
import copy


GROUPS = {
    "technical": "Техническая доработка",
    "engineering": "Инженерное восстановление",
    "product": "Продуктовое решение",
    "safety_exception": "Исключение из правил безопасности",
    "insufficient_data": "Недостаточно данных",
    "tool_error": "Ошибка инструмента",
    "unknown": "Причина не подтверждена",
}


def build_routing_observation(diagnostics, *, available, tool_errors=None):
    raw = diagnostics.get("routing_observation")
    valid = (available and isinstance(raw, dict)
             and raw.get("version") == "routing-observation-v1"
             and isinstance(raw.get("items"), list))
    groups = {key: [] for key in GROUPS}
    items = []
    if valid:
        for original in raw["items"][:128]:
            if not isinstance(original, dict):
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
            if item.get("route") == "decision" and not keys:
                keys.add("unknown")
            for key in keys:
                groups[key].append(item)
    errors = tool_errors if isinstance(tool_errors, dict) else {}
    groups["tool_error"] = copy.deepcopy(errors.get("items") or [])
    complete = bool(valid and raw.get("complete") is True
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
