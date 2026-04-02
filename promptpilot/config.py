"""Configuration settings."""

import json
import os
import sys
from pathlib import Path


def _load_dotenv():
    """Load .env file into os.environ (only for keys not already set).

    Search order:
      1. Directory of pp.exe  (when running as PyInstaller bundle)
      2. Current working directory
      3. ~/.promptpilot/.env  (permanent user config)
    """
    candidates = []

    if getattr(sys, "frozen", False):
        # Running as pp.exe — look next to the binary first
        candidates.append(Path(sys.executable).parent / ".env")

    candidates.append(Path.cwd() / ".env")
    candidates.append(Path.home() / ".promptpilot" / ".env")

    for env_file in candidates:
        if env_file.exists():
            try:
                with open(env_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            key, _, value = line.partition("=")
                            key = key.strip()
                            value = value.strip().strip('"').strip("'")
                            if key and key not in os.environ:
                                os.environ[key] = value
            except OSError:
                pass
            break  # use the first .env found


# Load .env BEFORE reading any os.environ values
_load_dotenv()

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
        "supports_skills": True,
    },
    "claude-z": {
        "cmd": f"{CLAUDE_EXE} -p --verbose --output-format stream-json {{prompt}}",
        "description": "Claude Code (GLM)",
        "supports_skills": True,
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4.7",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-4.7",
        },
    },
    "codex": {
        "cmd": "codex exec {prompt}",
        "description": "OpenAI Codex",
        "supports_skills": False,
    },
    "qwen": {
        "cmd": "qwen -p {prompt}",
        "description": "Qwen Code",
        "supports_skills": False,
    },
    "cursor": {
        "cmd": "cursor-agent --print --output-format text -f {prompt}",
        "description": "Cursor Agent",
        "supports_skills": False,
        "env": {
            "CURSOR_API_KEY": os.environ.get("CURSOR_API_KEY", ""),
            # Ensure rg (ripgrep) is on PATH — required by cursor-agent
            "PATH": os.environ.get("PATH", ""),
        },
    },
}


def _providers_file() -> Path:
    return DB_DIR / "providers.json"


def load_providers() -> dict:
    """Load providers: built-in + user overrides from providers.json.

    When a custom provider overrides a built-in one, it inherits supports_skills
    from the built-in if not explicitly set (so claude-z stays skill-capable even
    if the custom entry in providers.json doesn't repeat the flag).
    """
    providers = dict(BUILTIN_PROVIDERS)
    user_file = _providers_file()
    if user_file.exists():
        try:
            with open(user_file) as f:
                custom = json.load(f)
            for name, info in custom.items():
                if name in providers and "supports_skills" not in info:
                    info = dict(info)
                    info["supports_skills"] = providers[name].get("supports_skills", False)
                providers[name] = info
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


def build_cmd(provider: str, prompt: str, skip_permissions: bool = False, session_id: str = None, model: str = None):
    """Build the full command list for a provider + prompt."""
    providers = load_providers()
    if provider in providers:
        template = providers[provider]["cmd"]
    else:
        template = f"{provider} {{prompt}}"
    marker = "\x00PROMPT\x00"
    parts = template.replace("{prompt}", marker).split()
    cmd = [prompt if p == marker else p for p in parts]
    # Insert extra flags before the prompt argument
    extras = []
    if model:
        extras += ["--model", model]
    if session_id:
        extras += ["--resume", session_id]
    if skip_permissions:
        extras.append("--dangerously-skip-permissions")
    if extras:
        prompt_idx = cmd.index(prompt)
        cmd[prompt_idx:prompt_idx] = extras
    return cmd


def get_provider_env(provider: str) -> dict:
    """Get extra environment variables for a provider (merged with current env)."""
    providers = load_providers()
    extra = providers.get(provider, {}).get("env", {})
    if not extra:
        return os.environ.copy()
    env = os.environ.copy()
    env.update(extra)
    return env


def _parse_frontmatter(path: Path) -> dict:
    """Parse YAML-style frontmatter (---...---) from a markdown file."""
    try:
        content = path.read_text(encoding="utf-8-sig")  # utf-8-sig handles BOM
    except (UnicodeDecodeError, OSError):
        try:
            content = path.read_text(encoding="cp1251")
        except (UnicodeDecodeError, OSError):
            return {}
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    result = {}
    for line in content[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def get_skills(working_dir: str = None) -> list:
    """Return list of available Claude Code skills from ~/.claude/commands/ and plugins.

    Each item: {name, description, argument_hint, source}
    Skills are invoked as /skill-name [args] when passed as a task prompt to Claude Code.
    Only relevant for providers with supports_skills=True (claude, claude-z).
    """
    skills = []
    seen = set()

    def _add_from_dir(dir_path: Path, source: str):
        """Scan dir_path for skill definitions in two layouts:
        - Flat:  <dir>/<skill-name>.md        (name = file stem)
        - Subdir: <dir>/<skill-name>/<any>.md  (name = subdirectory name)
        """
        if not dir_path.is_dir():
            return
        # Flat .md files directly in the directory
        for cmd_file in sorted(dir_path.glob("*.md")):
            if cmd_file.name.lower() == "readme.md":
                continue
            name = cmd_file.stem
            if name.upper() == "SKILL":
                continue  # this is a subdir-style file, skip here
            if name in seen:
                continue
            seen.add(name)
            fm = _parse_frontmatter(cmd_file)
            skills.append({
                "name": name,
                "description": fm.get("description", ""),
                "argument_hint": fm.get("argument-hint", ""),
                "source": source,
            })
        # Subdir-style: <dir>/<skill-name>/*.md (Claude uses directory name as skill name)
        for sub in sorted(dir_path.iterdir()):
            if not sub.is_dir():
                continue
            md_files = sorted(sub.glob("*.md"))
            if not md_files:
                continue
            name = sub.name
            if name in seen:
                continue
            seen.add(name)
            fm = _parse_frontmatter(md_files[0])
            skills.append({
                "name": name,
                "description": fm.get("description", ""),
                "argument_hint": fm.get("argument-hint", ""),
                "source": source,
            })

    # Global user commands/skills (~/.claude/commands/ and ~/.claude/skills/)
    _add_from_dir(Path.home() / ".claude" / "commands", "user")
    _add_from_dir(Path.home() / ".claude" / "skills", "user")

    # Plugin commands — scan all directories named "commands" under ~/.claude/plugins/
    plugins_dir = Path.home() / ".claude" / "plugins"
    if plugins_dir.is_dir():
        for cmd_dir in sorted(plugins_dir.rglob("commands")):
            if cmd_dir.is_dir():
                plugin_name = cmd_dir.parent.name
                _add_from_dir(cmd_dir, f"plugin:{plugin_name}")

    # Project-local commands/skills
    if working_dir:
        _add_from_dir(Path(working_dir) / ".claude" / "commands", "local")
        _add_from_dir(Path(working_dir) / ".claude" / "skills", "local")

    return skills


# Projects root — optional directory whose subdirectories are offered as project choices
PROJECTS_ROOT = os.environ.get("PP_PROJECTS_ROOT", "")

# Optional password required to create tasks via Telegram bot
TASK_PASSWORD = os.environ.get("PP_TASK_PASSWORD", "")

# Server
HOST = os.environ.get("PP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PP_PORT", "8420"))
