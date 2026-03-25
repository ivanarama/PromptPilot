"""Configuration settings."""

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

# Server
HOST = os.environ.get("PP_HOST", "127.0.0.1")
PORT = int(os.environ.get("PP_PORT", "8420"))
