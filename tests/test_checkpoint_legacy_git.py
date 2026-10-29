"""Exercise the public checkpoint lifecycle with Apple's older Git capability."""
import os
import subprocess

import pytest

from promptpilot import stage_checkpoint, workflows
from promptpilot.models import WorkflowHumanInput
from test_checkpoint_hardening import (
    REVIEWER_PASS, _claim_and_finish, _git_repo, _start, _types, _wf,
)
from test_checkpoint_publication import _git, _pending, _resume


@pytest.fixture
def legacy_git(monkeypatch):
    real_run = subprocess.run
    calls = []

    def run(args, *rest, **kwargs):
        if list(args)[-3:] == ["symbolic-ref", "--no-recurse", "HEAD"]:
            calls.append(tuple(args))
            return subprocess.CompletedProcess(args, 129, b"", b"error: unknown option `no-recurse'\n")
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


posix_reader = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"), reason="legacy Git fallback requires POSIX no-follow open")


@posix_reader
def test_legacy_git_publishes_reviewed_checkpoint_once(isolated_db, tmp_path, monkeypatch, legacy_git):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    assert _resume(wf).status.value == "queued"
    assert _git(repo, "rev-parse", "HEAD^") == head
    assert _git(repo, "show", "HEAD:app.py") == "reviewed"
    assert _types(isolated_db, wf).count("stage.checkpoint") == 1
    # Capture, pre-publication, prepared update and database fence all probe.
    assert len(legacy_git) >= 4


@posix_reader
@pytest.mark.parametrize("replacement", ["alias", "other", "symlink"])
def test_legacy_git_refuses_changed_head_without_publication(
        isolated_db, tmp_path, monkeypatch, legacy_git, replacement):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    original = _git(repo, "symbolic-ref", "HEAD")
    head = _git(repo, "rev-parse", "HEAD")
    index = (repo / ".git/index").read_bytes()
    if replacement == "alias":
        _git(repo, "symbolic-ref", "refs/heads/alias", original)
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/alias")
    elif replacement == "other":
        _git(repo, "branch", "other", head)
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/other")
    else:
        target = tmp_path / "foreign-head"
        target.write_bytes((repo / ".git/HEAD").read_bytes())
        (repo / ".git/HEAD").unlink()
        (repo / ".git/HEAD").symlink_to(target)
    assert _resume(wf).status.value == "awaiting_human"
    assert _git(repo, "rev-parse", original) == head
    assert (repo / ".git/index").read_bytes() == index
    assert "stage.checkpoint" not in _types(isolated_db, wf)
    assert "stage.advanced" not in _types(isolated_db, wf)


@posix_reader
def test_legacy_git_explicit_restart_requires_new_review(isolated_db, tmp_path, monkeypatch, legacy_git):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    result = workflows.human_input(wf.id, WorkflowHumanInput(
        expected_version=isolated_db.get_workflow(wf.id).state_version,
        text="new acceptance", restart_checkpoint=True, checkpoint_head=head))
    assert result.status.value == "revision_required"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "stage.checkpoint" not in _types(isolated_db, wf)
    assert "stage.checkpoint_invalidated" in _types(isolated_db, wf)


@posix_reader
def test_legacy_git_linked_worktree_uses_its_own_head(isolated_db, tmp_path, legacy_git):
    main = _git_repo(tmp_path)
    repo = tmp_path / "linked"
    _git(main, "worktree", "add", "-b", "linked", str(repo))
    wf = _wf(isolated_db, repo, "linked-checkpoint")
    _start(isolated_db, wf)
    old_main = _git(main, "rev-parse", "HEAD")
    _claim_and_finish(isolated_db, "executor", write=lambda: (repo / "app.py").write_text("linked reviewed"))
    _claim_and_finish(isolated_db, REVIEWER_PASS)
    assert _types(isolated_db, wf).count("stage.checkpoint") == 1
    assert _git(repo, "show", "HEAD:app.py") == "linked reviewed"
    assert _git(main, "rev-parse", "HEAD") == old_main


@posix_reader
@pytest.mark.parametrize("kind", ["fifo", "directory", "oversized", "multiline", "nul", "detached"])
def test_legacy_git_malformed_head_does_not_publish(
        isolated_db, tmp_path, monkeypatch, legacy_git, kind):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    ref = _git(repo, "symbolic-ref", "HEAD")
    head = _git(repo, "rev-parse", "HEAD")
    path = repo / ".git/HEAD"
    before = path.read_bytes()
    index = (repo / ".git/index").read_bytes()
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes({"oversized": b"ref: refs/heads/" + b"x" * 5000 + b"\n",
                          "multiline": before + before, "nul": b"ref: refs/heads/ma\0in\n",
                          "detached": head.encode() + b"\n"}[kind])
    try:
        assert _resume(wf).status.value == "awaiting_human"
        assert (repo / ".git/index").read_bytes() == index
        assert "stage.checkpoint" not in _types(isolated_db, wf)
        assert "stage.advanced" not in _types(isolated_db, wf)
    finally:
        if kind == "directory":
            path.rmdir()
        else:
            path.unlink()
        path.write_bytes(before)
    assert _git(repo, "rev-parse", ref) == head


@pytest.mark.parametrize("returncode,stderr", [
    (128, b"HEAD permission denied"),
    (129, b"error: unknown option `unrelated'\nusage: symbolic-ref --no-recurse HEAD"),
])
def test_other_git_errors_never_fall_back(isolated_db, tmp_path, monkeypatch, returncode, stderr):
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "unrelated-git-error")
    _start(isolated_db, wf)
    _claim_and_finish(isolated_db, "executor", write=lambda: (repo / "app.py").write_text("reviewed"))
    real_run = subprocess.run
    recursive = []

    def failing(args, *rest, **kwargs):
        if list(args)[-3:] == ["symbolic-ref", "--no-recurse", "HEAD"]:
            return subprocess.CompletedProcess(args, returncode, b"", stderr)
        if list(args)[-2:] == ["symbolic-ref", "HEAD"]:
            recursive.append(args)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing)
    _claim_and_finish(isolated_db, REVIEWER_PASS)
    assert not recursive
    assert "stage.checkpoint" not in _types(isolated_db, wf)


@posix_reader
def test_legacy_prepared_update_and_verify_fence_real_refs(
        isolated_db, tmp_path, monkeypatch, legacy_git):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    branch = _git(repo, "symbolic-ref", "HEAD")
    _git(repo, "branch", "other", "HEAD")
    real_enter = stage_checkpoint._RefTransaction.__enter__
    observed = []

    def enter(tx):
        value = real_enter(tx)
        before = _git(repo, "rev-parse", "HEAD")
        for args in [("symbolic-ref", "HEAD", "refs/heads/other"),
                     ("update-ref", branch, _git(repo, "rev-parse", "refs/heads/other"), before)]:
            attempt = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=10)
            assert attempt.returncode != 0, (args, attempt.stdout, attempt.stderr)
        observed.append(bool(tx.new))
        assert _git(repo, "symbolic-ref", "HEAD") == branch
        return value

    monkeypatch.setattr(stage_checkpoint._RefTransaction, "__enter__", enter)
    assert _resume(wf).status.value == "queued"
    assert observed == [True, False]
    assert _types(isolated_db, wf).count("stage.checkpoint") == 1


def test_capture_error_survives_resume_without_fabricating_evidence(
        isolated_db, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "failed-evidence")
    _start(isolated_db, wf)
    _claim_and_finish(isolated_db, "executor", write=lambda: (repo / "app.py").write_text("reviewed"))
    head = _git(repo, "rev-parse", "HEAD")
    real_run = subprocess.run

    def unavailable(args, *rest, **kwargs):
        if list(args)[-3:] == ["symbolic-ref", "--no-recurse", "HEAD"]:
            return subprocess.CompletedProcess(args, 128, b"", b"HEAD permission denied")
        return real_run(args, *rest, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", unavailable)
        _claim_and_finish(isolated_db, REVIEWER_PASS)
    assert _resume(wf).status.value == "awaiting_human"
    events = isolated_db.list_workflow_events(wf.id, limit=200)
    failed = [e for e in events if e.event_type == "stage.checkpoint_failed"]
    assert failed and all("HEAD permission denied" in e.payload["reason"] for e in failed)
    assert all("legacy" not in e.payload["reason"] for e in failed)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "stage.advanced" not in _types(isolated_db, wf)
