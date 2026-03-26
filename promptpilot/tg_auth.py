"""Telegram authorization — phone-based access control."""

import json
import os
from pathlib import Path

from .config import DB_DIR


def _users_file() -> Path:
    return DB_DIR / "tg_users.json"


def load_allowed_phones() -> list[str]:
    """Load allowed phone numbers from PP_TG_ALLOWED_PHONES env or tg_config.json."""
    env_val = os.environ.get("PP_TG_ALLOWED_PHONES", "")
    if env_val:
        return [p.strip() for p in env_val.split(",") if p.strip()]

    config_file = DB_DIR / "tg_config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                data = json.load(f)
            return data.get("allowed_phones", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _load_users() -> dict:
    f = _users_file()
    if f.exists():
        try:
            with open(f) as fp:
                return json.load(fp)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_users(users: dict):
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with open(_users_file(), "w") as f:
        json.dump(users, f, indent=2)


def authorize_user(chat_id: int, phone: str):
    """Mark a user as authorized (stores chat_id → phone mapping)."""
    users = _load_users()
    users[str(chat_id)] = phone
    _save_users(users)


def deauthorize_user(chat_id: int):
    """Remove a user's authorization."""
    users = _load_users()
    users.pop(str(chat_id), None)
    _save_users(users)


def is_authorized(chat_id: int) -> bool:
    """Check if a user is authorized."""
    return str(chat_id) in _load_users()


def list_authorized() -> dict[str, str]:
    """Return all authorized users as {chat_id: phone}."""
    return _load_users()
