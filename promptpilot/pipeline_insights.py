"""Config-driven backlog/capacity diagnostics for external work queues."""

import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import DB_DIR


DEFAULT_PROFILES = {
    "onebase": {
        "title": "OneBase: GitHub-конвейер",
        "repository": "ivanarama/onebase",
        "target_clear_hours": 8,
        "queues": [
            {
                "id": "triage", "title": "Триаж заявок", "capacity": 5,
                "query": "is:issue is:open -label:needs-decision -label:ready-fix -label:approved -label:in-work -label:hold -label:manual",
                "series_contains": "TRIAGE",
                "manual_gate": True,
            },
            {
                "id": "fix", "title": "Исправления", "capacity": 1,
                "query": "is:issue is:open label:ready-fix -label:in-work -label:hold -label:manual",
                "series_contains": "FIX",
            },
            {
                "id": "review", "title": "Ревью PR", "capacity": 2,
                "query": "is:pr is:open -label:reviewed -label:changes-requested",
                "series_contains": "REVIEW",
            },
            {
                "id": "merge", "title": "Слияние", "capacity": 3,
                "query": "is:pr is:open label:ship",
                "series_contains": "MERGE",
            },
        ],
    }
}

_cache = {}
_INTERVAL_PRESETS = ((0.25, "15m"), (0.5, "30m"), (1, "1h"), (2, "2h"),
                     (4, "4h"), (8, "8h"), (12, "12h"), (24, "24h"))


def _profiles() -> dict:
    """Built-ins plus optional user overrides in ~/.promptpilot/pipeline_profiles.json."""
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


def _github_count(repository: str, query: str) -> int:
    command = [_gh_executable(), "api", "search/issues", "--method", "GET",
               "--field", f"q=repo:{repository} {query}", "--jq", ".total_count"]
    run = subprocess.run(command, capture_output=True, text=True, timeout=30,
                         encoding="utf-8", errors="replace")
    if run.returncode:
        raise RuntimeError((run.stderr or run.stdout or "gh api завершился с ошибкой").strip())
    return int(run.stdout.strip())


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
                    current_interval: str | None, target_hours: float) -> dict:
    runs_needed = backlog / capacity if backlog else 0
    current_hours = _interval_hours(current_interval)
    eta = round(runs_needed * current_hours, 1) if current_hours is not None else None
    if not backlog:
        return {"recommended_interval": None, "eta_hours": 0,
                "recommendation": "очередь пуста — можно оставить базовый интервал"}
    if item.get("manual_gate"):
        return {"recommended_interval": None, "eta_hours": eta,
                "recommendation": "не ускорять автоматически: этап зависит от решения человека"}
    required = target_hours / runs_needed
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
            "recommendation": message}


def analyze(profile_id: str, series: list[dict], *, use_cache: bool = True) -> dict:
    profiles = _profiles()
    if profile_id not in profiles:
        raise KeyError(profile_id)
    cached = _cache.get(profile_id)
    if use_cache and cached and time.time() - cached[0] < 300:
        return cached[1]
    profile = profiles[profile_id]
    target_hours = float(profile.get("target_clear_hours", 8))
    queues = []
    for item in profile["queues"]:
        backlog = _github_count(profile["repository"], item["query"])
        capacity = max(1, int(item.get("capacity", 1)))
        matching = next((s for s in series
                         if item.get("series_contains", "").lower() in s["title"].lower()), None)
        runs_needed = round(backlog / capacity, 1)
        recommendation = _recommendation(item, backlog, capacity,
                                         matching["effective_recurrence"] if matching else None,
                                         target_hours)
        queues.append({
            "id": item["id"], "title": item["title"], "backlog": backlog,
            "capacity": capacity, "runs_needed": runs_needed,
            "series_id": matching["id"] if matching else None,
            "interval": matching["effective_recurrence"] if matching else None,
            "failure_rate": matching["failure_rate"] if matching else None,
            "empty_rate": matching["empty_rate"] if matching else None,
            **recommendation,
        })
    bottleneck = max(queues, key=lambda q: q["runs_needed"], default=None)
    result = {
        "profile_id": profile_id, "title": profile["title"],
        "repository": profile["repository"], "queues": queues,
        "target_clear_hours": target_hours,
        "bottleneck": bottleneck["id"] if bottleneck and bottleneck["backlog"] else None,
        "generated_at": time.time(),
    }
    _cache[profile_id] = (time.time(), result)
    return result
