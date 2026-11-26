import sqlite3

import pytest

from tools import install_onebase_merge_checks as installer


ANCHOR = installer.ANCHOR_PREFIX + " fixture"
DIR = "/Users/test/Projects/onebase-automation/merge"
NOTE = installer.note("/release/helper.py", "a" * 64)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(installer, "ANCHOR_SHA", installer.sha(ANCHOR.encode()))
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE task_series(id, title, working_dir, prompt, ended_at, updated_at, paused);
        CREATE TABLE tasks(id, series_id, working_dir, prompt, status, started_at, session_id, retry_count);
    """)
    db.execute("INSERT INTO task_series VALUES(4,?,?,?,NULL,NULL,1)", (installer.TITLE, DIR, ANCHOR))
    for task, series, status, started, session, retries in (
        (1, 4, "pending", None, None, 0), (2, 4, "running", 10, "s", 0),
        (3, 4, "pending", None, None, 1), (4, 4, "pending", None, "s", 0),
        (5, 2, "pending", None, None, 0), (6, 4, "pending", 10, None, 0),
    ):
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)", (
            task, series, DIR, ANCHOR + "\n\nper-task tail", status, started, session, retries))
    db.commit()
    yield db
    db.close()


def test_schedule_binding_round_trip(db):
    preview = installer.bind(db, DIR, NOTE, "preview")
    assert preview["pending_changed"] == [1]
    with pytest.raises(ValueError, match="incomplete"):
        installer.bind(db, DIR, NOTE, "verify")
    changed = installer.bind(db, DIR, NOTE, "apply")
    assert changed["series_changed"] and changed["pending_changed"] == [1]
    assert installer.bind(db, DIR, NOTE, "verify")["template_sha256"] == changed["template_sha256"]
    assert not installer.bind(db, DIR, NOTE, "apply")["series_changed"]
    assert db.execute("SELECT paused FROM task_series").fetchone() == (1,)
    assert db.execute("SELECT prompt FROM tasks WHERE id=1").fetchone()[0].endswith("per-task tail")
    assert db.execute("SELECT count(*) FROM tasks WHERE instr(prompt,?)=0", (NOTE,)).fetchone() == (5,)
    installer.bind(db, DIR, NOTE, "rollback")
    assert db.execute("SELECT prompt FROM task_series").fetchone() == (ANCHOR,)
    assert db.execute("SELECT distinct prompt FROM tasks").fetchall() == [(ANCHOR + "\n\nper-task tail",)]


@pytest.mark.parametrize("bad", [ANCHOR + "\n" + ANCHOR, "missing", ANCHOR + "\n" + installer.MARKER])
def test_unrecognized_pending_template_aborts_transaction(db, bad):
    db.execute("INSERT INTO tasks VALUES(7,4,?,?,'pending',NULL,NULL,0)", (DIR, bad))
    db.commit()
    with pytest.raises(ValueError):
        installer.bind(db, DIR, NOTE, "apply")
    assert db.execute("SELECT prompt FROM task_series").fetchone() == (ANCHOR,)
    assert not db.execute("SELECT 1 FROM tasks WHERE instr(prompt,?)>0", (NOTE,)).fetchone()


def test_immutable_runner_install(tmp_path):
    home = tmp_path.resolve()
    (home / "PromptPilotBuild").mkdir()
    src = home / "source.py"
    src.write_bytes(b"verified runner\n")
    sha = installer.sha(src.read_bytes())
    path = installer.runner_path(home, sha)
    assert not installer.qualify(path, src, sha, False)
    assert installer.qualify(path, src, sha, True)
    assert installer.qualify(path, src, sha, True)
    with pytest.raises(ValueError, match="SHA-256"):
        installer.qualify(path, src, "0" * 64, True)
    path.chmod(0o600)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="conflicting"):
        installer.qualify(path, src, sha, True)
