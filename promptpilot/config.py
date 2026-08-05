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
def _find_claude() -> str:
    """Find Claude Code CLI — claude on POSIX, claude.exe on Windows."""
    import shutil
    resolved = shutil.which("claude")
    if resolved:
        return resolved
    for name in ("claude.exe", "claude"):
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.exists():
            return str(candidate)
    return "claude"


CLAUDE_EXE = os.environ.get("PP_CLAUDE_EXE", _find_claude())

def _cursor_agent_cmd() -> str:
    """Return command to invoke cursor-agent.

    On Windows the npm runner (runner.mjs) spawns the vendor .cmd file without
    shell:true and gets EINVAL.  We bypass it by calling the vendor node.exe +
    index.js directly.  Falls back to 'cursor-agent' if vendor not found.
    """
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
    # Fallback: common npm global bin locations
    candidates = [
        Path.home() / ".opencode" / "bin",                      # official install script
        Path.home() / "AppData" / "Roaming" / "npm",          # Windows
        Path("/usr/local/bin"),                                  # macOS / Linux (system npm)
        Path.home() / ".npm-global" / "bin",                    # Linux (user npm)
        Path.home() / ".local" / "bin",                         # generic
    ]
    for npm_bin in candidates:
        for ext in (".CMD", ".cmd", ""):
            candidate = npm_bin / f"opencode{ext}"
            if candidate.exists():
                return str(candidate)
    return "opencode"


BUILTIN_PROVIDERS = {
    "herdr-session": {
        "executor": "herdr",
        "session_target": True,
        "description": "Промпт в открытую сессию herdr",
    },
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

    A custom entry for a built-in name is MERGED over the built-in one, so
    partial overrides work (e.g. {"hidden": true} keeps the built-in cmd).
    """
    providers = dict(BUILTIN_PROVIDERS)
    user_file = _providers_file()
    if user_file.exists():
        try:
            with open(user_file) as f:
                custom = json.load(f)
            for name, info in custom.items():
                if name in providers:
                    providers[name] = {**providers[name], **info}
                else:
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


def _load_custom_providers() -> dict:
    user_file = _providers_file()
    if user_file.exists():
        try:
            with open(user_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_custom_providers(custom: dict):
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with open(_providers_file(), "w") as f:
        json.dump(custom, f, ensure_ascii=False, indent=2)


def save_provider(name: str, cmd: str = None, description: str = "", env: dict = None,
                  executor: str = None, kind: str = None, keep_pane: bool = False,
                  models: list = None, args: list = None):
    """Save a custom provider (cmd-template or executor-based) to providers.json."""
    custom = _load_custom_providers()
    entry = {"description": description}
    if models:
        entry["models"] = models
    if args:
        entry["args"] = args
    if executor:
        entry["executor"] = executor
        entry["kind"] = kind or "claude"
        if keep_pane:
            entry["keep_pane"] = True
        if kind in (None, "claude"):
            entry["supports_skills"] = True
    else:
        entry["cmd"] = cmd
    if env:
        entry["env"] = env
    custom[name] = entry
    _write_custom_providers(custom)


def set_provider_hidden(name: str, hidden: bool) -> bool:
    """Hide/unhide a provider in pickers (partial override, keeps built-in cmd)."""
    if name not in load_providers():
        return False
    custom = _load_custom_providers()
    entry = custom.get(name, {})
    if hidden:
        entry["hidden"] = True
    else:
        entry.pop("hidden", None)
    if entry:
        custom[name] = entry
    else:
        custom.pop(name, None)
    _write_custom_providers(custom)
    return True


def provider_available(info: dict) -> bool:
    """True when the provider's executable is present on this machine."""
    import shutil
    if info.get("executor") == "herdr":
        return shutil.which(HERDR_BIN) is not None
    parts = (info.get("cmd") or "").split()
    if not parts:
        return False
    return shutil.which(parts[0]) is not None or Path(parts[0]).exists()


def pickable_providers() -> dict:
    """Providers to offer in UI/bot pickers: installed and not hidden."""
    return {
        name: info for name, info in load_providers().items()
        if not info.get("hidden") and provider_available(info)
    }


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


# herdr executor (providers with "executor": "herdr" in providers.json)
HERDR_BIN = os.environ.get("PP_HERDR_BIN", "herdr")
HERDR_READ_LINES = int(os.environ.get("PP_HERDR_READ_LINES", "300"))
HERDR_START_TIMEOUT_MS = int(os.environ.get("PP_HERDR_START_TIMEOUT_MS", "60000"))
# Keep the pane open after a successful task (also per-provider "keep_pane")
HERDR_KEEP_PANE = os.environ.get("PP_HERDR_KEEP_PANE", "0") == "1"

# herdr → Telegram bridge: the bot watches ALL herdr agents (not only
# PromptPilot tasks) and notifies when one is blocked or finishes unseen.
HERDR_WATCH = os.environ.get("PP_HERDR_WATCH", "1") == "1"
HERDR_WATCH_INTERVAL = int(os.environ.get("PP_HERDR_WATCH_INTERVAL", "10"))

# Projects root — optional directory whose subdirectories are offered as project choices
PROJECTS_ROOT = os.environ.get("PP_PROJECTS_ROOT", "")

# Optional password required to create tasks via Telegram bot
TASK_PASSWORD = os.environ.get("PP_TASK_PASSWORD", "")

# Server
HOST = os.environ.get("PP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PP_PORT", "8420"))
# Optional API/Web UI token. When set, every request must carry it:
# browser — native Basic-auth prompt (any username, token as password);
# scripts — "Authorization: Bearer <token>" or curl -u x:<token>.
API_TOKEN = os.environ.get("PP_API_TOKEN", "")
