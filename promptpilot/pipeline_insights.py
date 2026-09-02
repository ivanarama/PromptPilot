"""Profile-driven, token-free diagnostics for external GitHub pipelines."""

import json
import math
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import DB_DIR, POLL_INTERVAL, TASK_TIMEOUT


DEFAULT_PROFILES: dict = {}

_cache = {}
_locks: dict[str, threading.Lock] = {}
_INTERVAL_PRESETS = ((0.25, "15m"), (0.5, "30m"), (1, "1h"), (2, "2h"),
                     (4, "4h"), (8, "8h"), (12, "12h"), (24, "24h"))
_HISTORY_WINDOWS = (5, 24)


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

    payload = fetch_page(1)
    total = int(payload.get("total_count", len(payload.get("items", []))))
    raw_items = list(payload.get("items", []))
    # GitHub Search exposes at most 1000 matches. Fetching all exposed pages keeps
    # movement metrics exact for ordinary queues instead of silently sampling 100.
    exposed_total = min(total, 1000)
    for page in range(2, math.ceil(exposed_total / 100) + 1):
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
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
            "url": item.get("html_url"),
        })
    return {"count": total, "items": items, "membership_complete": total <= len(items)}


def _github_count(repository: str, query: str) -> int:
    """Compatibility helper for callers that only need the current count."""
    return _github_search(repository, query)["count"]


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
    if paused_series:
        return {"state": "yellow", "label": "конвейер на паузе",
                "reason": f"приостановлено серий: {paused_series}"}
    if diagnostics and diagnostics.get("state") == "yellow":
        return {"state": "yellow", "label": "есть ожидания",
                "reason": diagnostics.get("summary", "health-check требует внимания")}
    recent = windows.get("5h", {})
    runs = recent.get("runs", {})
    if runs.get("failed", 0) or runs.get("unable", 0):
        return {"state": "red", "label": "прогон не отработал",
                "reason": f"упало: {runs.get('failed', 0)}; НЕ СМОГ: {runs.get('unable', 0)}"}
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
    titles = [item.get("title", "").lower() for item in series if not item.get("ended")]
    return any(queue.get("series_contains", "").lower() in title
               for queue in profile.get("queues", []) for title in titles
               if queue.get("series_contains"))


def analyze(profile_id: str, series: list[dict], *, use_cache: bool = True) -> dict:
    profiles = _profiles()
    if profile_id not in profiles:
        raise KeyError(profile_id)
    cached = _cache.get(profile_id)
    if use_cache and cached and time.time() - cached[0] < 300:
        return cached[1]

    lock = _locks.setdefault(profile_id, threading.Lock())
    with lock:
        cached = _cache.get(profile_id)
        if use_cache and cached and time.time() - cached[0] < 300:
            return cached[1]
        profile = profiles[profile_id]
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
            searches = [_github_search(profile["repository"], query) for query in queries]
            backlog = sum(search["count"] for search in searches)
            members = {}
            for search in searches:
                for member in search["items"]:
                    members[member["key"]] = member
                    all_items[member["key"]] = member
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
                **recommendation,
            })
            snapshot_queues[item["id"]] = {
                "backlog": backlog, "items": list(members.values()),
                "membership_complete": membership_complete,
            }

        snapshot = {"captured_at": now.isoformat(), "queues": snapshot_queues}
        db.add_pipeline_snapshot(profile_id, profile["repository"], snapshot, now)
        db.prune_pipeline_snapshots(now - timedelta(days=31))
        history_rows = db.list_pipeline_snapshots(
            profile_id, since=now - timedelta(hours=max(_HISTORY_WINDOWS) + 1), limit=10000)
        windows = {f"{hours}h": _window_metrics(
            history_rows, snapshot, series_ids, now, hours) for hours in _HISTORY_WINDOWS}

        bottleneck = max(
            queues,
            key=lambda q: q["eta_hours"] if q.get("eta_hours") is not None
            else q["runs_needed"],
            default=None,
        )
        backlog_total = sum(queue["backlog"] for queue in queues)
        diagnostics = _run_profile_health_check(profile)
        runtime = _pipeline_runtime(matching_series, now)
        result = {
            "profile_id": profile_id, "title": profile["title"],
            "repository": profile["repository"], "queues": queues,
            "target_clear_hours": target_hours, "backlog_total": backlog_total,
            "age": _age_stats(list(all_items.values()), now),
            "history": windows,
            "health": _health(
                backlog_total, windows, broken_series, paused_series, diagnostics, runtime),
            "diagnostics": diagnostics,
            "runtime": runtime,
            "bottleneck": bottleneck["id"] if bottleneck and bottleneck["backlog"] else None,
            "generated_at": now.timestamp(),
        }
        _cache[profile_id] = (time.time(), result)
        return result


def sample_active_profiles(series: list[dict]) -> dict[str, str]:
    """Refresh every configured pipeline that has a matching live series."""
    outcomes = {}
    for profile_id, profile in _profiles().items():
        if not _profile_active(profile, series):
            continue
        try:
            analyze(profile_id, series, use_cache=False)
            outcomes[profile_id] = "ok"
        except Exception as exc:  # one external repository must not stop the sampler
            outcomes[profile_id] = str(exc)
    return outcomes
