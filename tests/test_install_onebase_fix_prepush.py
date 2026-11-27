import sqlite3

import pytest

from tools import install_onebase_fix_postpush as postpush
from tools import install_onebase_fix_prepush as prepush


PUSH_ANCHOR = (
    "Use the already elected PR, exact completion and candidate SHAs, and this attempt's own worktree and "
    "private evidence directory. The callable fresh_pre_push_checks must execute every existing final "
    "pre-push check for this transaction, including its task-specific gates, in this invocation. It must "
    "raise on refusal and return the raw current REST PR object from the gated GET of "
    "repos/ivanarama/onebase/pulls/<number>. Cached authorization, a saved response or a callback that "
    "merely returns a PR object cannot authorize a push. Preserve the existing transition trailer and "
    "the canonical parent and tested-tree proof."
)


@pytest.mark.parametrize("first,second", [(prepush, postpush), (postpush, prepush)])
def test_old_and_new_recovery_can_be_installed_and_removed_independently(first, second, monkeypatch):
    old_anchor = postpush.ANCHOR + " passive observer fixture"
    monkeypatch.setattr(postpush, "ANCHOR_SHA", postpush.sha(old_anchor.encode()))
    original = "\n".join(("header", old_anchor, "more instructions", PUSH_ANCHOR, "footer"))
    a = first.note("/release/first.py", "a" * 64)
    b = second.note("/release/second.py", "b" * 64)
    both = second.transform(first.transform(original, a), b)
    assert first.transform(both, a) == second.transform(both, b) == both
    assert first.transform(both, a, remove=True) == second.transform(original, b)
    assert second.transform(both, b, remove=True) == first.transform(original, a)
    assert second.transform(first.transform(both, a, remove=True), b, remove=True) == original


def test_install_refuses_unexpected_or_duplicate_binding():
    binding = prepush.note("/release/runner.py", "a" * 64)
    installed = prepush.transform(PUSH_ANCHOR, binding)
    for unexpected in (PUSH_ANCHOR + " amended", PUSH_ANCHOR + "\n" + PUSH_ANCHOR,
                       installed + "\n" + binding, installed.replace("runner.py", "other.py")):
        with pytest.raises(ValueError):
            prepush.transform(unexpected, binding)


def test_live_upgrade_only_reaches_scheduled_unstarted_ordinary_fix():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE task_series(id,title,working_dir,prompt,ended_at,updated_at);
        CREATE TABLE tasks(id,series_id,working_dir,prompt,status,started_at,session_id,retry_count);
    """)
    db.execute("INSERT INTO task_series VALUES(2,?,?,?,NULL,NULL)", (prepush.TITLE, "FIX", PUSH_ANCHOR))
    for task, series, state, started, session, retry in (
        (1, 2, "pending", None, None, 0),
        (2, 2, "running", "start", "session", 0),
        (3, 2, "pending", "start", None, 0),
        (4, 2, "pending", None, "session", 0),
        (5, 2, "pending", None, None, 1),
        (6, 4, "pending", None, None, 0),
    ):
        db.execute("INSERT INTO tasks VALUES(?,?,'FIX',?,?,?,?,?)",
                   (task, series, PUSH_ANCHOR, state, started, session, retry))
    db.commit()
    binding = prepush.note("/release/runner.py", "f" * 64)

    def run(action):
        return prepush.bind(db, "FIX", binding, action, series=2, title=prepush.TITLE,
                            transform_prompt=prepush.transform)

    assert run("preview")["pending_changed"] == [1]
    assert run("apply")["pending_changed"] == [1]
    assert run("verify")["series_changed"] is False
    assert run("apply")["pending_changed"] == []
    assert db.execute("SELECT DISTINCT prompt FROM tasks WHERE id <> 1").fetchall() == [(PUSH_ANCHOR,)]
    assert run("rollback")["pending_changed"] == [1]
    assert db.execute("SELECT DISTINCT prompt FROM tasks").fetchall() == [(PUSH_ANCHOR,)]
    db.close()
