"""Config-driven backlog/capacity diagnostics for external work queues."""

import json
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
        "queues": [
            {
                "id": "triage", "title": "Триаж заявок", "capacity": 5,
                "query": "is:issue is:open -label:needs-decision -label:ready-fix -label:approved -label:in-work -label:hold -label:manual",
                "series_contains": "TRIAGE",
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


def analyze(profile_id: str, series: list[dict], *, use_cache: bool = True) -> dict:
    profiles = _profiles()
    if profile_id not in profiles:
        raise KeyError(profile_id)
    cached = _cache.get(profile_id)
    if use_cache and cached and time.time() - cached[0] < 300:
        return cached[1]
    profile = profiles[profile_id]
    queues = []
    for item in profile["queues"]:
        backlog = _github_count(profile["repository"], item["query"])
        capacity = max(1, int(item.get("capacity", 1)))
        matching = next((s for s in series
                         if item.get("series_contains", "").lower() in s["title"].lower()), None)
        runs_needed = round(backlog / capacity, 1)
        queues.append({
            "id": item["id"], "title": item["title"], "backlog": backlog,
            "capacity": capacity, "runs_needed": runs_needed,
            "series_id": matching["id"] if matching else None,
            "interval": matching["effective_recurrence"] if matching else None,
            "failure_rate": matching["failure_rate"] if matching else None,
            "empty_rate": matching["empty_rate"] if matching else None,
        })
    bottleneck = max(queues, key=lambda q: q["runs_needed"], default=None)
    result = {
        "profile_id": profile_id, "title": profile["title"],
        "repository": profile["repository"], "queues": queues,
        "bottleneck": bottleneck["id"] if bottleneck and bottleneck["backlog"] else None,
        "generated_at": time.time(),
    }
    _cache[profile_id] = (time.time(), result)
    return result
