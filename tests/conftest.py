from pathlib import Path

import pytest

from promptpilot import db


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch):
    """Point the process-wide DB module at a disposable SQLite database."""
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "promptpilot.db")
    db.init_db()
    return db
