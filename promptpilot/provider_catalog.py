"""Known CLI adapters for the reachable provider wizard.

Commands are static templates. Model IDs and endpoints are validated before
interpolation; secrets stay in provider environment, never command arguments.
"""
import json
import re
import sys
from urllib.parse import urlsplit

CATALOG = [
    {
        "id": "codex",
        "title": "Кодекс",
        "icon": "🧠",
        "desc": "Codex CLI (OpenAI) — агент для кода: план, правки, проверки.",
        "exe": "codex",
        "install": ["npm", "install", "-g", "@openai/codex"],
        "install_hint": "нужен Node.js (npm)",
        "cmd": "codex exec --skip-git-repo-check --model {model} --json -",
        "prompt_stdin": True,
        "api": "openai",
        "env_key": "OPENAI_API_KEY",
        "env_base": "OPENAI_BASE_URL",
        "default_base": "https://api.openai.com/v1",
    },
    {
        "id": "claude",
        "title": "Клод",
        "icon": "🤖",
        "desc": "Claude Code (Anthropic) — агент для кода от Anthropic.",
        "exe": "claude",
        "install": ["npm", "install", "-g", "@anthropic-ai/claude-code"],
        "install_hint": "нужен Node.js (npm)",
        "cmd": "claude -p --verbose --output-format stream-json --model {model} {prompt}",
        "prompt_stdin": True,
        "api": "anthropic",
        "env_key": "ANTHROPIC_AUTH_TOKEN",
        "env_base": "ANTHROPIC_BASE_URL",
        "default_base": "https://api.anthropic.com",
    },
    {
        "id": "goose",
        "title": "Гусь",
        "icon": "🪿",
        "desc": "Goose (Block) — универсальный агент; работает с любым OpenAI-совместимым адресом (например, z.ai/ГЛМ).",
        "exe": "goose",
        # Windows: официальный zip из GitHub Releases кладём в %USERPROFILE%\goose
        # и добавляем каталог в PATH пользователя (npm-пакета у goose нет).
        "install": [
            "powershell", "-NoProfile", "-Command",
            ("$d = Join-Path $env:USERPROFILE 'goose'; "
             "New-Item -ItemType Directory -Force $d | Out-Null; "
             "$z = Join-Path $env:TEMP 'goose-install.zip'; "
             "Invoke-WebRequest 'https://github.com/aaif-goose/goose/releases/latest/download/"
             "goose-x86_64-pc-windows-msvc.zip' -OutFile $z; "
             "Expand-Archive $z -DestinationPath $d -Force; "
             "$p = [Environment]::GetEnvironmentVariable('Path','User'); "
             "if ($p -notlike \"*$d*\") { "
             "[Environment]::SetEnvironmentVariable('Path', $p + ';' + $d, 'User') "
             "}; & (Join-Path $d 'goose.exe') --version"),
        ],
        "install_hint": "скачивает официальный zip с GitHub Releases",
        "cmd": "goose run --model {model} -i -",
        "prompt_stdin": True,
        "api": "openai",
        "env_key": "OPENAI_API_KEY",
        "env_base": "OPENAI_BASE_URL",
        "extra_env": {"GOOSE_PROVIDER": "openai"},
        "default_base": "",
    },
    {
        "id": "qwen",
        "title": "Квен",
        "icon": "🧐",
        "desc": "Qwen Code — строгий проверщик кода (Alibaba).",
        "exe": "qwen",
        "install": ["npm", "install", "-g", "@qwen-code/qwen-code"],
        "install_hint": "нужен Node.js (npm)",
        "cmd": "qwen --auth-type openai --model {model} --output-format stream-json",
        "prompt_stdin": True,
        "api": "openai",
        "env_key": "OPENAI_API_KEY",
        "env_base": "OPENAI_BASE_URL",
        "default_base": "",
    },
    {
        "id": "opencode",
        "title": "ОпенКод",
        "icon": "🛠️",
        "desc": "OpenCode — открытый агент, много бэкендов на выбор.",
        "exe": "opencode",
        "install": ["npm", "install", "-g", "opencode-ai"],
        "install_hint": "нужен Node.js (npm)",
        "cmd": "opencode run --model promptpilot/{model} {prompt}",
        "prompt_stdin": False,
        "api": "openai",
        "env_key": "OPENAI_API_KEY",
        "env_base": "OPENAI_BASE_URL",
        "default_base": "",
    },
    {
        "id": "pi",
        "title": "Пи",
        "icon": "🥧",
        "desc": "Pi — лёгкий быстрый агент (Mario Zechner).",
        "exe": "pi",
        "install": ["npm", "install", "-g", "@mariozechner/pi-coding-agent"],
        "install_hint": "нужен Node.js (npm)",
        "cmd": "pi --provider promptpilot --model {model} --print --no-session {prompt}",
        "prompt_stdin": False,
        "api": "openai",
        "env_key": "OPENAI_API_KEY",
        "env_base": "OPENAI_BASE_URL",
        "default_base": "",
    },
]

_BY_ID = {e["id"]: e for e in CATALOG}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}\Z")


def catalog_entry(cid: str):
    return _BY_ID.get(cid)


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()[:24]


def pretty_model(model_id: str) -> str:
    words = [w for w in re.split(r"[-_ ]+", (model_id or "").strip()) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def validate_base(base):
    try:
        u = urlsplit(base)
        if (u.scheme not in {"http", "https"} or not u.hostname or u.username is not None
                or u.password is not None or u.query or u.fragment or u.port == 0
                or any(ord(c) <= 32 or c in "\\{}" for c in base)):
            raise ValueError
        _ = u.port
    except (ValueError, TypeError):
        raise ValueError("Endpoint must be an HTTP(S) URL without credentials, query or fragment") from None
    return base.rstrip("/")


def validate_key(key):
    if any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("API key contains control characters")
    return key


def fetch_models(base, key, api="openai"):
    import urllib.error
    import urllib.request

    base, key = validate_base(base), validate_key(key)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    suffix = "/v1/models" if api == "anthropic" and not base.endswith("/v1") else "/models"
    req = urllib.request.Request(base + suffix)
    if api == "anthropic":
        req.add_header("x-api-key", key)
        req.add_header("anthropic-version", "2023-06-01")
    elif key:
        req.add_header("Authorization", "Bearer " + key)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=25) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Response too large")
        data = json.loads(raw.decode("utf-8"))
        values = data.get("data") or data.get("models") or []
        if not isinstance(values, list):
            raise ValueError("Invalid response shape")
        ids = sorted({m["id"] for m in values if isinstance(m, dict)
                      and isinstance(m.get("id"), str) and MODEL_ID.fullmatch(m["id"])})
        if not ids or len(ids) > 2048:
            raise ValueError("No usable models")
        return ids
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RuntimeError("Could not fetch models; check endpoint and credentials") from None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        # Never reflect network diagnostics: they can contain credentials/URLs.
        raise RuntimeError("Could not fetch models; check endpoint and credentials") from exc


def install_command(entry, platform=None):
    platform = platform or sys.platform
    if entry["id"] != "goose" or platform == "win32":
        return list(entry["install"])
    if platform == "darwin":
        return ["brew", "install", "block-goose-cli"]
    if platform.startswith("linux"):
        return ["bash", "-c", "set -o pipefail; curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh | CONFIGURE=false bash"]
    raise ValueError("Install this CLI manually on this platform")


def build_provider_entry(entry: dict, base: str, key: str, model: str,
                         title_override: str = "") -> dict:
    if not MODEL_ID.fullmatch(model):
        raise ValueError("Invalid model ID")
    base = validate_base(base or entry.get("default_base") or "https://api.openai.com/v1")
    key = validate_key(key)
    env = dict(entry.get("extra_env") or {})
    env[entry["env_base"]] = base
    if key:
        env[entry["env_key"]] = key
    cid = entry["id"]
    if cid == "goose":
        u = urlsplit(base)
        env.update(OPENAI_HOST=f"{u.scheme}://{u.netloc}",
                   OPENAI_BASE_PATH=u.path.rstrip("/").lstrip("/") + "/chat/completions",
                   GOOSE_MODEL=model)
    if cid == "qwen":
        env["OPENAI_MODEL"] = model
    if cid == "opencode":
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"provider": {"promptpilot": {
            "npm": "@ai-sdk/openai-compatible", "name": "PromptPilot",
            "options": {"baseURL": base, "apiKey": "{env:OPENAI_API_KEY}"},
            "models": {model: {"name": model}},
        }}})
    configured_model = f"promptpilot/{model}" if cid == "opencode" else model
    prov = {
        "cmd": entry["cmd"].replace("{model}", model),
        "description": f"{entry['title']}, model {model}",
        "models": [configured_model], "env": env,
        "human": {"label": (title_override or f"{entry['title']} · {pretty_model(model)}").strip(),
                  "role": "writer", "desc": f"{model} via {entry['title']}"},
        "ui_order": 20, "catalog_id": cid,
        "prompt_stdin": bool(entry.get("prompt_stdin")),
    }
    if cid in {"claude", "codex"}:
        prov["kind"] = cid
        prov["supports_effort"] = True
    if cid == "claude":
        prov["supports_skills"] = True
    if cid == "pi":
        prov["catalog_models_config"] = {"providers": {"promptpilot": {
            "baseUrl": base, "api": "openai-completions", "apiKey": "$OPENAI_API_KEY",
            "models": [{"id": model}],
        }}}
    return prov
