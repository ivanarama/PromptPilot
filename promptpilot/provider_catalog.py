"""Каталог известных CLI-агентов для визарда подключения помощников.

Директива владельца 2026-10-04: настройка провайдеров «через одну жопу» —
рядовой пользователь не должен видеть cmd/env-простыни. Визард:
Установить CLI → вставить ключ и адрес → Подобрать модели → клик по модели →
готовый помощник с коротким именем «Кодекс · Астра».

install-команды проверены по факту (npm-пакеты существуют, goose-asset
Windows зовётся goose-x86_64-pc-windows-msvc.zip); cmd-шаблоны сняты с
боевых провайдеров этой машины (goose-zai / codex / qwen-review / pi).
"""

import re

# Каждая запись: как узнать установлен, как поставить, как запускать,
# как спросить список моделей и какие env нужны.
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
        "cmd": "claude -p {prompt} --model {model} --output-format text",
        "prompt_stdin": False,
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
             "Invoke-WebRequest 'https://github.com/block/goose/releases/latest/download/"
             "goose-x86_64-pc-windows-msvc.zip' -OutFile $z; "
             "Expand-Archive $z -DestinationPath $d -Force; "
             "$p = [Environment]::GetEnvironmentVariable('Path','User'); "
             "if ($p -notlike \"*$d*\") { "
             "[Environment]::SetEnvironmentVariable('Path', $p + ';' + $d, 'User') "
             "}; goose --version"),
        ],
        "install_hint": "скачивает официальный zip с GitHub Releases",
        "cmd": "goose run -i -",
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
        "cmd": "qwen --approval-mode auto --max-subagent-depth 1 -p -",
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
        "cmd": "opencode run --model {model} {prompt}",
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
        "install": ["npm", "install", "-g", "@mariozechner/pi"],
        "install_hint": "нужен Node.js (npm)",
        "cmd": "pi --print --no-session {prompt}",
        "prompt_stdin": False,
        "api": "openai",
        "env_key": "OPENAI_API_KEY",
        "env_base": "OPENAI_BASE_URL",
        "default_base": "",
    },
]

_BY_ID = {e["id"]: e for e in CATALOG}


def catalog_entry(cid: str):
    return _BY_ID.get(cid)


def slugify(text: str) -> str:
    """Латинский/цифровой слаг для технического имени провайдера."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return s[:24]


def pretty_model(model_id: str) -> str:
    """«astra-3.1-pro» -> «Astra 3.1 Pro»: короткое имя для карточки."""
    words = [w for w in re.split(r"[-_ ]+", (model_id or "").strip()) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def build_provider_entry(entry: dict, base: str, key: str, model: str,
                         title_override: str = "") -> dict:
    """Собрать запись providers.json по выбору пользователя.

    cmd получает модель сразу (как у боевого codex-провайдера: диспетчер
    подставляет только {prompt}); ключ и адрес живут в env и не светятся
    в UI дальше маски.
    """
    cmd = entry["cmd"]
    if "{model}" in cmd:
        cmd = cmd.replace("{model}", model)
    env = {}
    if entry.get("extra_env"):
        env.update(entry["extra_env"])
    if base:
        env[entry["env_base"]] = base.rstrip("/")
    if key:
        env[entry["env_key"]] = key
    label = (title_override or f"{entry['title']} · {pretty_model(model)}").strip()
    prov = {
        "cmd": cmd,
        "description": f"{entry['title']} ({entry['desc'].split('.')[0]}), модель {model}",
        "models": [model],
        "human": {
            "label": label,
            "role": "writer",
            "desc": f"Модель {pretty_model(model)} через {entry['title']}",
        },
        "ui_order": 20,
        "catalog_id": entry["id"],
    }
    if env:
        prov["env"] = env
    if entry.get("prompt_stdin"):
        prov["prompt_stdin"] = True
    return prov
