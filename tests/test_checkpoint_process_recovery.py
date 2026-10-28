"""Real process death: inspect the intent, clear fixture-owned stale locks, retry."""
import json
import os
import subprocess
import sys

import pytest

from test_checkpoint_publication import _pending, _resume, _git


@pytest.mark.parametrize("point", ["after_index_before_ref", "after_ref_before_database"])
def test_process_death_keeps_recoverable_intent(isolated_db, tmp_path, monkeypatch, point):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    count = int(_git(repo, "rev-list", "--count", "HEAD"))
    branch = _git(repo, "symbolic-ref", "HEAD")
    script = (
        "import os; from promptpilot import db,workflows,stage_checkpoint; "
        "from promptpilot.models import WorkflowHumanInput; "
        "print('checkpoint-child-pid',os.getpid(),flush=True); "
        f"stage_checkpoint._fault=lambda point: os._exit(79) if point=={point!r} else None; "
        f"w=db.get_workflow({wf.id!r}); "
        "workflows.human_input(w.id,WorkflowHumanInput(text='retry',resume=True,"
        "expected_version=w.state_version))"
    )
    env = {**os.environ, "PP_DATA_DIR": str(isolated_db.DB_DIR)}
    with subprocess.Popen([sys.executable, "-X", "utf8", "-c", script],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as child:
        stdout, stderr = child.communicate(timeout=40)
        assert child.returncode == 79, (stdout, stderr)
        # Windows venv launchers can forward to a different interpreter PID.
        child_pid = int(stdout.decode().split("checkpoint-child-pid ", 1)[1].splitlines()[0])
    # This is explicit operator inspection in a disposable test repository.
    # Production never removes a stale lock or resets an unknown ref/index.
    gitdir = repo / ".git"
    marker = json.loads((gitdir / "index.lock").read_bytes())
    assert marker["pid"] == child_pid and marker["checkpoint"]
    allowed = {gitdir / "index.lock", gitdir / "HEAD.lock", gitdir / (branch + ".lock")}
    for lock in gitdir.rglob("*.lock"):
        assert lock in allowed
        lock.unlink()
    assert _resume(wf).status.value == "queued"
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == count + 1
    assert _git(repo, "show", "HEAD:app.py") == "reviewed"
    events = isolated_db.list_workflow_events(wf.id, limit=200)
    assert sum(e.event_type == "stage.checkpoint" for e in events) == 1
    assert sum(e.event_type == "stage.advanced" for e in events) == 1
