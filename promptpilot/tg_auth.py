"""Telegram authorization — phone-based access control."""

import json
import os
import sys
from pathlib import Path

from .config import DB_DIR, _atomic_write_json


def _users_file() -> Path:
    return DB_DIR / "tg_users.json"


def _norm_phone(p) -> str:
    """Compare phones by digits only: Telegram may or may not send the '+'."""
    return "".join(ch for ch in str(p or "") if ch.isdigit())


_warned_users = False


def load_allowed_phones() -> list:
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
    global _warned_users
    f = _users_file()
    if f.exists():
        try:
            with open(f) as fp:
                return json.load(fp)
        except json.JSONDecodeError as e:
            # A truncated file used to be swallowed as {} — silently logging
            # everyone out. Surface it and keep no one authorized only for this
            # read, without overwriting the file.
            if not _warned_users:
                _warned_users = True
                print(f"⚠ {f} повреждён ({e}) — авторизации временно недоступны, файл не тронут",
                      file=sys.stderr)
        except OSError:
            pass
    return {}


def _save_users(users: dict):
    _atomic_write_json(_users_file(), users)


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
    """Check if a user is authorized.

    Beyond having authorized once, the stored phone must still be in the allowed
    list — so removing a number from PP_TG_ALLOWED_PHONES/tg_config.json actually
    revokes access instead of leaving the old chat authorized forever. When no
    allow-list is configured, fall back to presence (the original behaviour).
    """
    phone = _load_users().get(str(chat_id))
    if phone is None:
        return False
    allowed = load_allowed_phones()
    if not allowed:
        return True
    return _norm_phone(phone) in {_norm_phone(a) for a in allowed}


def list_authorized() -> dict:
    """Return all authorized users as {chat_id: phone}."""
    return _load_users()
