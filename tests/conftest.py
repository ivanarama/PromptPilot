import os
from pathlib import Path
import tempfile

import pytest

# Configure the test process before importing db/config: fixtures run too late
# to stop config._load_dotenv from reading the operator's permanent settings.
_TEST_HOME = tempfile.TemporaryDirectory(prefix="promptpilot-pytest-home-")
os.environ["HOME"] = _TEST_HOME.name
os.environ["USERPROFILE"] = _TEST_HOME.name
os.environ["PP_DATA_DIR"] = str(Path(_TEST_HOME.name) / ".promptpilot")

from promptpilot import db


@pytest.fixture(scope="session", autouse=True)
def isolated_user_home():
    yield
    _TEST_HOME.cleanup()


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch):
    """Point the process-wide DB module at a disposable SQLite database."""
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "promptpilot.db")
    db.init_db()
    return db
