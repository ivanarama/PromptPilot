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

# CLI providers — name -> command template with {prompt} placeholder
# Can be overridden/extended via ~/.promptpilot/providers.json
CLAUDE_EXE = os.environ.get(
    "PP_CLAUDE_EXE",
    str(Path.home() / ".local" / "bin" / "claude.exe"),
)

BUILTIN_PROVIDERS = {
    "claude": {
        "cmd": f"{CLAUDE_EXE} -p --verbose --output-format stream-json {{prompt}}",
        "description": "Claude Code (Anthropic)",
    },
    "claude-z": {
        "cmd": f"{CLAUDE_EXE} -p --verbose --output-format stream-json {{prompt}}",
        "description": "Claude Code (GLM)",
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4.7",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-4.7",
        },
    },
    "codex": {
        "cmd": "codex -q {prompt}",
        "description": "OpenAI Codex",
    },
    "qwen": {
        "cmd": "qwen -p {prompt}",
        "description": "Qwen Code",
    },
}


def _providers_file() -> Path:
    return DB_DIR / "providers.json"


def load_providers() -> dict:
    """Load providers: built-in + user overrides from providers.json."""
    providers = dict(BUILTIN_PROVIDERS)
    user_file = _providers_file()
    if user_file.exists():
        try:
            with open(user_file) as f:
                custom = json.load(f)
            providers.update(custom)
        except (json.JSONDecodeError, OSError):
            pass
    return providers


def save_provider(name: str, cmd: str, description: str = "", env: dict = None):
    """Save a custom provider to providers.json."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    user_file = _providers_file()
    custom = {}
    if user_file.exists():
        try:
            with open(user_file) as f:
                custom = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    entry = {"cmd": cmd, "description": description}
    if env:
        entry["env"] = env
    custom[name] = entry
    with open(user_file, "w") as f:
        json.dump(custom, f, indent=2)


def remove_provider(name: str) -> bool:
    """Remove a custom provider from providers.json."""
    user_file = _providers_file()
    if not user_file.exists():
        return False
    try:
        with open(user_file) as f:
            custom = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    if name not in custom:
        return False
    del custom[name]
    with open(user_file, "w") as f:
        json.dump(custom, f, indent=2)
    return True


def build_cmd(provider: str, prompt: str) -> list[str]:
    """Build the full command list for a provider + prompt."""
    providers = load_providers()
    if provider in providers:
        template = providers[provider]["cmd"]
    else:
        template = f"{provider} {{prompt}}"
    marker = "\x00PROMPT\x00"
    parts = template.replace("{prompt}", marker).split()
    return [prompt if p == marker else p for p in parts]


def get_provider_env(provider: str) -> dict:
    """Get extra environment variables for a provider (merged with current env)."""
    providers = load_providers()
    extra = providers.get(provider, {}).get("env", {})
    if not extra:
        return os.environ.copy()
    env = os.environ.copy()
    env.update(extra)
    return env


# Server
HOST = os.environ.get("PP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PP_PORT", "8420"))
