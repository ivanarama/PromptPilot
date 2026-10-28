"""Public lifecycle regressions with actual Git refs, index and crash intent."""
import subprocess
import threading

import pytest

from promptpilot import db, stage_checkpoint, workflows
from promptpilot.models import WorkflowHumanInput
from test_checkpoint_hardening import (
    _git_repo, _wf, _start, _claim_and_finish, _types, REVIEWER_PASS,
)


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


def _pending(isolated_db, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "publication")
    _start(isolated_db, wf)
    _claim_and_finish(isolated_db, "executor", write=lambda: (repo / "app.py").write_text("reviewed"))
    with monkeypatch.context() as patch:
        patch.setattr(workflows, "_stage_checkpoint_commit", lambda *a, **k: False)
        _claim_and_finish(isolated_db, REVIEWER_PASS)
    return repo, wf


def _resume(wf, *, version=None):
    return workflows.human_input(wf.id, WorkflowHumanInput(
        text="retry checkpoint", resume=True,
        expected_version=(db.get_workflow(wf.id).state_version if version is None else version)))


def test_stale_and_cancelled_resume_has_no_git_side_effects(isolated_db, tmp_path, monkeypatch):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    index = (repo / ".git/index").read_bytes()
    version = isolated_db.get_workflow(wf.id).state_version
    with pytest.raises(db.WorkflowConflictError):
        _resume(wf, version=version - 1)
    workflows.cancel_workflow(wf.id, version)
    with pytest.raises(db.WorkflowConflictError):
        _resume(wf)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git/index").read_bytes() == index
    assert not (repo / ".git/promptpilot-checkpoints").exists()


def test_changed_reviewed_bytes_require_new_acceptance(isolated_db, tmp_path, monkeypatch):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("unreviewed replacement")
    assert _resume(wf).status.value == "awaiting_human"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "stage.advanced" not in _types(isolated_db, wf)
    assert "stage.checkpoint" not in _types(isolated_db, wf)


@pytest.mark.parametrize("point", ["after_index_before_ref", "after_ref_before_database"])
def test_known_partial_publication_recovers_once(isolated_db, tmp_path, monkeypatch, point):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    original_count = int(_git(repo, "rev-list", "--count", "HEAD"))
    def crash(here):
        if here == point:
            raise RuntimeError("simulated process death")
    with monkeypatch.context() as patch:
        patch.setattr(stage_checkpoint, "_fault", crash)
        with pytest.raises(RuntimeError, match="process death"):
            _resume(wf)
    assert "stage.advanced" not in _types(isolated_db, wf)
    assert _resume(wf).status.value == "queued"
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == original_count + 1
    assert _git(repo, "show", "HEAD:app.py") == "reviewed"
    assert _types(isolated_db, wf).count("stage.checkpoint") == 1
    assert _types(isolated_db, wf).count("stage.advanced") == 1


def test_divergent_staged_bytes_on_owned_path_are_preserved(isolated_db, tmp_path, monkeypatch):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("foreign staged")
    _git(repo, "add", "--", "app.py")
    (repo / "app.py").write_text("reviewed")
    assert _resume(wf).status.value == "awaiting_human"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "show", ":app.py") == "foreign staged"


def test_branch_identity_change_is_not_hidden_by_equal_commit(isolated_db, tmp_path, monkeypatch):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    original = _git(repo, "symbolic-ref", "HEAD")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "other", head)
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/other")
    assert _resume(wf).status.value == "awaiting_human"
    assert _git(repo, "rev-parse", original) == head
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "stage.checkpoint" not in _types(isolated_db, wf)


def test_cancellation_during_preparation_is_responsive_and_prevents_publication(isolated_db, tmp_path, monkeypatch):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    index = (repo / ".git/index").read_bytes()
    prepared, release = threading.Event(), threading.Event()
    def wait(point):
        if point == "after_preparation_before_admission":
            prepared.set()
            assert release.wait(10)
    monkeypatch.setattr(stage_checkpoint, "_fault", wait)
    errors = []
    def resume():
        try:
            _resume(wf)
        except db.WorkflowConflictError as exc:
            errors.append(exc)
    thread = threading.Thread(target=resume)
    thread.start()
    try:
        assert prepared.wait(10)
        # Another real task and cancellation both acquire the SQLite writer
        # while Git objects are prepared; neither waits on the checkpoint.
        from promptpilot.models import TaskCreate
        task = isolated_db.create_task(TaskCreate(prompt="unrelated", provider="test"))
        assert isolated_db.get_next_runnable().id == task.id
        current = isolated_db.get_workflow(wf.id)
        workflows.cancel_workflow(wf.id, current.state_version)
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and len(errors) == 1
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git/index").read_bytes() == index
    assert isolated_db.get_workflow(wf.id).status.value == "cancelled"
    assert "stage.advanced" not in _types(isolated_db, wf)
