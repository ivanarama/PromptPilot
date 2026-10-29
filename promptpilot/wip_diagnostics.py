"""Read-only explanations of intake refusal; never an admission policy."""
from __future__ import annotations

from datetime import datetime, timezone
import math


RESOURCES = {"core", "search", "graphql"}
STATES = {
    "low", "budget_in_flight", "ledger_changed", "priority_waiter", "fairness_waiter",
    "invalid_config", "lease_unavailable", "ledger_unavailable", "rate_limit_unavailable",
    "reservation_unavailable", "lease_lost", "busy", "worker_paused", "github_budget", "ok",
    "scan_in_progress",
}


def _number(value):
    if type(value) is int:
        return value if value >= 0 else None
    return value if type(value) is float and math.isfinite(value) and value >= 0 else None


def _state(value):
    return value if isinstance(value, str) and value in STATES else "unknown"


def _retry_at(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def refresh_budget_diagnostic(decision) -> dict | None:
    """Keep the *scan's* existing admission result, with no raw error strings.

    A zero-cost dashboard view is a different decision and is never a fallback.
    Signed effective_after and the priority-aware floor are copied, not derived.
    """
    if not isinstance(decision, dict) or decision.get("budget_route") != "insights":
        return None
    result = {"budget_route": "insights", "state": _state(decision.get("state"))}
    for key in ("minimum_remaining", "base_minimum_remaining", "priority_one_headroom",
                "requested_cost", "reserved_other", "effective_after"):
        values = decision.get(key)
        if isinstance(values, dict):
            result[key] = {name: value for name, value in values.items()
                           if name in RESOURCES and type(value) is int
                           and (key == "effective_after" or value >= 0)}
    for key in ("allowed", "priority_headroom_applied"):
        if type(decision.get(key)) is bool:
            result[key] = decision[key]
    count = decision.get("active_reservations")
    if type(count) is int and count >= 0:
        result["active_reservations"] = count
    blocked = decision.get("blocked_resources")
    result["blocked_resources"] = []
    if isinstance(blocked, list):
        for item in blocked:
            if (not isinstance(item, dict) or not isinstance(item.get("resource"), str)
                    or item["resource"] not in RESOURCES):
                continue
            safe = {"resource": item["resource"]}
            for key in ("remaining", "reported_remaining", "reserved_other", "requested_cost",
                        "effective_after", "minimum_remaining"):
                value = item.get(key)
                if type(value) is int and (key == "effective_after" or value >= 0):
                    safe[key] = value
            if "remaining" not in safe and "reported_remaining" in safe:
                safe["remaining"] = safe["reported_remaining"]
            result["blocked_resources"].append(safe)
    return result


def backpressure_details(policy: dict, data: dict, *, refresh_failed=False) -> dict:
    cache = data.get("cache") if isinstance(data.get("cache"), dict) else {}
    if not cache or cache.get("available") is False or cache.get("source") == "none":
        state = "missing"
    elif cache.get("complete") is not True:
        state = "partial"
    elif cache.get("stale"):
        state = "stale"
    else:
        state = "fresh"
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
    fields = policy.get("active_fields") or []
    count = None
    if (isinstance(fields, list) and fields
            and all(isinstance(field, str) and isinstance(diagnostics.get(field), list) for field in fields)):
        count = len({item["number"] for field in fields for item in diagnostics[field]
                     if isinstance(item, dict) and type(item.get("number")) is int
                     and item.get("head")})
    limit = policy.get("max_active")
    limit = limit if type(limit) is int and limit > 0 else None
    blocked = cache.get("refresh_blocked")
    budget = None if refresh_failed else refresh_budget_diagnostic(cache.get("refresh_budget"))
    if budget and (not blocked or budget["state"] == "unknown" or budget["state"] != _state(blocked)):
        budget = None
    return {
        "schema": "pp-wip-diagnostics-v1", "authoritative": False,
        "snapshot": {"state": state, "complete": cache.get("complete") is True,
                     "stale": bool(cache.get("stale")),
                     "age_seconds": _number(cache.get("age_seconds")),
                     "ttl_seconds": _number(cache.get("ttl_seconds")),
                     "refresh_state": ("failed" if refresh_failed else _state(blocked) if blocked else None),
                     "retry_at": (None if refresh_failed else _retry_at(cache.get("refresh_deferred_until")))},
        "refresh_budget": budget,
        "wip": {"max_active": limit, "observed_active": count,
                "observation": ("unavailable" if count is None else "complete_snapshot"
                                if cache.get("complete") is True else "partial_snapshot"),
                "limit_reached_in_observation": (count >= limit
                                                if count is not None and limit is not None else None)},
    }


def explain_backpressure(details: dict) -> str:
    snapshot = details["snapshot"]
    parts = ["снимок: " + snapshot["state"]]
    if snapshot["age_seconds"] is not None:
        parts.append(f"возраст {snapshot['age_seconds']} с")
    if snapshot["ttl_seconds"] is not None:
        parts.append(f"TTL {snapshot['ttl_seconds']} с")
    if snapshot["refresh_state"]:
        parts.append("обновление: " + snapshot["refresh_state"])
    budget = details["refresh_budget"]
    if budget:
        for item in budget["blocked_resources"]:
            required = {"remaining", "reserved_other", "requested_cost", "effective_after",
                        "minimum_remaining"}
            if required <= item.keys():
                parts.append(f"{item['resource']}: остаток {item['remaining']}, "
                             f"резервы {item['reserved_other']}, стоимость {item['requested_cost']}, "
                             f"прогноз {item['effective_after']}, безопасный остаток {item['minimum_remaining']}")
    if snapshot["retry_at"]:
        parts.append("повтор после " + snapshot["retry_at"])
    wip = details["wip"]
    if wip["observed_active"] is not None:
        label = "последний полный WIP" if wip["observation"] == "complete_snapshot" else "неполный WIP"
        parts.append(f"{label}: {wip['observed_active']}")
    if wip["max_active"] is not None:
        parts.append(f"порог новых задач {wip['max_active']}")
    return "; ".join(parts)
