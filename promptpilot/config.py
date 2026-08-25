"""Configuration settings."""

import json
import os
import sys
from pathlib import Path


def _load_dotenv():
    """Load .env files into os.environ (only for keys not already set).

    All candidates are read, earlier ones winning (a key already set is never
    overwritten). This matters: running `pp` from a project directory that has
    its own .env must NOT hide the permanent ~/.promptpilot/.env — otherwise the
    bot token or data dir silently depends on the current directory.

    Search order (first wins):
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
            # No break: read every .env, earlier files win via "key not in environ"


# Load .env BEFORE reading any os.environ values
_load_dotenv()


def _int_env(name: str, default: int) -> int:
    """int() an env var, tolerating garbage.

    A typo like PP_PORT='8420 ' or PP_POLL_INTERVAL='5s' must not crash every
    single pp invocation — including the guard hook, whose failure would leave
    an unattended run with no guard — with a ValueError on import.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"⚠ {name}={raw!r} — не число, использую {default}", file=sys.stderr)
        return default


_warned: set = set()


def _warn_once(key: str, msg: str):
    """Print a warning at most once per process (called from hot load paths)."""
    if key not in _warned:
        _warned.add(key)
        print(msg, file=sys.stderr)


def _q(path: str) -> str:
    """Double-quote a path containing spaces so _split_cmd keeps it one token."""
    if path and " " in path and not path.startswith(('"', "'")):
        return f'"{path}"'
    return path


def _split_cmd(template: str) -> list:
    """Split a command template into argv, honoring quotes.

    A naive str.split() mangles `--system "be nice"` and paths with spaces
    (C:\\Users\\John Smith\\...). POSIX uses standard shell splitting; on Windows
    keep backslashes (posix=False) and strip the wrapping quotes ourselves.
    """
    import shlex
    if os.name == "nt":
        toks = shlex.split(template or "", posix=False)
        return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in toks]
    return shlex.split(template or "", posix=True)


def _atomic_write_json(path: "Path", data):
    """Write JSON atomically with owner-only perms.

    Temp file + os.replace so a crash mid-write can't truncate a config full of
    API keys; chmod 600 so another local user can't read those keys.
    """
    DB_DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(DB_DIR, 0o700)
        except OSError:
            pass
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


# Database
DB_DIR = Path(os.environ.get("PP_DATA_DIR", Path.home() / ".promptpilot"))
DB_PATH = DB_DIR / "promptpilot.db"

# Worker
POLL_INTERVAL = _int_env("PP_POLL_INTERVAL", 5)
TASK_TIMEOUT = _int_env("PP_TASK_TIMEOUT", 0)
BASE_DELAY = _int_env("PP_BASE_DELAY", 60)
MAX_DELAY = _int_env("PP_MAX_DELAY", 3600)
MAX_RETRIES = _int_env("PP_MAX_RETRIES", 5)

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
    if sys.platform != "win32":
        return "cursor-agent"  # the .cmd/EINVAL workaround is Windows-only
    try:
        sdk_root = Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@nothumanwork" / "cursor-agents-sdk"
        manifest = json.loads((sdk_root / "vendor" / "manifest.json").read_text())
        vendor_dir = (sdk_root / manifest["path"]).parent
        node_exe = vendor_dir / "node.exe"
        index_js = vendor_dir / "index.js"
        if node_exe.exists() and index_js.exists():
            return f"{_q(str(node_exe))} {_q(str(index_js))}"
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
        return ""  # the registry-PATH lookup below is Windows-only
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
    # Fallback: common install locations, per platform.
    if sys.platform == "win32":
        for npm_bin in (Path.home() / "AppData" / "Roaming" / "npm",):
            for ext in (".CMD", ".cmd", ""):
                candidate = npm_bin / f"opencode{ext}"
                if candidate.exists():
                    return str(candidate)
    else:
        candidates = [
            Path.home() / ".opencode" / "bin",   # official install script
            Path("/usr/local/bin"),              # system npm
            Path("/usr/bin"),
            Path.home() / ".npm-global" / "bin", # user npm
            Path.home() / ".local" / "bin",      # generic
        ]
        for npm_bin in candidates:
            candidate = npm_bin / "opencode"
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
        "cmd": f"{_q(CLAUDE_EXE)} -p --verbose --output-format stream-json {{prompt}}",
        "description": "Claude Code (Anthropic)",
        "supports_skills": True,
    },
    "claude-z": {
        "cmd": f"{_q(CLAUDE_EXE)} -p --verbose --output-format stream-json {{prompt}}",
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
        "cmd": f"{_q(_find_opencode())} run {{prompt}}",
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
        except json.JSONDecodeError as e:
            _warn_once("providers.json",
                       f"⚠ {user_file} не читается ({e}) — кастомные провайдеры игнорируются")
        except OSError:
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
        except json.JSONDecodeError as e:
            _warn_once("providers.json",
                       f"⚠ {user_file} не читается ({e}) — кастомные провайдеры игнорируются")
        except OSError:
            pass

    return providers


def _load_custom_providers() -> dict:
    user_file = _providers_file()
    if user_file.exists():
        try:
            with open(user_file) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            _warn_once("providers.json",
                       f"⚠ {user_file} не читается ({e}) — кастомные провайдеры игнорируются")
        except OSError:
            pass
    return {}


def _write_custom_providers(custom: dict):
    _atomic_write_json(_providers_file(), custom)


def save_provider(name: str, cmd: str = None, description: str = "", env: dict = None,
                  executor: str = None, kind: str = None, keep_pane: bool = False,
                  models: list = None, args: list = None, effort: str = None):
    """Save a custom provider (cmd-template or executor-based) to providers.json."""
    custom = _load_custom_providers()
    entry = {"description": description}
    if models:
        entry["models"] = models
    if args:
        entry["args"] = args
    # Эффорт хранится полем, а не флагом в cmd/args: так его видно в форме и в
    # списке провайдеров, и он одинаково работает для обоих типов провайдеров.
    if (effort or "").strip().lower() in EFFORT_LEVELS:
        entry["effort"] = effort.strip().lower()
    if executor:
        entry["executor"] = executor
        entry["kind"] = kind or "claude"
        if keep_pane:
            entry["keep_pane"] = True
        if kind in (None, "claude"):
            entry["supports_skills"] = True
    else:
        entry["cmd"] = cmd
        # Клон встроенного claude, собранный руками в форме, — это тот же Claude
        # Code, и вести себя он должен так же: скилы, выбор модели, --effort,
        # --resume. Без этой отметки провайдер выглядел «неизвестным CLI» и молча
        # ронял всё перечисленное на пол.
        if cmd_runs_claude(cmd):
            entry["supports_skills"] = True
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


def _machines_file() -> Path:
    return DB_DIR / "machines.json"


def load_machines() -> dict:
    """Registry of remote machines: {name: {host, providers: [...], shell}}."""
    f = _machines_file()
    if f.exists():
        try:
            with open(f) as fp:
                return json.load(fp)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_machine(name: str, host: str, providers: list = None, shell: str = None):
    machines = load_machines()
    machines[name] = {"host": host, "providers": providers or [],
                      "shell": shell or "posix"}
    _atomic_write_json(_machines_file(), machines)


def machine_remote(machine: dict):
    """Remote() for a machines.json entry (entries predating the shell field
    are POSIX — that is what they were probed as)."""
    from .remote import Remote
    return Remote(machine["host"], machine.get("shell") or "posix")


def remove_machine(name: str) -> bool:
    machines = load_machines()
    if name not in machines:
        return False
    del machines[name]
    _atomic_write_json(_machines_file(), machines)
    return True


def probe_machine(host: str):
    """(providers, shell) for a remote machine: which of our providers are
    installed there, and which shell dialect it speaks.

    herdr providers need `herdr` on that machine plus the agent CLI of their
    kind (claude, opencode, ...); the session-target provider needs herdr only.
    An unreachable machine gives ([], "").
    """
    import subprocess

    from .remote import POWERSHELL, Remote, detect_shell, ps_quote, ssh_script
    shell = detect_shell(host)
    if not shell:
        return [], ""

    bases = {}         # cmd provider  -> executable basename
    herdr_needs = {}   # herdr provider -> agent binary it starts ("" = none)
    for name, info in load_providers().items():
        if info.get("hidden"):
            continue
        if info.get("executor") == "herdr":
            herdr_needs[name] = "" if info.get("session_target") else (info.get("kind") or "claude")
            continue
        if info.get("executor"):
            continue  # unknown executor — nothing to probe
        parts = _split_cmd(info.get("cmd") or "")
        if parts:
            bases[name] = os.path.basename(parts[0])

    herdr_bin = os.path.basename(HERDR_BIN)
    probes = set(bases.values()) | {b for b in herdr_needs.values() if b}
    if herdr_needs:
        probes.add(herdr_bin)
    if not probes:
        return [], shell
    if shell == POWERSHELL:
        script = ("foreach ($c in @(%s)) { if (Get-Command $c -ErrorAction "
                  "SilentlyContinue) { $c } }" % ",".join(ps_quote(c) for c in sorted(probes)))
    else:
        script = "for c in %s; do command -v \"$c\" >/dev/null 2>&1 && echo \"$c\"; done" % " ".join(
            sorted(probes))
    try:
        proc = subprocess.run(
            ssh_script(Remote(host, shell), script),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], shell
    found = set((proc.stdout or "").split())
    result = [name for name, b in bases.items() if b in found]
    if herdr_bin in found:
        result += [name for name, need in herdr_needs.items() if not need or need in found]
    return sorted(result), shell


def machine_has_herdr(machine: dict) -> bool:
    """True when the registry entry lists at least one herdr provider —
    i.e. the machine's herdr sessions are worth watching/listing."""
    providers = load_providers()
    return any(providers.get(p, {}).get("executor") == "herdr"
               for p in (machine.get("providers") or []))


def provider_available(info: dict) -> bool:
    """True when the provider's executable is present on this machine."""
    import shutil
    if info.get("executor") == "herdr":
        return shutil.which(HERDR_BIN) is not None
    parts = _split_cmd(info.get("cmd") or "")
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
    _atomic_write_json(user_file, custom)
    return True


# Уровни усилия рассуждений Claude Code (--effort), от дешёвого к дорогому —
# в этом же порядке они стоят в селектах UI и в клавиатуре бота.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def provider_is_claude(cfg: dict) -> bool:
    """Провайдер запускает Claude Code — у него свои флаги (--resume, --effort)."""
    return bool(cfg.get("supports_skills") or cfg.get("kind") == "claude")


def cmd_runs_claude(cmd: str) -> bool:
    """Шаблон команды запускает сам Claude Code (с путём, .exe и кавычками)."""
    parts = _split_cmd(cmd or "")
    return bool(parts) and os.path.basename(parts[0]).lower().startswith("claude")


def resolve_effort(provider_cfg: dict, task_effort: str = None) -> str:
    """Эффорт запуска: задача → провайдер → дефолт CLI (пустая строка).

    Уровня два, потому что и запросы разные: этап конвейера живёт на своём
    эффорте постоянно (это свойство провайдера), а разовому прогону иногда
    нужен max здесь и сейчас — не заводя ради этого клон провайдера.
    Незнакомое значение молча игнорируется: подсунуть агенту чужой флаг
    хуже, чем отработать на дефолте.
    """
    value = (task_effort or provider_cfg.get("effort") or "").strip().lower()
    return value if value in EFFORT_LEVELS else ""


def build_cmd(provider: str, prompt: str, skip_permissions: bool = False, session_id: str = None,
              model: str = None, guard: bool = True, effort: str = None):
    """Build the full command list for a provider + prompt.

    guard=False for a run that happens on another machine: the settings file
    with the hook lives here, and that path means nothing over there.
    """
    providers = load_providers()
    cfg = providers.get(provider, {})
    if provider in providers:
        template = cfg["cmd"]
    else:
        template = f"{provider} {{prompt}}"
    marker = "\x00PROMPT\x00"
    parts = _split_cmd(template.replace("{prompt}", marker))
    cmd = [prompt if p == marker else p for p in parts]
    # Claude Code owns --resume and --dangerously-skip-permissions; adding them
    # to codex/qwen/opencode just makes the CLI abort on an unknown argument
    # (codex spells the skip flag differently, qwen has none). --model is shared
    # by Claude and opencode, so it stays general.
    is_claude = provider_is_claude(cfg)
    extras = []
    if model:
        extras += ["--model", model]
    # --effort понимает только Claude Code (у opencode это --variant), а если
    # флаг уже вписан в шаблон руками — второй не добавляем, чтобы не спорить
    # с тем, что человек написал явно.
    eff = resolve_effort(cfg, effort)
    if eff and is_claude and "--effort" not in cmd:
        extras += ["--effort", eff]
    if session_id and is_claude:
        extras += ["--resume", session_id]
    if skip_permissions and is_claude:
        extras.append("--dangerously-skip-permissions")
    if guard and guard_enabled(providers.get(provider, {}), skip_permissions):
        settings = guard_settings_file()
        if settings:
            extras += ["--settings", settings]
    if extras:
        prompt_idx = cmd.index(prompt)
        cmd[prompt_idx:prompt_idx] = extras
    return cmd


# PromptPilot's own secrets: the agent process never needs them, and an
# autonomous run (skip_permissions) can read its own environment — a prompt
# injection would otherwise exfiltrate the bot token or the Web-UI token.
_SECRET_ENV = ("PP_TG_TOKEN", "PP_API_TOKEN", "PP_TASK_PASSWORD")


# Names that clearly aren't secrets and are useful to see in full.
_NONSECRET_ENV_HINTS = ("url", "host", "port", "path", "region", "model",
                        "version", "endpoint", "base", "lang", "locale")


def mask_secret_value(name: str, value: str) -> str:
    """Mask a provider env value for display (bot cards, settings API).

    Default is to mask fully as '***' — no prefix/suffix leak (the old code
    showed the first 4 and last 3 chars of a key). Only obvious non-secrets
    (URLs, model names, hosts) are shown in full. '***' also doubles as the
    "unchanged secret" sentinel the provider-save endpoint already recognises.
    """
    if not value:
        return value
    if any(h in (name or "").lower() for h in _NONSECRET_ENV_HINTS):
        return value
    return "***"


def get_provider_env(provider: str) -> dict:
    """Environment for a provider's subprocess: current env minus PromptPilot's
    own secrets, plus the provider's declared env on top."""
    providers = load_providers()
    extra = providers.get(provider, {}).get("env", {})
    env = os.environ.copy()
    for k in _SECRET_ENV:
        env.pop(k, None)
    # Skip empty values — don't override existing env vars with empty strings
    env.update({k: v for k, v in extra.items() if v})
    return env


# ── Dynamic model discovery (LiteLLM / OpenAI-compatible /models) ─────────────
# Instead of the static sonnet/opus/haiku tiers, ask a provider's OpenAI-style
# /models endpoint for the real list (e.g. a LiteLLM proxy). The endpoint is
# resolved from the PROVIDER's OWN ANTHROPIC_BASE_URL, not the global one — so a
# LiteLLM `claude` and a `claude-z` on api.z.ai don't cross-list each other.
import time as _time
import urllib.request as _ureq

MODELS_URL = os.environ.get("PP_MODELS_URL", "").strip()
MODELS_TOKEN = os.environ.get("PP_MODELS_TOKEN", "").strip()
_MODELS_TTL = 300      # keep a good answer this long
_MODELS_NEG_TTL = 60   # keep a failure this long, so a dead host isn't polled every call
_MODELS_CACHE = {}     # url -> {"ts": float, "data": list|None}
_CLAUDE_TIERS = ["sonnet", "opus", "haiku"]


def _models_url_for(base_url: str) -> str:
    """OpenAI-style /v1/models URL derived from an Anthropic base URL, or ''.

    PP_MODELS_URL overrides everything. A LiteLLM proxy serves the list at
    /v1/models on its root while ANTHROPIC_BASE_URL points at /anthropic.
    """
    if MODELS_URL:
        return MODELS_URL
    base = (base_url or "").strip()
    if not base:
        return ""
    root = base.rstrip("/")
    for suffix in ("/anthropic", "/v1"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
            break
    return root + "/v1/models"


def fetch_available_models(base_url: str = None, token: str = None):
    """Model ids from an OpenAI-compatible /models endpoint, or None.

    Cached PER-URL with a short negative TTL, so a misconfigured/unreachable
    endpoint isn't re-hit on every call (which would otherwise block for 5s each
    time). BLOCKING — call via asyncio.to_thread from async code. Returns None →
    caller falls back to the tiers.
    """
    if base_url is None:
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    url = _models_url_for(base_url)
    if not url:
        return None
    now = _time.time()
    cached = _MODELS_CACHE.get(url)
    if cached is not None:
        ttl = _MODELS_TTL if cached["data"] else _MODELS_NEG_TTL
        if now - cached["ts"] < ttl:
            return cached["data"]
    if token is None:
        token = MODELS_TOKEN or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    data = None
    try:
        req = _ureq.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with _ureq.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        items = payload.get("data", []) if isinstance(payload, dict) else payload
        data = sorted({m["id"] for m in items
                       if isinstance(m, dict) and m.get("id")}) or None
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        data = None  # unreachable/garbage → negative-cache below, fall back to tiers
    _MODELS_CACHE[url] = {"ts": now, "data": data}
    return data


def get_provider_models(provider: str) -> list:
    """Models to offer for a provider in the UI/bot pickers.

    An explicit `models` list on the provider wins. Otherwise, for a Claude-type
    provider (supports_skills), try dynamic discovery against THAT provider's
    endpoint, falling back to the sonnet/opus/haiku tiers. Non-Claude providers
    without an explicit list get nothing (no model picker).
    """
    info = load_providers().get(provider, {})
    if "models" in info:
        return list(info["models"])
    if info.get("supports_skills", False):
        penv = info.get("env") or {}
        base = penv.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", "")
        token = penv.get("ANTHROPIC_AUTH_TOKEN") or None
        dynamic = fetch_available_models(base_url=base, token=token)
        return dynamic if dynamic else list(_CLAUDE_TIERS)
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


# herdr executor (providers with "executor": "herdr" in providers.json)
HERDR_BIN = os.environ.get("PP_HERDR_BIN", "herdr")
HERDR_READ_LINES = _int_env("PP_HERDR_READ_LINES", 300)
HERDR_START_TIMEOUT_MS = _int_env("PP_HERDR_START_TIMEOUT_MS", 60000)
# Keep the pane open after a successful task (also per-provider "keep_pane")
HERDR_KEEP_PANE = os.environ.get("PP_HERDR_KEEP_PANE", "0") == "1"

# herdr → Telegram bridge: the bot watches ALL herdr agents (not only
# PromptPilot tasks) and notifies when one is blocked or finishes unseen.
HERDR_WATCH = os.environ.get("PP_HERDR_WATCH", "1") == "1"
HERDR_WATCH_INTERVAL = _int_env("PP_HERDR_WATCH_INTERVAL", 10)
# Повторное blocked с ТЕМ ЖЕ экраном в течение этого срока — не событие,
# а дребезг датчика статуса; молчим. Новый текст на экране уведомляет сразу.
HERDR_RENOTIFY_COOLDOWN = _int_env("PP_HERDR_RENOTIFY_COOLDOWN", 600)

# Сторож расписания: серия без запланированного вхождения не продолжится сама,
# и заметить это раньше можно было только в вебе. Бот проверяет серии с этим
# интервалом (сек) и пишет об обрыве; 0 — выключить.
SCHEDULE_WATCH_INTERVAL = _int_env("PP_SCHEDULE_WATCH_INTERVAL", 900)

# Journal prompts sent straight into herdr panes from the bot (💬 in «🖥 Окна»
# and notifications) into the prompt_log table, keyed by the pane's cwd.
LOG_PROMPTS = os.environ.get("PP_LOG_PROMPTS", "1") == "1"

# Git worktrees — a task edits its own checkout instead of the user's work tree.
# Branch name is <prefix>t<task id>; the checkout lands next to the repository
# (same machine, same filesystem) unless PP_WORKTREES_ROOT names a directory.
WORKTREE_BRANCH_PREFIX = os.environ.get("PP_WORKTREE_PREFIX", "pp/")
WORKTREES_ROOT = os.environ.get("PP_WORKTREES_ROOT", "")
WORKTREE_DIRNAME = ".pp-worktrees"
# Gitignored files a fresh checkout needs to be runnable at all (.env, tokens).
# Comma-separated names, copied from the source work tree; empty disables it.
WORKTREE_COPY = [p.strip() for p in os.environ.get("PP_WORKTREE_COPY", ".env").split(",") if p.strip()]

# How many tasks the worker runs at once. 1 = the historical sequential worker.
CONCURRENCY = max(1, _int_env("PP_CONCURRENCY", 1))
# Don't start another agent with less than this much RAM available (MB).
# Slots alone say nothing about whether the box can carry one more run: what
# it actually does is swap, and then the API starts refusing. 0 = no check.
MIN_FREE_MB = _int_env("PP_MIN_FREE_MB", 0)

# Ask every task to end with "ИТОГ: ..." so a finished task says WHAT happened,
# not just that the process exited 0. Off by default — it appends to the prompt.
VERDICT_REQUIRED = os.environ.get("PP_VERDICT", "0") == "1"

# Guard — hard limits for unattended runs, enforced by a PreToolUse hook
# (see promptpilot/guard.py). "auto" wires it into exactly the runs where
# nothing else asks anybody: those with --dangerously-skip-permissions.
#   auto (default) | 1 (always) | 0 (never)
GUARD = os.environ.get("PP_GUARD", "auto").strip().lower()


def _guard_hook_command() -> str:
    """How Claude Code should invoke the guard, quoted for the shell it uses.

    The data directory is spelled out rather than inherited: a herdr pane gets
    only the variables its tab was created with, so PP_DATA_DIR does not reach
    the hook there and it would read its rules from the wrong place.
    """
    argv = ([sys.executable, "guard-hook"] if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "promptpilot", "guard-hook"])
    argv += ["--data-dir", str(DB_DIR)]
    if os.name == "nt":
        import subprocess
        return subprocess.list2cmdline(argv)
    import shlex
    return " ".join(shlex.quote(a) for a in argv)


def guard_settings_file() -> str:
    """Path of the Claude Code settings file that wires the guard in.

    Written on demand rather than shipped: the hook command depends on where
    Python (or pp.exe) lives, and that is only known at run time. Returns ""
    if it could not be written — a guard that cannot be installed must not
    take the task down with it.
    """
    path = DB_DIR / "claude-settings.json"
    hook = {"type": "command", "command": _guard_hook_command()}
    content = json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]},
                                  {"matcher": "Write|Edit|MultiEdit", "hooks": [hook]},
                                  {"matcher": "mcp__.*", "hooks": [hook]}]}},
        ensure_ascii=False, indent=2)
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            DB_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    except OSError:
        return ""
    return str(path)


def guard_enabled(provider_cfg: dict, skip_permissions: bool) -> bool:
    """Should this run carry the guard?

    Only for Claude Code providers — the hook format is theirs. `supports_skills`
    is how this codebase already recognises them among plain providers; herdr
    providers say so with `kind`.
    """
    if GUARD == "0":
        return False
    if not (provider_cfg.get("supports_skills") or provider_cfg.get("kind") == "claude"):
        return False
    return True if GUARD == "1" else bool(skip_permissions)

# Projects root — optional directory whose subdirectories are offered as project choices
PROJECTS_ROOT = os.environ.get("PP_PROJECTS_ROOT", "")

# Optional password required to create tasks via Telegram bot
TASK_PASSWORD = os.environ.get("PP_TASK_PASSWORD", "")

# Server
HOST = os.environ.get("PP_HOST", "127.0.0.1")
PORT = _int_env("PP_PORT", 8420)


def is_loopback_host(host: str) -> bool:
    """Whether the server would bind to loopback only (safe without a token)."""
    return (host or "").strip() in ("127.0.0.1", "::1", "localhost", "")
# Optional API/Web UI token. When set, every request must carry it:
# browser — native Basic-auth prompt (any username, token as password);
# scripts — "Authorization: Bearer <token>" or curl -u x:<token>.
API_TOKEN = os.environ.get("PP_API_TOKEN", "")

# Escape hatch for the loopback guard on `pp server`: inside a container binding
# 0.0.0.0 is normal (the security boundary is the host port publish, not the
# container's bind), so the image sets this. On bare metal leave it unset.
ALLOW_INSECURE_BIND = os.environ.get("PP_ALLOW_INSECURE_BIND", "").strip() not in ("", "0", "false", "False")


# ── Outbound proxy for the Telegram bot ───────────────────────────────────────
# Telegram's API is often blocked in corporate networks; route the bot through an
# HTTP(S) or SOCKS5 proxy. Docker does NOT inherit host env, so pass it in via
# PP_TG_PROXY (or the standard *_proxy names) — see docker-compose.yml.
_PROXY_KEYS = (
    "PP_TG_PROXY",              # explicit override (highest priority)
    "https_proxy", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
    "http_proxy", "HTTP_PROXY",
)


def get_proxy_url() -> str:
    """Proxy URL for the Telegram bot, or '' when none is configured."""
    for key in _PROXY_KEYS:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def mask_proxy_url(url: str) -> str:
    """Hide user:pass in a proxy URL before logging it."""
    if not url or "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if not rest or "@" not in rest:
        return url
    creds, _, host = rest.rpartition("@")
    user = creds.split(":", 1)[0] if creds else ""
    prefix = f"{scheme}://" if scheme else ""
    return f"{prefix}{(user + ':***@') if user else ''}{host}"
