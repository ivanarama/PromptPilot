"""Configuration settings."""

import json
import os
import sys
from pathlib import Path


def _load_dotenv():
    """Load .env file into os.environ (only for keys not already set).

    Search order:
      1. Directory of pp.exe  (when running as PyInstaller bundle)
      2. Parent of pp.exe directory (e.g. project root when exe is in dist/)
      3. Current working directory
      4. ~/.promptpilot/.env  (permanent user config)
    """
    candidates = []

    if getattr(sys, "frozen", False):
        # Running as pp.exe — look next to the binary first, then one level up
        exe_dir = Path(sys.executable).parent
        candidates.append(exe_dir / ".env")
        candidates.append(exe_dir.parent / ".env")

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
TASK_TIMEOUT = int(os.environ.get("PP_TASK_TIMEOUT", "0"))
BASE_DELAY = int(os.environ.get("PP_BASE_DELAY", "60"))
MAX_DELAY = int(os.environ.get("PP_MAX_DELAY", "3600"))
MAX_RETRIES = int(os.environ.get("PP_MAX_RETRIES", "5"))

# Default CLI command
DEFAULT_CLI = os.environ.get("PP_DEFAULT_CLI", "claude")

# CLI providers — name -> command template with {prompt} placeholder
# Can be overridden/extended via ~/.promptpilot/providers.json
def _default_claude_exe() -> str:
    """OS-aware default path to the Claude Code CLI binary."""
    if sys.platform == "win32":
        return str(Path.home() / ".local" / "bin" / "claude.exe")
    return str(Path.home() / ".local" / "bin" / "claude")


CLAUDE_EXE = os.environ.get("PP_CLAUDE_EXE", _default_claude_exe())

def _cursor_agent_cmd() -> str:
    """Return command to invoke cursor-agent.

    On Windows the npm runner (runner.mjs) spawns the vendor .cmd file without
    shell:true and gets EINVAL.  We bypass it by calling the vendor node.exe +
    index.js directly.  Falls back to 'cursor-agent' if vendor not found.
    """
    if sys.platform != "win32":
        return "cursor-agent"
    try:
        sdk_root = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@nothumanwork" / "cursor-agents-sdk"
        manifest = json.loads((sdk_root / "vendor" / "manifest.json").read_text())
        vendor_dir = (sdk_root / manifest["path"]).parent
        node_exe = vendor_dir / "node.exe"
        index_js = vendor_dir / "index.js"
        if node_exe.exists() and index_js.exists():
            return f"{node_exe} {index_js}"
    except Exception:
        pass
    return "cursor-agent"


def _find_rg_dir() -> str:
    """Find ripgrep (rg) directory from Windows registry PATH if not in current PATH."""
    import shutil
    import subprocess as _sp
    if shutil.which("rg"):
        return ""
    if sys.platform != "win32":
        return ""
    try:
        result = _sp.run(
            ["powershell", "-Command", '[System.Environment]::GetEnvironmentVariable("PATH","User")'],
            capture_output=True, text=True, timeout=5,
        )
        for entry in result.stdout.strip().split(";"):
            entry = entry.strip()
            if entry and Path(entry, "rg.exe").exists():
                return entry
    except Exception:
        pass
    return ""


def _find_opencode() -> str:
    """Find opencode CLI — resolves full path to avoid PATH issues in worker."""
    import shutil
    resolved = shutil.which("opencode")
    if resolved:
        return resolved
    if sys.platform == "win32":
        # Windows npm global bin locations with .cmd/.bat wrappers
        candidates = [
            Path.home() / "AppData" / "Roaming" / "npm",
        ]
        for npm_bin in candidates:
            for ext in (".CMD", ".cmd", ""):
                candidate = npm_bin / f"opencode{ext}"
                if candidate.exists():
                    return str(candidate)
    else:
        # Unix npm/system bin locations (no extension)
        candidates = [
            Path("/usr/local/bin"),                # system npm
            Path("/usr/bin"),
            Path.home() / ".local" / "bin",        # generic
            Path.home() / ".npm-global" / "bin",   # user npm
        ]
        for npm_bin in candidates:
            candidate = npm_bin / "opencode"
            if candidate.exists():
                return str(candidate)
    return "opencode"


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
        "cmd": f"{_cursor_agent_cmd()} --print --output-format text -f {{prompt}}",
        "description": "Cursor Agent",
        "supports_skills": False,
        "env": {
            "CURSOR_API_KEY": os.environ.get("CURSOR_API_KEY", ""),
            "PATH": os.pathsep.join(filter(None, [_find_rg_dir(), os.environ.get("PATH", "")])),
        },
    },
    "opencode": {
        "cmd": f"{_find_opencode()} run {{prompt}}",
        "description": "OpenCode AI",
        "supports_skills": False,
        "models": [
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "openai/gpt-5",
            "openai/gpt-5-mini",
            "openai/o1",
            "openai/o1-mini",
            "openai/o3",
            "openai/o3-mini",
            "opencode/big-pickle",
            "opencode/minimax-m2.5-free",
            "opencode/nemotron-3-super-free",
        ],
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


def load_providers_detailed() -> dict:
    """Load providers with source tracking.

    Each provider entry gets extra fields:
      _source: "builtin", "providers.json", or "builtin + providers.json"
      _source_path: path to providers.json (if applicable)
    """
    providers = {}
    builtin_names = set(BUILTIN_PROVIDERS)

    for name, info in BUILTIN_PROVIDERS.items():
        entry = dict(info)
        entry["_source"] = "builtin"
        entry["_source_path"] = None
        providers[name] = entry

    user_file = _providers_file()
    custom_names = set()
    if user_file.exists():
        try:
            with open(user_file) as f:
                custom = json.load(f)
            for name, info in custom.items():
                custom_names.add(name)
                entry = dict(info)
                if name in providers:
                    if "supports_skills" not in entry:
                        entry["supports_skills"] = providers[name].get("supports_skills", False)
                    entry["_source"] = "builtin + providers.json"
                else:
                    entry["_source"] = "providers.json"
                entry["_source_path"] = str(user_file)
                providers[name] = entry
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
    # Skip empty values — don't override existing env vars with empty strings
    env.update({k: v for k, v in extra.items() if v})
    return env


# ── Dynamic model discovery ────────────────────────────────────────────────
# Fetch the actual available models from a provider's /models endpoint (e.g. a
# LiteLLM proxy) instead of hardcoding the sonnet/opus/haiku tiers. Claude Code
# is then invoked with `--model <real-id>` and routes the request correctly.

MODELS_URL = os.environ.get("PP_MODELS_URL", "").strip()
MODELS_TOKEN = os.environ.get("PP_MODELS_TOKEN", "").strip()

import time as _time
import urllib.request as _ureq

_MODELS_CACHE = {"ts": 0.0, "data": None}
_MODELS_TTL = 300  # seconds
_CLAUDE_TIERS = ["sonnet", "opus", "haiku"]


def _resolve_models_url() -> str:
    """Return the URL to query for available models.

    Priority: explicit PP_MODELS_URL > derived from ANTHROPIC_BASE_URL.
    A LiteLLM proxy serves the OpenAI-compatible list at /v1/models on its root,
    while ANTHROPIC_BASE_URL usually points at the /anthropic passthrough route.
    """
    if MODELS_URL:
        return MODELS_URL
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if not base:
        return ""
    root = base.rstrip("/")
    for suffix in ("/anthropic", "/v1"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    return root + "/v1/models"


def fetch_available_models():
    """Fetch available model IDs from the provider's /models endpoint.

    Returns a sorted list of model id strings, or None when no endpoint is
    configured or the request fails (caller then falls back to the tiers).
    Cached for _MODELS_TTL seconds; stale cache is served on transient errors.
    """
    url = _resolve_models_url()
    if not url:
        return None
    now = _time.time()
    if _MODELS_CACHE["data"] is not None and now - _MODELS_CACHE["ts"] < _MODELS_TTL:
        return _MODELS_CACHE["data"]
    token = MODELS_TOKEN or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    try:
        req = _ureq.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with _ureq.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        models = sorted({m["id"] for m in payload.get("data", []) if m.get("id")})
        if not models:
            return _MODELS_CACHE["data"]
        _MODELS_CACHE["data"] = models
        _MODELS_CACHE["ts"] = now
        return models
    except (ValueError, OSError):
        return _MODELS_CACHE["data"]


def get_provider_models(provider: str) -> list:
    """Return the model list for a provider.

    For Claude-type providers (supports_skills=True) with a configured models
    endpoint, returns the dynamically fetched list. Otherwise falls back to the
    provider's own `models` field or the sonnet/opus/haiku tiers.
    """
    providers = load_providers()
    info = providers.get(provider, {})
    if "models" in info:
        return list(info["models"])
    if info.get("supports_skills", False):
        dynamic = fetch_available_models()
        if dynamic:
            return dynamic
        return list(_CLAUDE_TIERS)
    return []


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

# Optional shared secret for the HTTP API. When set, all /api/* requests must
# include `Authorization: Bearer <token>`. Leave empty to disable (loopback only).
API_TOKEN = os.environ.get("PP_API_TOKEN", "")


# ── Outbound proxy (Telegram bot) ──────────────────────────────────────────
# Telegram API endpoints are often blocked in corporate networks; route the bot
# through an HTTP(S) or SOCKS5 proxy. Note: Docker does NOT inherit host env
# vars, so the proxy must be passed into the container explicitly (PP_TG_PROXY
# or the standard *_proxy names — see docker-compose.yml).

_PROXY_KEYS = (
    "PP_TG_PROXY",          # explicit override (highest priority)
    "https_proxy", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
    "http_proxy", "HTTP_PROXY",
)


def get_proxy_url() -> str:
    """Return a proxy URL for the Telegram bot, or '' when none is configured."""
    for key in _PROXY_KEYS:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""
