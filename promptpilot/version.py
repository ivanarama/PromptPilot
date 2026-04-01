"""Version and update checking."""

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.2.0"

_RELEASES_URL = "https://api.github.com/repos/ivanarama/PromptPilot/releases/latest"
_CACHE_FILE = Path.home() / ".promptpilot" / "version-check.json"
_CACHE_HOURS = 24


def _compare(a: str, b: str) -> int:
    """Returns -1 if a < b, 0 if equal, 1 if a > b."""
    try:
        at = tuple(int(x) for x in a.split(".")[:3])
        bt = tuple(int(x) for x in b.split(".")[:3])
        if at < bt:
            return -1
        if at > bt:
            return 1
        return 0
    except (ValueError, AttributeError):
        return 0


def check_for_update() -> dict:
    """Return update info dict. Uses 24h file cache."""
    base = {"current": __version__, "latest": None, "update_available": False}

    # Try cache first
    try:
        if _CACHE_FILE.exists():
            cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            checked_at = datetime.fromisoformat(cached.get("checked_at", "2000-01-01T00:00:00+00:00"))
            if datetime.now(timezone.utc) - checked_at < timedelta(hours=_CACHE_HOURS):
                return {**base, **cached}
    except Exception:
        pass

    # Fetch from GitHub
    try:
        req = urllib.request.Request(
            _RELEASES_URL,
            headers={"User-Agent": "PromptPilot", "Accept": "application/vnd.github.v3+json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        latest = data.get("tag_name", "").lstrip("v")
        update_available = bool(latest) and _compare(__version__, latest) < 0
        result = {
            **base,
            "latest": latest,
            "update_available": update_available,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(result), encoding="utf-8")
        return result
    except Exception as e:
        return {**base, "error": str(e)}
