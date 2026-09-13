"""Profile-driven, token-free diagnostics for external GitHub pipelines."""

import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import db
from .config import DB_DIR, POLL_INTERVAL, TASK_TIMEOUT


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


class _GitHubScanPaused(RuntimeError):
    """A multi-request GitHub observation stopped at a page boundary."""


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


def _profile_fingerprint(profile: dict) -> str:
    encoded = json.dumps(
        profile, ensure_ascii=False, sort_keys=True,
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
        if not db.set_setting_if_newer_revision(
                _cache_key(profile_id, profile_hash), payload,
                revision_key=_published_revision_key(profile_id, profile_hash),
                revision=revision,
                guard_key=_CACHE_EPOCH_KEY, expected_guard=str(epoch),
                guard_default="0"):
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
        command = [
            _gh_executable(), "api", "search/issues", "--method", "GET",
            "--field", f"q=repo:{repository} {query}", "--field", "per_page=100",
            "--field", f"page={page}",
        ]
        run = subprocess.run(command, capture_output=True, text=True, timeout=30,
                             encoding="utf-8", errors="replace")
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
    command = [_gh_executable(), "api", *args]
    encoded = None
    if input_value is not None:
        command += ["--input", "-"]
        encoded = json.dumps(input_value, ensure_ascii=False)
    run = subprocess.run(
        command, input=encoded, capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="strict",
    )
    if run.returncode:
        raise RuntimeError((run.stderr or run.stdout or "gh api failed").strip())
    return json.loads(run.stdout) if run.stdout.strip() else None


def _github_rate_limits() -> dict | None:
    """Return the authenticated GitHub budgets without spending core quota."""
    try:
        payload = _gh_api_json(["rate_limit"])
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError):
        return None
    resources = payload.get("resources", {}) if isinstance(payload, dict) else {}
    result = {}
    for name in ("core", "search", "graphql"):
        item = resources.get(name)
        if not isinstance(item, dict):
            continue
        reset = item.get("reset")
        result[name] = {
            "limit": int(item.get("limit") or 0),
            "used": int(item.get("used") or 0),
            "remaining": int(item.get("remaining") or 0),
            "reset": int(reset) if reset is not None else None,
            "reset_at": datetime.fromtimestamp(int(reset), timezone.utc).isoformat()
            if reset is not None else None,
        }
    return result or None


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
    if run_now:
        marker = str(queue.get("series_contains") or "").lower()
        target = next((entry for entry in series
                       if marker and marker in str(entry.get("title", "")).lower()
                       and not entry.get("ended")), None)
        if target:
            woke = db.series_action(int(target["id"]), "run_now")
            paused = bool(target.get("paused"))
    _discard_cache(profile_id)
    return {
        "ok": True, "profile_id": profile_id, "queue_id": queue_id,
        "kind": kind, "number": number, "level": level,
        "label": selected, "run_now": run_now, "series_woken": woke,
        "series_paused": paused,
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
    for queue_id, queue in current.get("queues", {}).items():
        old = baseline.get("queues", {}).get(queue_id, {}).get("backlog", 0)
        queue_deltas[queue_id] = int(queue.get("backlog", 0)) - int(old)
    current_total = sum(q.get("backlog", 0) for q in current.get("queues", {}).values())
    baseline_total = sum(q.get("backlog", 0) for q in baseline.get("queues", {}).values())

    return {
        "hours": hours, "coverage_hours": round(min(coverage, hours), 1),
        "complete": complete, "backlog_delta": current_total - baseline_total,
        "entered": len(entered), "exited": len(exited), "moved": len(moved),
        "transitions": transitions, "churn_items": churn_items,
        "queue_deltas": queue_deltas,
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
            if cache.get("stale") or not cache.get("complete"):
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


def _matching_queue(task) -> tuple[str, dict, dict] | None:
    """Return the profile and queue owning a recurring task, if configured."""
    if not getattr(task, "series_id", None):
        return None
    title = (getattr(task, "series_title", None) or task.prompt.splitlines()[0]).lower()
    for profile_id, profile in _profiles().items():
        for queue in profile.get("queues", []):
            marker = str(queue.get("series_contains", "")).lower()
            if marker and marker in title:
                return profile_id, profile, queue
    return None


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
    if cache.get("stale") or cache.get("complete") is not True:
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
        if fingerprint is None:
            db.wake_series_once(
                None, latch_key, None, cache_guard=cache_guard)
            continue
        target = next((item for item in series
                       if marker in str(item.get("title", "")).lower()
                       and not item.get("ended") and not item.get("paused")), None)
        if target and db.wake_series_once(
                int(target["id"]), latch_key, fingerprint,
                cache_guard=cache_guard):
            woken.append(str(queue.get("id")))
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
    profile_id, profile, _queue = matched
    series = db.list_series()
    # A productive stage changes the GitHub protocol state. Refresh the project
    # checker once here so the next stage is woken immediately; routine sampler
    # and dispatch reads can then share that result.
    data = analyze(profile_id, series, use_cache=False, refresh_diagnostics=True)
    if db.is_paused():
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
            result = subprocess.run(
                probe_command, cwd=str(root), capture_output=True, text=True,
                timeout=max(1, min(int(execution.get("probe_timeout_seconds", 30)), 120)),
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"probe не выполнен: {exc}"
        if result.returncode:
            detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
            return False, f"probe завершился с ошибкой: {detail[-1] if detail else 'unknown error'}"
    return True, "инструмент доступен"


def _tool_preflight(execution: dict, command: list[str], working_dir: str | None) -> dict:
    root = Path(working_dir or os.getcwd())
    try:
        result = subprocess.run(
            command, cwd=str(root), capture_output=True, text=True,
            timeout=max(1, min(int(execution.get("timeout_seconds", 180)), 900)),
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"pipeline preflight не выполнен: {exc}") from exc
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


def execution_route(task, fallback_prompt: str, working_dir: str | None = None) -> dict:
    """Choose the project tool or the original skill prompt without invoking an LLM.

    The profile owns this opt-in. PromptPilot only checks local availability and
    renders a compact executor-neutral prompt; all repository semantics stay in
    the project tool and its fallback skill.
    """
    matched = _matching_queue(task)
    if matched is None:
        return {"action": "prompt", "mode": "skill", "prompt": fallback_prompt}
    profile_id, _profile, queue = matched
    execution = queue.get("execution")
    if not isinstance(execution, dict):
        return {"action": "prompt", "mode": "skill", "prompt": fallback_prompt}

    mode = str(execution.get("mode", "auto")).lower()
    if mode not in {"auto", "tool", "skill"}:
        return {
            "action": "block", "mode": mode,
            "reason": f"неизвестный pipeline execution mode: {mode}",
            "profile_id": profile_id, "queue_id": queue.get("id"),
        }
    if mode == "skill":
        return {"action": "prompt", "mode": "skill", "prompt": fallback_prompt}

    stage = str(execution.get("stage") or queue.get("id") or "").lower()
    command = _tool_command(execution, stage)
    if command is None:
        available, reason = False, "execution.command должен быть непустым массивом строк"
    else:
        available, reason = _tool_available(execution, command, working_dir, stage)
    if not available:
        if mode == "auto":
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
        preflight = _tool_preflight(execution, command, working_dir)
    except RuntimeError as exc:
        reason = str(exc)
        if mode == "auto":
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
    preflight_reason = str(
        preflight.get("reason") or preflight.get("error") or preflight_action
    )
    if preflight_action in {"empty", "wait"}:
        return {
            "action": "complete_empty", "mode": "tool", "reason": preflight_reason,
            "verdict": str(preflight.get("verdict") or "ПУСТО"),
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
        if mode == "auto":
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
        if mode == "auto":
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
        run = subprocess.run(
            command, cwd=config.get("working_dir") or None, env=env,
            capture_output=True, text=True,
            timeout=max(1, min(int(config.get("timeout_seconds", 180)), 900)),
            encoding="utf-8", errors="replace",
        )
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
            diagnostics: dict | None = None, runtime: dict | None = None) -> dict:
    if runtime and runtime.get("required") and runtime.get("state") != "online":
        age = runtime.get("age_seconds")
        detail = f"; последний heartbeat {age} сек назад" if age is not None else ""
        return {"state": "red", "label": "worker не работает",
                "reason": f"нет свежего heartbeat worker{detail}"}
    if runtime and runtime.get("stalled"):
        tasks = ", ".join(f"#{item['task_id']}" for item in runtime["stalled"])
        return {"state": "red", "label": "зависший запуск",
                "reason": f"превышен task timeout: {tasks}"}
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


def _series_for_queue(queue: dict, series: list[dict]) -> dict | None:
    marker = str(queue.get("series_contains") or "").lower()
    if not marker:
        return None
    return next((item for item in series
                 if marker in str(item.get("title") or "").lower()), None)


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
        matching = _series_for_queue(config, series)
        if matching:
            matching_series.append(matching)
            series_ids.append(int(matching["id"]))
            broken_series += int(bool(matching.get("broken")))
            paused_series += int(bool(matching.get("paused")))
        queue.update({
            "capacity": max(1, int(config.get("capacity", queue.get("capacity", 1)))),
            "series_id": matching["id"] if matching else None,
            "task_id": matching.get("next_task_id") if matching else None,
            "task_status": matching.get("next_status") if matching else None,
            "interval": matching.get("effective_recurrence") if matching else None,
            "failure_rate": matching.get("failure_rate") if matching else None,
            "empty_rate": matching.get("empty_rate") if matching else None,
        })
        backlog = queue.get("backlog")
        if isinstance(backlog, int):
            queue["runs_needed"] = round(backlog / queue["capacity"], 1)
            queue.update(_recommendation(
                config, backlog, queue["capacity"], queue["interval"], target_hours,
                matching.get("avg_duration_seconds") if matching else None,
            ))
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

    bottleneck = max(
        (queue for queue in data.get("queues", [])
         if isinstance(queue.get("backlog"), int)),
        key=lambda queue: (queue["eta_hours"]
                           if queue.get("eta_hours") is not None
                           else queue["runs_needed"]),
        default=None,
    )
    data["bottleneck"] = (
        bottleneck["id"] if bottleneck and bottleneck.get("backlog") else None)

    activity = db.pipeline_series_activity(series_ids)
    empty_runs = db.pipeline_run_metrics([], now - timedelta(hours=5))
    metrics_by_series = {}
    aggregate_runs = dict(empty_runs)
    for queue in data.get("queues", []):
        queue["last_run"] = activity.get(queue.get("series_id"))
        series_id = queue.get("series_id")
        if series_id is not None and series_id not in metrics_by_series:
            metrics = db.pipeline_run_metrics(
                [series_id], now - timedelta(hours=5))
            metrics_by_series[series_id] = metrics
            for key, value in metrics.items():
                aggregate_runs[key] = aggregate_runs.get(key, 0) + value
        queue["runs_5h"] = metrics_by_series.get(series_id, dict(empty_runs))

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
                "transitions": 0, "churn_items": 0, "queue_deltas": {},
            }
            history[key] = window
        if hours == 5:
            window["runs"] = aggregate_runs
        elif not isinstance(window.get("runs"), dict):
            window["runs"] = dict(empty_runs)

    runtime = _pipeline_runtime(matching_series, now)
    backlog_total = data.get("backlog_total")
    health = _health(
        backlog_total if isinstance(backlog_total, int) else 0,
        history, broken_series, paused_series, diagnostics, runtime,
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
        matching = _series_for_queue(config, series)
        capacity = max(1, int(config.get("capacity", 1)))
        recommendation = (_recommendation(
            config, backlog, capacity,
            matching.get("effective_recurrence") if matching else None,
            target_hours,
            matching.get("avg_duration_seconds") if matching else None,
        ) if isinstance(backlog, int) else _unknown_recommendation())
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
            "runs_needed": round(backlog / capacity, 1)
            if isinstance(backlog, int) else None,
            "membership_complete": bool(saved.get("membership_complete")),
            "age": _age_stats(members, now),
            "items": ordered_members[:priority_settings["max_items"]]
            if priority_settings else [],
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
            key=lambda queue: queue["eta_hours"] if queue.get("eta_hours") is not None
            else queue["runs_needed"],
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
        return _refresh_local_state(
            cached[1], profile, series, source=source or "memory",
            generated_at=float(cached[0]), entry_epoch=int(cached[2]),
            entry_revision=int(cached[4]), current_epoch=current_epoch,
        )
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
        return _refresh_local_state(
            result, profile, series, source="snapshot",
            generated_at=generated_at, entry_epoch=None,
            entry_revision=None, current_epoch=current_epoch,
        )
    return _refresh_local_state(
        _empty_cached_result(profile_id, profile), profile, series,
        source="none", generated_at=None, entry_epoch=None,
        entry_revision=None, current_epoch=current_epoch,
    )


def _paused_cached(profile_id: str, series: list[dict]) -> dict:
    result = read_cached(profile_id, series)
    result["cache"]["refresh_blocked"] = "worker_paused"
    return result


def analyze(profile_id: str, series: list[dict], *, use_cache: bool = True,
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
            matching = next((s for s in series
                             if item.get("series_contains", "").lower() in s["title"].lower()), None)
            if matching:
                matching_series.append(matching)
                series_ids.append(matching["id"])
                broken_series += int(bool(matching.get("broken")))
                paused_series += int(bool(matching.get("paused")))
            runs_needed = round(backlog / capacity, 1)
            recommendation = _recommendation(
                item, backlog, capacity,
                matching["effective_recurrence"] if matching else None, target_hours,
                matching.get("avg_duration_seconds") if matching else None)
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
                "interval": matching["effective_recurrence"] if matching else None,
                "failure_rate": matching["failure_rate"] if matching else None,
                "empty_rate": matching["empty_rate"] if matching else None,
                "membership_complete": membership_complete, "age": age,
                "items": ordered_members[:priority_settings["max_items"]]
                if priority_settings else [],
                "execution": _execution_status(item, matching.get("working_dir") if matching else None),
                "wake": _wake_status(profile_id, item, diagnostics),
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
            key=lambda q: q["eta_hours"] if q.get("eta_hours") is not None
            else q["runs_needed"],
            default=None,
        )
        backlog_total = sum(queue["backlog"] for queue in queues)
        activity = db.pipeline_series_activity(series_ids)
        for queue in queues:
            queue["last_run"] = activity.get(queue.get("series_id"))
            queue["runs_5h"] = db.pipeline_run_metrics(
                [queue["series_id"]] if queue.get("series_id") else [],
                now - timedelta(hours=5),
            )
        runtime = _pipeline_runtime(matching_series, now)
        if db.is_paused():
            github_rate_limit = (cached[1].get("github_rate_limit")
                                 if cached else None)
        else:
            github_rate_limit = _github_rate_limits()
        result = {
            "profile_id": profile_id, "title": profile["title"],
            "repository": profile["repository"], "queues": queues,
            "target_clear_hours": target_hours, "backlog_total": backlog_total,
            "age": _age_stats(list(all_items.values()), now),
            "history": windows,
            "health": _health(
                backlog_total, windows, broken_series, paused_series, diagnostics, runtime),
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
        published = _publish_cache(
            profile_id, profile, cache_generation, cache_epoch,
            refresh_revision, result)
        if not published:
            return read_cached(profile_id, series)
        # Raw trend history records only observations that won the same CAS as
        # the full cache. A rejected/stale writer must not poison later charts.
        db.add_pipeline_snapshot(profile_id, profile["repository"], snapshot, now)
        db.prune_pipeline_snapshots(now - timedelta(days=31))
        return _refresh_local_state(
            result, profile, series, source="live",
            generated_at=float(result["generated_at"]), entry_epoch=cache_epoch,
            entry_revision=refresh_revision, current_epoch=_cache_epoch(),
        )


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
