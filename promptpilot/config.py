"""Configuration settings."""

import json
import os
from pathlib import Path

# Database
DB_DIR = Path(os.environ.get("PP_DATA_DIR", Path.home() / ".promptpilot"))
DB_PATH = DB_DIR / "promptpilot.db"

# Worker
POLL_INTERVAL = int(os.environ.get("PP_POLL_INTERVAL", "5"))
TASK_TIMEOUT = int(os.environ.get("PP_TASK_TIMEOUT", "300"))
BASE_DELAY = int(os.environ.get("PP_BASE_DELAY", "60"))
MAX_DELAY = int(os.environ.get("PP_MAX_DELAY", "3600"))
MAX_RETRIES = int(os.environ.get("PP_MAX_RETRIES", "5"))

# Default CLI command
DEFAULT_CLI = os.environ.get("PP_DEFAULT_CLI", "claude")

# CLI presets — name -> command + args
# Can be overridden via ~/.promptpilot/providers.json
BUILTIN_PROVIDERS = {
    "claude": {
        "cmd": ["claude", "-p", "--output-format", "json"],
        "description": "Claude Code (Anthropic)",
    },
    "claude-z": {
        "cmd": ["claude-z", "-p", "--output-format", "json"],
        "description": "Claude Code (GLM)",
    },
}


def load_providers() -> dict:
    """Load providers: built-in + user overrides from providers.json."""
    providers = dict(BUILTIN_PROVIDERS)
    user_file = DB_DIR / "providers.json"
    if user_file.exists():
        try:
            with open(user_file) as f:
                custom = json.load(f)
            providers.update(custom)
        except (json.JSONDecodeError, OSError):
            pass
    return providers


def get_provider_cmd(provider: str) -> list[str]:
    """Get command list for a provider name. Falls back to treating it as a raw command."""
    providers = load_providers()
    if provider in providers:
        return list(providers[provider]["cmd"])
    # If not a known preset, treat as a raw command name
    return [provider, "-p", "--output-format", "json"]


# Server
HOST = os.environ.get("PP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PP_PORT", "8420"))
