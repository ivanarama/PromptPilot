"""Profile-driven, token-free diagnostics for external GitHub pipelines."""

import copy
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import db
from .config import (CONCURRENCY, DB_DIR, DEFAULT_CLI, POLL_INTERVAL,
                     TASK_TIMEOUT, load_providers)


DEFAULT_PROFILES: dict = {}

_cache = {}
_locks: dict[str, threading.Lock] = {}
_cache_state_lock = threading.Lock()
_cache_generation = 0
_CACHE_TTL_SECONDS = 300
_CACHE_SCHEMA_VERSION = 1
_CACHE_EPOCH_KEY = "pipeline_insights_cache_epoch:v1"
_CACHE_KEY_PREFIX = "pipeline_insights_cache:v1:"
_CACHE_REFRESH_REVISION_PREFIX = "pipeline_insights_refresh_revision:v1:"
_CACHE_PUBLISHED_REVISION_PREFIX = "pipeline_insights_published_revision:v1:"
_CACHE_PUBLISHED_PROFILE_PREFIX = "pipeline_insights_published_profile:v1:"
_CACHE_PUBLISHED_PROFILE_REVISION_PREFIX = \
    "pipeline_insights_published_profile_revision:v1:"
_INTERVAL_PRESETS = ((0.25, "15m"), (0.5, "30m"), (1, "1h"), (2, "2h"),
                     (4, "4h"), (8, "8h"), (12, "12h"), (24, "24h"))
_HISTORY_WINDOWS = (5, 24, 24 * 7, 24 * 30)
_PRIORITY_LEVELS = ("p0", "p1", "p2", "p3")
_DEFAULT_PRIORITY_RULES = (
    ({"security", "severity:critical", "blocker", "data-loss"}, 0, "critical label"),
    ({"bug"}, 1, "bug"),
    ({"enhancement", "documentation"}, 2, "planned change"),
    ({"question"}, 3, "question"),
)
_DEFAULT_GITHUB_BUDGET_MINIMUM = {
    "core": 4000,
    "search": 10,
    "graphql": 500,
}
_GITHUB_BUDGET_ROUTES = (
    "insights",
    "skill",
    "tool_preflight",
    "tool",
    "fallback_targeted",
)
_GITHUB_SCAN_LEASE_SCOPE = "github-default"
_GITHUB_SCAN_LEASE_RENEW_RETRY_DELAYS = (0.05, 0.15)
_GITHUB_SCAN_LEASE_RENEW_RETRY_WINDOW_SECONDS = 1.0
_GITHUB_SCAN_LEASE_RENEW_LOCK_TIMEOUT_SECONDS = 1.0
_scan_lease_context = threading.local()
_execution_budget_context = threading.local()


class _GitHubScanPaused(RuntimeError):
    """A multi-request GitHub observation stopped at a page boundary."""


class _GitHubScanLeaseFailure(RuntimeError):
    """A scan must stop because its SQLite lease cannot be proved safe."""


class _GitHubScanLeaseLost(_GitHubScanLeaseFailure):
    """The process can no longer prove exclusive ownership of a live scan."""


class _GitHubScanLeaseUnavailable(_GitHubScanLeaseFailure):
    """SQLite temporarily prevented verification of an otherwise owned lease."""


class _GitHubRateLimitUnavailable(RuntimeError):
    """The authenticated GitHub budget cannot be proved from valid responses."""


class _GitHubScanLease:
    """Renew one SQLite-backed scan lease while external work is in flight."""

    def __init__(self, scope: str, token: str, ttl_seconds: int,
                 status_revision: int | None = None):
        self.scope = scope
        self.token = token
        self.ttl_seconds = ttl_seconds
        self.status_revision = status_revision
        self.expires_at: float | None = None
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._unavailable = threading.Event()
        self._unavailable_reason: str | None = None
        self._thread: threading.Thread | None = None
        self._renew_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._released = False

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def unavailable(self) -> bool:
        return self._unavailable.is_set()

    @property
    def guard(self) -> dict:
        return {"scope": self.scope, "token": self.token}

    def _mark_lost(self) -> None:
        with self._state_lock:
            self._lost.set()
            self._unavailable.clear()
            self._unavailable_reason = None

    def _mark_unavailable(self, exc: BaseException) -> None:
        with self._state_lock:
            if self._lost.is_set():
                return
            self._unavailable_reason = str(exc) or type(exc).__name__
            self._unavailable.set()

    def _mark_renewed(self, expires_at) -> None:
        if (isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(float(expires_at))):
            raise ValueError("SQLite lease renewal returned an invalid expiry")
        with self._state_lock:
            if self._lost.is_set():
                return
            self.expires_at = float(expires_at)
            self._unavailable.clear()
            self._unavailable_reason = None

    def _renew_with_retry(self) -> None:
        """Renew once, retrying transient storage failures without losing fencing.

        ``None`` is the durable compare-and-swap answer that our exact token no
        longer owns the lease, so it is sticky. Exceptions only prove that the
        database is currently unavailable: after bounded retries the caller is
        stopped fail-closed, but a later successful heartbeat may recover.
        """
        if not self._renew_lock.acquire(
                timeout=_GITHUB_SCAN_LEASE_RENEW_LOCK_TIMEOUT_SECONDS):
            error = TimeoutError(
                "другая проверка SQLite lease не завершилась вовремя")
            self._mark_unavailable(error)
            raise _GitHubScanLeaseUnavailable(
                "SQLite lease GitHub-сканирования временно недоступна: "
                f"{error}") from error
        try:
            if self.lost:
                raise _GitHubScanLeaseLost(
                    "межпроцессная lease GitHub-сканирования потеряна")
            last_error: BaseException | None = None
            attempts = len(_GITHUB_SCAN_LEASE_RENEW_RETRY_DELAYS) + 1
            retry_deadline = time.monotonic() + \
                _GITHUB_SCAN_LEASE_RENEW_RETRY_WINDOW_SECONDS
            for attempt in range(attempts):
                try:
                    renewed = db.renew_pipeline_scan_lease(
                        self.scope, self.token, self.ttl_seconds)
                    if renewed is None:
                        self._mark_lost()
                        raise _GitHubScanLeaseLost(
                            "межпроцессная lease GitHub-сканирования потеряна")
                    self._mark_renewed(renewed)
                    return
                except _GitHubScanLeaseLost:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= attempts - 1:
                        break
                    delay = _GITHUB_SCAN_LEASE_RENEW_RETRY_DELAYS[attempt]
                    if time.monotonic() + delay > retry_deadline:
                        break
                    if self._stop.wait(delay):
                        break
            assert last_error is not None
            self._mark_unavailable(last_error)
            raise _GitHubScanLeaseUnavailable(
                "SQLite lease GitHub-сканирования временно недоступна: "
                f"{last_error}") from last_error
        finally:
            self._renew_lock.release()

    def start(self) -> None:
        interval = max(1.0, min(30.0, self.ttl_seconds / 3))

        def heartbeat() -> None:
            while not self._stop.wait(interval):
                try:
                    self._renew_with_retry()
                except _GitHubScanLeaseLost:
                    return
                except _GitHubScanLeaseUnavailable:
                    # Keep retrying while the main scan fails closed at its next
                    # boundary. A later successful renewal clears this state.
                    continue

        self._thread = threading.Thread(
            target=heartbeat, name="promptpilot-github-scan-lease", daemon=True)
        self._thread.start()

    def ensure_owned(self, *, renew: bool = False) -> None:
        if renew and not self.lost:
            self._renew_with_retry()
        if self.lost:
            raise _GitHubScanLeaseLost(
                "межпроцессная lease GitHub-сканирования потеряна")
        if self.unavailable:
            with self._state_lock:
                reason = self._unavailable_reason or "неизвестная ошибка SQLite"
            raise _GitHubScanLeaseUnavailable(
                "SQLite lease GitHub-сканирования временно недоступна: "
                f"{reason}")

    def release(self, *, refresh_status: dict | None = None) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=2)
            try:
                db.release_pipeline_scan_lease(
                    self.scope, self.token, refresh_status=refresh_status)
            except Exception:
                # A failed release stays fail-closed until the renewable lease
                # expires; never let cleanup hide the scan's original outcome.
                pass

def _bounded_int(config: dict, name: str, default: int,
                 minimum: int, maximum: int) -> int:
    value = config.get(name, default)
    if (isinstance(value, bool)
            or isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"github_budget.{name} должен быть целым числом")
    try:
        parsed = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(
            f"github_budget.{name} должен быть целым числом") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(
            f"github_budget.{name} должен быть от {minimum} до {maximum}")
    return parsed


def _minimum_remaining(raw: dict, path: str, defaults: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"github_budget.{path} должен быть JSON-объектом")
    result = {}
    for resource, default in defaults.items():
        value = raw.get(resource, default)
        if (isinstance(value, bool)
                or isinstance(value, float) and not value.is_integer()):
            raise ValueError(
                f"github_budget.{path}.{resource} должен быть целым числом")
        try:
            value = int(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(
                f"github_budget.{path}.{resource} должен быть целым числом"
            ) from exc
        if value < 0:
            raise ValueError(
                f"github_budget.{path}.{resource} не может быть отрицательным")
        result[resource] = value
    return result


def _strict_budget_vector(raw: dict, path: str) -> dict:
    """Parse a cost promise without accepting JSON strings or booleans."""
    resources = set(_DEFAULT_GITHUB_BUDGET_MINIMUM)
    if not isinstance(raw, dict) or set(raw) != resources:
        raise ValueError(
            f"github_budget.{path} должен содержать ровно "
            + ", ".join(_DEFAULT_GITHUB_BUDGET_MINIMUM))
    result = {}
    for resource in _DEFAULT_GITHUB_BUDGET_MINIMUM:
        value = raw[resource]
        if type(value) is not int or value < 0:
            raise ValueError(
                f"github_budget.{path}.{resource} должен быть "
                "неотрицательным целым числом")
        result[resource] = value
    return result


def _github_budget_policy(profile: dict) -> dict | None:
    """Normalize the opt-in admission policy without changing legacy profiles."""
    raw = profile.get("github_budget")
    if raw is None or raw is False:
        return None
    if not isinstance(raw, dict):
        raise ValueError("github_budget должен быть JSON-объектом")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("github_budget.enabled должен быть true или false")
    if not enabled:
        return None

    minimum_remaining = _minimum_remaining(
        raw.get("minimum_remaining", {}), "minimum_remaining",
        _DEFAULT_GITHUB_BUDGET_MINIMUM)
    priority_one_headroom_raw = raw.get("priority_one_headroom")
    priority_one_headroom = (
        {resource: 0 for resource in _DEFAULT_GITHUB_BUDGET_MINIMUM}
        if priority_one_headroom_raw is None else
        _strict_budget_vector(
            priority_one_headroom_raw, "priority_one_headroom"))
    configured_costs = raw.get("costs")
    costs = None
    if configured_costs is not None:
        if not isinstance(configured_costs, dict):
            raise ValueError("github_budget.costs должен быть JSON-объектом")
        unknown_routes = sorted(
            set(configured_costs) - set(_GITHUB_BUDGET_ROUTES))
        missing_routes = sorted(
            set(_GITHUB_BUDGET_ROUTES) - set(configured_costs))
        if unknown_routes:
            raise ValueError(
                "github_budget.costs содержит неизвестные маршруты: "
                + ", ".join(unknown_routes))
        if missing_routes:
            raise ValueError(
                "github_budget.costs не содержит маршруты: "
                + ", ".join(missing_routes))
        costs = {
            route: _strict_budget_vector(
                configured_costs[route], f"costs.{route}")
            for route in _GITHUB_BUDGET_ROUTES
        }
    if any(priority_one_headroom.values()) and costs is None:
        raise ValueError(
            "github_budget.priority_one_headroom требует github_budget.costs")

    return {
        "minimum_remaining": minimum_remaining,
        "priority_one_headroom": priority_one_headroom,
        "costs": costs,
        "reset_grace_seconds": _bounded_int(
            raw, "reset_grace_seconds", 60, 0, 3600),
        "lease_seconds": _bounded_int(raw, "lease_seconds", 900, 30, 3600),
        "busy_retry_seconds": _bounded_int(
            raw, "busy_retry_seconds", 30, 5, 300),
        "unavailable_retry_seconds": _bounded_int(
            raw, "unavailable_retry_seconds", 300, 30, 3600),
        # Opt in explicitly: once this deadline expires the oldest durable
        # reservation waiter temporarily outranks new admissions, including
        # priority 1.  Its own priority floor still applies, so fairness drains
        # competing reservations without borrowing urgent-task headroom.
        "starvation_timeout_seconds": _bounded_int(
            raw, "starvation_timeout_seconds", 0, 0, 86400),
        # GitHub primary budgets belong to the authenticated account, not to a
        # repository/profile. Keep one scope per shared PromptPilot database so
        # profiles cannot accidentally opt out of each other's reservation.
        "lease_scope": _GITHUB_SCAN_LEASE_SCOPE,
    }


def _with_shared_budget_floor(policy: dict) -> dict:
    """Use the strongest hard reserve of every profile sharing this account."""
    floor = dict(policy["minimum_remaining"])
    priority_one_headroom = dict(policy["priority_one_headroom"])
    starvation_timeouts = []
    if policy.get("starvation_timeout_seconds", 0) > 0:
        starvation_timeouts.append(policy["starvation_timeout_seconds"])
    for configured_profile in _profiles().values():
        candidate = _github_budget_policy(configured_profile)
        if candidate is None or candidate["lease_scope"] != policy["lease_scope"]:
            continue
        for resource, value in candidate["minimum_remaining"].items():
            floor[resource] = max(floor[resource], value)
        for resource, value in candidate["priority_one_headroom"].items():
            priority_one_headroom[resource] = max(
                priority_one_headroom[resource], value)
        if candidate.get("starvation_timeout_seconds", 0) > 0:
            starvation_timeouts.append(
                candidate["starvation_timeout_seconds"])
    selected = dict(policy)
    selected["minimum_remaining"] = floor
    selected["priority_one_headroom"] = priority_one_headroom
    # The reservation ledger is account-wide. Every caller must therefore use
    # one deterministic deadline; the tightest explicit bound wins.
    selected["starvation_timeout_seconds"] = (
        min(starvation_timeouts) if starvation_timeouts else 0)
    if any(priority_one_headroom.values()) and selected.get("costs") is None:
        raise ValueError(
            "общий github_budget.priority_one_headroom требует "
            "github_budget.costs в каждом профиле с тем же GitHub token")
    return selected


def _budget_policy_for_admission_priority(policy: dict,
                                          priority: int | None) -> dict:
    """Keep configured quota headroom available to priority-1 work.

    The headroom is added to the ordinary hard floor only for lower-priority
    attempts. Priority 1 can consume it, while every other priority leaves it
    available for a MERGE or another urgent wake-up that appears later.
    """
    selected = dict(policy)
    headroom = dict(policy.get("priority_one_headroom") or {
        resource: 0 for resource in policy["minimum_remaining"]})
    selected["priority_one_headroom"] = headroom
    selected["base_minimum_remaining"] = dict(policy["minimum_remaining"])
    selected["priority_headroom_applied"] = False
    selected["admission_priority"] = None
    if not any(headroom.values()):
        return selected
    if type(priority) is not int or not 1 <= priority <= 10:
        raise ValueError(
            "admission priority is unavailable for GitHub priority headroom")
    selected["admission_priority"] = priority
    if priority == 1:
        return selected
    selected["minimum_remaining"] = {
        resource: policy["minimum_remaining"][resource] + headroom[resource]
        for resource in policy["minimum_remaining"]
    }
    selected["priority_headroom_applied"] = True
    return selected


def _post_preflight_admission_priority(
        profile: dict, series: list[dict], stage: str,
        provider_route: str | None,
        validated_target_stage: str | None,
        effective_priority_headroom: dict | None) -> int | None:
    """Let the exact integration REVIEW use the MERGE headroom it unlocks.

    ``validated_target_stage`` is populated only from the signed, task-fenced
    replica lease.  Ordinary content review remains at the configured series
    priority and therefore continues to preserve priority-1 capacity.
    """
    from .fallback_handoff import INTEGRATION_REVIEW_STAGES

    if (stage != "review" or provider_route != "fallback_targeted"
            or validated_target_stage not in INTEGRATION_REVIEW_STAGES):
        return None
    # Admission has already combined every profile that shares this GitHub
    # account. Looking only at the current profile would reintroduce the cycle
    # whenever another repository owns the strongest shared headroom.
    headroom = effective_priority_headroom or {}
    if (not isinstance(headroom, dict)
            or not any(type(value) is int and value > 0
                       for value in headroom.values())):
        return None
    priorities = []
    for queue in profile.get("queues", []):
        execution = queue.get("execution") or {}
        queue_stage = str(execution.get("stage") or queue.get("id") or "").lower()
        if queue_stage != "merge":
            continue
        for item in _series_replicas_for_queue(queue, series):
            priority = item.get("priority")
            if (not item.get("paused") and not item.get("broken")
                    and type(priority) is int and 1 <= priority <= 10):
                priorities.append(priority)
    # This reserve is specifically consumable by priority 1. A lower-urgency
    # MERGE series does not justify leaving the REVIEW occurrence promoted.
    return 1 if 1 in priorities else None


def _budget_policy_for_route(policy: dict, route: str | None) -> dict:
    """Attach an estimated route cost; profiles without costs stay legacy."""
    if route is None or policy.get("costs") is None:
        selected = dict(policy)
        selected["budget_route"] = route
        selected["requested_cost"] = {
            resource: 0 for resource in policy["minimum_remaining"]}
        selected["cost_accounting"] = False
        return selected
    if route not in _GITHUB_BUDGET_ROUTES:
        raise ValueError(f"неизвестный GitHub budget route: {route}")
    selected = dict(policy)
    selected["budget_route"] = route
    selected["requested_cost"] = dict(policy["costs"][route])
    selected["cost_accounting"] = True
    return selected


def _defer_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _projected_post_reservation(
        effective_after: dict | None, minimum_remaining: dict) -> dict:
    """Render a non-negative projection without changing admission arithmetic.

    ``effective_after`` remains the signed, authoritative value used by the
    safety gate. The UI projection splits a negative result into a zero-clamped
    available amount and an explicit deficit so operators never mistake a
    display-only clamp for extra GitHub quota.
    """
    projected = {}
    values = effective_after or {}
    for resource, hard_reserve in minimum_remaining.items():
        value = values.get(resource)
        if type(value) is not int:
            continue
        projected[resource] = {
            "available": max(0, value),
            "deficit": max(0, -value),
            "hard_reserve": int(hard_reserve),
            "headroom_above_hard_reserve": max(0, value - hard_reserve),
            "shortfall_to_hard_reserve": max(0, hard_reserve - value),
        }
    return projected


def _priority_budget_metadata(policy: dict) -> dict:
    resources = policy["minimum_remaining"]
    base = policy.get("base_minimum_remaining") or resources
    headroom = policy.get("priority_one_headroom") or {
        resource: 0 for resource in resources}
    return {
        "base_minimum_remaining": dict(base),
        "priority_one_headroom": dict(headroom),
        "priority_headroom_applied": bool(
            policy.get("priority_headroom_applied")),
        "admission_priority": policy.get("admission_priority"),
    }


def _budget_blocked_summary(item: dict, active_reservations: int) -> str:
    """Explain signed admission arithmetic without implying a GitHub value."""
    actual_remaining = item.get(
        "remaining", item.get("reported_remaining", "—"))
    return (
        f"{item['resource']}: прогноз после резервов и оценки запуска "
        f"{item['effective_after']} < безопасный остаток "
        f"{item['minimum_remaining']} (фактический остаток GitHub "
        f"{actual_remaining}; "
        f"активных резервов {int(active_reservations)}; другими задачами "
        f"зарезервировано {item['reserved_other']}; оценка этого запуска "
        f"{item['requested_cost']})"
    )


def _budget_denied(policy: dict, *, state: str, reason: str,
                   now: float, limits: dict | None = None,
                   defer_at: float | None = None,
                   blocked_resources: list[dict] | None = None,
                   status_revision: int | None = None,
                   reserved_other: dict | None = None,
                   effective_after: dict | None = None,
                   active_reservations: int = 0) -> dict:
    retry_at = defer_at or (now + policy["unavailable_retry_seconds"])
    effective = dict(effective_after or {})
    return {
        "enabled": True, "allowed": False, "state": state,
        "reason": reason, "defer_until": _defer_at(retry_at),
        "github_rate_limit": limits,
        "minimum_remaining": dict(policy["minimum_remaining"]),
        "requested_cost": dict(policy.get("requested_cost") or {}),
        "reserved_other": dict(reserved_other or {}),
        "effective_after": effective,
        "projected_post_reservation": _projected_post_reservation(
            effective, policy["minimum_remaining"]),
        "active_reservations": int(active_reservations),
        "blocked_resources": blocked_resources or [],
        "lease_scope": policy["lease_scope"],
        "budget_route": policy.get("budget_route"),
        "status_revision": status_revision,
        **_priority_budget_metadata(policy),
    }


def _lease_failure_decision(profile: dict, reason: str, *,
                            status_revision: int | None = None,
                            unavailable: bool = False) -> dict:
    try:
        policy = _github_budget_policy(profile)
        if policy is not None:
            policy = _with_shared_budget_floor(policy)
    except (TypeError, ValueError):
        policy = None
    policy = policy or {
        "minimum_remaining": dict(_DEFAULT_GITHUB_BUDGET_MINIMUM),
        "unavailable_retry_seconds": 300,
        "lease_scope": _GITHUB_SCAN_LEASE_SCOPE,
    }
    return _budget_denied(
        policy, state=("lease_unavailable" if unavailable else "lease_lost"),
        reason=reason, now=time.time(),
        status_revision=status_revision)


def _lease_exception_decision(
        profile: dict, exc: _GitHubScanLeaseFailure, *,
        status_revision: int | None = None) -> dict:
    return _lease_failure_decision(
        profile, str(exc), status_revision=status_revision,
        unavailable=isinstance(exc, _GitHubScanLeaseUnavailable))


def _replace_admission_with_lease_failure(
        admission: dict, profile: dict, exc: _GitHubScanLeaseFailure) -> dict:
    """Keep the context manager's final durable status aligned with its caller."""
    lease = admission.get("_lease")
    denied = _lease_exception_decision(
        profile, exc,
        status_revision=_current_github_scan_status_revision())
    if lease is not None:
        denied["_lease"] = lease
    admission.clear()
    admission.update(denied)
    return admission


def _rate_limit_failure_decision(
        profile: dict, exc: _GitHubRateLimitUnavailable, *,
        status_revision: int | None = None) -> dict:
    try:
        policy = _github_budget_policy(profile)
        if policy is not None:
            policy = _with_shared_budget_floor(policy)
    except (TypeError, ValueError):
        policy = None
    policy = policy or {
        "minimum_remaining": dict(_DEFAULT_GITHUB_BUDGET_MINIMUM),
        "unavailable_retry_seconds": 300,
        "lease_scope": _GITHUB_SCAN_LEASE_SCOPE,
    }
    return _budget_denied(
        policy, state="rate_limit_unavailable",
        reason=f"GitHub API budget unavailable: {exc}", now=time.time(),
        status_revision=status_revision)


def _replace_admission_with_rate_limit_failure(
        admission: dict, profile: dict,
        exc: _GitHubRateLimitUnavailable) -> dict:
    """Keep the durable refresh denial aligned with a failed final sample."""
    lease = admission.get("_lease")
    denied = _rate_limit_failure_decision(
        profile, exc,
        status_revision=_current_github_scan_status_revision())
    if lease is not None:
        denied["_lease"] = lease
    admission.clear()
    admission.update(denied)
    return admission


def _evaluate_github_budget(policy: dict, limits: dict | None, *,
                            now: float,
                            status_revision: int | None = None,
                            reserved_other: dict | None = None,
                            active_reservations: int = 0) -> dict:
    if not isinstance(limits, dict):
        return _budget_denied(
            policy, state="rate_limit_unavailable",
            reason="GitHub /rate_limit недоступен; сканирование запрещено",
            now=now, status_revision=status_revision,
        )
    requested = dict(policy.get("requested_cost") or {
        resource: 0 for resource in policy["minimum_remaining"]})
    reserved = dict(reserved_other or {
        resource: 0 for resource in policy["minimum_remaining"]})
    blocked = []
    malformed = []
    effective_after = {}
    for resource, minimum in policy["minimum_remaining"].items():
        item = limits.get(resource)
        try:
            remaining = int(item["remaining"])
            reset = int(item["reset"])
            promised = int(reserved.get(resource, 0))
            cost = int(requested.get(resource, 0))
        except (KeyError, OverflowError, TypeError, ValueError):
            malformed.append(resource)
            continue
        if promised < 0 or cost < 0:
            malformed.append(resource)
            continue
        after = remaining - promised - cost
        effective_after[resource] = after
        if after < minimum:
            blocked.append({
                "resource": resource, "remaining": remaining,
                "reserved_other": promised, "requested_cost": cost,
                "effective_after": after,
                "minimum_remaining": minimum, "reset": reset,
                "blocked_by": (
                    "live" if remaining - cost < minimum
                    else "reservation"),
            })
    if malformed:
        return _budget_denied(
            policy, state="rate_limit_unavailable",
            reason=("GitHub /rate_limit не содержит корректный budget: "
                    + ", ".join(malformed)),
            now=now, limits=limits, status_revision=status_revision,
        )
    if blocked:
        live_blocked = [item for item in blocked
                        if item["blocked_by"] == "live"]
        if live_blocked:
            reset_at = max(item["reset"] for item in live_blocked) + \
                policy["reset_grace_seconds"]
            if reset_at <= now:
                reset_at = now + policy["unavailable_retry_seconds"]
            state = "low"
            reason_prefix = "GitHub API-бюджет ниже безопасного остатка"
        else:
            reset_at = now + policy["busy_retry_seconds"]
            state = "budget_in_flight"
            reason_prefix = "GitHub API-бюджет временно занят выполняемой задачей"
        summary = ", ".join(
            _budget_blocked_summary(item, active_reservations)
            for item in blocked)
        return _budget_denied(
            policy, state=state, reason=f"{reason_prefix}: {summary}",
            now=now, limits=limits, defer_at=reset_at,
            blocked_resources=blocked, status_revision=status_revision,
            reserved_other=reserved, effective_after=effective_after,
            active_reservations=active_reservations,
        )
    return {
        "enabled": True, "allowed": True, "state": "ok",
        "reason": "GitHub API-бюджет достаточен",
        "defer_until": None, "github_rate_limit": limits,
        "minimum_remaining": dict(policy["minimum_remaining"]),
        "requested_cost": requested, "reserved_other": reserved,
        "effective_after": effective_after,
        "projected_post_reservation": _projected_post_reservation(
            effective_after, policy["minimum_remaining"]),
        "active_reservations": int(active_reservations),
        "blocked_resources": [], "lease_scope": policy["lease_scope"],
        "budget_route": policy.get("budget_route"),
        "status_revision": status_revision,
        **_priority_budget_metadata(policy),
    }


def _budget_reservation_ledger(policy: dict, *, now: float | None = None) -> dict:
    """Read the shared ledger, adding fairness options only when enabled."""
    timeout = int(policy.get("starvation_timeout_seconds") or 0)
    if not timeout:
        return db.pipeline_github_budget_reservations(policy["lease_scope"])
    return db.pipeline_github_budget_reservations(
        policy["lease_scope"], starvation_timeout_seconds=timeout, now=now)


def _priority_waiter_decision(
        policy: dict, limits: dict | None, reservations: dict, *,
        priority: int | None, task_id: int | None,
        status_revision: int | None) -> dict | None:
    """Yield to priority normally, then to an aged oldest-waiter baton."""
    woken = reservations.get("woken_waiters") or {}
    waiting = reservations.get("waiting_waiters") or {}
    fairness_waiter = (
        reservations.get("fairness_waiter")
        or waiting.get("fairness_waiter"))
    if isinstance(fairness_waiter, dict):
        fair_task_id = fairness_waiter.get("task_id")
        if type(fair_task_id) is int and fair_task_id == task_id:
            # Aging affects ordering only. The selected task proceeds with its
            # unchanged admission priority and hard/headroom floor.
            return None
        now = time.time()
        decision = _budget_denied(
            policy, state="fairness_waiter",
            reason=(
                "GitHub API-бюджет передан самой старой ожидающей задаче "
                f"#{fair_task_id} после {fairness_waiter.get('wait_seconds', 0)} с "
                "ожидания; новые резервы временно приостановлены"),
            now=now, limits=limits,
            defer_at=now + policy["busy_retry_seconds"],
            status_revision=status_revision,
            reserved_other=reservations.get("totals"),
            active_reservations=int(reservations.get("count") or 0),
        )
        decision["fairness_waiter"] = copy.deepcopy(fairness_waiter)
        revision = reservations.get("revision")
        if type(revision) is int and revision >= 0:
            decision["_budget_reservation_revision"] = revision
        return decision
    priorities = [
        value for value in (
            woken.get("min_priority"), waiting.get("min_priority"))
        if type(value) is int
    ]
    waiter_priority = min(priorities) if priorities else None
    if (type(priority) is not int or not 1 <= priority <= 10
            or type(waiter_priority) is not int
            or waiter_priority >= priority):
        return None
    now = time.time()
    decision = _budget_denied(
        policy, state="priority_waiter",
        reason=(
            "GitHub API-бюджет освобождён; сначала разбужена задача "
            f"с более высоким приоритетом {waiter_priority}"),
        now=now, limits=limits,
        defer_at=now + policy["busy_retry_seconds"],
        status_revision=status_revision,
        reserved_other=reservations.get("totals"),
        active_reservations=int(reservations.get("count") or 0),
    )
    revision = reservations.get("revision")
    if type(revision) is int and revision >= 0:
        decision["_budget_reservation_revision"] = revision
    return decision


def _arm_budget_waiter(task, decision: dict) -> dict:
    """Persist a budget handoff before the serial admission lease is released."""
    state = decision.get("state")
    if state not in {
            "budget_in_flight", "ledger_changed", "priority_waiter",
            "fairness_waiter", "scan_in_progress"}:
        return decision
    scope = decision.get("lease_scope")
    revision = decision.get("_budget_reservation_revision")
    task_id = getattr(task, "id", None)
    started_at = getattr(task, "started_at", None)
    revision_valid = type(revision) is int and revision >= 0
    if (not isinstance(scope, str) or not scope
            or (state != "scan_in_progress" and not revision_valid)
            or type(task_id) is not int or task_id <= 0
            or started_at is None):
        raise ValueError(
            "running task identity or reservation revision is unavailable "
            "for GitHub budget wait")
    armed = db.arm_pipeline_github_budget_waiter(
        scope, task_id=task_id, task_started_at=started_at,
        expected_revision=(revision if revision_valid else None))
    if not armed.get("armed"):
        raise ValueError(
            "running task attempt changed before GitHub budget wait was armed")
    current_revision = armed.get("revision")
    if type(current_revision) is not int or current_revision < 0:
        raise ValueError("GitHub budget waiter returned an invalid revision")
    decision["_budget_reservation_revision"] = current_revision
    if armed.get("revision_changed"):
        # A release may race the budget observation, but cannot race a lower
        # admission while this scan lease is held. Keep the priority marker and
        # make this exact task runnable immediately after it is deferred.
        decision["defer_until"] = _defer_at(time.time())
    return decision


@contextmanager
def _github_scan_admission(profile: dict, purpose: str,
                           *, profile_id: str | None = None,
                           budget_route: str | None = None, task=None):
    """Admit one expensive scan and hold its cross-process reservation."""
    now = time.time()
    try:
        policy = _github_budget_policy(profile)
        if policy is not None:
            policy = _with_shared_budget_floor(policy)
            policy = _budget_policy_for_route(policy, budget_route)
            if task is not None:
                policy = _budget_policy_for_admission_priority(
                    policy, getattr(task, "priority", None))
            elif budget_route == "insights":
                # Full queue refreshes are useful but never more urgent than
                # an already queued P1 integration task.
                policy = _budget_policy_for_admission_priority(policy, 10)
    except (TypeError, ValueError) as exc:
        fallback = {
            "minimum_remaining": dict(_DEFAULT_GITHUB_BUDGET_MINIMUM),
            "unavailable_retry_seconds": 300,
            "lease_scope": _GITHUB_SCAN_LEASE_SCOPE,
        }
        decision = _budget_denied(
            fallback, state="invalid_config",
            reason=f"Некорректный github_budget: {exc}", now=now)
        _clear_budget_waiter_after_denial(task)
        yield decision
        return
    if policy is None:
        _clear_budget_waiter_after_denial(task)
        yield {"enabled": False, "allowed": True, "state": "legacy"}
        return

    token = uuid.uuid4().hex
    try:
        acquired = db.acquire_pipeline_scan_lease(
            policy["lease_scope"], token, policy["lease_seconds"], now=now)
    except Exception as exc:
        decision = _budget_denied(
            policy, state="lease_unavailable",
            reason=f"SQLite lease GitHub-сканирования недоступна: {exc}", now=now)
        _clear_budget_waiter_after_denial(task)
        yield decision
        return
    if not acquired.get("acquired"):
        status_revision = acquired.get("status_revision")
        state = acquired.get("state")
        if state == "busy":
            existing_expiry = acquired.get("expires_at")
            retry_at = now + policy["busy_retry_seconds"]
            if isinstance(existing_expiry, (int, float)):
                retry_at = max(now + 1, min(retry_at, float(existing_expiry) + 1))
            reason = "GitHub scan уже выполняется другим PromptPilot-процессом"
            blocked_state = "scan_in_progress"
        else:
            retry_at = now + policy["unavailable_retry_seconds"]
            reason = "SQLite lease GitHub-сканирования повреждена; scan запрещён"
            blocked_state = "lease_unavailable"
        decision = _budget_denied(
            policy, state=blocked_state, reason=reason, now=now,
            defer_at=retry_at,
            status_revision=(status_revision
                             if isinstance(status_revision, int) else None))
        if task is not None:
            try:
                decision = _arm_budget_waiter(task, decision)
            except (TypeError, ValueError, sqlite3.Error) as exc:
                decision = _budget_denied(
                    policy, state="lease_unavailable",
                    reason=f"GitHub budget handoff недоступен: {exc}",
                    now=now, defer_at=now + policy["unavailable_retry_seconds"],
                    status_revision=(
                        status_revision
                        if isinstance(status_revision, int) else None))
        if decision.get("state") not in {
                "budget_in_flight", "ledger_changed", "priority_waiter",
                "fairness_waiter", "scan_in_progress"}:
            _clear_budget_waiter_after_denial(task)
        yield decision
        return

    status_revision = acquired.get("status_revision")
    lease = _GitHubScanLease(
        policy["lease_scope"], token, policy["lease_seconds"],
        status_revision=(status_revision
                         if isinstance(status_revision, int) else None))
    lease.expires_at = acquired.get("expires_at")
    previous = getattr(_scan_lease_context, "lease", None)
    _scan_lease_context.lease = lease
    try:
        try:
            lease.start()
            try:
                limits = _github_rate_limits()
            except _GitHubScanLeaseFailure:
                raise
            except _GitHubRateLimitUnavailable as exc:
                decision = _rate_limit_failure_decision(
                    profile, exc, status_revision=lease.status_revision)
                if task is not None:
                    _clear_budget_waiter_after_denial(task)
                decision["_lease"] = lease
                yield decision
                return
            lease.ensure_owned(renew=True)
            if lease.lost:
                decision = _lease_failure_decision(
                    profile, "SQLite lease потеряна во время GitHub /rate_limit",
                    status_revision=lease.status_revision)
            else:
                try:
                    reservations = _budget_reservation_ledger(policy)
                except Exception as exc:
                    decision = _budget_denied(
                        policy, state="ledger_unavailable",
                        reason=f"Журнал резервов GitHub API недоступен: {exc}",
                        now=time.time(), limits=limits,
                        status_revision=lease.status_revision)
                else:
                    admission_priority = getattr(
                        task, "priority",
                        10 if budget_route == "insights" else None)
                    decision = _priority_waiter_decision(
                        policy, limits, reservations,
                        priority=admission_priority,
                        task_id=getattr(task, "id", None),
                        status_revision=lease.status_revision)
                    if decision is None:
                        decision = _evaluate_github_budget(
                            policy, limits, now=time.time(),
                            status_revision=lease.status_revision,
                            reserved_other=reservations["totals"],
                            active_reservations=reservations["count"])
                        decision["_budget_reservation_revision"] = \
                            reservations["revision"]
                    if task is not None:
                        decision = _arm_budget_waiter(task, decision)
        except _GitHubScanLeaseFailure as exc:
            decision = _lease_exception_decision(
                profile, exc, status_revision=lease.status_revision)
        except Exception as exc:
            decision = _lease_failure_decision(
                profile, f"GitHub budget admission не выполнен: {exc}",
                status_revision=lease.status_revision, unavailable=True)
        if (task is not None and not decision.get("allowed")
                and decision.get("state") not in {
                    "budget_in_flight", "ledger_changed", "priority_waiter",
                    "fairness_waiter", "scan_in_progress"}):
            _clear_budget_waiter_after_admission(task)
        decision["_lease"] = lease
        yield decision
    finally:
        _scan_lease_context.lease = previous
        refresh_status = ({
            "profile_id": profile_id,
            "repository": profile.get("repository", ""),
            "status": (None if decision.get("allowed")
                       else _refresh_blocked_status(decision)),
        } if profile_id is not None and not lease.lost else None)
        lease.release(refresh_status=refresh_status)


def _ensure_github_scan_lease(*, renew: bool = False) -> None:
    lease = getattr(_scan_lease_context, "lease", None)
    if lease is not None:
        lease.ensure_owned(renew=renew)


def _current_github_scan_status_revision() -> int | None:
    lease = getattr(_scan_lease_context, "lease", None)
    return lease.status_revision if lease is not None else None


class _GitHubBudgetReservation:
    """A durable quota promise fenced to one exact running task attempt."""

    def __init__(self, scope: str, token: str, task_id: int, started_at):
        self.scope = scope
        self.token = token
        self.task_id = task_id
        self.started_at = started_at
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            try:
                db.release_pipeline_github_budget(
                    self.scope, token=self.token, task_id=self.task_id,
                    task_started_at=self.started_at)
            except Exception:
                # Terminal task status is also a fence. A later ledger read will
                # prune this row even when explicit cleanup hit a locked DB.
                pass


def _reservation_denied(policy: dict, limits: dict | None, result: dict, *,
                        status_revision: int | None) -> dict:
    now = time.time()
    blocked = result.get("blocked_resources") or []
    active_reservations = int(result.get("active_reservations") or 0)
    if result.get("state") == "ledger_changed":
        defer_at = now + 1
        state = "ledger_changed"
        reason = str(result.get("reason") or
                     "GitHub budget changed during pipeline admission")
    elif result.get("state") == "priority_waiter":
        defer_at = now + policy["busy_retry_seconds"]
        state = "priority_waiter"
        reason = str(result.get("reason") or
                     "GitHub budget yielded to a higher-priority waiter")
    elif result.get("state") == "fairness_waiter":
        defer_at = now + policy["busy_retry_seconds"]
        state = "fairness_waiter"
        reason = str(result.get("reason") or
                     "GitHub budget yielded to the oldest aged waiter")
    elif blocked:
        live_blocked = [item for item in blocked
                        if item.get("blocked_by") == "live"]
        if live_blocked:
            defer_at = max(int(item["reset"]) for item in live_blocked) + \
                policy["reset_grace_seconds"]
            if defer_at <= now:
                defer_at = now + policy["unavailable_retry_seconds"]
            state = "low"
            reason_prefix = "GitHub API-бюджет недостаточен"
        else:
            defer_at = now + policy["busy_retry_seconds"]
            state = "budget_in_flight"
            reason_prefix = "GitHub API-бюджет временно занят выполняемой задачей"
        summary = ", ".join(
            _budget_blocked_summary(item, active_reservations)
            for item in blocked)
        reason = f"{reason_prefix}: {summary}"
    else:
        defer_at = now + policy["unavailable_retry_seconds"]
        state = str(result.get("state") or "reservation_unavailable")
        reason = str(result.get("reason") or
                     "GitHub API reservation не создана")
    decision = _budget_denied(
        policy, state=state, reason=reason, now=now, limits=limits,
        defer_at=defer_at, blocked_resources=blocked,
        status_revision=status_revision,
        reserved_other=result.get("reserved_other"),
        effective_after=result.get("effective_after"),
        active_reservations=active_reservations)
    revision = result.get("revision")
    if type(revision) is int and revision >= 0:
        decision["_budget_reservation_revision"] = revision
    if isinstance(result.get("fairness_waiter"), dict):
        decision["fairness_waiter"] = copy.deepcopy(
            result["fairness_waiter"])
    return decision


def _clear_budget_waiter_after_admission(task) -> None:
    """Settle a durable handoff when no in-flight reservation will do it."""
    task_id = getattr(task, "id", None)
    started_at = getattr(task, "started_at", None)
    # Synthetic/read-only callers cannot own a durable marker. Real worker
    # attempts always carry both values and are fenced before provider launch.
    if type(task_id) is not int or task_id <= 0 or started_at is None:
        return
    if not db.clear_pipeline_github_budget_waiter(
            task_id=task_id, task_started_at=started_at):
        raise ValueError(
            "running task attempt changed before GitHub budget waiter clear")


def _clear_budget_waiter_after_denial(task) -> None:
    """Best-effort cleanup for a denial that will not preserve a handoff."""
    try:
        _clear_budget_waiter_after_admission(task)
    except (sqlite3.Error, TypeError, ValueError):
        # The denial remains fail-closed. If SQLite itself is unavailable the
        # worker defer/terminal path will retry cleanup once storage recovers.
        pass


def _reserve_execution_admission(
        task, profile_id: str, profile: dict, queue: dict, admission: dict,
        budget_route: str, *, retain_budget: bool,
        admission_priority: int | None = None) -> dict:
    """Recheck the elected route and optionally reserve its in-flight cost."""
    lease = admission.get("_lease")
    expected_revision = admission.get("_budget_reservation_revision")
    effective_priority = (
        getattr(task, "priority", None)
        if admission_priority is None else admission_priority)
    if type(expected_revision) is not int or expected_revision < 0:
        expected_revision = None
    try:
        policy = _github_budget_policy(profile)
        if policy is None:
            _clear_budget_waiter_after_admission(task)
            return admission
        policy = _with_shared_budget_floor(policy)
        policy = _budget_policy_for_route(policy, budget_route)
        policy = _budget_policy_for_admission_priority(
            policy, effective_priority)
        if not policy.get("cost_accounting"):
            # Profiles without route costs keep the original one-shot floor
            # admission; they neither re-read limits nor create reservations.
            _clear_budget_waiter_after_admission(task)
            return admission
        if not isinstance(lease, _GitHubScanLease):
            raise _GitHubScanLeaseLost(
                "GitHub execution admission lost its scan lease")
        lease.ensure_owned(renew=True)
        try:
            limits = _github_rate_limits()
        except _GitHubScanLeaseFailure:
            raise
        except _GitHubRateLimitUnavailable as exc:
            decision = _rate_limit_failure_decision(
                profile, exc, status_revision=lease.status_revision)
            decision["_lease"] = lease
            admission.clear()
            admission.update(decision)
            return admission
        if not isinstance(limits, dict):
            decision = _evaluate_github_budget(
                policy, limits, now=time.time(),
                status_revision=lease.status_revision)
        elif not retain_budget:
            reservations = _budget_reservation_ledger(policy)
            decision = _priority_waiter_decision(
                policy, limits, reservations,
                priority=effective_priority,
                task_id=getattr(task, "id", None),
                status_revision=lease.status_revision)
            if decision is None:
                decision = _evaluate_github_budget(
                    policy, limits, now=time.time(),
                    status_revision=lease.status_revision,
                    reserved_other=reservations["totals"],
                    active_reservations=reservations["count"])
                decision["_budget_reservation_revision"] = \
                    reservations["revision"]
        else:
            task_id = getattr(task, "id", None)
            started_at = getattr(task, "started_at", None)
            if task_id is None or started_at is None:
                raise ValueError(
                    "running task identity is unavailable for GitHub reservation")
            token = uuid.uuid4().hex
            reserved = db.reserve_pipeline_github_budget(
                policy["lease_scope"], token=token, task_id=task_id,
                task_started_at=started_at, profile_id=profile_id,
                queue_id=str(queue.get("id") or ""), route=budget_route,
                cost=policy["requested_cost"], limits=limits,
                minimum_remaining=policy["minimum_remaining"],
                starvation_timeout_seconds=policy.get(
                    "starvation_timeout_seconds", 0),
                scan_lease_guard=lease.guard,
                expected_revision=expected_revision)
            if not reserved.get("allowed"):
                decision = _reservation_denied(
                    policy, limits, reserved,
                    status_revision=lease.status_revision)
            else:
                decision = _evaluate_github_budget(
                    policy, limits, now=time.time(),
                    status_revision=lease.status_revision,
                    reserved_other=reserved.get("reserved_other"),
                    active_reservations=int(
                        reserved.get("active_reservations") or 0))
                decision["_budget_reservation_revision"] = \
                    reserved.get("revision")
                if not decision.get("allowed"):
                    db.release_pipeline_github_budget(
                        policy["lease_scope"], token=token, task_id=task_id,
                        task_started_at=started_at)
                else:
                    current = getattr(
                        _execution_budget_context, "reservation", None)
                    if current is not None:
                        db.release_pipeline_github_budget(
                            policy["lease_scope"], token=token,
                            task_id=task_id, task_started_at=started_at)
                        raise RuntimeError(
                            "worker thread already owns a GitHub budget reservation")
                    _execution_budget_context.reservation = \
                        _GitHubBudgetReservation(
                            policy["lease_scope"], token, task_id, started_at)
        decision = _arm_budget_waiter(task, decision)
        if decision.get("allowed") and not retain_budget:
            _clear_budget_waiter_after_admission(task)
    except _GitHubScanLeaseFailure as exc:
        decision = _lease_exception_decision(
            profile, exc,
            status_revision=(lease.status_revision
                             if isinstance(lease, _GitHubScanLease) else None))
    except (TypeError, ValueError, sqlite3.Error) as exc:
        fallback_policy = {
            "minimum_remaining": dict(_DEFAULT_GITHUB_BUDGET_MINIMUM),
            "requested_cost": {
                resource: 0 for resource in _DEFAULT_GITHUB_BUDGET_MINIMUM},
            "costs": None, "lease_scope": _GITHUB_SCAN_LEASE_SCOPE,
            "unavailable_retry_seconds": 300,
            "reset_grace_seconds": 60,
            "budget_route": budget_route,
        }
        decision = _budget_denied(
            fallback_policy,
            state="reservation_unavailable",
            reason=f"GitHub API reservation не создана: {exc}",
            now=time.time(),
            status_revision=(lease.status_revision
                             if isinstance(lease, _GitHubScanLease) else None))
    if (not decision.get("allowed") and decision.get("state") not in {
            "budget_in_flight", "ledger_changed", "priority_waiter",
            "fairness_waiter", "scan_in_progress"}):
        _clear_budget_waiter_after_denial(task)
    decision["_lease"] = lease
    admission.clear()
    admission.update(decision)
    return admission


def release_execution_admission() -> None:
    """Release a provider-spanning reservation; safe after every worker path."""
    reservation = getattr(_execution_budget_context, "reservation", None)
    _execution_budget_context.reservation = None
    if isinstance(reservation, _GitHubBudgetReservation):
        reservation.release()


def _public_budget_decision(decision: dict) -> dict:
    """Strip internal ordering and duplicate rate-limit fields from the UI."""
    return {
        key: copy.deepcopy(value) for key, value in decision.items()
        if key not in {"github_rate_limit", "status_revision"}
        and not key.startswith("_")
    }


def _refresh_blocked_status(decision: dict) -> dict:
    return {
        "refresh_blocked": str(decision.get("state") or "github_budget"),
        "refresh_blocked_reason": str(
            decision.get("reason") or "GitHub scan запрещён"),
        "refresh_deferred_until": decision.get("defer_until"),
        "github_rate_limit": copy.deepcopy(decision.get("github_rate_limit")),
        "github_budget": _public_budget_decision(decision),
    }


def _record_refresh_blocked(profile_id: str, profile: dict,
                            decision: dict) -> bool:
    """Persist a denial without replacing the last-good GitHub snapshot."""
    lease = getattr(_scan_lease_context, "lease", None)
    guard = lease.guard if lease is not None and not lease.lost else None
    try:
        return db.publish_pipeline_refresh_status(
            profile_id, profile.get("repository", ""),
            _refresh_blocked_status(decision),
            revision=(decision.get("status_revision")
                      if isinstance(decision.get("status_revision"), int)
                      else None),
            lease_guard=guard,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # The current caller still receives the denial. Persistence is a
        # visibility aid and must not turn a safe refusal into a failed task.
        return False


def _profiles() -> dict:
    """Load user-owned profiles; PromptPilot ships without project-specific data."""
    result = dict(DEFAULT_PROFILES)
    path = Path(os.environ.get("PP_PIPELINE_PROFILES", DB_DIR / "pipeline_profiles.json"))
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        result.update(payload.get("profiles", payload))
    return result


def list_profiles() -> list[dict]:
    return [{"id": key, "title": value["title"], "repository": value["repository"]}
            for key, value in _profiles().items()]


def _cache_snapshot(profile_id: str) -> tuple[object | None, int]:
    """Read one entry and the process generation guarding an eventual write."""
    with _cache_state_lock:
        return _cache.get(profile_id), _cache_generation


def _cache_namespace_prefix(profile_id: str) -> str:
    return f"{_CACHE_KEY_PREFIX}{quote(profile_id, safe='')}:"


def _legacy_cache_key(profile_id: str) -> str:
    """Key used by the first durable-cache build before fingerprint namespaces."""
    return f"{_CACHE_KEY_PREFIX}{profile_id}"


def _cache_key(profile_id: str, profile_hash: str) -> str:
    return f"{_cache_namespace_prefix(profile_id)}{profile_hash}"


def _refresh_revision_key(profile_id: str) -> str:
    return f"{_CACHE_REFRESH_REVISION_PREFIX}{profile_id}"


def _published_revision_key(profile_id: str, profile_hash: str) -> str:
    return (f"{_CACHE_PUBLISHED_REVISION_PREFIX}"
            f"{quote(profile_id, safe='')}:{profile_hash}")


def _published_profile_key(profile_id: str) -> str:
    return f"{_CACHE_PUBLISHED_PROFILE_PREFIX}{quote(profile_id, safe='')}"


def _published_profile_revision_key(profile_id: str) -> str:
    return (f"{_CACHE_PUBLISHED_PROFILE_REVISION_PREFIX}"
            f"{quote(profile_id, safe='')}")


def _profile_fingerprint(profile: dict) -> str:
    # Admission tuning changes when a scan may run, not what the observation
    # means. Keep last-good data readable when an operator enables/tunes the
    # budget while GitHub quota is already low.
    observed_profile = copy.deepcopy(profile)
    observed_profile.pop("github_budget", None)
    encoded = json.dumps(
        observed_profile, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cache_epoch() -> int:
    try:
        return max(0, int(db.get_setting(_CACHE_EPOCH_KEY, "0")))
    except (TypeError, ValueError):
        return 0


def _cache_matches_profile(result: object, profile_id: str, profile: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if (result.get("profile_id") != profile_id
            or result.get("repository") != profile.get("repository")):
        return False
    cached_queues = result.get("queues")
    if not isinstance(cached_queues, list):
        return False
    expected_ids = {str(queue.get("id")) for queue in profile.get("queues", [])}
    actual_ids = {str(queue.get("id")) for queue in cached_queues
                  if isinstance(queue, dict)}
    return expected_ids == actual_ids and len(actual_ids) == len(cached_queues)


def _decode_durable_cache(profile_id: str, profile: dict,
                          raw: str | None) -> tuple | None:
    profile_hash = _profile_fingerprint(profile)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if payload.get("version") != _CACHE_SCHEMA_VERSION:
            return None
        if payload.get("profile_hash") != profile_hash:
            return None
        result = payload.get("result")
        if not _cache_matches_profile(result, profile_id, profile):
            return None
        generated_at = float(payload.get("generated_at", result.get("generated_at")))
        epoch = max(0, int(payload.get("epoch", 0)))
        revision = max(0, int(payload.get("revision", 0)))
        return generated_at, result, epoch, profile_hash, revision
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _cached_entry(
        profile_id: str, profile: dict) -> tuple[tuple | None, str | None, int]:
    """Load one coherent full-cache payload, revision and invalidation epoch."""
    cached, generation = _cache_snapshot(profile_id)
    profile_hash = _profile_fingerprint(profile)
    cache_key = _cache_key(profile_id, profile_hash)
    revision_key = _published_revision_key(profile_id, profile_hash)
    settings = db.get_settings_snapshot(
        [cache_key, revision_key, _CACHE_EPOCH_KEY])
    try:
        current_epoch = max(0, int(settings.get(_CACHE_EPOCH_KEY, "0")))
    except (TypeError, ValueError):
        current_epoch = 0
    try:
        published_revision = max(0, int(settings.get(revision_key, "0")))
    except (TypeError, ValueError):
        published_revision = 0
    if (cached and len(cached) >= 5 and cached[3] == profile_hash
            and int(cached[4]) == published_revision
            and _cache_matches_profile(cached[1], profile_id, profile)):
        return cached, "memory", current_epoch
    durable = _decode_durable_cache(
        profile_id, profile, settings.get(cache_key))
    if durable is None or int(durable[4]) != published_revision:
        return None, None, current_epoch
    with _cache_state_lock:
        if generation == _cache_generation:
            _cache[profile_id] = durable
    return durable, "durable", current_epoch


def _profile_lock(profile_id: str) -> threading.Lock:
    # Do not hold the state lock while acquiring the returned per-profile lock.
    # analyze() may take the locks in the opposite order when it publishes.
    with _cache_state_lock:
        return _locks.setdefault(profile_id, threading.Lock())


def _publish_cache(profile_id: str, profile: dict, generation: int, epoch: int,
                   revision: int, result: dict) -> bool:
    """Atomically publish a complete last-good response if still current."""
    with _cache_state_lock:
        if generation != _cache_generation:
            return False
        profile_hash = _profile_fingerprint(profile)
        generated_at = float(result["generated_at"])
        payload = json.dumps({
            "version": _CACHE_SCHEMA_VERSION,
            "profile_id": profile_id,
            "repository": result.get("repository"),
            "generated_at": generated_at,
            "epoch": epoch,
            "revision": revision,
            "profile_hash": profile_hash,
            "result": result,
        }, ensure_ascii=False, separators=(",", ":"))
        lease = getattr(_scan_lease_context, "lease", None)
        if not db.set_setting_if_newer_revision(
                _cache_key(profile_id, profile_hash), payload,
                revision_key=_published_revision_key(profile_id, profile_hash),
                revision=revision,
                guard_key=_CACHE_EPOCH_KEY, expected_guard=str(epoch),
                guard_default="0",
                lease_guard=lease.guard if lease is not None else None,
                publication_revision_key=(
                    _published_profile_revision_key(profile_id)),
                companion_key=_published_profile_key(profile_id),
                companion_value=profile_hash):
            if lease is not None:
                # The CAS can also lose to an ordinary cache invalidation or a
                # newer refresh. Distinguish that benign race from a rejected
                # lease fence by proving ownership again under SQLite.
                lease.ensure_owned(renew=True)
            return False
        _cache[profile_id] = (
            generated_at, result, epoch, profile_hash, revision)
        return True


def _discard_cache(profile_id: str | None = None, *,
                   invalidate_durable: bool = True) -> None:
    global _cache_generation
    with _cache_state_lock:
        _cache_generation += 1
        if invalidate_durable:
            # Keep the last expensive snapshot available, but make its
            # staleness durable across every server/bot process and restart.
            db.increment_int_setting(_CACHE_EPOCH_KEY)
        if profile_id is None:
            _cache.clear()
        else:
            _cache.pop(profile_id, None)


def invalidate_cache() -> None:
    """Clear process-local views after pause changes; saved GitHub data survives."""
    # Pause is only a local runtime projection over external queue data. Do not
    # advance either generation: a complete refresh already in flight remains
    # valid and must still become the durable last-good snapshot.
    with _cache_state_lock:
        _cache.clear()


def _gh_executable() -> str:
    configured = os.environ.get("PP_GH_EXE")
    found = configured or shutil.which("gh") or shutil.which("gh.exe")
    if not found:
        raise RuntimeError("GitHub CLI (gh) не найден. Установите gh и выполните gh auth login.")
    return found


def _github_search(repository: str, query: str) -> dict:
    """Return count plus public item metadata used for age and movement metrics."""
    def fetch_page(page: int) -> dict:
        _ensure_github_scan_lease(renew=True)
        command = [
            _gh_executable(), "api", "search/issues", "--method", "GET",
            "--field", f"q=repo:{repository} {query}", "--field", "per_page=100",
            "--field", f"page={page}",
        ]
        run = subprocess.run(command, capture_output=True, text=True, timeout=30,
                             encoding="utf-8", errors="replace")
        _ensure_github_scan_lease()
        if run.returncode:
            raise RuntimeError(
                (run.stderr or run.stdout or "gh api завершился с ошибкой").strip())
        return json.loads(run.stdout)

    if db.is_paused():
        raise _GitHubScanPaused("pipeline scan interrupted by global pause")
    payload = fetch_page(1)
    total = int(payload.get("total_count", len(payload.get("items", []))))
    raw_items = list(payload.get("items", []))
    # GitHub Search exposes at most 1000 matches. Fetching all exposed pages keeps
    # movement metrics exact for ordinary queues instead of silently sampling 100.
    exposed_total = min(total, 1000)
    for page in range(2, math.ceil(exposed_total / 100) + 1):
        # Page 1 may have been in flight when pause was enabled. It is safe to
        # finish that request, but never start another page from a partial scan.
        if db.is_paused():
            raise _GitHubScanPaused("pipeline scan interrupted by global pause")
        page_items = fetch_page(page).get("items", [])
        raw_items.extend(page_items)
        if len(page_items) < 100:
            break

    items = []
    for item in raw_items:
        kind = "pr" if item.get("pull_request") is not None else "issue"
        number = int(item["number"])
        items.append({
            "key": f"{kind}:{number}", "kind": kind, "number": number,
            "title": item.get("title") or f"#{number}",
            "labels": sorted(label.get("name", "") for label in item.get("labels", [])
                             if label.get("name")),
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
            "url": item.get("html_url"),
        })
    return {"count": total, "items": items, "membership_complete": total <= len(items)}


def _github_count(repository: str, query: str) -> int:
    """Compatibility helper for callers that only need the current count."""
    return _github_search(repository, query)["count"]


def _priority_settings(profile: dict) -> dict | None:
    control = profile.get("priority_control")
    if not isinstance(control, dict) or control.get("enabled", True) is False:
        return None
    manual = control.get("manual_labels") or {
        "p0": "queue:p0", "p1": "queue:p1", "p2": "queue:p2", "p3": "queue:p3",
    }
    automatic = control.get("auto_labels") or {
        "p0": "queue:auto:p0", "p1": "queue:auto:p1",
        "p2": "queue:auto:p2", "p3": "queue:auto:p3",
    }
    if set(manual) != set(_PRIORITY_LEVELS) or set(automatic) != set(_PRIORITY_LEVELS):
        raise ValueError("priority_control labels must define p0, p1, p2 and p3")
    default_level = str(control.get("default_level", "p2")).lower()
    if default_level not in _PRIORITY_LEVELS:
        raise ValueError("priority_control default_level must be p0, p1, p2 or p3")
    return {
        **control, "manual_labels": manual, "auto_labels": automatic,
        "default_level": default_level,
        "aging_hours": max(1, int(control.get("aging_hours", 168))),
        "max_items": max(1, min(int(control.get("max_items", 12)), 50)),
    }


def _item_priority(item: dict, settings: dict, now: datetime) -> dict:
    labels = set(item.get("labels") or [])
    manual_matches = [(index, label) for index, level in enumerate(_PRIORITY_LEVELS)
                      if (label := settings["manual_labels"][level]) in labels]
    auto_matches = [(index, label) for index, level in enumerate(_PRIORITY_LEVELS)
                    if (label := settings["auto_labels"][level]) in labels]
    if manual_matches:
        base, label = min(manual_matches)
        source, reason = "manual", label
    elif auto_matches:
        base, label = min(auto_matches)
        source, reason = "auto", label
    else:
        base = _PRIORITY_LEVELS.index(settings["default_level"])
        source, reason = "auto", "default"
        for rule_labels, rule_level, rule_reason in _DEFAULT_PRIORITY_RULES:
            if labels & rule_labels:
                base, reason = rule_level, rule_reason
                break
    created = _parse_time(item.get("created_at"))
    age_hours = max(0, (now - created).total_seconds() / 3600) if created else 0
    boost = min(max(0, base - 1), int(age_hours // settings["aging_hours"]))
    effective = base - boost
    return {
        "level": _PRIORITY_LEVELS[effective], "base_level": _PRIORITY_LEVELS[base],
        "source": source, "reason": reason, "age_boost": boost,
        "conflict": len(manual_matches) > 1 or len(auto_matches) > 1,
    }


def _gh_api_json(args: list[str], input_value: dict | None = None):
    _ensure_github_scan_lease(renew=True)
    command = [_gh_executable(), "api", *args]
    encoded = None
    if input_value is not None:
        command += ["--input", "-"]
        encoded = json.dumps(input_value, ensure_ascii=False)
    run = subprocess.run(
        command, input=encoded, capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="strict",
    )
    _ensure_github_scan_lease()
    if run.returncode:
        raise RuntimeError((run.stderr or run.stdout or "gh api failed").strip())
    return json.loads(run.stdout) if run.stdout.strip() else None


_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_GITHUB_RATE_LIMIT_QUERY = """query PromptPilotRateLimit {
  viewer { login }
  rateLimit { limit remaining resetAt used }
}"""


def _valid_github_login(value) -> bool:
    return isinstance(value, str) and _GITHUB_LOGIN.fullmatch(value) is not None


def _trusted_github_accounts() -> set[str]:
    """Return safe identity contracts for budget-enabled shared profiles."""
    try:
        profiles = _profiles()
    except Exception as exc:
        raise _GitHubRateLimitUnavailable(
            "pipeline profiles could not be read for identity validation") from exc
    if not isinstance(profiles, dict):
        raise _GitHubRateLimitUnavailable(
            "pipeline profiles are invalid for identity validation")
    accounts = set()
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        budget = profile.get("github_budget")
        if budget is None or budget is False:
            continue
        if isinstance(budget, dict) and budget.get("enabled", True) is False:
            continue
        control = profile.get("priority_control")
        trusted = control.get("trusted_account") \
            if isinstance(control, dict) else None
        if trusted is None:
            continue
        if not _valid_github_login(trusted):
            raise _GitHubRateLimitUnavailable(
                "configured trusted GitHub account is invalid")
        accounts.add(trusted)
    if len({account.casefold() for account in accounts}) > 1:
        raise _GitHubRateLimitUnavailable(
            "pipeline profiles configure different trusted GitHub accounts")
    return accounts


def _rate_limit_integer(item: dict, field: str, source: str) -> int:
    value = item.get(field)
    if type(value) is not int or value < 0:
        raise _GitHubRateLimitUnavailable(
            f"GitHub {source} rate limit response is invalid")
    return value


def _rest_rate_limit_resource(payload: dict, name: str) -> dict:
    try:
        item = payload["resources"][name]
    except (KeyError, TypeError) as exc:
        raise _GitHubRateLimitUnavailable(
            f"GitHub REST /rate_limit response is invalid for {name}") from exc
    if not isinstance(item, dict):
        raise _GitHubRateLimitUnavailable(
            f"GitHub REST /rate_limit response is invalid for {name}")
    limit = _rate_limit_integer(item, "limit", f"REST {name}")
    used = _rate_limit_integer(item, "used", f"REST {name}")
    remaining = _rate_limit_integer(item, "remaining", f"REST {name}")
    reset = _rate_limit_integer(item, "reset", f"REST {name}")
    # A smaller sum is conservative if GitHub adjusts quota mid-window. A
    # larger sum claims overlapping spent and available quota, so ``remaining``
    # is not safe enough for admission.
    if used + remaining > limit:
        raise _GitHubRateLimitUnavailable(
            f"GitHub REST /rate_limit response is invalid for {name}")
    try:
        reset_at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise _GitHubRateLimitUnavailable(
            f"GitHub REST /rate_limit response is invalid for {name}") from exc
    return {
        "limit": limit, "used": used, "remaining": remaining,
        "reset": reset, "reset_at": reset_at,
    }


def _graphql_rate_limit_resource(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("errors"):
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit response is invalid")
    try:
        data = payload["data"]
        item = data["rateLimit"]
        login = data["viewer"]["login"]
    except (KeyError, TypeError) as exc:
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit or viewer response is invalid") from exc
    if not isinstance(item, dict):
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit response is invalid")
    if not _valid_github_login(login):
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL viewer response is invalid")
    trusted_accounts = _trusted_github_accounts()
    if trusted_accounts:
        expected = next(iter(trusted_accounts))
        if login.casefold() != expected.casefold():
            raise _GitHubRateLimitUnavailable(
                f"GitHub identity mismatch: authenticated as {login}, "
                f"expected {expected}")
    limit = _rate_limit_integer(item, "limit", "GraphQL")
    used = _rate_limit_integer(item, "used", "GraphQL")
    remaining = _rate_limit_integer(item, "remaining", "GraphQL")
    if used + remaining > limit:
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit response is invalid")
    reset_at = item.get("resetAt")
    if not isinstance(reset_at, str):
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit resetAt is invalid")
    try:
        reset_time = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
        if reset_time.tzinfo is None:
            raise ValueError("timezone is missing")
        reset = int(reset_time.timestamp())
        normalized_reset = reset_time.astimezone(timezone.utc).isoformat()
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit resetAt is invalid") from exc
    if reset < 0:
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit resetAt is invalid")
    return {
        "limit": limit, "used": used, "remaining": remaining,
        "reset": reset, "reset_at": normalized_reset,
    }


def _github_rate_limits() -> dict:
    """Read REST core/search and the authenticated GraphQL budget/identity.

    ``GET /rate_limit`` does not spend primary REST quota. The small GraphQL
    query is necessary because the REST endpoint can report a stale/default
    GraphQL bucket; its returned ``rateLimit`` includes that query's cost.
    """
    try:
        rest_payload = _gh_api_json(["rate_limit"])
    except _GitHubScanLeaseFailure:
        raise
    except Exception as exc:
        raise _GitHubRateLimitUnavailable(
            "GitHub REST /rate_limit request failed") from exc
    core = _rest_rate_limit_resource(rest_payload, "core")
    search = _rest_rate_limit_resource(rest_payload, "search")
    try:
        graphql_payload = _gh_api_json(
            ["graphql"], {"query": _GITHUB_RATE_LIMIT_QUERY})
    except _GitHubScanLeaseFailure:
        raise
    except Exception as exc:
        raise _GitHubRateLimitUnavailable(
            "GitHub GraphQL rateLimit request failed") from exc

    result = {
        "core": core,
        "search": search,
        "graphql": _graphql_rate_limit_resource(graphql_payload),
    }
    lease = getattr(_scan_lease_context, "lease", None)
    if isinstance(lease, _GitHubScanLease):
        try:
            stored = db.record_pipeline_github_rate_snapshot(
                lease.scope, result, observed_at=time.time(),
                scan_lease_guard=lease.guard)
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise _GitHubRateLimitUnavailable(
                "GitHub rate limit snapshot could not be stored") from exc
        if not stored:
            raise _GitHubScanLeaseLost(
                "SQLite lease rejected GitHub rate snapshot")
    return result


def set_item_priority(profile_id: str, queue_id: str, kind: str, number: int,
                      level: str, run_now: bool, series: list[dict]) -> dict:
    profiles = _profiles()
    profile = profiles.get(profile_id)
    if not profile:
        raise KeyError(profile_id)
    settings = _priority_settings(profile)
    if not settings:
        raise ValueError("priority control is not enabled for this profile")
    queue = next((item for item in profile.get("queues", []) if item.get("id") == queue_id), None)
    if not queue:
        raise ValueError("unknown pipeline queue")
    if kind not in {"issue", "pr"} or number < 1:
        raise ValueError("invalid GitHub item")
    if level not in {*_PRIORITY_LEVELS, "auto"}:
        raise ValueError("priority must be p0, p1, p2, p3 or auto")

    identity = _gh_api_json(["user"])
    trusted = settings.get("trusted_account")
    if trusted and identity.get("login") != trusted:
        raise RuntimeError(f"authenticated as {identity.get('login')}, expected {trusted}")
    repository = profile["repository"]
    item = _gh_api_json([f"repos/{repository}/issues/{number}"])
    actual_kind = "pr" if item.get("pull_request") else "issue"
    if actual_kind != kind or item.get("state") != "open":
        raise ValueError(f"open {kind} #{number} not found")

    current = {entry["name"] for entry in item.get("labels", [])}
    manual_labels = set(settings["manual_labels"].values())
    selected = settings["manual_labels"].get(level)
    if selected and selected not in current:
        _gh_api_json(
            [f"repos/{repository}/issues/{number}/labels", "--method", "POST"],
            {"labels": [selected]},
        )
    for label in sorted((current & manual_labels) - ({selected} if selected else set())):
        _gh_api_json([
            f"repos/{repository}/issues/{number}/labels/{quote(label, safe='')}",
            "--method", "DELETE",
        ])

    woke = False
    paused = False
    woken_series = []
    if run_now:
        targets = _series_replicas_for_queue(queue, series)
        for target in targets:
            if db.series_action(int(target["id"]), "run_now"):
                woken_series.append(int(target["id"]))
        woke = bool(woken_series)
        paused = any(bool(target.get("paused")) for target in targets)
    _discard_cache(profile_id)
    return {
        "ok": True, "profile_id": profile_id, "queue_id": queue_id,
        "kind": kind, "number": number, "level": level,
        "label": selected, "run_now": run_now, "series_woken": woke,
        "series_woken_count": len(woken_series),
        "series_woken_ids": woken_series, "series_paused": paused,
    }


def _interval_hours(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().lower()
    try:
        if value.endswith("m"):
            return float(value[:-1]) / 60
        if value.endswith("h"):
            return float(value[:-1])
    except ValueError:
        return None
    return None


def _adaptive_cadence_policy(queue: dict) -> dict | None:
    """Validate the generic queue-owned cadence policy, if configured."""
    raw = queue.get("adaptive_cadence")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("adaptive_cadence должен быть JSON-объектом")
    allowed = {
        "idle_recurrence", "busy_recurrence", "backlog_above",
        "empty_runs_before_idle", "event_wake",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            "adaptive_cadence содержит неизвестные поля: " + ", ".join(unknown))
    idle = raw.get("idle_recurrence")
    busy = raw.get("busy_recurrence")
    if not isinstance(idle, str) or db.parse_recurrence(idle) is None:
        raise ValueError("adaptive_cadence.idle_recurrence не разобран")
    if busy is not None and (
            not isinstance(busy, str) or db.parse_recurrence(busy) is None):
        raise ValueError("adaptive_cadence.busy_recurrence не разобран")
    threshold = raw.get("backlog_above", 0)
    empty_runs = raw.get("empty_runs_before_idle", 0)
    event_wake = raw.get("event_wake", False)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise ValueError("adaptive_cadence.backlog_above должен быть >= 0")
    if (isinstance(empty_runs, bool) or not isinstance(empty_runs, int)
            or not 0 <= empty_runs <= 20):
        raise ValueError(
            "adaptive_cadence.empty_runs_before_idle должен быть от 0 до 20")
    if not isinstance(event_wake, bool):
        raise ValueError("adaptive_cadence.event_wake должен быть true или false")
    if busy is None and (threshold or empty_runs):
        raise ValueError(
            "adaptive_cadence.backlog_above/empty_runs_before_idle требуют "
            "busy_recurrence")
    return {
        "idle_recurrence": idle,
        "busy_recurrence": busy,
        "backlog_above": threshold,
        "empty_runs_before_idle": empty_runs,
        "event_wake": event_wake,
    }


def _matching_series_list(matching) -> list[dict]:
    if isinstance(matching, dict):
        return [matching]
    if isinstance(matching, list):
        return [item for item in matching if isinstance(item, dict)]
    return []


def _adaptive_cadence_status(queue: dict, matching,
                             backlog: int | None) -> dict | None:
    policy = _adaptive_cadence_policy(queue)
    if policy is None:
        return None
    matches = _matching_series_list(matching)
    busy = bool(
        policy["busy_recurrence"] is not None
        and isinstance(backlog, int)
        and backlog > policy["backlog_above"]
    )
    temporary = [item.get("temporary_recurrence") for item in matches]
    empty_counts = [int(item.get("temporary_empty_count") or 0)
                    for item in matches]
    draining = bool(
        not busy and policy["empty_runs_before_idle"]
        and any(value == policy["busy_recurrence"] for value in temporary)
    )
    mode = "busy" if busy else "draining" if draining else (
        "event" if policy["event_wake"] else "idle")
    recurrences = list(dict.fromkeys(
        item.get("effective_recurrence") for item in matches
        if item.get("effective_recurrence")))
    return {
        **policy,
        "mode": mode,
        "effective_recurrence": (
            recurrences[0] if len(recurrences) == 1 else
            ", ".join(recurrences) if recurrences else None),
        "empty_runs": min(empty_counts) if empty_counts else 0,
        "series_present": bool(matches),
        "series_count": len(matches),
    }


def _reconcile_adaptive_cadence(
        queue: dict, matching, backlog: int | None,
        publication_guard: dict | None = None) -> dict | None:
    """Apply cadence only during a successful live queue observation."""
    policy = _adaptive_cadence_policy(queue)
    matches = _matching_series_list(matching)
    if policy is None or not matches or not isinstance(backlog, int):
        return _adaptive_cadence_status(queue, matching, backlog)
    boost = bool(
        policy["busy_recurrence"] is not None
        and backlog > policy["backlog_above"])
    for item in matches:
        result = db.apply_pipeline_series_cadence(
            int(item["id"]),
            idle_recurrence=policy["idle_recurrence"],
            busy_recurrence=policy["busy_recurrence"],
            boost=boost,
            empty_runs_before_idle=policy["empty_runs_before_idle"],
            publication_guard=publication_guard,
        )
        if result is not None:
            item.update({
                "recurrence": result["base_recurrence"],
                "effective_recurrence": result["effective_recurrence"],
                "temporary_recurrence": result["temporary_recurrence"],
                "temporary_empty_limit": result["temporary_empty_limit"],
                "temporary_empty_count": result["temporary_empty_count"],
            })
    return _adaptive_cadence_status(queue, matching, backlog)


def _recommendation(item: dict, backlog: int, capacity: int,
                    current_interval: str | None, target_hours: float,
                    avg_duration_seconds: int | None = None) -> dict:
    runs_needed = backlog / capacity if backlog else 0
    current_hours = _interval_hours(current_interval)
    duration_hours = max(0, avg_duration_seconds or 0) / 3600
    cycle_hours = current_hours + duration_hours if current_hours is not None else None
    eta = round(runs_needed * cycle_hours, 1) if cycle_hours is not None else None
    throughput = (round(capacity / cycle_hours, 2)
                  if cycle_hours is not None and cycle_hours > 0 else None)
    timing = {
        "avg_duration_seconds": avg_duration_seconds,
        "cycle_hours": round(cycle_hours, 2) if cycle_hours is not None else None,
        "throughput_per_hour": throughput,
    }
    if not backlog:
        return {"recommended_interval": None, "eta_hours": 0,
                "recommendation": "очередь пуста — можно оставить базовый интервал",
                **timing}
    if item.get("manual_gate"):
        return {"recommended_interval": None, "eta_hours": eta,
                "recommendation": "не ускорять автоматически: этап зависит от решения человека",
                **timing}
    required = target_hours / runs_needed - duration_hours
    if required <= 0:
        duration_minutes = round(duration_hours * 60)
        return {
            "recommended_interval": _INTERVAL_PRESETS[0][1], "eta_hours": eta,
            "recommendation": (
                f"одной частоты недостаточно: прогон занимает около {duration_minutes} мин; "
                "увеличьте ёмкость или параллелизм"),
            **timing,
        }
    recommended_hours, recommended = min(
        _INTERVAL_PRESETS, key=lambda pair: abs(math.log(pair[0]) - math.log(required)))
    if current_hours is None:
        message = f"настроить {recommended}: очередь примерно за {target_hours:g} ч"
    elif current_hours > recommended_hours * 1.15:
        message = f"ускорить до {recommended}: примерно {target_hours:g} ч вместо {eta:g} ч"
    elif current_hours < recommended_hours / 1.5:
        message = f"текущий {current_interval} быстрее необходимого; {recommended} достаточно"
    else:
        message = f"оставить {current_interval}: очередь примерно за {eta:g} ч"
    return {"recommended_interval": recommended, "eta_hours": eta,
            "recommendation": message, **timing}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _age_stats(items: list[dict], now: datetime) -> dict:
    ages = sorted(max(0.0, (now - created).total_seconds() / 3600)
                  for created in (_parse_time(item.get("created_at")) for item in items)
                  if created is not None)
    if not ages:
        return {"median_hours": None, "p90_hours": None, "oldest_hours": None}
    middle = len(ages) // 2
    median = ((ages[middle - 1] + ages[middle]) / 2 if len(ages) % 2 == 0
              else ages[middle])
    p90 = ages[max(0, math.ceil(len(ages) * 0.9) - 1)]
    return {"median_hours": round(median, 1), "p90_hours": round(p90, 1),
            "oldest_hours": round(ages[-1], 1)}


def _item_locations(snapshot: dict) -> dict[str, str]:
    locations = {}
    for queue_id, queue in snapshot.get("queues", {}).items():
        for item in queue.get("items", []):
            locations.setdefault(item["key"], queue_id)
    return locations


def _window_metrics(snapshots: list[dict], current: dict, series_ids: list[int],
                    now: datetime, hours: int) -> dict:
    target = now - timedelta(hours=hours)
    candidates = [(row, _parse_time(row["captured_at"])) for row in snapshots]
    candidates = [(row, stamp) for row, stamp in candidates if stamp is not None]
    before = [(row, stamp) for row, stamp in candidates if stamp <= target]
    baseline_row, baseline_at = (before[-1] if before else candidates[0])
    baseline = baseline_row["payload"]
    coverage = max(0.0, (now - baseline_at).total_seconds() / 3600)
    complete = coverage >= hours * 0.95

    baseline_locations = _item_locations(baseline)
    current_locations = _item_locations(current)
    entered = set(current_locations) - set(baseline_locations)
    exited = set(baseline_locations) - set(current_locations)
    moved = {key for key in set(current_locations) & set(baseline_locations)
             if current_locations[key] != baseline_locations[key]}

    in_window = [row["payload"] for row, stamp in candidates if stamp >= baseline_at]
    sequences: dict[str, list[str]] = {}
    transitions = 0
    for snapshot in in_window:
        for key, queue_id in _item_locations(snapshot).items():
            seq = sequences.setdefault(key, [])
            if not seq or seq[-1] != queue_id:
                if seq:
                    transitions += 1
                seq.append(queue_id)
    churn_items = sum(1 for seq in sequences.values() if len(seq) >= 3)

    queue_deltas = {}
    queue_throughput = {}
    for queue_id, queue in current.get("queues", {}).items():
        old_queue = baseline.get("queues", {}).get(queue_id, {})
        old = old_queue.get("backlog", 0)
        queue_deltas[queue_id] = int(queue.get("backlog", 0)) - int(old)
        old_keys = {item.get("key") for item in old_queue.get("items", [])
                    if isinstance(item, dict) and item.get("key")}
        new_keys = {item.get("key") for item in queue.get("items", [])
                    if isinstance(item, dict) and item.get("key")}
        queue_throughput[queue_id] = (
            len(old_keys - new_keys)
            if (bool(old_queue.get("membership_complete"))
                and bool(queue.get("membership_complete")))
            else None
        )
    current_total = sum(q.get("backlog", 0) for q in current.get("queues", {}).values())
    baseline_total = sum(q.get("backlog", 0) for q in baseline.get("queues", {}).values())
    # Rates describe the observations we actually have. If the closest
    # baseline is ten hours old, dividing its delta by a five-hour display
    # window would overstate throughput by 2x.
    measured_hours = coverage
    complete_membership = all(
        bool(queue.get("membership_complete"))
        and bool(baseline.get("queues", {}).get(queue_id, {}).get(
            "membership_complete"))
        for queue_id, queue in current.get("queues", {}).items()
    )

    def hourly(value: int) -> float | None:
        return round(value / measured_hours, 2) if measured_hours > 0 else None

    return {
        "hours": hours, "coverage_hours": round(min(coverage, hours), 1),
        "complete": complete, "backlog_delta": current_total - baseline_total,
        "backlog_delta_per_hour": hourly(current_total - baseline_total),
        "entered": len(entered), "exited": len(exited), "moved": len(moved),
        "entered_per_hour": hourly(len(entered)) if complete_membership else None,
        "exited_per_hour": hourly(len(exited)) if complete_membership else None,
        "transitions": transitions, "churn_items": churn_items,
        "queue_deltas": queue_deltas,
        "queue_deltas_per_hour": {
            queue_id: hourly(delta) for queue_id, delta in queue_deltas.items()},
        "queue_throughput": queue_throughput,
        "queue_throughput_per_hour": {
            queue_id: hourly(count) if count is not None else None
            for queue_id, count in queue_throughput.items()},
        "runs": db.pipeline_run_metrics(series_ids, now - timedelta(hours=hours)),
    }


def dispatch_gate(task) -> dict | None:
    """Evaluate an optional user-owned, token-free gate for a series task.

    Profiles are deliberately generic: PromptPilot knows neither OneBase nor
    any stage semantics unless the local profile opts into these conditions.
    """
    if not getattr(task, "series_id", None):
        return None
    title = (getattr(task, "series_title", None) or task.prompt.splitlines()[0]).lower()
    for profile_id, profile in _profiles().items():
        for queue_config in profile.get("queues", []):
            marker = queue_config.get("series_contains", "").lower()
            config = queue_config.get("dispatch_gate")
            if not marker or marker not in title or not isinstance(config, dict):
                continue
            # Dispatch only decides whether starting an agent is useful; every
            # mutation is still protected by the project's own fresh gate.
            # Reuse the five-minute snapshot so several due stages cannot each
            # spend hundreds of GitHub requests on the same queue state.
            data = read_cached(profile_id, db.list_series())
            cache = data.get("cache") or {}
            # A stale/partial empty snapshot must never complete a live stage as
            # empty, and stale diagnostics must not defer it. The project-owned
            # preflight remains the authoritative fallback.
            if (cache.get("refresh_blocked") or cache.get("stale")
                    or not cache.get("complete")):
                return None
            queue = next((item for item in data["queues"]
                          if item["id"] == queue_config["id"]), None)
            if config.get("skip_when_empty") and queue and queue["backlog"] == 0:
                return {
                    "action": "complete_empty",
                    "reason": f"очередь «{queue['title']}» пуста",
                    "profile_id": profile_id, "queue_id": queue["id"],
                }
            diagnostics = data.get("diagnostics") or {}
            for field in config.get("defer_when_diagnostics_nonempty", []):
                value = diagnostics.get(field)
                if value:
                    count = len(value) if isinstance(value, list) else 1
                    return {
                        "action": "defer",
                        "defer_for": config.get("defer_for", "10m"),
                        "reason": f"ожидание этапа по diagnostics.{field} ({count})",
                        "profile_id": profile_id, "queue_id": queue_config["id"],
                    }
            for rule in config.get("defer_when_diagnostics_match", []):
                if not isinstance(rule, dict):
                    continue
                field = rule.get("field")
                key = rule.get("key")
                values = rule.get("values")
                source = diagnostics.get(field) if isinstance(field, str) else None
                if not isinstance(source, list) or not isinstance(key, str) or not isinstance(values, list):
                    continue
                allowed = set(values)
                matches = [item for item in source
                           if isinstance(item, dict) and item.get(key) in allowed]
                if matches:
                    return {
                        "action": "defer",
                        "defer_for": config.get("defer_for", "10m"),
                        "reason": (
                            f"ожидание этапа по diagnostics.{field}: "
                            f"{key} совпал ({len(matches)})"),
                        "profile_id": profile_id, "queue_id": queue_config["id"],
                    }
            return None
    return None


def _repeat_guard_series_identity(prompt, fallback_title=None) -> str:
    lines = str(prompt or "").splitlines()
    return (lines[0] if lines else str(fallback_title or "")).lower()


def _matching_queue(task) -> tuple[str, dict, dict] | None:
    """Return the profile and queue owning a recurring task, if configured."""
    if not getattr(task, "series_id", None):
        return None
    title = _repeat_guard_series_identity(
        getattr(task, "prompt", ""), getattr(task, "series_title", None))
    for profile_id, profile in _profiles().items():
        for queue in profile.get("queues", []):
            marker = str(queue.get("series_contains", "")).lower()
            if marker and marker in title:
                return profile_id, profile, queue
    return None


def repeat_guard_series_ids(series: list[dict]) -> list[int]:
    """Return every active series owned by a configured pipeline queue.

    Startup repair is otherwise generic, so the caller passes this explicit
    allowlist before applying project-pipeline-only recurrence policy.
    """
    markers = {
        str(queue.get("series_contains") or "").lower()
        for profile in _profiles().values()
        for queue in profile.get("queues", [])
        if isinstance(queue, dict) and queue.get("series_contains")
    }
    matching = []
    for item in series:
        if item.get("ended") or item.get("ended_at"):
            continue
        value = item.get("id")
        if type(value) is not int or value <= 0:
            continue
        identity = _repeat_guard_series_identity(
            item.get("prompt"), item.get("title"))
        if any(marker in identity for marker in markers):
            matching.append(value)
    return matching


def worker_lane_policy() -> dict | None:
    """Load generic, profile-owned worker lanes.

    A lane is one concurrency slot with an ordered list of preferred queues.
    ``borrow`` lets its idle slot execute work from another lane.  Profiles
    without this opt-in retain the historical global priority/FIFO scheduler.
    """
    profiles = _profiles()
    lanes = []
    seen = set()
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ValueError(f"pipeline profile {profile_id} должен быть JSON-объектом")
        scheduler = profile.get("scheduler")
        if scheduler is None:
            continue
        if not isinstance(scheduler, dict) or set(scheduler) != {"lanes"}:
            raise ValueError("scheduler должен содержать только массив lanes")
        configured = scheduler.get("lanes")
        if not isinstance(configured, list) or not configured:
            raise ValueError("scheduler.lanes должен быть непустым массивом")
        queue_ids = {
            str(queue.get("id")) for queue in profile.get("queues", [])
            if isinstance(queue, dict) and queue.get("id") is not None
        }
        for raw in configured:
            if not isinstance(raw, dict) or set(raw) - {"id", "queues", "borrow"}:
                raise ValueError(
                    "каждая scheduler lane допускает только id, queues и borrow")
            lane_id = raw.get("id")
            queues = raw.get("queues")
            borrow = raw.get("borrow", True)
            if (not isinstance(lane_id, str) or not lane_id.strip()
                    or not re.fullmatch(r"[A-Za-z0-9._-]+", lane_id)):
                raise ValueError("scheduler lane id должен быть непустым safe-id")
            qualified = f"{profile_id}:{lane_id}"
            if qualified in seen:
                raise ValueError(f"повтор scheduler lane id: {qualified}")
            seen.add(qualified)
            if (not isinstance(queues, list) or not queues
                    or any(not isinstance(value, str) or not value.strip()
                           for value in queues)
                    or len(set(queues)) != len(queues)):
                raise ValueError(
                    f"scheduler lane {qualified}.queues должен быть "
                    "непустым массивом уникальных id")
            unknown = sorted(set(queues) - queue_ids)
            if unknown:
                raise ValueError(
                    f"scheduler lane {qualified} ссылается на неизвестные "
                    f"очереди: {', '.join(unknown)}")
            if not isinstance(borrow, bool):
                raise ValueError(f"scheduler lane {qualified}.borrow должен быть boolean")
            lanes.append({
                "id": qualified, "profile_id": profile_id,
                "queues": list(queues), "borrow": borrow,
                "order": len(lanes),
            })
        for queue in profile.get("queues", []):
            replicas = _queue_replica_count(queue)
            if replicas <= 1:
                continue
            queue_id = str(queue.get("id"))
            eligible = sum(
                lane["profile_id"] == profile_id and queue_id in lane["queues"]
                for lane in lanes
            )
            if eligible < replicas:
                raise ValueError(
                    f"очередь {profile_id}/{queue_id} настроена на {replicas} "
                    f"реплики, но доступна только в {eligible} scheduler lanes")
    if lanes:
        fairness_enabled = any(
            (budget := _github_budget_policy(profile)) is not None
            and budget.get("starvation_timeout_seconds", 0) > 0
            for profile in profiles.values()
        )
        if fairness_enabled and not any(lane["borrow"] for lane in lanes):
            raise ValueError(
                "scheduler with GitHub budget fairness requires at least one "
                "borrow:true recovery lane")
    return {"profiles": profiles, "lanes": lanes} if lanes else None


def worker_budget_fairness_policy() -> dict | None:
    """Return the shared opt-in claim policy without touching GitHub.

    Admission fairness is ineffective if the selected task cannot win a local
    worker slot.  Every profile shares the authenticated account's ledger, so
    the shortest positive configured timeout is also used by the scheduler.
    """
    timeouts = []
    scope = None
    for profile in _profiles().values():
        policy = _github_budget_policy(profile)
        if policy is None or policy.get("starvation_timeout_seconds", 0) <= 0:
            continue
        timeouts.append(policy["starvation_timeout_seconds"])
        scope = policy["lease_scope"] if scope is None else scope
        if scope != policy["lease_scope"]:
            raise ValueError(
                "GitHub budget fairness profiles must share one lease scope")
    if not timeouts:
        return None
    return {
        "scope": scope,
        "starvation_timeout_seconds": min(timeouts),
    }


def worker_lane_rank(task, policy: dict, busy_lane_ids=()) -> tuple[tuple, str] | None:
    """Return the best free lane and deterministic rank for one task.

    Preferred work always beats borrowed work in a free slot.  Within a lane,
    the configured queue order is authoritative; ordinary task priority and
    FIFO order break ties.  ``None`` means no configured slot can claim now.
    """
    busy = set(busy_lane_ids or ())
    free = [lane for lane in policy.get("lanes", []) if lane["id"] not in busy]
    if not free:
        return None
    title = (getattr(task, "series_title", None)
             or str(getattr(task, "prompt", "")).splitlines()[0]).lower()
    matched = None
    if getattr(task, "series_id", None):
        for profile_id, profile in policy.get("profiles", {}).items():
            for queue in profile.get("queues", []):
                marker = str(queue.get("series_contains") or "").lower()
                if marker and marker in title:
                    matched = (profile_id, str(queue.get("id")))
                    break
            if matched:
                break
    priority = int(getattr(task, "priority", 5))
    created = str(getattr(task, "created_at", ""))
    task_id = int(getattr(task, "id", 0))
    preferred = []
    if matched:
        profile_id, queue_id = matched
        for lane in free:
            if lane["profile_id"] != profile_id or queue_id not in lane["queues"]:
                continue
            preferred.append((
                (0, lane["queues"].index(queue_id), lane["order"],
                 priority, created, task_id),
                lane["id"],
            ))
    if preferred:
        return min(preferred, key=lambda item: item[0])
    borrowing = [lane for lane in free if lane["borrow"]]
    if not borrowing:
        return None
    lane = min(borrowing, key=lambda item: item["order"])
    return ((1, priority, created, task_id, lane["order"]), lane["id"])


def _diagnostic_match_count(diagnostics: dict, condition: dict) -> int:
    field = condition.get("field")
    if not isinstance(field, str):
        return 0
    value = diagnostics.get(field)
    key, values = condition.get("key"), condition.get("values")
    if key is None and values is None:
        if isinstance(value, (list, dict, str)):
            return len(value)
        return int(bool(value))
    if not isinstance(key, str) or not isinstance(values, list):
        return 0
    allowed = set(values)
    if isinstance(value, dict):
        return int(value.get(key) in allowed)
    if isinstance(value, list):
        return sum(1 for item in value if isinstance(item, dict) and item.get(key) in allowed)
    return 0


def _wake_latch_key(profile_id: str, queue_id: str) -> str:
    return f"pipeline_wake:{profile_id}:{queue_id}"


def _wake_fingerprint(diagnostics: dict, condition: dict) -> str | None:
    """Return a stable fingerprint of the work matched by one wake condition."""
    if _diagnostic_match_count(diagnostics, condition) == 0:
        return None
    value = diagnostics.get(condition.get("field"))
    key, values = condition.get("key"), condition.get("values")
    if isinstance(key, str) and isinstance(values, list):
        allowed = set(values)
        if isinstance(value, list):
            value = [item for item in value
                     if isinstance(item, dict) and item.get(key) in allowed]
        elif isinstance(value, dict):
            value = value if value.get(key) in allowed else None
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _wake_status(profile_id: str, queue: dict, diagnostics: dict) -> dict | None:
    condition = queue.get("wake_when")
    if not isinstance(condition, dict):
        return None
    fingerprint = _wake_fingerprint(diagnostics, condition)
    latch = db.get_setting(_wake_latch_key(profile_id, str(queue.get("id"))))
    return {
        "matches": _diagnostic_match_count(diagnostics, condition),
        "ready": fingerprint is not None,
        "suppressed": fingerprint is not None and latch == fingerprint,
        "fingerprint": fingerprint[:12] if fingerprint else None,
    }


def _wake_cache_guard(profile_id: str, profile: dict,
                      cache: dict) -> dict | None:
    token = cache.get("token")
    profile_hash = _profile_fingerprint(profile)
    if (not isinstance(token, dict)
            or token.get("profile_hash") != profile_hash):
        return None
    try:
        return {
            "cache_key": _cache_key(profile_id, profile_hash),
            "revision_key": _published_revision_key(profile_id, profile_hash),
            "epoch_key": _CACHE_EPOCH_KEY,
            "epoch": int(token["epoch"]),
            "revision": int(token["revision"]),
            "profile_hash": profile_hash,
            "generated_at": float(token["generated_at"]),
            "ttl_seconds": _CACHE_TTL_SECONDS,
        }
    except (KeyError, TypeError, ValueError):
        return None


def _wake_ready_queues(profile_id: str, profile: dict, data: dict,
                       series: list[dict]) -> list[str]:
    """Wake queues only while the accepted full-cache token is still current."""
    cache = data.get("cache") or {}
    if (cache.get("refresh_blocked") or cache.get("stale")
            or cache.get("complete") is not True):
        return []
    cache_guard = _wake_cache_guard(profile_id, profile, cache)
    if cache_guard is None:
        return []
    if db.is_paused():
        return []
    diagnostics = data.get("diagnostics") or {}
    woken = []
    for queue in profile.get("queues", []):
        if db.is_paused():
            break
        condition = queue.get("wake_when")
        marker = str(queue.get("series_contains") or "").lower()
        if not isinstance(condition, dict) or not marker:
            continue
        latch_key = _wake_latch_key(profile_id, str(queue.get("id")))
        fingerprint = _wake_fingerprint(diagnostics, condition)
        targets = [item for item in _series_replicas_for_queue(queue, series)
                   if not item.get("paused")]
        replicated = _queue_replica_count(queue) > 1
        if fingerprint is None:
            if replicated:
                db.wake_series_group_once(
                    [], latch_key, None, cache_guard=cache_guard)
            else:
                db.wake_series_once(
                    None, latch_key, None, cache_guard=cache_guard)
            continue
        if replicated:
            woke = db.wake_series_group_once(
                [int(item["id"]) for item in targets], latch_key, fingerprint,
                cache_guard=cache_guard)
        else:
            target = targets[0] if targets else None
            woke = bool(target and db.wake_series_once(
                int(target["id"]), latch_key, fingerprint,
                cache_guard=cache_guard))
        if woke:
            woken.append(str(queue.get("id")))
    return woken


def _wake_configured_successors(profile: dict, queue: dict,
                                series: list[dict]) -> list[str] | None:
    """Wake explicit successors locally; None preserves legacy scan-based wake."""
    configured = queue.get("wake_after_success")
    if configured is None:
        return None
    if (not isinstance(configured, list)
            or any(not isinstance(item, str) or not item.strip()
                   for item in configured)):
        raise ValueError("wake_after_success должен быть массивом id очередей")
    queue_by_id = {
        str(item.get("id")): item for item in profile.get("queues", [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    woken = []
    for queue_id in dict.fromkeys(item.strip() for item in configured):
        target_queue = queue_by_id.get(queue_id)
        if target_queue is None:
            raise ValueError(
                f"wake_after_success содержит неизвестную очередь: {queue_id}")
        execution = target_queue.get("execution")
        mode = (str(execution.get("mode", "auto")).lower()
                if isinstance(execution, dict) else "")
        if (mode not in {"auto", "tool"}
                or _tool_command(execution, queue_id) is None):
            raise ValueError(
                "wake_after_success может будить только очередь с project "
                f"execution preflight: {queue_id}")
        targets = [item for item in _series_replicas_for_queue(target_queue, series)
                   if not item.get("paused")]
        accepted = False
        for target in targets:
            wake = db.request_pipeline_series_wake(int(target["id"]))
            accepted = accepted or bool(wake.get("accepted"))
        if accepted:
            woken.append(queue_id)
    return woken


def after_task_completed(task, verdict: str | None) -> list[str]:
    """Immediately advance ready pipeline stages after a productive run."""
    if str(verdict or "").upper() != "ГОТОВО":
        return []
    matched = _matching_queue(task)
    if matched is None:
        return []
    # A global pause is also a GitHub-I/O barrier. A task that was already
    # running may finish during a drain, but its completion must not launch the
    # expensive cross-repository analysis. Resume/sampler will refresh later.
    if db.is_paused():
        return []
    profile_id, profile, queue = matched
    series = db.list_series()
    # The completed action changed external protocol state. Mark the old
    # dashboard snapshot stale, then use an explicit local dependency graph to
    # wake likely successors. Their own fresh project preflight remains the
    # authority, so this path spends no GitHub quota and cannot mutate GitHub.
    if queue.get("wake_after_success") is not None:
        _discard_cache(profile_id)
        return _wake_configured_successors(profile, queue, series) or []
    # A productive stage changes the GitHub protocol state. Refresh the project
    # checker once here so the next stage is woken immediately; routine sampler
    # and dispatch reads can then share that result.
    data = analyze(profile_id, series, use_cache=False, refresh_diagnostics=True)
    if db.is_paused():
        return []
    # A denied/busy budgeted refresh returns last-good cached data. Never use
    # that pre-completion state to wake a downstream stage.
    if (data.get("cache") or {}).get("refresh_blocked"):
        return []
    current_profile = _profiles().get(profile_id)
    if current_profile is None:
        return []
    return _wake_ready_queues(profile_id, current_profile, data, series)


def _expanded_command(values, stage: str) -> list[str] | None:
    command = values
    if not isinstance(command, list) or not command or not all(
            isinstance(value, str) and value for value in command):
        return None
    expanded = [
        value.replace("{python}", sys.executable).replace("{stage}", stage)
        for value in command
    ]
    # In a PyInstaller bundle sys.executable is pp.exe, not a Python
    # interpreter. Keep existing portable profile commands working by routing
    # the bundled project adapter through pp's hidden pipelinectl command.
    if (getattr(sys, "frozen", False) and len(expanded) >= 3 and
            expanded[:3] == [sys.executable, "-m", "promptpilot.project_pipeline"]):
        return [sys.executable, "pipelinectl", *expanded[3:]]
    return expanded


def _tool_command(execution: dict, stage: str) -> list[str] | None:
    return _expanded_command(execution.get("command"), stage)


def _tool_available(execution: dict, command: list[str], working_dir: str | None,
                    stage: str) -> tuple[bool, str]:
    root = Path(working_dir or os.getcwd())
    for value in execution.get("required_paths", []):
        if not isinstance(value, str) or not value:
            return False, "required_paths должен содержать непустые строки"
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            return False, f"не найден {value}"

    executable = command[0]
    candidate = Path(executable)
    if candidate.is_absolute() or candidate.parent != Path("."):
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            return False, f"не найдена команда {executable}"
    elif shutil.which(executable) is None and executable != sys.executable:
        return False, f"команда {executable} отсутствует в PATH"
    probe = execution.get("probe_command")
    if probe is not None:
        probe_command = _expanded_command(probe, stage)
        if probe_command is None:
            return False, "probe_command должен быть непустым массивом строк"
        try:
            _ensure_github_scan_lease(renew=True)
            result = subprocess.run(
                probe_command, cwd=str(root), capture_output=True, text=True,
                timeout=max(1, min(int(execution.get("probe_timeout_seconds", 30)), 120)),
                encoding="utf-8", errors="replace",
            )
            _ensure_github_scan_lease()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"probe не выполнен: {exc}"
        if result.returncode:
            detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
            return False, f"probe завершился с ошибкой: {detail[-1] if detail else 'unknown error'}"
    return True, "инструмент доступен"


def _tool_preflight(execution: dict, command: list[str], working_dir: str | None,
                    *, env_extra: dict[str, str] | None = None) -> dict:
    root = Path(working_dir or os.getcwd())
    environment = os.environ.copy()
    # Replica identity is per claimed task, never process-global operator
    # configuration. A stale shell variable must not opt a legacy queue into
    # replicated election without the scheduler's reservation contract.
    environment.pop("PP_TASK_ID", None)
    environment.pop("PP_TASK_STARTED_AT", None)
    environment.pop("PP_PIPELINE_REPLICAS", None)
    environment.pop("PP_PROVIDER_OWNERSHIP_KIND", None)
    environment.pop("PP_PIPELINE_TARGET_TOKEN", None)
    if env_extra:
        environment.update(env_extra)
    try:
        _ensure_github_scan_lease(renew=True)
        result = subprocess.run(
            command, cwd=str(root), capture_output=True, text=True,
            timeout=max(1, min(int(execution.get("timeout_seconds", 180)), 900)),
            encoding="utf-8", errors="replace", env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"pipeline preflight не выполнен: {exc}") from exc
    _ensure_github_scan_lease()
    if len(result.stdout) > 131072:
        raise RuntimeError("pipeline preflight вернул слишком большой ответ")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or result.stdout or "пустой ответ").strip().splitlines()
        raise RuntimeError(
            f"pipeline preflight вернул неверный JSON: {detail[-1] if detail else exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
        raise RuntimeError("pipeline preflight должен вернуть JSON-объект с action")
    if result.returncode and payload.get("action") != "error":
        raise RuntimeError(
            (result.stderr or payload.get("reason") or
             f"pipeline preflight завершился с кодом {result.returncode}").strip()
        )
    return payload


def _bounded_defer_text(value, limit: int = 240) -> str:
    """Keep untrusted project/GitHub diagnostics compact and single-line."""
    return " ".join(str(value or "").split())[:limit]


def _budget_defer_context(admission: dict, phase: str,
                          preflight: dict | None) -> dict:
    """Return diagnostic-only preflight facts, never its authority/lease."""
    context = {
        "phase": phase,
        "budget_route": _bounded_defer_text(
            admission.get("budget_route"), 48),
    }
    if not isinstance(preflight, dict):
        return context
    context["preflight_action"] = _bounded_defer_text(
        preflight.get("action"), 48)
    context["preflight_reason"] = _bounded_defer_text(
        preflight.get("reason") or preflight.get("error"))
    context["handoff_present"] = "handoff" in preflight
    target = preflight.get("target")
    if isinstance(target, dict):
        safe_target = {}
        number = target.get("number")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            safe_target["number"] = number
        head = str(target.get("head") or "")
        if re.fullmatch(r"[0-9a-fA-F]{40}", head):
            safe_target["head"] = head.lower()
        stage = _bounded_defer_text(target.get("stage"), 48)
        if stage:
            safe_target["stage"] = stage
        if safe_target:
            context["target"] = safe_target
    return context


def _format_budget_defer_context(context: dict) -> str:
    parts = [str(context.get("phase") or "admission")]
    if context.get("budget_route"):
        parts.append(f"route={context['budget_route']}")
    if context.get("preflight_action"):
        parts.append(f"action={context['preflight_action']}")
    target = context.get("target") or {}
    if target.get("number"):
        parts.append(f"target=#{target['number']}")
    if target.get("stage"):
        parts.append(f"stage={target['stage']}")
    if target.get("head"):
        parts.append(f"head={target['head']}")
    if "handoff_present" in context:
        parts.append(
            "handoff=" + ("present" if context["handoff_present"] else "absent"))
    if context.get("preflight_reason"):
        parts.append(f"reason={context['preflight_reason']}")
    return ", ".join(parts)


def _budget_defer_route(admission: dict, profile_id: str, profile: dict,
                        queue: dict,
                        *, mode: str = "tool", phase: str | None = None,
                        preflight: dict | None = None) -> dict:
    _record_refresh_blocked(profile_id, profile, admission)
    reason = str(admission.get("reason") or "GitHub scan отложен")
    context = None
    if phase:
        context = _budget_defer_context(admission, phase, preflight)
        reason = f"{reason}; preflight: {_format_budget_defer_context(context)}"
    reservation_revision = admission.get("_budget_reservation_revision")
    reservation_handoff = (
        admission.get("state") in {
            "budget_in_flight", "ledger_changed", "priority_waiter",
            "fairness_waiter",
            "scan_in_progress"}
        and type(reservation_revision) is int
        and reservation_revision >= 0
        and isinstance(admission.get("lease_scope"), str)
        and bool(admission.get("lease_scope")))
    defer_policy = (
        "reservation_release" if (
            reservation_handoff
            and admission.get("state") == "budget_in_flight")
        else "retry" if admission.get("state") in {
            "ledger_changed", "priority_waiter", "fairness_waiter",
            "scan_in_progress"}
        else "hard_not_before")
    result = {
        "action": "defer", "mode": mode,
        "reason": reason,
        "defer_until": admission.get("defer_until"),
        "defer_policy": defer_policy,
        "profile_id": profile_id, "queue_id": queue.get("id"),
        "github_budget": _public_budget_decision(admission),
        "github_rate_limit": admission.get("github_rate_limit"),
    }
    if reservation_handoff:
        result["budget_wait_scope"] = admission.get("lease_scope")
        result["budget_wait_revision"] = reservation_revision
    if context is not None:
        result["defer_context"] = context
    return result


def execution_route(task, fallback_prompt: str, working_dir: str | None = None,
                    *, retain_budget: bool = False) -> dict:
    """Choose the project tool or the original skill prompt without invoking an LLM.

    The profile owns this opt-in. PromptPilot only checks local availability and
    renders a compact executor-neutral prompt; all repository semantics stay in
    the project tool and its fallback skill.
    """
    matched = _matching_queue(task)
    if matched is None:
        # A profile/series marker may have been renamed while this exact
        # attempt was waiting. It is now a legacy prompt path with no final
        # budget reservation to settle the old shared-scope baton.
        _clear_budget_waiter_after_admission(task)
        return {"action": "prompt", "mode": "skill", "prompt": fallback_prompt}
    profile_id, profile, queue = matched
    try:
        replica_count = _queue_replica_count(queue)
    except ValueError as exc:
        return {
            "action": "block", "mode": "tool", "reason": str(exc),
            "profile_id": profile_id, "queue_id": queue.get("id"),
        }
    replicated = replica_count > 1
    profile_series = db.list_series() if replicated else []
    if replicated:
        replica_status = _queue_replica_status(queue, profile_series)
        stage_name = str((queue.get("execution") or {}).get("stage")
                         or queue.get("id") or "").lower()
        issues = list(replica_status["issues"])
        if stage_name != "review":
            issues.append("replicas > 1 сейчас поддерживаются только для REVIEW")
        if getattr(task, "machine", None):
            issues.append("реплицированная REVIEW-задача должна выполняться локально")
        if getattr(task, "worktree", False):
            issues.append(
                "реплицированная REVIEW-задача требует постоянный отдельный working_dir, "
                "а не динамический worktree")
        if getattr(task, "detached", False):
            issues.append(
                "реплицированная REVIEW-задача не может запускаться detached")
        if getattr(task, "herdr_target", None):
            issues.append(
                "реплицированная REVIEW-задача не может использовать пользовательский herdr_target")
        current_replica = next((
            item for item in replica_status["matching"]
            if int(item.get("id")) == int(task.series_id)
        ), None)
        if current_replica is None:
            issues.append("текущая серия не входит в настроенный набор реплик")
        elif os.path.normcase(os.path.realpath(str(working_dir or ""))) != \
                os.path.normcase(os.path.realpath(
                    str(current_replica.get("working_dir") or ""))):
            issues.append("working_dir текущей задачи не совпадает с её replica series")
        if issues:
            return {
                "action": "block", "mode": "tool",
                "reason": "небезопасная конфигурация pipeline replicas: "
                          + "; ".join(dict.fromkeys(issues)),
                "profile_id": profile_id, "queue_id": queue.get("id"),
                "replica_status": {
                    key: value for key, value in replica_status.items()
                    if key != "matching"
                },
            }
    execution = queue.get("execution")
    if not isinstance(execution, dict):
        if replicated:
            return {
                "action": "block", "mode": "tool",
                "reason": "replicas > 1 требуют project execution preflight",
                "profile_id": profile_id, "queue_id": queue.get("id"),
            }
        with _github_scan_admission(
                profile, f"pipeline task {profile_id}/{queue.get('id')}",
                profile_id=profile_id, budget_route="skill",
                task=task) as admission:
            if not admission.get("allowed"):
                return _budget_defer_route(
                    admission, profile_id, profile, queue, mode="skill")
            admission = _reserve_execution_admission(
                task, profile_id, profile, queue, admission, "skill",
                retain_budget=retain_budget)
            if not admission.get("allowed"):
                return _budget_defer_route(
                    admission, profile_id, profile, queue, mode="skill")
        return {
            "action": "prompt", "mode": "skill", "prompt": fallback_prompt,
            "profile_id": profile_id, "queue_id": queue.get("id"),
        }

    mode = str(execution.get("mode", "auto")).lower()
    if mode not in {"auto", "tool", "skill"}:
        return {
            "action": "block", "mode": mode,
            "reason": f"неизвестный pipeline execution mode: {mode}",
            "profile_id": profile_id, "queue_id": queue.get("id"),
        }
    if mode == "skill":
        if replicated:
            return {
                "action": "block", "mode": "skill",
                "reason": "replicas > 1 несовместимы с execution.mode=skill",
                "profile_id": profile_id, "queue_id": queue.get("id"),
            }
        with _github_scan_admission(
                profile, f"pipeline task {profile_id}/{queue.get('id')}",
                profile_id=profile_id, budget_route="skill",
                task=task) as admission:
            if not admission.get("allowed"):
                return _budget_defer_route(
                    admission, profile_id, profile, queue, mode="skill")
            admission = _reserve_execution_admission(
                task, profile_id, profile, queue, admission, "skill",
                retain_budget=retain_budget)
            if not admission.get("allowed"):
                return _budget_defer_route(
                    admission, profile_id, profile, queue, mode="skill")
        return {
            "action": "prompt", "mode": "skill", "prompt": fallback_prompt,
            "profile_id": profile_id, "queue_id": queue.get("id"),
        }

    stage = str(execution.get("stage") or queue.get("id") or "").lower()
    command = _tool_command(execution, stage)
    with _github_scan_admission(
            profile, f"pipeline preflight {profile_id}/{stage}",
            profile_id=profile_id, budget_route="tool_preflight",
            task=task) as admission:
        if not admission.get("allowed"):
            return _budget_defer_route(
                admission, profile_id, profile, queue)
        try:
            if command is None:
                available, reason = (
                    False, "execution.command должен быть непустым массивом строк")
            else:
                available, reason = _tool_available(
                    execution, command, working_dir, stage)
        except _GitHubScanLeaseFailure as exc:
            return _budget_defer_route(
                _replace_admission_with_lease_failure(
                    admission, profile, exc),
                profile_id,
                profile, queue)
        if not available:
            if mode == "auto" and not replicated:
                admission = _reserve_execution_admission(
                    task, profile_id, profile, queue, admission, "skill",
                    retain_budget=retain_budget)
                if not admission.get("allowed"):
                    return _budget_defer_route(
                        admission, profile_id, profile, queue, mode="skill")
                return {
                    "action": "prompt", "mode": "skill", "prompt": fallback_prompt,
                    "fallback_reason": reason, "profile_id": profile_id,
                    "queue_id": queue.get("id"),
                }
            return {
                "action": "block", "mode": "tool", "reason": reason,
                "profile_id": profile_id, "queue_id": queue.get("id"),
            }
        try:
            if replicated:
                # Both worktrees must reserve in the scheduler's actual DB.
                # Do not let a project-local .env silently select one DB per
                # checkout and destroy cross-replica atomicity.
                scheduler_data_dir = str(Path(db.DB_PATH).resolve().parent)
                configured_key = os.environ.get("PP_PIPELINE_LEASE_KEY_FILE")
                scheduler_lease_key = str(
                    (Path(configured_key) if configured_key else
                     Path(scheduler_data_dir) / "pipeline-lease.key").resolve())
                task_started_at = getattr(task, "started_at", None)
                if not isinstance(task_started_at, datetime):
                    raise RuntimeError(
                        "replicated pipeline task has no claimed attempt timestamp")
                if task_started_at.tzinfo is None:
                    task_started_at = task_started_at.astimezone()
                scheduler_task_started_at = task_started_at.astimezone(
                    timezone.utc).isoformat()
                provider_cfg = load_providers().get(
                    getattr(task, "provider", None) or DEFAULT_CLI, {})
                provider_ownership_kind = (
                    "herdr" if provider_cfg.get("executor") == "herdr"
                    else "headless")
                preflight = _tool_preflight(
                    execution, command, working_dir,
                    env_extra={
                        "PP_TASK_ID": str(task.id),
                        "PP_TASK_STARTED_AT": scheduler_task_started_at,
                        "PP_PIPELINE_REPLICAS": str(replica_count),
                        "PP_PROVIDER_OWNERSHIP_KIND": provider_ownership_kind,
                        "PP_DATA_DIR": scheduler_data_dir,
                        "PP_PIPELINE_LEASE_KEY_FILE": scheduler_lease_key,
                    },
                )
            else:
                preflight = _tool_preflight(execution, command, working_dir)
        except _GitHubScanLeaseFailure as exc:
            return _budget_defer_route(
                _replace_admission_with_lease_failure(
                    admission, profile, exc),
                profile_id,
                profile, queue)
        except RuntimeError as exc:
            reason = str(exc)
            if mode == "auto" and not replicated:
                admission = _reserve_execution_admission(
                    task, profile_id, profile, queue, admission, "skill",
                    retain_budget=retain_budget)
                if not admission.get("allowed"):
                    return _budget_defer_route(
                        admission, profile_id, profile, queue, mode="skill")
                return {
                    "action": "prompt", "mode": "skill", "prompt": fallback_prompt,
                    "fallback_reason": reason, "profile_id": profile_id,
                    "queue_id": queue.get("id"),
                }
            return {
                "action": "block", "mode": "tool", "reason": reason,
                "profile_id": profile_id, "queue_id": queue.get("id"),
            }

        preflight_action = preflight["action"].lower()
        target_reservation = None
        validated_target_stage = None
        fallback_lease = None
        if replicated and preflight_action in {"audit", "fallback"}:
            try:
                from . import project_pipeline

                if preflight_action == "audit":
                    lease = project_pipeline.decode_signed_lease(
                        str(preflight.get("lease") or ""))
                else:
                    handoff = preflight.get("handoff") or {}
                    lease = project_pipeline.decode_signed_lease(
                        str(handoff.get("lease") or ""))
                reservation = lease.get("target_reservation")
                repository, target_stage, number, head, owner_task, _token = \
                    project_pipeline._reservation_identity(reservation)
                target = (lease.get("target") if preflight_action == "fallback"
                          else {
                              "stage": lease.get("target_stage") or lease.get("stage"),
                              "number": lease.get("number"), "head": lease.get("head"),
                          })
                preflight_target = preflight.get("target") or {}
                if (owner_task != int(task.id)
                        or reservation.get("task_started_at") != scheduler_task_started_at
                        or reservation.get("ownership_kind") != provider_ownership_kind
                        or lease.get("pipeline_replicas") != replica_count
                        or repository != str(profile.get("repository") or "").lower()
                        or repository != str(lease.get("repository") or "").lower()
                        or (target_stage, number, head) != (
                            str(target.get("stage") or "").lower(),
                            target.get("number"), str(target.get("head") or "").lower())
                        or (target_stage, number, head) != (
                            str(preflight_target.get("stage") or "").lower(),
                            preflight_target.get("number"),
                            str(preflight_target.get("head") or "").lower())):
                    raise project_pipeline.PipelineError(
                        "pipeline target reservation contradicts task, repository, or target")
                target_reservation = reservation
                validated_target_stage = target_stage
            except (project_pipeline.PipelineError, TypeError, ValueError) as exc:
                return {
                    "action": "block", "mode": "tool",
                    "reason": f"replicated REVIEW preflight has no valid reservation: {exc}",
                    "profile_id": profile_id, "queue_id": queue.get("id"),
                    "preflight": preflight,
                }
        if (preflight_action == "fallback" and "handoff" in preflight
                and target_reservation is not None):
            from .fallback_handoff import validate
            from .project_pipeline import PipelineError

            try:
                fallback_lease = validate(preflight, stage)
                if command[-2:] != ["next", stage]:
                    raise PipelineError(
                        "fallback handoff command must end with next and the exact stage")
            except (PipelineError, TypeError, ValueError) as exc:
                try:
                    task_id = getattr(task, "id", None)
                    started_at = getattr(task, "started_at", None)
                    if type(task_id) is int and task_id > 0 and started_at is not None:
                        db.restore_running_attempt_priority(task_id, started_at)
                except (sqlite3.Error, TypeError, ValueError) as restore_exc:
                    denied = _replace_admission_with_lease_failure(
                        admission, profile, _GitHubScanLeaseUnavailable(
                            "pipeline admission priority restoration is unavailable "
                            f"after invalid fallback handoff: {restore_exc}"))
                    return _budget_defer_route(
                        denied, profile_id, profile, queue, mode="skill",
                        phase="post_preflight", preflight=preflight)
                return {
                    "action": "block", "mode": "tool", "reason": str(exc),
                    "profile_id": profile_id, "queue_id": queue.get("id"),
                    "preflight": preflight,
                }
            validated_target_stage = str(
                (fallback_lease.get("target") or {}).get("stage") or "").lower()
        provider_route = None
        if preflight_action == "fallback" and mode == "auto":
            provider_route = ("fallback_targeted" if "handoff" in preflight
                              else "skill")
        elif preflight_action in {"audit", "merge", "cleanup"}:
            provider_route = "tool"
        elif preflight_action not in {"empty", "wait", "error"} and mode == "auto":
            provider_route = "skill"
        routed_priority = _post_preflight_admission_priority(
            profile, profile_series, stage, provider_route,
            validated_target_stage, admission.get("priority_one_headroom"))
        # Only a signed target that is also fenced to this exact replica task
        # may borrow the priority-1 integration headroom.
        if routed_priority is not None and target_reservation is None:
            routed_priority = None
        task_id = getattr(task, "id", None)
        task_started_at = getattr(task, "started_at", None)
        admission_priority = getattr(task, "priority", None)
        if type(task_id) is int and task_id > 0 and task_started_at is not None:
            try:
                if routed_priority is None:
                    admission_priority = db.restore_running_attempt_priority(
                        task_id, task_started_at)
                else:
                    admission_priority = db.promote_running_attempt_priority(
                        task_id, task_started_at, routed_priority)
            except (sqlite3.Error, TypeError, ValueError) as exc:
                denied = _replace_admission_with_lease_failure(
                    admission, profile, _GitHubScanLeaseUnavailable(
                        f"pipeline admission priority transition is unavailable: {exc}"))
                return _budget_defer_route(
                    denied, profile_id, profile, queue,
                    mode=("tool" if provider_route == "tool" else "skill"),
                    phase="post_preflight", preflight=preflight)
            if admission_priority is None:
                return {
                    "action": "block", "mode": "tool",
                    "reason": (
                        "running task attempt changed before pipeline admission "
                        "priority transition"),
                    "profile_id": profile_id, "queue_id": queue.get("id"),
                    "preflight": preflight,
                }
        elif routed_priority is not None:
            return {
                "action": "block", "mode": "tool",
                "reason": (
                    "integration REVIEW admission has no exact running task attempt"),
                "profile_id": profile_id, "queue_id": queue.get("id"),
                "preflight": preflight,
            }
        if provider_route is not None:
            admission = _reserve_execution_admission(
                task, profile_id, profile, queue, admission, provider_route,
                retain_budget=retain_budget,
                admission_priority=admission_priority)
            if not admission.get("allowed"):
                return _budget_defer_route(
                    admission, profile_id, profile, queue,
                    mode=("tool" if provider_route == "tool" else "skill"),
                    phase="post_preflight", preflight=preflight)

    preflight_action = preflight["action"].lower()
    preflight_reason = str(
        preflight.get("reason") or preflight.get("error") or preflight_action
    )
    if preflight_action in {"empty", "wait"}:
        preflight_verdict = str(preflight.get("verdict") or "ПУСТО").strip() or "ПУСТО"
        # Only a validated targeted fallback route can authorize the silent,
        # immediate stale outcome.  An ordinary project tool returning
        # empty/wait must never mint that capability through an arbitrary JSON
        # field.
        if preflight_verdict.upper() == "УСТАРЕЛО":
            preflight_verdict = "НЕ СМОГ"
        return {
            "action": "complete_empty", "mode": "tool", "reason": preflight_reason,
            "verdict": preflight_verdict,
            "profile_id": profile_id, "queue_id": queue.get("id"),
            "preflight": preflight,
        }
    if preflight_action == "error":
        return {
            "action": "defer", "mode": "tool", "reason": preflight_reason,
            "defer_for": str(execution.get("error_defer_for") or "30m"),
            "profile_id": profile_id, "queue_id": queue.get("id"),
            "preflight": preflight,
        }
    if preflight_action == "fallback":
        if "handoff" in preflight:
            if fallback_lease is None:
                from .fallback_handoff import validate
                from .project_pipeline import PipelineError

                try:
                    fallback_lease = validate(preflight, stage)
                    if command[-2:] != ["next", stage]:
                        raise PipelineError(
                            "fallback handoff command must end with next and the exact stage")
                except (PipelineError, TypeError, ValueError) as exc:
                    return {
                        "action": "block", "mode": "tool", "reason": str(exc),
                        "profile_id": profile_id, "queue_id": queue.get("id"),
                        "preflight": preflight,
                    }
            if mode == "auto":
                gate_command = [*command[:-2], "gate-fallback", stage,
                                "--lease", preflight["handoff"]["lease"]]
                envelope = {"protocol": "promptpilot-fallback-target-v1",
                            "next_already_run": True, "command": command,
                            "gate_command": gate_command, "preflight": preflight}
                pre_review_guard = ""
                if fallback_lease["target"]["stage"] == "pre-review-validation":
                    pre_review_guard = (
                        "Это специальный content-lane этап pre-review-validation, а не "
                        "integration review. Подписанный envelope только фиксирует все "
                        "восемь полей pre_review_sync и HEAD; он не доказывает provenance "
                        "и не разрешает быстрый результат. До gate_command, оставаясь полностью "
                        "read-only, сначала выполни предусмотренную скиллом полную стабильную "
                        "GraphQL-проверку происхождения sync-коммита. Только после её успеха "
                        "проверь весь diff PR как обычное содержательное ревью и выполни все "
                        "уместные полные тесты. Этот target нельзя завершать через быстрый "
                        "action=audit или `complete review`. Лишь когда provenance и аудит "
                        "полностью завершены и ты готов к первой мутации, переходи к описанному "
                        "ниже одноразовому gate_command непосредственно перед этой мутацией. "
                        "При любой ошибке provenance остановись без gate и без мутаций.\n\n"
                    )
                prompt = (
                    "PromptPilot уже выполнил election next. Не запускай next повторно "
                    "и не выбирай другую цель. Полностью прочитай канонический скилл "
                    "и его legacy-протокол. Используй только exact target из envelope. "
                    f"{pre_review_guard}"
                    "Непосредственно перед первой мутацией выполни gate_command ровно один "
                    "раз: он заново "
                    "запускает полный pipelinehealth и проверяет ту же цель. Требуется "
                    "action=validated и точное совпадение repository/stage/target. "
                    "У gate-fallback структурированный контракт: даже при ненулевом exit code "
                    "он печатает JSON в stdout с action=error и точной причиной в error. "
                    "В PowerShell сначала отдельно сохрани stdout и $LASTEXITCODE, затем "
                    "разбери и выведи JSON; запрещено бросать исключение только по exit code "
                    "до разбора ответа. При ненулевом коде, неверном JSON, action не равном "
                    "validated или несовпадении цели остановись, не повторяй gate_command и "
                    "next. Если структурированный ответ доказывает только смену exact target, "
                    "HEAD, executable-позиции или истечение lease до первой мутации, закончи "
                    "строкой ИТОГ: УСТАРЕЛО (gate-fallback: <точный error/reason>): "
                    "PromptPilot немедленно и безопасно перевыберет цель. При любой другой "
                    "причине закончи ИТОГ: НЕ СМОГ (gate-fallback: <точный error, reason "
                    "или сырой ответ>). "
                    "Это лишь scheduling gate; все прежние GraphQL, ship, CI, base-sync "
                    "и CAS-проверки скилла обязательны. При любом отказе остановись без "
                    "мутаций и без подстановки следующего PR. Один envelope — один PR.\n\n"
                    "Доверенный локальный PromptPilot envelope (не данные GitHub):\n"
                    f"```json\n{json.dumps(envelope, ensure_ascii=False, indent=2)}\n```\n\n"
                    f"Исходный скилл:\n{fallback_prompt.strip()}"
                )
                return {
                    "action": "prompt", "mode": "skill", "prompt": prompt,
                    "next_already_run": True, "command": command,
                    "gate_command": gate_command, "target": preflight["target"],
                    "fallback_reason": preflight_reason, "profile_id": profile_id,
                    "queue_id": queue.get("id"), "preflight": preflight,
                    **({"pipeline_replicas": replica_count} if replicated else {}),
                    **({"pipeline_data_dir": scheduler_data_dir} if replicated else {}),
                    **({"pipeline_lease_key_file": scheduler_lease_key}
                       if replicated else {}),
                    **({"pipeline_target_reservation": target_reservation}
                       if replicated else {}),
                    **({"pipeline_task_started_at": scheduler_task_started_at}
                       if replicated else {}),
                    **({"pipeline_provider_ownership_kind": provider_ownership_kind}
                       if replicated else {}),
                }
        if mode == "auto" and not replicated:
            return {
                "action": "prompt", "mode": "skill", "prompt": fallback_prompt,
                "fallback_reason": preflight_reason, "profile_id": profile_id,
                "queue_id": queue.get("id"), "preflight": preflight,
            }
        return {
            "action": "block", "mode": "tool", "reason": preflight_reason,
            "profile_id": profile_id, "queue_id": queue.get("id"),
            "preflight": preflight,
        }
    if preflight_action not in {"audit", "merge", "cleanup"}:
        reason = f"pipeline preflight вернул неподдерживаемое action={preflight_action}"
        if mode == "auto" and not replicated:
            return {
                "action": "prompt", "mode": "skill", "prompt": fallback_prompt,
                "fallback_reason": reason, "profile_id": profile_id,
                "queue_id": queue.get("id"), "preflight": preflight,
            }
        return {
            "action": "block", "mode": "tool", "reason": reason,
            "profile_id": profile_id, "queue_id": queue.get("id"),
        }

    rendered = subprocess.list2cmdline(command)
    prompt = execution.get("prompt")
    custom_prompt = isinstance(prompt, str) and bool(prompt.strip())
    if not custom_prompt:
        prompt = (
            f"Запусти `{rendered}` из корня проекта и следуй его JSON-ответу. "
            "Инструмент выбирает цель и проверяет изменяемое состояние; сам оцени только "
            "код, дифф и результаты тестов. Если он вернул action=fallback, выполни "
            "исходный скилл ниже. Не заменяй отказ инструмента ручными GitHub-мутациями."
        )
    if not custom_prompt:
        prompt = (
            f"PromptPilot уже выполнил `{rendered}` и зафиксировал цель и lease. "
            "Не запускай next повторно. Проверь только код, diff и тесты, затем вызови complete "
            "с lease из JSON ниже. Не заменяй отказ complete ручными GitHub-мутациями."
        )
    prompt = (
        f"{prompt.strip()}\n\n"
        "Результат preflight (точные данные):\n"
        f"```json\n{json.dumps(preflight, ensure_ascii=False, indent=2)}\n```\n\n"
        "Исходный скилл для fallback:\n"
        f"{fallback_prompt.strip()}"
    )
    return {
        "action": "prompt", "mode": "tool", "prompt": prompt,
        "command": command, "preflight": preflight,
        "profile_id": profile_id, "queue_id": queue.get("id"),
        **({"pipeline_replicas": replica_count} if replicated else {}),
        **({"pipeline_data_dir": scheduler_data_dir} if replicated else {}),
        **({"pipeline_lease_key_file": scheduler_lease_key}
           if replicated else {}),
        **({"pipeline_target_reservation": target_reservation}
           if replicated else {}),
        **({"pipeline_task_started_at": scheduler_task_started_at}
           if replicated else {}),
        **({"pipeline_provider_ownership_kind": provider_ownership_kind}
           if replicated else {}),
    }


def _execution_status(queue: dict, working_dir: str | None) -> dict:
    execution = queue.get("execution")
    if not isinstance(execution, dict):
        return {"configured": "skill", "effective": "skill", "available": None}
    mode = str(execution.get("mode", "auto")).lower()
    if mode == "skill":
        return {"configured": mode, "effective": "skill", "available": None}
    if db.is_paused():
        return {
            "configured": mode, "effective": mode, "available": None,
            "reason": "проверка маршрута пропущена во время общей паузы",
        }
    stage = str(execution.get("stage") or queue.get("id") or "").lower()
    command = _tool_command(execution, stage)
    available, reason = ((False, "execution.command не настроен") if command is None
                         else _tool_available(execution, command, working_dir, stage))
    effective = "tool" if available else ("skill" if mode == "auto" else "blocked")
    return {"configured": mode, "effective": effective, "available": available,
            "reason": reason, "command": command}


def _run_profile_health_check(profile: dict) -> dict | None:
    """Run an optional project-owned, token-free invariant checker."""
    config = profile.get("health_check")
    if not config:
        return None
    command = config.get("command")
    if not isinstance(command, list) or not command or not all(
            isinstance(value, str) and value for value in command):
        return {
            "state": "red", "summary": "health_check настроен неверно",
            "checker_failed": True,
            "findings": [{"severity": "red", "code": "invalid_health_check",
                          "message": "command должен быть непустым массивом строк"}],
        }
    env = os.environ.copy()
    if env.get("PP_GH_EXE"):
        env.setdefault("GH_EXE", env["PP_GH_EXE"])
        gh_dir = str(Path(env["PP_GH_EXE"]).parent)
        path_parts = env.get("PATH", "").split(os.pathsep)
        if gh_dir and gh_dir not in path_parts:
            env["PATH"] = gh_dir + os.pathsep + env.get("PATH", "")
    try:
        _ensure_github_scan_lease(renew=True)
        run = subprocess.run(
            command, cwd=config.get("working_dir") or None, env=env,
            capture_output=True, text=True,
            timeout=max(1, min(int(config.get("timeout_seconds", 180)), 900)),
            encoding="utf-8", errors="replace",
        )
        _ensure_github_scan_lease()
        payload = json.loads(run.stdout)
        if not isinstance(payload, dict) or payload.get("state") not in (
                "green", "yellow", "red"):
            raise ValueError("ожидался JSON со state green/yellow/red")
        payload.setdefault("findings", [])
        payload["exit_code"] = run.returncode
        return payload
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        return {
            "state": "red", "summary": f"health-check не выполнен: {exc}",
            "checker_failed": True,
            "findings": [{"severity": "red", "code": "health_check_failed",
                          "message": str(exc)}],
        }


def _pipeline_runtime(matching_series: list[dict], now: datetime) -> dict:
    runtime = db.worker_runtime_status(
        now=now, stale_after_seconds=max(30, POLL_INTERVAL * 4))
    live = [series for series in matching_series
            if not series.get("ended") and not series.get("paused")]
    runtime["required"] = bool(live)
    stalled = []
    for series in live:
        if series.get("next_status") != "running":
            continue
        started = _parse_time(series.get("next_started_at"))
        if started is None:
            continue
        age_seconds = max(0, round((now - started).total_seconds()))
        timeout = series.get("task_timeout")
        timeout = TASK_TIMEOUT if timeout is None else int(timeout)
        if timeout > 0 and age_seconds > timeout + max(30, POLL_INTERVAL * 4):
            stalled.append({
                "series_id": series["id"], "title": series["title"],
                "task_id": series.get("next_task_id"), "age_seconds": age_seconds,
                "timeout_seconds": timeout,
            })
    runtime["stalled"] = stalled
    return runtime


def _health(backlog: int, windows: dict, broken_series: int, paused_series: int = 0,
            diagnostics: dict | None = None, runtime: dict | None = None,
            invalid_replica_queues: list[dict] | None = None) -> dict:
    if runtime and runtime.get("required") and runtime.get("state") != "online":
        age = runtime.get("age_seconds")
        detail = f"; последний heartbeat {age} сек назад" if age is not None else ""
        return {"state": "red", "label": "worker не работает",
                "reason": f"нет свежего heartbeat worker{detail}"}
    if runtime and runtime.get("stalled"):
        tasks = ", ".join(f"#{item['task_id']}" for item in runtime["stalled"])
        return {"state": "red", "label": "зависший запуск",
                "reason": f"превышен task timeout: {tasks}"}
    if invalid_replica_queues:
        details = []
        for queue in invalid_replica_queues:
            issues = (queue.get("replica_status") or {}).get("issues") or []
            suffix = f": {'; '.join(str(issue) for issue in issues)}" if issues else ""
            details.append(f"{queue.get('id') or '?'}{suffix}")
        return {
            "state": "red", "label": "невалидная конфигурация реплик",
            "reason": "очереди без безопасной исполнимой ёмкости — "
                      + " | ".join(details),
        }
    if broken_series:
        return {"state": "red", "label": "требует внимания",
                "reason": f"оборванных серий: {broken_series}"}
    if diagnostics and diagnostics.get("checker_failed"):
        return {"state": "red", "label": "диагностика не выполнена",
                "reason": diagnostics.get("summary", "health-check недоступен")}
    if diagnostics and diagnostics.get("state") == "red":
        return {"state": "red", "label": "нарушен инвариант",
                "reason": diagnostics.get("summary", "health-check обнаружил ошибку")}
    recent = windows.get("5h", {})
    runs = recent.get("runs", {})
    failed = runs.get("unresolved_failed", runs.get("failed", 0))
    unable = runs.get("unresolved_unable", runs.get("unable", 0))
    if failed or unable:
        recovered = runs.get("recovered_failed", 0) + runs.get("recovered_unable", 0)
        recovered_note = f"; восстановлено: {recovered}" if recovered else ""
        return {"state": "red", "label": "прогон не отработал",
                "reason": f"активно — упало: {failed}; НЕ СМОГ: {unable}{recovered_note}"}
    if runtime and runtime.get("required") and runtime.get("paused"):
        return {"state": "yellow", "label": "конвейер на паузе",
                "reason": "включена общая пауза: активные серии не запускаются"}
    if paused_series:
        return {"state": "yellow", "label": "конвейер на паузе",
                "reason": f"приостановлено серий: {paused_series}"}
    if diagnostics and diagnostics.get("state") == "yellow":
        return {"state": "yellow", "label": "есть ожидания",
                "reason": diagnostics.get("summary", "health-check требует внимания")}
    if runs.get("human", 0):
        return {"state": "yellow", "label": "нужен человек",
                "reason": f"прогонов с НУЖЕН ЧЕЛОВЕК: {runs['human']}"}
    if not recent.get("complete"):
        return {"state": "warming", "label": "копится история",
                "reason": "для динамики нужно около 5 часов снимков"}
    if recent.get("churn_items", 0):
        return {"state": "yellow", "label": "высокий churn",
                "reason": f"по кругу ходят элементов: {recent['churn_items']}"}
    if backlog and recent.get("backlog_delta", 0) >= 0 and not recent.get("exited", 0):
        return {"state": "yellow", "label": "нет чистого выхода",
                "reason": "за 5 часов ни один элемент не вышел из цепочки"}
    if backlog == 0:
        return {"state": "green", "label": "очередь пуста", "reason": "backlog отсутствует"}
    return {"state": "green", "label": "движется", "reason": "есть чистый выход из цепочки"}


def _profile_active(profile: dict, series: list[dict]) -> bool:
    if profile.get("always_sample"):
        return True
    # Individually paused pipelines need no background samples. A global pause
    # is enforced by sample_active_profiles() and analyze(), including profiles
    # that opt into always_sample.
    titles = [item.get("title", "").lower() for item in series
              if not item.get("ended") and not item.get("paused")]
    return any(queue.get("series_contains", "").lower() in title
               for queue in profile.get("queues", []) for title in titles
               if queue.get("series_contains"))


def _queue_replica_count(queue: dict) -> int:
    value = queue.get("replicas", 1)
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= 16):
        raise ValueError("replicas должен быть целым числом от 1 до 16")
    return value


def _series_replicas_for_queue(queue: dict, series: list[dict]) -> list[dict]:
    marker = str(queue.get("series_contains") or "").lower()
    if not marker:
        return []
    matching = [item for item in series
                if marker in str(item.get("title") or "").lower()
                and not item.get("ended") and not item.get("ended_at")]
    # Preserve the historical first-match behavior unless the operator opted
    # into replicas explicitly. A broad legacy marker must not wake multiple
    # unreserved tasks merely because an accidental duplicate series exists.
    return matching if _queue_replica_count(queue) > 1 else matching[:1]


def _local_directory_identity(path: Path) -> tuple[int, int]:
    """Filesystem identity, including case-insensitive and symlink aliases."""
    stat_result = os.stat(path)
    return int(stat_result.st_dev), int(stat_result.st_ino)


def _queue_replica_status(queue: dict, series: list[dict]) -> dict:
    """Validate an opt-in local replica set and expose every matched series."""
    configured = _queue_replica_count(queue)
    matching = _series_replicas_for_queue(queue, series)
    issues = []
    if configured > 1 and len(matching) != configured:
        issues.append(
            f"ожидалось реплик: {configured}, найдено активных серий: {len(matching)}")
    if configured > 1 and CONCURRENCY < configured:
        issues.append(
            f"PP_CONCURRENCY={CONCURRENCY} меньше числа реплик {configured}")
    seen_paths = {}
    rendered = []
    for item in matching:
        working_dir = str(item.get("working_dir") or "").strip()
        normalized = None
        if configured > 1:
            if item.get("machine"):
                issues.append(
                    f"серия #{item.get('id')} удалённая; реплики требуют общий локальный SQLite")
            if item.get("worktree"):
                issues.append(
                    f"серия #{item.get('id')} использует динамический worktree; "
                    "репликам нужен постоянный отдельный working_dir")
            if item.get("detached"):
                issues.append(
                    f"серия #{item.get('id')} запускается detached; "
                    "репликам требуется управляемый lifetime провайдера")
            if item.get("herdr_target"):
                issues.append(
                    f"серия #{item.get('id')} использует пользовательский herdr_target")
            if not working_dir:
                issues.append(f"у серии #{item.get('id')} не задан working_dir")
            else:
                path = Path(working_dir).expanduser()
                if not path.is_absolute():
                    issues.append(
                        f"working_dir серии #{item.get('id')} должен быть абсолютным")
                else:
                    if not path.is_dir():
                        issues.append(
                            f"working_dir серии #{item.get('id')} не существует: {working_dir}")
                    else:
                        try:
                            normalized = _local_directory_identity(path)
                        except OSError as exc:
                            issues.append(
                                f"working_dir серии #{item.get('id')} недоступен: {exc}")
                        if normalized is not None:
                            previous = seen_paths.get(normalized)
                            if previous is not None:
                                issues.append(
                                    f"серии #{previous} и #{item.get('id')} "
                                    "используют один working_dir")
                            else:
                                seen_paths[normalized] = item.get("id")
        rendered.append({
            "series_id": item.get("id"), "title": item.get("title"),
            "working_dir": working_dir or None,
            "task_id": item.get("next_task_id"),
            "task_status": item.get("next_status"),
            "interval": item.get("effective_recurrence"),
            "paused": bool(item.get("paused")),
            "broken": bool(item.get("broken")),
            "worktree": bool(item.get("worktree")),
            "detached": bool(item.get("detached")),
            "herdr_target": item.get("herdr_target"),
            "failure_rate": item.get("failure_rate"),
            "empty_rate": item.get("empty_rate"),
            "avg_duration_seconds": item.get("avg_duration_seconds"),
        })
    return {
        "configured": configured,
        "present": len(matching),
        "active": sum(not item.get("paused") and not item.get("broken")
                      for item in matching),
        "valid": not issues,
        "issues": issues,
        "series": rendered,
        "matching": matching,
    }


def _queue_replica_projection(queue: dict, series: list[dict],
                              capacity: int) -> dict:
    status = _queue_replica_status(queue, series)
    matching = status["matching"]
    primary = matching[0] if matching else None
    active = int(status["active"])
    replicated = int(status["configured"]) > 1
    effective_replicas = active if (not replicated or status["valid"]) else 0
    recurrences = list(dict.fromkeys(
        item.get("effective_recurrence") for item in matching
        if item.get("effective_recurrence")))
    parseable = [(value, _interval_hours(value)) for value in recurrences]
    parseable = [(value, hours) for value, hours in parseable if hours is not None]
    interval = (max(parseable, key=lambda pair: pair[1])[0] if parseable
                else recurrences[0] if recurrences else None)
    durations = [int(item["avg_duration_seconds"]) for item in matching
                 if item.get("avg_duration_seconds") is not None]
    failures = [float(item.get("failure_rate") or 0) for item in matching]
    empties = [float(item.get("empty_rate") or 0) for item in matching]
    public_status = {key: value for key, value in status.items()
                     if key != "matching"}
    return {
        "matching": matching,
        "primary": primary,
        "replica_count": status["configured"],
        "replicas_present": status["present"],
        "replicas_active": active,
        "replica_status": public_status,
        "series_replicas": public_status["series"],
        "parallel_capacity": capacity * effective_replicas,
        "interval": interval,
        "avg_duration_seconds": (
            round(sum(durations) / len(durations)) if durations else None),
        "failure_rate": (
            round(sum(failures) / len(failures), 3) if failures else None),
        "empty_rate": round(sum(empties) / len(empties), 3) if empties else None,
    }


def _replica_recommendation(queue: dict, backlog: int, projection: dict,
                            target_hours: float) -> dict:
    capacity = int(projection["parallel_capacity"])
    if capacity > 0:
        return _recommendation(
            queue, backlog, capacity, projection["interval"], target_hours,
            projection["avg_duration_seconds"],
        )
    invalid = not bool(projection["replica_status"].get("valid", True))
    return {
        "recommended_interval": None, "eta_hours": None,
        "recommendation": (
            "невалидная конфигурация реплик — исправьте series/working_dir"
            if invalid else
            "нет активных реплик — восстановите или возобновите серию"
        ),
        "avg_duration_seconds": projection["avg_duration_seconds"],
        "cycle_hours": None, "throughput_per_hour": 0,
    }


def _replica_runs_needed(backlog: int, projection: dict) -> float | None:
    capacity = int(projection["parallel_capacity"])
    return round(backlog / capacity, 1) if capacity > 0 else None


def _bottleneck_rank(queue: dict) -> float:
    """Rank a positive backlog with no executable capacity as a hard stall."""
    backlog = queue.get("backlog")
    if not isinstance(backlog, int) or backlog <= 0:
        return 0
    if queue.get("eta_hours") is not None:
        return float(queue["eta_hours"])
    if queue.get("runs_needed") is not None:
        return float(queue["runs_needed"])
    return math.inf


def _series_for_queue(queue: dict, series: list[dict]) -> dict | None:
    matching = _series_replicas_for_queue(queue, series)
    return matching[0] if matching else None


def _unknown_recommendation() -> dict:
    return {
        "recommended_interval": None, "eta_hours": None,
        "recommendation": "нет сохранённого снимка GitHub — нажмите «Обновить»",
        "avg_duration_seconds": None, "cycle_hours": None,
        "throughput_per_hour": None,
    }


def _cache_metadata(source: str, generated_at: float | None,
                    entry_epoch: int | None, entry_revision: int | None,
                    current_epoch: int, profile_hash: str) -> dict:
    age_seconds = None
    if generated_at is not None:
        age_seconds = max(0, round(time.time() - generated_at))
    invalidated = entry_epoch is not None and entry_epoch != current_epoch
    available = source != "none"
    complete = source in {"live", "memory", "durable"}
    token = ({
        "profile_hash": profile_hash, "epoch": int(entry_epoch),
        "revision": int(entry_revision), "generated_at": generated_at,
    } if complete and entry_epoch is not None
         and entry_revision is not None and generated_at is not None else None)
    return {
        "source": source,
        "available": available,
        "complete": complete,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "ttl_seconds": _CACHE_TTL_SECONDS,
        "invalidated": invalidated,
        "stale": (not available or age_seconds is None
                  or age_seconds >= _CACHE_TTL_SECONDS or invalidated),
        "profile_hash": profile_hash,
        "token": token,
    }


def _refresh_local_state(result: dict, profile: dict, series: list[dict], *,
                         source: str, generated_at: float | None,
                         entry_epoch: int | None, entry_revision: int | None,
                         current_epoch: int) -> dict:
    """Overlay a saved GitHub snapshot with current, quota-free local state."""
    data = copy.deepcopy(result)
    now = datetime.now(timezone.utc)
    diagnostics = data.get("diagnostics")
    target_hours = float(profile.get("target_clear_hours", 8))
    priority_settings = _priority_settings(profile)
    queue_configs = {str(item.get("id")): item for item in profile.get("queues", [])}
    matching_series = []
    series_ids = []
    broken_series = 0
    paused_series = 0

    for queue in data.get("queues", []):
        config = queue_configs.get(str(queue.get("id")), {})
        capacity = max(1, int(config.get("capacity", queue.get("capacity", 1))))
        projection = _queue_replica_projection(config, series, capacity)
        matches = projection.pop("matching")
        matching = projection.pop("primary")
        if matches:
            matching_series.extend(matches)
            series_ids.extend(int(item["id"]) for item in matches)
            broken_series += sum(int(bool(item.get("broken"))) for item in matches)
            paused_series += sum(int(bool(item.get("paused"))) for item in matches)
        queue.update({
            "capacity": capacity,
            "series_id": matching["id"] if matching else None,
            "task_id": matching.get("next_task_id") if matching else None,
            "task_status": matching.get("next_status") if matching else None,
            **projection,
        })
        backlog = queue.get("backlog")
        if isinstance(backlog, int):
            queue["runs_needed"] = _replica_runs_needed(backlog, queue)
            queue.update(_replica_recommendation(
                config, backlog, queue, target_hours))
        else:
            queue["runs_needed"] = None
            queue.update(_unknown_recommendation())
        if not isinstance(queue.get("execution"), dict):
            configured = str((config.get("execution") or {}).get("mode", "skill"))
            queue["execution"] = {
                "configured": configured, "effective": configured,
                "available": None, "reason": "не проверяется при чтении снимка",
            }
        queue["wake"] = (_wake_status(data["profile_id"], config, diagnostics)
                         if isinstance(diagnostics, dict) else None)
        queue["adaptive_cadence"] = _adaptive_cadence_status(
            config, matches, backlog if isinstance(backlog, int) else None)

    bottleneck = max(
        (queue for queue in data.get("queues", [])
         if isinstance(queue.get("backlog"), int)),
        key=_bottleneck_rank,
        default=None,
    )
    data["bottleneck"] = (
        bottleneck["id"] if bottleneck and bottleneck.get("backlog") else None)

    activity = db.pipeline_series_activity(series_ids)
    empty_runs = db.pipeline_run_metrics([], now - timedelta(hours=5))
    aggregate_runs = dict(empty_runs)
    for queue in data.get("queues", []):
        queue_series_ids = [int(item["series_id"])
                            for item in queue.get("series_replicas", [])
                            if item.get("series_id") is not None]
        last_runs = [activity[value] for value in queue_series_ids
                     if value in activity]
        queue["last_run"] = max(
            last_runs, key=lambda value: value.get("at") or "", default=None)
        metrics = db.pipeline_run_metrics(
            queue_series_ids, now - timedelta(hours=5))
        queue["runs_5h"] = metrics
        for replica in queue.get("series_replicas", []):
            replica["last_run"] = activity.get(replica.get("series_id"))
        for key, value in metrics.items():
            aggregate_runs[key] = aggregate_runs.get(key, 0) + value

    history = data.get("history")
    if not isinstance(history, dict):
        history = {}
        data["history"] = history
    for hours in _HISTORY_WINDOWS:
        key = f"{hours}h"
        window = history.get(key)
        if not isinstance(window, dict):
            window = {
                "hours": hours, "coverage_hours": 0, "complete": False,
                "backlog_delta": 0, "entered": 0, "exited": 0, "moved": 0,
                "backlog_delta_per_hour": None,
                "entered_per_hour": None, "exited_per_hour": None,
                "transitions": 0, "churn_items": 0, "queue_deltas": {},
                "queue_deltas_per_hour": {}, "queue_throughput": {},
                "queue_throughput_per_hour": {},
            }
            history[key] = window
        if hours == 5:
            window["runs"] = aggregate_runs
        elif not isinstance(window.get("runs"), dict):
            window["runs"] = dict(empty_runs)

    safe_deferrals = sum(
        1 for item in matching_series
        if item.get("next_status") in {"pending", "rate_limited"}
        and bool(item.get("next_error"))
    )
    data["outcomes"] = {
        "safe_deferrals_now": safe_deferrals,
        "stale_reselections_5h": aggregate_runs.get("stale", 0),
        "safe_refusals_5h": aggregate_runs.get("safe_refusal", 0),
        "real_errors_5h": (
            aggregate_runs.get("unresolved_unable", aggregate_runs.get("unable", 0))
            + aggregate_runs.get("unresolved_failed", aggregate_runs.get("failed", 0))
        ),
    }

    runtime = _pipeline_runtime(matching_series, now)
    backlog_total = data.get("backlog_total")
    invalid_replica_queues = [
        queue for queue in data.get("queues", [])
        if isinstance(queue.get("backlog"), int) and queue["backlog"] > 0
        and isinstance(queue.get("replica_status"), dict)
        and not queue["replica_status"].get("valid", True)
    ]
    health = _health(
        backlog_total if isinstance(backlog_total, int) else 0,
        history, broken_series, paused_series, diagnostics, runtime,
        invalid_replica_queues,
    )
    if source == "none" and health.get("state") in {"green", "warming"}:
        health = {
            "state": "warming", "label": "нет снимка GitHub",
            "reason": "данные ещё не загружены; нажмите «Обновить» для явного запроса",
        }
    data["runtime"] = runtime
    data["health"] = health
    data["target_clear_hours"] = target_hours
    data["priority_control"] = ({
        "levels": list(_PRIORITY_LEVELS),
        "aging_hours": priority_settings["aging_hours"],
        "manual_labels": priority_settings["manual_labels"],
    } if priority_settings else None)
    data["cache"] = _cache_metadata(
        source, generated_at, entry_epoch, entry_revision, current_epoch,
        _profile_fingerprint(profile))
    return data


def _snapshot_fallback(profile_id: str, profile: dict,
                       series: list[dict], *,
                       allow_legacy: bool) -> tuple[dict, float] | None:
    """Rebuild a partial response from append-only snapshots made by older PP."""
    try:
        row = db.latest_pipeline_snapshot(
            profile_id, profile_hash=_profile_fingerprint(profile),
            allow_legacy=allow_legacy)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not row or row.get("repository") != profile.get("repository"):
        return None
    snapshot = row.get("payload")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("queues"), dict):
        return None
    captured = _parse_time(row.get("captured_at"))
    if captured is None:
        return None
    now = datetime.now(timezone.utc)
    target_hours = float(profile.get("target_clear_hours", 8))
    priority_settings = _priority_settings(profile)
    queues = []
    all_items = {}
    for config in profile.get("queues", []):
        saved = snapshot["queues"].get(str(config.get("id")))
        saved = saved if isinstance(saved, dict) else {}
        backlog = saved.get("backlog")
        backlog = int(backlog) if isinstance(backlog, (int, float)) else None
        members = [item for item in saved.get("items", []) if isinstance(item, dict)]
        for member in members:
            if member.get("key"):
                all_items[member["key"]] = member
        capacity = max(1, int(config.get("capacity", 1)))
        projection = _queue_replica_projection(config, series, capacity)
        projection.pop("matching")
        projection.pop("primary")
        recommendation = (_replica_recommendation(
            config, backlog, projection, target_hours)
            if isinstance(backlog, int) else _unknown_recommendation())
        ordered_members = copy.deepcopy(members)
        if priority_settings:
            for member in ordered_members:
                member["priority"] = _item_priority(member, priority_settings, now)
            ordered_members.sort(key=lambda member: (
                _PRIORITY_LEVELS.index(member["priority"]["level"]),
                member.get("created_at") or "", member.get("number") or 0,
            ))
        queues.append({
            "id": config["id"], "title": config["title"], "backlog": backlog,
            "capacity": capacity,
            "runs_needed": _replica_runs_needed(backlog, projection)
            if isinstance(backlog, int) else None,
            "membership_complete": bool(saved.get("membership_complete")),
            "age": _age_stats(members, now),
            "items": ordered_members[:priority_settings["max_items"]]
            if priority_settings else [],
            **projection,
            **recommendation,
        })

    backlog_values = [queue["backlog"] for queue in queues
                      if isinstance(queue.get("backlog"), int)]
    complete_backlog = len(backlog_values) == len(queues)
    result = {
        "profile_id": profile_id, "title": profile["title"],
        "repository": profile["repository"], "queues": queues,
        "target_clear_hours": target_hours,
        "backlog_total": sum(backlog_values) if complete_backlog else None,
        "age": _age_stats(list(all_items.values()), now),
        # Reconstructing churn from thousands of legacy JSON rows would make a
        # supposedly cheap read slow. The newest queue counts remain useful;
        # a full background/explicit refresh restores precomputed trends.
        "history": {}, "diagnostics": None,
        "diagnostics_generated_at": None, "github_rate_limit": None,
        "priority_control": ({
            "levels": list(_PRIORITY_LEVELS),
            "aging_hours": priority_settings["aging_hours"],
            "manual_labels": priority_settings["manual_labels"],
        } if priority_settings else None),
        "bottleneck": None, "generated_at": captured.timestamp(),
    }
    if complete_backlog:
        bottleneck = max(
            queues,
            key=_bottleneck_rank,
            default=None,
        )
        result["bottleneck"] = (
            bottleneck["id"] if bottleneck and bottleneck["backlog"] else None)
    return result, captured.timestamp()


def _empty_cached_result(profile_id: str, profile: dict) -> dict:
    return {
        "profile_id": profile_id, "title": profile["title"],
        "repository": profile["repository"],
        "queues": [{
            "id": config["id"], "title": config["title"], "backlog": None,
            "capacity": max(1, int(config.get("capacity", 1))),
            "runs_needed": None, "membership_complete": False,
            "age": {"median_hours": None, "p90_hours": None, "oldest_hours": None},
            "items": [], **_unknown_recommendation(),
        } for config in profile.get("queues", [])],
        "target_clear_hours": float(profile.get("target_clear_hours", 8)),
        "backlog_total": None,
        "age": {"median_hours": None, "p90_hours": None, "oldest_hours": None},
        "history": None, "diagnostics": None,
        "diagnostics_generated_at": None, "github_rate_limit": None,
        "priority_control": None, "bottleneck": None, "generated_at": None,
    }


def _with_persisted_refresh_status(result: dict, profile_id: str,
                                   profile: dict) -> dict:
    """Overlay a still-actionable denial on a quota-free cached response."""
    try:
        if _github_budget_policy(profile) is None:
            return result
    except (TypeError, ValueError):
        # Invalid opt-in configuration is itself an admission blocker and its
        # persisted explanation remains useful until the profile is corrected.
        pass
    try:
        event = db.get_pipeline_refresh_status(profile_id)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return result
    if (not isinstance(event, dict)
            or event.get("repository") != str(profile.get("repository", ""))):
        return result
    status = event.get("status")
    if not isinstance(status, dict):
        return result
    raw_defer_until = status.get("refresh_deferred_until")
    defer_until = _parse_time(raw_defer_until)
    if defer_until is None or defer_until <= datetime.now(timezone.utc):
        return result
    data = copy.deepcopy(result)
    cache = data.setdefault("cache", {})
    cache["refresh_blocked"] = str(
        status.get("refresh_blocked") or "github_budget")
    cache["refresh_blocked_reason"] = str(
        status.get("refresh_blocked_reason") or "GitHub scan запрещён")
    cache["refresh_deferred_until"] = status.get("refresh_deferred_until")
    if status.get("github_rate_limit") is not None:
        data["github_rate_limit"] = copy.deepcopy(status["github_rate_limit"])
    if isinstance(status.get("github_budget"), dict):
        data["github_budget"] = copy.deepcopy(status["github_budget"])
    return data


def _with_live_github_budget_state(result: dict, profile: dict) -> dict:
    """Rebuild the public budget decision from local live state only.

    A persisted refresh denial describes the admission attempt that produced
    it.  Its reservation ledger may already have changed by the time the
    dashboard is read, so copying ``allowed``/``state``/``reason`` from that
    denial while refreshing only the counters produces a contradictory view.
    Re-evaluate the zero-cost, before-route decision from the latest locally
    observed GitHub limits and the current reservation ledger instead.
    """
    try:
        policy = _github_budget_policy(profile)
        if policy is not None:
            policy = _with_shared_budget_floor(policy)
    except (TypeError, ValueError) as exc:
        data = copy.deepcopy(result)
        data["github_budget"] = {
            "enabled": True, "state": "invalid_config", "allowed": False,
            "reason": f"Некорректный github_budget: {exc}",
        }
        return data
    if policy is None:
        return result

    data = copy.deepcopy(result)
    # A cached payload may carry an older rate-limit object.  It is useful only
    # when the durable snapshot can also provide its observation time; without
    # that source it must not be presented as the current actual GitHub value.
    data["github_rate_limit"] = None
    data["github_rate_limit_observed_at"] = None

    # The dashboard describes shared capacity before a future execution route
    # is elected. Route admission remains a separate, costed decision in
    # ``_reserve_execution_admission``.
    policy = _budget_policy_for_route(policy, None)
    now = time.time()
    snapshot = None
    snapshot_reason = None
    try:
        snapshot = db.get_pipeline_github_rate_snapshot(policy["lease_scope"])
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        snapshot_reason = str(exc)
    if snapshot is None and snapshot_reason is None:
        snapshot_reason = "Локальный снимок GitHub /rate_limit отсутствует"
    if snapshot is not None:
        data["github_rate_limit"] = copy.deepcopy(snapshot["limits"])
        data["github_rate_limit_observed_at"] = _defer_at(
            snapshot["observed_at"])

    reservations = None
    ledger_reason = None
    try:
        reservations = _budget_reservation_ledger(policy, now=now)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        ledger_reason = str(exc)

    if ledger_reason is not None:
        decision = _budget_denied(
            policy, state="ledger_unavailable",
            reason=f"Журнал резервов GitHub API недоступен: {ledger_reason}",
            now=now,
            limits=(snapshot["limits"] if snapshot is not None else None),
        )
    elif snapshot is None:
        decision = _budget_denied(
            policy, state="rate_limit_unavailable",
            reason=f"GitHub /rate_limit недоступен: {snapshot_reason}",
            now=now,
            reserved_other=reservations["totals"],
            active_reservations=reservations["count"],
        )
    else:
        decision = _evaluate_github_budget(
            policy, snapshot["limits"], now=now,
            reserved_other=reservations["totals"],
            active_reservations=reservations["count"],
        )

    budget = _public_budget_decision(decision)
    if snapshot is None:
        budget["rate_snapshot_state"] = "unavailable"
        budget["rate_snapshot_reason"] = snapshot_reason
        budget.pop("effective_after", None)
        budget.pop("projected_post_reservation", None)
    else:
        budget["rate_snapshot_state"] = "ok"
        budget["rate_snapshot_age_seconds"] = max(
            0, round(now - snapshot["observed_at"]))

    if reservations is None:
        budget["ledger_state"] = "unavailable"
        budget["ledger_reason"] = ledger_reason
        # The denial helper uses numeric defaults, but zero would incorrectly
        # claim that an unreadable ledger contains no reservations.
        for key in (
                "active_reservations", "reserved_other", "effective_after",
                "projected_post_reservation", "spendable_before_route"):
            budget.pop(key, None)
    else:
        totals = dict(reservations["totals"])
        budget["ledger_state"] = "ok"
        budget["reserved_in_flight"] = totals
        route_counts = {}
        for item in reservations["items"]:
            route = str(item.get("route") or "unknown")
            route_counts[route] = route_counts.get(route, 0) + 1
        budget["reservation_routes"] = route_counts
        budget["waiting_waiters"] = copy.deepcopy(
            reservations.get("waiting_waiters") or {})
        budget["fairness_waiter"] = copy.deepcopy(
            reservations.get("fairness_waiter"))
        if snapshot is not None:
            effective_after = decision["effective_after"]
            budget["spendable_before_route"] = {
                resource: max(
                    0, effective_after[resource]
                    - policy["minimum_remaining"][resource])
                for resource in policy["minimum_remaining"]
            }
    data["github_budget"] = budget
    return data


def read_cached(profile_id: str, series: list[dict]) -> dict:
    """Return saved insights plus live local state without any GitHub request."""
    profiles = _profiles()
    if profile_id not in profiles:
        raise KeyError(profile_id)
    profile = profiles[profile_id]
    # The payload/revision/epoch come from one SQLite snapshot. Re-read the
    # epoch afterwards as a seqlock: invalidation during the load must either
    # retry against the new epoch or return the old last-good explicitly stale.
    cached = None
    source = None
    current_epoch = 0
    for _attempt in range(3):
        cached, source, snapshot_epoch = _cached_entry(profile_id, profile)
        current_epoch = _cache_epoch()
        if snapshot_epoch == current_epoch:
            break
    if cached:
        return _with_live_github_budget_state(
            _with_persisted_refresh_status(_refresh_local_state(
                cached[1], profile, series, source=source or "memory",
                generated_at=float(cached[0]), entry_epoch=int(cached[2]),
                entry_revision=int(cached[4]), current_epoch=current_epoch,
            ), profile_id, profile), profile)
    # A full cache that no longer matches the complete profile fingerprint
    # proves that query semantics changed. Do not reinterpret its legacy raw
    # snapshot under the new profile; wait for an explicit successful refresh.
    incompatible_full_cache = (
        db.get_setting(_legacy_cache_key(profile_id)) is not None
        or db.has_setting_prefix(_cache_namespace_prefix(profile_id))
    )
    # Exact-fingerprint raw history is safe even if another configuration has
    # a full cache. Only the unnamespaced legacy rows become ambiguous once a
    # fingerprinted full-cache namespace exists.
    fallback = _snapshot_fallback(
        profile_id, profile, series,
        allow_legacy=not incompatible_full_cache)
    if fallback:
        result, generated_at = fallback
        return _with_live_github_budget_state(
            _with_persisted_refresh_status(_refresh_local_state(
                result, profile, series, source="snapshot",
                generated_at=generated_at, entry_epoch=None,
                entry_revision=None, current_epoch=current_epoch,
            ), profile_id, profile), profile)
    return _with_live_github_budget_state(
        _with_persisted_refresh_status(_refresh_local_state(
            _empty_cached_result(profile_id, profile), profile, series,
            source="none", generated_at=None, entry_epoch=None,
            entry_revision=None, current_epoch=current_epoch,
        ), profile_id, profile), profile)


def _paused_cached(profile_id: str, series: list[dict]) -> dict:
    result = read_cached(profile_id, series)
    result["cache"]["refresh_blocked"] = "worker_paused"
    return result


def _budget_blocked_cached(profile_id: str, profile: dict, series: list[dict],
                           admission: dict) -> dict:
    """Return last-good data and explain why no external refresh was started."""
    _record_refresh_blocked(profile_id, profile, admission)
    result = read_cached(profile_id, series)
    if admission.get("github_rate_limit") is not None:
        result["github_rate_limit"] = admission["github_rate_limit"]
    runtime_budget = copy.deepcopy(result.get("github_budget") or {})
    runtime_budget.update(_public_budget_decision(admission))
    result["github_budget"] = runtime_budget
    cache = result.setdefault("cache", {})
    cache["refresh_blocked"] = str(admission.get("state") or "github_budget")
    cache["refresh_blocked_reason"] = str(
        admission.get("reason") or "GitHub scan запрещён")
    cache["refresh_deferred_until"] = admission.get("defer_until")
    return result


def _analyze_without_budget(profile_id: str, series: list[dict], *,
                            use_cache: bool = True,
                            refresh_diagnostics: bool = False) -> dict:
    profiles = _profiles()
    if profile_id not in profiles:
        raise KeyError(profile_id)
    if use_cache:
        return read_cached(profile_id, series)
    if db.is_paused():
        return _paused_cached(profile_id, series)

    lock = _profile_lock(profile_id)
    with lock:
        if db.is_paused():
            return _paused_cached(profile_id, series)
        # The profile file can change while this process waits for a refresh
        # already in flight. Reload it before allocating this scan's revision.
        profiles = _profiles()
        if profile_id not in profiles:
            raise KeyError(profile_id)
        profile = profiles[profile_id]
        cached, _source, cache_epoch = _cached_entry(profile_id, profile)
        _unused, cache_generation = _cache_snapshot(profile_id)
        refresh_revision = db.increment_int_setting(
            _refresh_revision_key(profile_id))
        if db.is_paused():
            return _paused_cached(profile_id, series)
        priority_settings = _priority_settings(profile)
        health_config = profile.get("health_check") or {}
        health_cache_seconds = max(0, int(health_config.get("cache_seconds", 1800)))
        diagnostics = None
        diagnostics_generated_at = None
        if not refresh_diagnostics and cached and int(cached[2]) == cache_epoch:
            diagnostics_generated_at = cached[1].get("diagnostics_generated_at")
            cached_diagnostics = cached[1].get("diagnostics")
            effective_health_ttl = min(60, health_cache_seconds) if (
                isinstance(cached_diagnostics, dict)
                and cached_diagnostics.get("checker_failed")) else health_cache_seconds
            if (diagnostics_generated_at is not None
                    and time.time() - float(diagnostics_generated_at) < effective_health_ttl):
                diagnostics = cached_diagnostics
        if diagnostics is None:
            diagnostics = _run_profile_health_check(profile)
            diagnostics_generated_at = time.time()
        if db.is_paused():
            return _paused_cached(profile_id, series)
        target_hours = float(profile.get("target_clear_hours", 8))
        now = datetime.now(timezone.utc)
        queues = []
        snapshot_queues = {}
        series_ids = []
        broken_series = 0
        paused_series = 0
        matching_series = []
        all_items = {}
        cadence_reconciliations = []

        for item in profile["queues"]:
            queries = item.get("queries") or [item["query"]]
            searches = []
            for query in queries:
                if db.is_paused():
                    return _paused_cached(profile_id, series)
                try:
                    searches.append(_github_search(profile["repository"], query))
                except _GitHubScanPaused:
                    # Never reinterpret page 1 of an interrupted paginated
                    # query as a complete queue observation.
                    return _paused_cached(profile_id, series)
            backlog = sum(search["count"] for search in searches)
            members = {}
            for search in searches:
                for member in search["items"]:
                    members[member["key"]] = member
            diagnostic_field = item.get("backlog_diagnostic_field")
            diagnostic_items = (diagnostics or {}).get(diagnostic_field) if isinstance(diagnostic_field, str) else None
            diagnostic_order = {}
            if isinstance(diagnostic_items, list):
                diagnostic_order = {
                    int(candidate["number"]): index
                    for index, candidate in enumerate(diagnostic_items)
                    if isinstance(candidate, dict) and str(candidate.get("number", "")).isdigit()
                }
                allowed_numbers = {int(candidate["number"]) for candidate in diagnostic_items
                                   if isinstance(candidate, dict) and str(candidate.get("number", "")).isdigit()}
                backlog = len(diagnostic_items)
                members = {key: member for key, member in members.items()
                           if str(member.get("number", "")).isdigit()
                           and int(member["number"]) in allowed_numbers}
            all_items.update(members)
            capacity = max(1, int(item.get("capacity", 1)))
            projection = _queue_replica_projection(item, series, capacity)
            matches = projection.pop("matching")
            matching = projection.pop("primary")
            if matches:
                matching_series.extend(matches)
                series_ids.extend(int(value["id"]) for value in matches)
                broken_series += sum(int(bool(value.get("broken")))
                                     for value in matches)
                paused_series += sum(int(bool(value.get("paused")))
                                     for value in matches)
            cadence = _adaptive_cadence_status(item, matches, backlog)
            cadence_reconciliations.append((item, matches, backlog))
            runs_needed = _replica_runs_needed(backlog, projection)
            recommendation = _replica_recommendation(
                item, backlog, projection, target_hours)
            age = _age_stats(list(members.values()), now)
            membership_complete = all(search["membership_complete"] for search in searches)
            ordered_members = list(members.values())
            if priority_settings:
                for member in ordered_members:
                    member["priority"] = _item_priority(member, priority_settings, now)
                ordered_members.sort(key=lambda member: (
                    diagnostic_order.get(int(member["number"]), len(diagnostic_order))
                    if diagnostic_order else 0,
                    _PRIORITY_LEVELS.index(member["priority"]["level"]),
                    member.get("created_at") or "", member["number"],
                ))
            queues.append({
                "id": item["id"], "title": item["title"], "backlog": backlog,
                "capacity": capacity, "runs_needed": runs_needed,
                "series_id": matching["id"] if matching else None,
                "task_id": matching.get("next_task_id") if matching else None,
                "task_status": matching.get("next_status") if matching else None,
                **projection,
                "membership_complete": membership_complete, "age": age,
                "items": ordered_members[:priority_settings["max_items"]]
                if priority_settings else [],
                "execution": _execution_status(item, matching.get("working_dir") if matching else None),
                "wake": _wake_status(profile_id, item, diagnostics),
                "adaptive_cadence": cadence,
                **recommendation,
            })
            snapshot_queues[item["id"]] = {
                "backlog": backlog, "items": list(members.values()),
                "membership_complete": membership_complete,
            }

        profile_hash = _profile_fingerprint(profile)
        snapshot = {
            "captured_at": now.isoformat(), "profile_hash": profile_hash,
            "queues": snapshot_queues,
        }
        history_rows = db.list_pipeline_snapshots(
            profile_id, since=now - timedelta(hours=max(_HISTORY_WINDOWS) + 1), limit=10000)
        history_rows = [
            row for row in history_rows
            if isinstance(row.get("payload"), dict)
            and row["payload"].get("profile_hash") == profile_hash
        ]
        history_rows.append({"captured_at": now.isoformat(), "payload": snapshot})
        windows = {f"{hours}h": _window_metrics(
            history_rows, snapshot, series_ids, now, hours) for hours in _HISTORY_WINDOWS}

        bottleneck = max(
            queues,
            key=_bottleneck_rank,
            default=None,
        )
        backlog_total = sum(queue["backlog"] for queue in queues)
        activity = db.pipeline_series_activity(series_ids)
        for queue in queues:
            queue_series_ids = [int(item["series_id"])
                                for item in queue.get("series_replicas", [])
                                if item.get("series_id") is not None]
            last_runs = [activity[value] for value in queue_series_ids
                         if value in activity]
            queue["last_run"] = max(
                last_runs, key=lambda value: value.get("at") or "", default=None)
            queue["runs_5h"] = db.pipeline_run_metrics(
                queue_series_ids,
                now - timedelta(hours=5),
            )
            for replica in queue.get("series_replicas", []):
                replica["last_run"] = activity.get(replica.get("series_id"))
        runtime = _pipeline_runtime(matching_series, now)
        invalid_replica_queues = [
            queue for queue in queues
            if queue.get("backlog", 0) > 0
            and isinstance(queue.get("replica_status"), dict)
            and not queue["replica_status"].get("valid", True)
        ]
        if db.is_paused():
            github_rate_limit = (cached[1].get("github_rate_limit")
                                 if cached else None)
        else:
            try:
                github_rate_limit = _github_rate_limits()
            except _GitHubRateLimitUnavailable:
                # Without an admission policy this is optional UI telemetry,
                # not a safety gate. Budget-enabled profiles still propagate
                # the error to analyze(), which records a fail-closed denial.
                if _github_budget_policy(profile) is not None:
                    raise
                github_rate_limit = None
        result = {
            "profile_id": profile_id, "title": profile["title"],
            "repository": profile["repository"], "queues": queues,
            "target_clear_hours": target_hours, "backlog_total": backlog_total,
            "age": _age_stats(list(all_items.values()), now),
            "history": windows,
            "health": _health(
                backlog_total, windows, broken_series, paused_series,
                diagnostics, runtime, invalid_replica_queues),
            "diagnostics": diagnostics,
            "diagnostics_generated_at": diagnostics_generated_at,
            "github_rate_limit": github_rate_limit,
            "runtime": runtime,
            "priority_control": ({
                "levels": list(_PRIORITY_LEVELS),
                "aging_hours": priority_settings["aging_hours"],
                "manual_labels": priority_settings["manual_labels"],
            } if priority_settings else None),
            "bottleneck": bottleneck["id"] if bottleneck and bottleneck["backlog"] else None,
            "generated_at": now.timestamp(),
        }
        _ensure_github_scan_lease()
        published = _publish_cache(
            profile_id, profile, cache_generation, cache_epoch,
            refresh_revision, result)
        if not published:
            return read_cached(profile_id, series)
        # Raw trend history records only observations that won the same CAS as
        # the full cache. A rejected/stale writer must not poison later charts.
        lease = getattr(_scan_lease_context, "lease", None)
        saved_snapshot = db.add_pipeline_snapshot(
            profile_id, profile["repository"], snapshot, now,
            lease_guard=({"scope": lease.scope, "token": lease.token}
                         if lease is not None else None),
        )
        if lease is not None and saved_snapshot is None:
            raise _GitHubScanLeaseLost(
                "SQLite lease/snapshot fence rejected GitHub scan publication")
        db.prune_pipeline_snapshots(now - timedelta(days=31))
        # Cadence changes are local mutations derived from this observation.
        # Apply them only after both the full-cache CAS and the fenced history
        # snapshot accepted the observation; a stale/losing scan must never
        # retime a live series.
        publication_guard = {
            "epoch_key": _CACHE_EPOCH_KEY,
            "epoch": str(cache_epoch),
            "epoch_default": "0",
            "profile_key": _published_profile_key(profile_id),
            "profile_hash": profile_hash,
            "revision_key": _published_profile_revision_key(profile_id),
            "revision": str(refresh_revision),
        }
        try:
            current_profile = _profiles().get(profile_id)
            profile_still_current = (
                isinstance(current_profile, dict)
                and _profile_fingerprint(current_profile) == profile_hash
            )
        except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
            profile_still_current = False
        if profile_still_current:
            for item, matching, backlog in cadence_reconciliations:
                _reconcile_adaptive_cadence(
                    item, matching, backlog,
                    publication_guard=publication_guard)
        return _refresh_local_state(
            result, profile, series, source="live",
            generated_at=float(result["generated_at"]), entry_epoch=cache_epoch,
            entry_revision=refresh_revision, current_epoch=_cache_epoch(),
        )


def analyze(profile_id: str, series: list[dict], *, use_cache: bool = True,
            refresh_diagnostics: bool = False) -> dict:
    """Read cached insights or run one budgeted, cross-process live scan."""
    if use_cache:
        return read_cached(profile_id, series)
    profiles = _profiles()
    if profile_id not in profiles:
        raise KeyError(profile_id)
    if db.is_paused():
        return _paused_cached(profile_id, series)
    profile = profiles[profile_id]
    with _github_scan_admission(
            profile, f"pipeline insights {profile_id}",
            profile_id=profile_id, budget_route="insights") as admission:
        if not admission.get("allowed"):
            return _budget_blocked_cached(
                profile_id, profile, series, admission)
        try:
            result = _analyze_without_budget(
                profile_id, series, use_cache=False,
                refresh_diagnostics=refresh_diagnostics)
            _ensure_github_scan_lease(renew=True)
        except _GitHubScanLeaseFailure as exc:
            return _budget_blocked_cached(
                profile_id, profile, series,
                _replace_admission_with_lease_failure(
                    admission, profile, exc))
        except _GitHubRateLimitUnavailable as exc:
            return _budget_blocked_cached(
                profile_id, profile, series,
                _replace_admission_with_rate_limit_failure(
                    admission, profile, exc))
    if admission.get("enabled"):
        result["github_budget"] = _public_budget_decision(admission)
    cache = result.get("cache")
    if isinstance(cache, dict) and cache.get("refresh_blocked") != "worker_paused":
        cache.pop("refresh_blocked", None)
        cache.pop("refresh_blocked_reason", None)
        cache.pop("refresh_deferred_until", None)
    return _with_live_github_budget_state(result, profile)


def sample_active_profiles(series: list[dict]) -> dict[str, str]:
    """Refresh every configured pipeline that has a matching live series."""
    if db.is_paused():
        return {}
    outcomes = {}
    for profile_id, profile in _profiles().items():
        if not _profile_active(profile, series):
            continue
        try:
            data = analyze(profile_id, series, use_cache=False)
            if db.is_paused():
                outcomes[profile_id] = "paused"
                break
            blocked = (data.get("cache") or {}).get("refresh_blocked")
            if blocked:
                outcomes[profile_id] = f"deferred: {blocked}"
                continue
            current_profile = _profiles().get(profile_id)
            if current_profile is None:
                outcomes[profile_id] = "profile removed"
                continue
            woken = _wake_ready_queues(
                profile_id, current_profile, data, series)
            outcomes[profile_id] = "ok" + (f"; woken={','.join(woken)}" if woken else "")
        except Exception as exc:  # one external repository must not stop the sampler
            outcomes[profile_id] = str(exc)
    return outcomes
