"""Public lifecycle regressions with actual Git refs, index and crash intent."""
import subprocess
import threading

import pytest

from promptpilot import db, stage_checkpoint, workflows, checkpoint_cleanup
from promptpilot.models import WorkflowHumanInput, TaskStatus, WorkflowEventCreate
from test_checkpoint_hardening import (
    _git_repo, _wf, _start, _claim_and_finish, _types, REVIEWER_PASS,
)


@pytest.fixture(autouse=True)
def retire_private_checkpoint_cleanup_resources():
    saved=set(checkpoint_cleanup._RESOURCES)
    yield
    for generation in set(checkpoint_cleanup._RESOURCES)-saved:
        resource=checkpoint_cleanup._RESOURCES[generation]
        assert resource['ready'](), 'private checkpoint resource did not stop'
        resource['cleanup']()
        checkpoint_cleanup._RESOURCES.pop(generation,None)


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


def test_transient_read_buffer_cannot_bypass_reviewed_hash(isolated_db, tmp_path, monkeypatch):
    from pathlib import Path
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    read = Path.read_bytes
    def swapped_read(path):
        if path == repo / "app.py":
            # Models an ABA writer: independent signature reads see reviewed
            # bytes, while the buffer read in between sees transient content.
            return b"UNREVIEWED-TRANSIENT-BYTES"
        return read(path)
    monkeypatch.setattr(Path, "read_bytes", swapped_read)
    assert _resume(wf).status.value == "awaiting_human"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / "app.py").read_text() == "reviewed"
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


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_restart_revokes_old_pass_and_requires_fresh_execution_and_review(
        isolated_db, tmp_path, monkeypatch, legacy):
    from promptpilot.models import WorkflowEventCreate, TaskStatus
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    if legacy:
        with isolated_db._connect() as conn:
            pending = workflows._pending_stage_checkpoint(conn, wf.id)
        pending.pop("reviewed")
        pending.pop("checkpoint_paths")
        pending.pop("pending_seq")
        isolated_db.append_workflow_event(WorkflowEventCreate(
            workflow_id=wf.id, event_type="stage.checkpoint_pending",
            idempotency_key="legacy-pending", payload=pending))
    (repo / "app.py").write_text("new unreviewed bytes")
    current = isolated_db.get_workflow(wf.id)
    result = workflows.human_input(wf.id, WorkflowHumanInput(
        expected_version=current.state_version, text="review changed files again",
        restart_checkpoint=True, checkpoint_head=head, note="retain this correction"))
    assert result.status.value == "revision_required"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "stage.checkpoint" not in _types(isolated_db, wf)
    invalidated = [e for e in isolated_db.list_workflow_events(wf.id, limit=200)
                   if e.event_type == "stage.checkpoint_invalidated"]
    assert len(invalidated) == 1 and invalidated[0].payload["legacy_ambiguous"] is legacy
    assert _resume(wf).status.value in {"queued", "executing"}
    workflows.advance_workflow(wf.id)
    tasks = isolated_db.list_tasks(status=TaskStatus.PENDING)
    assert len(tasks) == 1 and "retain this correction" in tasks[0].prompt
    assert isolated_db.get_workflow(wf.id).current_round == 2
    _claim_and_finish(isolated_db, "executor", write=lambda: (repo / "app.py").write_text("fresh execution"))
    _claim_and_finish(isolated_db, REVIEWER_PASS)
    assert _git(repo, "show", "HEAD:app.py") == "fresh execution"
    assert _types(isolated_db, wf).count("stage.checkpoint") == 1


@pytest.mark.parametrize("point", ["after_index_before_ref", "after_ref_before_database"])
def test_restart_only_rolls_back_a_proven_unpublished_index(isolated_db, tmp_path, monkeypatch, point):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    original_head = _git(repo, "rev-parse", "HEAD")
    original_index = (repo / ".git/index").read_bytes()
    def crash(here):
        if here == point:
            raise RuntimeError("crash")
    with monkeypatch.context() as patch:
        patch.setattr(stage_checkpoint, "_fault", crash)
        with pytest.raises(RuntimeError, match="crash"):
            _resume(wf)
    current = isolated_db.get_workflow(wf.id)
    action = WorkflowHumanInput(expected_version=current.state_version, text="restart safely",
        restart_checkpoint=True, checkpoint_head=_git(repo, "rev-parse", "HEAD"))
    if point == "after_ref_before_database":
        with pytest.raises(db.WorkflowConflictError, match="published"):
            workflows.human_input(wf.id, action)
        assert _resume(wf).status.value == "queued"
    else:
        (repo / "app.py").write_text("changed after crash")
        assert workflows.human_input(wf.id, action).status.value == "revision_required"
        assert _git(repo, "rev-parse", "HEAD") == original_head
        assert (repo / ".git/index").read_bytes() == original_index
        assert (repo / "app.py").read_text() == "changed after crash"


def test_restart_http_contract_rejects_stale_head_and_requires_explicit_action(isolated_db, tmp_path, monkeypatch):
    import asyncio
    import httpx
    from promptpilot.api import app
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    version = isolated_db.get_workflow(wf.id).state_version
    head = _git(repo, "rev-parse", "HEAD")
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8420") as client:
            path = f"/api/workflows/{wf.id}/human-input"
            body = {"expected_version": version, "restart_checkpoint": True,
                    "text": "new acceptance cycle", "checkpoint_head": "0" * 40}
            assert (await client.post(path, json=body)).status_code == 409
            body["checkpoint_head"] = head
            body["resume"] = True
            assert (await client.post(path, json=body)).status_code == 409
            body["resume"] = False
            result = await client.post(path, json=body)
            assert result.status_code == 200 and result.json()["status"] == "revision_required"
    asyncio.run(request())
    assert _git(repo, "rev-parse", "HEAD") == head


def test_restart_does_not_close_a_pipe_while_its_reader_is_still_finishing(isolated_db, tmp_path, monkeypatch):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "HEAD")
    eof, release, reader_finished = threading.Event(), threading.Event(), threading.Event()
    closing, premature_close = threading.Event(), threading.Event()
    real_start = stage_checkpoint.OwnedProcess.start

    class DelayedEOF:
        def __init__(self, stream): self.stream = stream
        def __iter__(self):
            yield from self.stream
            eof.set()
            assert release.wait(15)
            reader_finished.set()
        def close(self):
            if not reader_finished.is_set():
                premature_close.set()
            closing.set()
            self.stream.close()

    def start(*args, **kwargs):
        tree = real_start(*args, **kwargs)
        tree.process.stdout = DelayedEOF(tree.process.stdout)
        real_close = tree.close
        def close():
            closing.set()
            real_close()
        tree.close = close
        return tree

    monkeypatch.setattr(stage_checkpoint.OwnedProcess, 'start', start)
    results, errors = [], []
    def restart():
        try:
            version = isolated_db.get_workflow(wf.id).state_version
            results.append(workflows.human_input(wf.id, WorkflowHumanInput(
                expected_version=version, text='Restart with a fresh review',
                restart_checkpoint=True, checkpoint_head=head)))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=restart)
    thread.start()
    try:
        assert eof.wait(15) and closing.wait(15)
        assert not premature_close.is_set(), 'the live reader lost its pipe before finishing'
    finally:
        release.set()
        thread.join(20)
    assert not thread.is_alive() and not errors, errors
    assert reader_finished.is_set()
    assert len(results) == 1 and results[0].status.value == 'revision_required'
    assert _git(repo, 'rev-parse', 'HEAD') == head
    assert 'stage.checkpoint' not in _types(isolated_db, wf)


@pytest.mark.parametrize('fault', ['reader', 'owned-boundary'])
def test_final_ref_cleanup_failure_keeps_checkpoint_pending_and_recovers_once(isolated_db, tmp_path, monkeypatch, fault):
    from test_workflow_pause_race import request
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    before = int(_git(repo, 'rev-list', '--count', 'HEAD'))
    release = threading.Event()
    readers, owned_trees = [], []
    real_start = stage_checkpoint.OwnedProcess.start
    calls = []

    class HeldEOF:
        def __init__(self, stream): self.stream = stream
        def __iter__(self):
            readers.append(threading.current_thread())
            yield from self.stream
            assert release.wait(20)
        def close(self): self.stream.close()

    def start(*args, **kwargs):
        tree = real_start(*args, **kwargs)
        calls.append(tree)
        if len(calls) == 2:
            owned_trees.append(tree)
            if fault == 'reader':
                tree.process.stdout = HeldEOF(tree.process.stdout)
            else:
                real_close = tree.close
                failed = []
                def close():
                    real_close()
                    if not failed:
                        failed.append(True)
                        raise OSError('reported owned-boundary cleanup failure')
                tree.close = close
        return tree

    try:
        with monkeypatch.context() as patch:
            patch.setattr(stage_checkpoint.OwnedProcess, 'start', start)
            version = isolated_db.get_workflow(wf.id).state_version
            response = request('POST', f'/api/workflows/{wf.id}/human-input', json={
                'expected_version': version, 'text': 'Retry reviewed checkpoint', 'resume': True})
        assert response.status_code == 200, response.text
        assert isolated_db.get_workflow(wf.id).status.value == 'awaiting_human'
        assert _types(isolated_db, wf).count('stage.checkpoint') == 1
        assert _types(isolated_db, wf).count('stage.advanced') == 1
        with isolated_db._connect() as conn:
            assert checkpoint_cleanup.active(conn,wf.id)
        assert not isolated_db.list_tasks(status=TaskStatus.PENDING)
        assert _git(repo, 'show', 'HEAD:app.py') == _git(repo, 'show', ':app.py') == 'reviewed'
        assert int(_git(repo, 'rev-list', '--count', 'HEAD')) == before + 1
    finally:
        release.set()
        for reader in readers:
            reader.join(10)
            assert not reader.is_alive()
        for tree in owned_trees:
            tree.close()
            for stream in (tree.process.stdin, tree.process.stdout, tree.process.stderr):
                stream.close()
    assert _resume(wf).status.value == 'queued'
    assert int(_git(repo, 'rev-list', '--count', 'HEAD')) == before + 1
    assert _types(isolated_db, wf).count('stage.checkpoint') == 1
    assert _types(isolated_db, wf).count('stage.advanced') == 1


@pytest.mark.parametrize('drift', ['bytes', 'head', 'branch', 'index'])
def test_checkpoint_receipt_rechecks_context_after_cleanup(isolated_db, tmp_path, monkeypatch, drift):
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    original = stage_checkpoint.confirm

    def changed(*args):
        if drift == 'bytes':
            (repo/'app.py').write_text('post-cleanup edit')
        elif drift == 'head':
            old = _git(repo, 'rev-parse', 'HEAD')
            new = _git(repo, 'commit-tree', old+'^{tree}', '-p', old, '-m', 'external new HEAD')
            _git(repo, 'update-ref', 'HEAD', new, old)
        elif drift == 'branch':
            _git(repo, 'branch', 'other', 'HEAD')
            _git(repo, 'symbolic-ref', 'HEAD', 'refs/heads/other')
        else:
            (repo/'app.py').write_text('different staged bytes')
            _git(repo, 'add', '--', 'app.py')
            (repo/'app.py').write_text('reviewed')
        return original(*args)

    monkeypatch.setattr(stage_checkpoint, 'confirm', changed)
    assert _resume(wf).status.value == 'awaiting_human'
    assert _types(isolated_db, wf).count('stage.checkpoint') == 1
    assert _types(isolated_db, wf).count('stage.advanced') == 1
    with isolated_db._connect() as conn:
        assert checkpoint_cleanup.active(conn,wf.id)
    if drift == 'bytes': assert (repo/'app.py').read_text() == 'post-cleanup edit'
    if drift == 'index': assert _git(repo, 'show', ':app.py') == 'different staged bytes'


def test_cancellation_between_cleaned_receipt_and_projection_wins(isolated_db, tmp_path, monkeypatch):
    from contextlib import contextmanager
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    original = workflows._stage_checkpoint_commit

    @contextmanager
    def cancel_after_cleanup(*args, **kwargs):
        with original(*args, **kwargs) as commit:
            yield commit
        current = isolated_db.get_workflow(wf.id)
        workflows.cancel_workflow(wf.id, current.state_version)

    monkeypatch.setattr(workflows, '_stage_checkpoint_commit', cancel_after_cleanup)
    assert _resume(wf).status.value == 'cancelled'
    assert isolated_db.get_workflow(wf.id).status.value == 'cancelled'
    assert _git(repo, 'show', 'HEAD:app.py') == 'reviewed'
    assert _types(isolated_db, wf).count('stage.checkpoint') == 1
    assert _types(isolated_db, wf).count('stage.advanced') == 1
    with isolated_db._connect() as conn:
        assert checkpoint_cleanup.active(conn,wf.id) is None


def test_source_acceptance_retains_real_git_index_and_ref_fences(isolated_db, tmp_path, monkeypatch):
    from test_workflow_pause_race import request
    repo, wf = _pending(isolated_db, tmp_path, monkeypatch)
    original = db._append_workflow_event
    projecting, release = threading.Event(), threading.Event()
    results, errors = [], []
    def event(conn, value):
        if value.workflow_id == wf.id and value.event_type == 'stage.checkpoint':
            projecting.set()
            assert release.wait(30)
        return original(conn, value)
    monkeypatch.setattr(db, '_append_workflow_event', event)
    version = db.get_workflow(wf.id).state_version
    def resume():
        try:
            results.append(request('POST', f'/api/workflows/{wf.id}/human-input', json={
                'expected_version': version, 'text': 'Accept fenced checkpoint', 'resume': True}))
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=resume)
    thread.start()
    try:
        assert projecting.wait(30)
        head = _git(repo, 'rev-parse', 'HEAD')
        foreign = _git(repo, 'commit-tree', head+'^{tree}', '-p', head, '-m', 'foreign writer')
        ref = subprocess.run(['git','-C',str(repo),'update-ref','HEAD',foreign,head],capture_output=True,timeout=15)
        index = subprocess.run(['git','-C',str(repo),'add','--','app.py'],capture_output=True,timeout=15)
        assert ref.returncode != 0 and b'lock' in ref.stderr.lower(), ref.stderr
        assert index.returncode != 0 and b'index.lock' in index.stderr.lower(), index.stderr
        assert _git(repo,'rev-parse','HEAD') == head
    finally:
        release.set()
        thread.join(30)
    assert not thread.is_alive() and not errors, errors
    assert len(results)==1 and results[0].status_code==200, results
    assert db.get_workflow(wf.id).status.value == 'queued'
    assert _types(db,wf).count('stage.checkpoint') == _types(db,wf).count('stage.advanced') == 1


@pytest.mark.parametrize('mode', ['lost-owner','automatic','cancelled','malformed'])
def test_cleanup_barrier_never_infers_completion_or_reaccepts_source(isolated_db,tmp_path,monkeypatch,mode):
    from contextlib import contextmanager
    repo,wf = _pending(isolated_db,tmp_path,monkeypatch)
    original = workflows._stage_checkpoint_commit
    attempts=[]
    def close():
        attempts.append(True)
        if mode in {'automatic','cancelled'} and len(attempts)==1:
            raise OSError('still unconfirmed')
    @contextmanager
    def fail_cleanup(*args,**kwargs):
        with original(*args,**kwargs) as sha:yield sha
        raise stage_checkpoint.CheckpointCleanupError('retain original owner',close,lambda:True)
    with monkeypatch.context() as patch:
        patch.setattr(workflows,'_stage_checkpoint_commit',fail_cleanup)
        assert _resume(wf).status.value=='awaiting_human'
    with db._connect() as conn:pending=checkpoint_cleanup.active(conn,wf.id)
    generation=pending['generation']
    assert _types(db,wf).count('stage.checkpoint')==1
    if mode=='lost-owner':
        resource=checkpoint_cleanup._RESOURCES.pop(generation)
        try:
            assert workflows.advance_workflow(wf.id).status.value=='awaiting_human'
            with pytest.raises(db.WorkflowConflictError,match='owner is unavailable'):_resume(wf)
            assert not attempts
        finally:checkpoint_cleanup._RESOURCES[generation]=resource
    elif mode=='automatic':
        assert workflows.advance_workflow(wf.id).status.value=='awaiting_human'
        assert workflows.advance_workflow(wf.id).status.value=='awaiting_human'
        assert len(attempts)==1
        assert _resume(wf).status.value=='queued'
        assert len(attempts)==2
    elif mode=='cancelled':
        current=db.get_workflow(wf.id)
        workflows.cancel_workflow(wf.id,current.state_version)
        checkpoint_cleanup.retry(wf.id,db.get_workflow(wf.id).state_version,manual=True)
        checkpoint_cleanup.retry(wf.id,db.get_workflow(wf.id).state_version,manual=True)
        assert db.get_workflow(wf.id).status.value=='cancelled'
    else:
        with db._connect(immediate=True) as conn:
            db._append_workflow_event(conn,WorkflowEventCreate(
                workflow_id=wf.id,event_type=checkpoint_cleanup.PENDING,idempotency_key='corrupt-test',
                payload=dict(pending,target=[])))
        assert workflows.advance_workflow(wf.id).status.value=='awaiting_human'
        with pytest.raises(db.WorkflowConflictError,match='history is unknown'):_resume(wf)
        assert not attempts
    assert _types(db,wf).count('stage.checkpoint')==_types(db,wf).count('stage.advanced')==1
    assert not db.list_tasks(status=TaskStatus.PENDING)


@pytest.mark.parametrize('fault',['reader','owned-boundary'])
def test_restart_cleanup_failure_stays_nonrunnable_and_recovers_original_owner(isolated_db,tmp_path,monkeypatch,fault):
    from test_workflow_pause_race import request
    repo,wf=_pending(isolated_db,tmp_path,monkeypatch)
    head=_git(repo,'rev-parse','HEAD')
    release=threading.Event()
    transactions=[]
    original_read=stage_checkpoint._RefTransaction._read
    original_close=stage_checkpoint._RefTransaction.close
    def read(tx):
        transactions.append(tx)
        original_read(tx)
        assert release.wait(40)
    def close(tx):
        original_close(tx)
        if not release.is_set():raise OSError('original owned cleanup unconfirmed')
    try:
        with monkeypatch.context() as patch:
            if fault=='reader':patch.setattr(stage_checkpoint._RefTransaction,'_read',read)
            else:patch.setattr(stage_checkpoint._RefTransaction,'close',close)
            response=request('POST',f'/api/workflows/{wf.id}/human-input',json={
                'expected_version':db.get_workflow(wf.id).state_version,'text':'Review source again',
                'restart_checkpoint':True,'checkpoint_head':head})
        before=len(db.list_tasks())
        request('POST',f'/api/workflows/{wf.id}/advance')
        assert len(db.list_tasks())==before,'restart escaped its original resource cleanup barrier'
        assert response.status_code==200 and response.json()['status']=='awaiting_human',response.text
        with db._connect() as conn:assert checkpoint_cleanup.active(conn,wf.id)
        assert _git(repo,'rev-parse','HEAD')==head
        assert _types(db,wf).count('stage.checkpoint_invalidated')==1
        assert 'stage.checkpoint' not in _types(db,wf)
    finally:
        release.set()
        for tx in transactions:
            tx.reader.join(10)
            tx.close()
    assert _resume(wf).status.value=='revision_required'
    assert _types(db,wf).count('stage.checkpoint_invalidated')==1
    with db._connect() as conn:assert checkpoint_cleanup.active(conn,wf.id) is None
    workflows.advance_workflow(wf.id)
    assert len(db.list_tasks())==before+1


@pytest.mark.parametrize('fault',['protocol','generation','event-type','terminal','release','raw-json','duplicate-key'])
def test_malformed_cleanup_done_cannot_release_any_task(isolated_db,tmp_path,monkeypatch,fault):
    from contextlib import contextmanager
    from test_workflow_pause_race import request
    _repo,wf=_pending(isolated_db,tmp_path,monkeypatch)
    original=workflows._stage_checkpoint_commit
    calls=[]
    @contextmanager
    def uncertain(*args,**kwargs):
        with original(*args,**kwargs) as sha:yield sha
        raise stage_checkpoint.CheckpointCleanupError('unknown original cleanup',lambda:calls.append(True),lambda:True)
    with monkeypatch.context() as patch:
        patch.setattr(workflows,'_stage_checkpoint_commit',uncertain)
        assert _resume(wf).status.value=='awaiting_human'
    with db._connect(immediate=True) as conn:
        pending=checkpoint_cleanup.active(conn,wf.id)
        generation=pending['generation']
        value=dict(protocol=checkpoint_cleanup.PROTOCOL,generation=generation,terminal=False,
            release_seq=1,version=1,status='queued')
        if fault=='protocol':value['protocol']='unknown'
        elif fault=='generation':value['generation']=checkpoint_cleanup.new_generation()
        elif fault=='terminal':value['terminal']='false'
        elif fault=='release':value['release_seq']=2**60
        if fault in {'raw-json','duplicate-key'}:
            import json
            raw='{' if fault=='raw-json' else json.dumps(value)[:-1]+',"generation":'+json.dumps(generation)+'}'
            # Private corrupted-history fixture: append raw bytes directly.
            # Keep the actual append-only triggers enabled; never UPDATE it.
            conn.execute('INSERT INTO workflow_events(workflow_id,event_type,payload_json,idempotency_key,created_at) VALUES (?,?,?,?,?)',
                (wf.id,checkpoint_cleanup.DONE,raw,checkpoint_cleanup.DONE+':'+generation,db._now()))
        else:
            db._append_workflow_event(conn,WorkflowEventCreate(workflow_id=wf.id,
                event_type='unrelated' if fault=='event-type' else checkpoint_cleanup.DONE,
                idempotency_key=checkpoint_cleanup.DONE+':'+generation,payload=value))
    before=len(db.list_tasks())
    with pytest.raises(db.WorkflowConflictError,match='history is unknown'):_resume(wf)
    response=request('POST',f'/api/workflows/{wf.id}/advance')
    assert response.status_code==200 and response.json()['status']=='awaiting_human'
    assert len(db.list_tasks())==before and not calls
    with db._connect() as conn:assert checkpoint_cleanup.active(conn,wf.id).get('invalid')
