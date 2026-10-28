"""Stage notes must survive pending/restarted checkpoint acceptance."""
import pytest

from promptpilot import db, stage_checkpoint, workflows
from promptpilot.models import WorkflowHumanInput, TaskStatus
from test_checkpoint_hardening import (
    _git_repo, _wf, _start, _claim_and_finish, REVIEWER_PASS,
)
from test_checkpoint_publication import _git, _resume

pytestmark = pytest.mark.skipif(not hasattr(db, "save_memory"), reason="requires scoped-memory schema")


def _mission_with_notes(isolated_db, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "checkpoint-memory")
    _start(isolated_db, wf)
    first, second = isolated_db.list_workflow_stages(wf.id)
    assert isolated_db.save_memory(wf.id, "stage.md", "remember-stage-before-publish", scope_stage=first.id)[0]
    assert isolated_db.save_memory(wf.id, "next.md", "next-stage-note", scope_stage=second.id)[0]
    assert isolated_db.save_memory(wf.id, "global.md", "global-note")[0]
    _claim_and_finish(isolated_db, "executor", write=lambda: (repo / "app.py").write_text("reviewed"))
    with monkeypatch.context() as patch:
        patch.setattr(workflows, "_stage_checkpoint_commit", lambda *a, **k: False)
        _claim_and_finish(isolated_db, REVIEWER_PASS)
    assert {note["name"] for note in isolated_db.list_memory(wf.id)} == {"stage.md", "next.md", "global.md"}
    return repo, wf


def test_restart_retains_scoped_notes_until_new_checkpoint_acceptance(isolated_db, tmp_path, monkeypatch):
    repo, wf = _mission_with_notes(isolated_db, tmp_path, monkeypatch)
    (repo / "app.py").write_text("new unreviewed bytes")
    current = isolated_db.get_workflow(wf.id)
    workflows.human_input(wf.id, WorkflowHumanInput(
        text="review changed bytes again", expected_version=current.state_version,
        restart_checkpoint=True, checkpoint_head=_git(repo, "rev-parse", "HEAD")))
    assert {note["name"] for note in isolated_db.list_memory(wf.id)} == {"stage.md", "next.md", "global.md"}
    _resume(wf)
    workflows.advance_workflow(wf.id)
    tasks = isolated_db.list_tasks(status=TaskStatus.PENDING)
    assert len(tasks) == 1
    # The restarted run can still retrieve the retained mission note.
    import asyncio
    import httpx
    from promptpilot.api import app
    async def memory():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8420") as client:
            response = await client.get(f"/api/workflows/{wf.id}/memory")
            assert response.status_code == 200
            return response.json()
    assert next(note for note in asyncio.run(memory()) if note["name"] == "stage.md")["content"] == "remember-stage-before-publish"
    _claim_and_finish(isolated_db, "executor", write=lambda: (repo / "app.py").write_text("fresh accepted bytes"))
    _claim_and_finish(isolated_db, REVIEWER_PASS)
    assert {note["name"] for note in isolated_db.list_memory(wf.id)} == {"next.md", "global.md"}
    assert _git(repo, "show", "HEAD:app.py") == "fresh accepted bytes"


def test_published_checkpoint_keeps_notes_until_database_recovery_commits(isolated_db, tmp_path, monkeypatch):
    repo, wf = _mission_with_notes(isolated_db, tmp_path, monkeypatch)
    def crash(point):
        if point == "after_ref_before_database":
            raise RuntimeError("crash before database commit")
    with monkeypatch.context() as patch:
        patch.setattr(stage_checkpoint, "_fault", crash)
        with pytest.raises(RuntimeError, match="crash before database"):
            _resume(wf)
    assert _git(repo, "show", "HEAD:app.py") == "reviewed"
    assert {note["name"] for note in isolated_db.list_memory(wf.id)} == {"stage.md", "next.md", "global.md"}
    _resume(wf)
    assert {note["name"] for note in isolated_db.list_memory(wf.id)} == {"next.md", "global.md"}
