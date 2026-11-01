"""Fresh reviewer probes. All mutation is confined to private fixtures."""
from contextlib import redirect_stdout
from io import StringIO
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from promptpilot import project_pipeline as pp, worker
from promptpilot import pipeline_insights as insights
from promptpilot import review_routing_start_request as starts
from promptpilot.review_routing_execution import OneUseStart
from promptpilot.review_routing_journal import Journal
from promptpilot.review_routing_push import Push
from promptpilot.review_routing_workspace import Workspace
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_control import run as run_control, close_controls
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge, ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_technical_worker import technical_worker as technical_worker  # noqa: F401
from test_review_routing_heartbeat import producer as producer, advance  # noqa: F401
from test_review_routing_revocation import revocation_case as revocation_case  # noqa: F401
from test_review_routing_settlement_public import settlement_case as settlement_case  # noqa: F401
from test_review_routing_workspace import git
from test_review_routing_execution import update

_ORIGINAL_GIT = Workspace._git


def cli(path, *args):
    stream = StringIO()
    with redirect_stdout(stream):
        code = pp.run(['--config', str(path), *args])
    return code, json.loads(stream.getvalue())


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_start_refuses_author_drift_from_committed_review(pipeline_case):
    peer, health, path, task, database, config = pipeline_case
    canonical_author = ledger(peer, path).proofs[-1].publication.payload['author']
    pr = peer.current['data']['repository']['pullRequest']
    pr['author'] = dict(id='U_REPLACED_AUTHOR', login='replacement-author')
    assert pr['author'] != canonical_author
    code, selected = cli(path, 'next', 'fix')
    if code == 0 and selected.get('action') == 'pipeline-fix':
        code, prepared = cli(path, 'prepare-fix-start', 'fix', '--lease', selected['lease'])
        assert code == 2, 'public START accepted a different author than the committed REVIEW: ' + str(prepared.get('action'))
    else:
        assert selected.get('action') in {'error', 'wait', 'empty'}


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_push_refuses_url_rewrite_added_after_journal_wait(technical_worker, monkeypatch, tmp_path):
    peer, path, task, database, calls, factories, pushes, remote, ref, mode = technical_worker
    old = peer.current['data']['repository']['pullRequest']['headRefOid']
    diverted = tmp_path / 'diverted.git'
    git(tmp_path, 'init', '-q', '--bare', str(diverted))
    git(task.working_dir, 'push', str(diverted), old + ':' + ref)
    # Absolutely no HTTPS/SSH network is possible in this independent probe.
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL', 'file')
    prior_transition = Push._transition
    prior_git = Workspace._git
    attempted = []

    def transition(push, source, target, **kwargs):
        result = prior_transition(push, source, target, **kwargs)
        if target == 'sending':
            git(task.working_dir, 'config', 'url.' + diverted.as_posix() + '.insteadOf',
                'https://github.com/owner/repo.git')
        return result

    def actual_git(workspace, root, *args, **kwargs):
        if args and args[0] == 'push':
            attempted.append(args)
            assert args[-2] == 'https://github.com/owner/repo.git'
            return _ORIGINAL_GIT(workspace, root, *args, **kwargs)
        return prior_git(workspace, root, *args, **kwargs)

    monkeypatch.setattr(Push, '_transition', transition)
    monkeypatch.setattr(Workspace, '_git', actual_git)
    worker._execute_task_body(task)
    actual = git(tmp_path, '--git-dir=' + str(diverted), 'rev-parse', ref)
    assert actual == old, 'late URL rewrite redirected an actual CAS to a different bare repository; attempted=' + str(len(attempted))


@pytest.mark.parametrize('fault', ['cancel', 'reservation-token'])
def test_public_heartbeat_refuses_local_drift_after_writer_wait(producer, monkeypatch, fault):
    peer, path, database, scope, store, tree, start, run = producer
    advance(producer, 26)
    code, prepared = run('prepare-fix-heartbeat', start)
    assert code == 0, prepared
    before = list(peer.posts)
    original = Journal.arm

    def arm(journal, *args, **kwargs):
        result = original(journal, *args, **kwargs)
        if fault == 'cancel':
            database.request_cancel(scope['task_id'])
        else:
            update(database.DB_PATH, 'UPDATE pipeline_target_reservations SET token=?', ('f' * 32,))
        return result

    monkeypatch.setattr(Journal, 'arm', arm)
    code, refused = run('renew-fix-heartbeat', prepared['lease'])
    assert code == 2 and refused['action'] == 'error'
    assert peer.posts == before


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
@pytest.mark.parametrize('index_flag', ['--assume-unchanged', '--skip-worktree'])
def test_public_factory_gate_refuses_hidden_modified_tracked_bytes(pipeline_case, index_flag):
    peer, health, path, task, database, config = pipeline_case
    code, selected = cli(path, 'next', 'fix')
    assert code == 0 and selected['action'] == 'pipeline-fix', selected
    code, prepared = cli(path, 'prepare-fix-start', 'fix', '--lease', selected['lease'])
    assert code == 0, prepared
    request = starts.decode(prepared['lease'])
    scope = request['scope']
    workspace = Workspace(database.DB_PATH, scope, task.working_dir)
    checkout = workspace.prepare()
    git(checkout, 'update-index', index_flag, '--', 'app.py')
    (checkout / 'app.py').write_text('unreviewed hidden bytes', encoding='utf-8')
    assert git(checkout, 'status', '--porcelain=v1') == ''
    OneUseStart(database.DB_PATH, create=False, allow_new=False)._consume(scope)
    code, result = cli(path, 'gate-fix-start', 'fix', '--lease', prepared['lease'])
    assert code == 2, 'public gate accepted bytes different from reviewed commit: ' + str(result.get('action'))


def test_public_initial_fix_preflight_leaves_no_descendant_after_timeout(pipeline_case, monkeypatch, tmp_path):
    peer, health, path, task, database, config = pipeline_case
    flag = tmp_path / 'escaped-initial-control'
    child = 'import pathlib,time; time.sleep(3); pathlib.Path(' + repr(str(flag)) + ').write_text("survived")'
    parent = ('import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child)
        + '],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,'
          'creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)); time.sleep(30)')
    queue = dict(id='fix', execution=dict(mode='auto', review_routing_protocol='review-routing-v1',
        command=[sys.executable, '-c', parent, 'next', 'fix'], timeout_seconds=1))
    monkeypatch.setattr(insights, '_matching_queue', lambda _task: ('p', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, '_tool_available', lambda *args: (True, ''))
    monkeypatch.setattr(insights, 'load_providers', lambda: {})
    monkeypatch.setattr(insights, 'dispatch_gate', lambda _task: None)
    monkeypatch.setattr(worker, 'effective_prompt', lambda _task: 'Project - FIX')
    monkeypatch.setattr(worker, 'build_cmd', lambda *args, **kwargs: pytest.fail('provider fallback'))
    task.series_id = task.id
    worker._execute_task_body(task)
    saved = database.get_task(task.id)
    assert saved.status.value == 'completed'
    time.sleep(3.5)
    assert not flag.exists(), 'initial public next fix control left a live descendant after task completion'


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_published_success_retains_completion_intent_on_sqlite_recovery(technical_worker, monkeypatch):
    peer, path, task, database, calls, factories, pushes, remote, ref, mode = technical_worker
    original = worker._mark_completed
    attempts = []
    notifications = []
    monkeypatch.setattr(worker, '_notify_pipeline_completion',
        lambda current, verdict: notifications.append((current.id, current.status.value, verdict)))

    def busy_once(current, *args, **kwargs):
        if current.id == task.id and not attempts:
            attempts.append(True)
            raise sqlite3.OperationalError('database is locked: finite independent completion probe')
        return original(current, *args, **kwargs)

    monkeypatch.setattr(worker, '_mark_completed', busy_once)
    with pytest.raises(worker.ProviderOwnershipError) as caught:
        worker._execute_task_body(task)
    state = ledger(peer, path)
    assert len(factories) == len(pushes) == 1
    assert 'fix-done' in state.attempt_phases(state.attempts[0])
    assert database.get_task(task.id).status.value == 'running'
    assert worker._fail_stuck(task, caught.value)
    saved = database.get_task(task.id)
    assert saved.status.value == 'completed', 'recovery replaced an already-published successful result with ' + saved.status.value
    assert notifications == [(task.id, 'completed', '\u0413\u041e\u0422\u041e\u0412\u041e')]


@pytest.mark.parametrize('settlement_case', ['author'], indirect=True)
def test_public_settlement_retains_task_and_reservation_on_control_cleanup_failure(settlement_case, monkeypatch):
    case = settlement_case
    task = case['task']
    database = case['database']
    prior_control = insights._tool_preflight
    prior_start = OwnedProcess.start
    held = []
    permit = {'close': False}

    def start(*args, **kwargs):
        tree = prior_start(*args, **kwargs)
        original_close = tree.close
        held.append((tree, original_close))

        def close():
            if not permit['close']:
                raise OSError('finite private control cleanup refusal')
            return original_close()

        tree.close = close
        return tree

    def control(execution, command, directory, *, env_extra=None):
        if 'complete-fix-settlement' in command:
            return run_control([sys.executable, '-c', 'print("private complete control")'],
                env=env_extra, capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return prior_control(execution, command, directory, env_extra=env_extra)

    monkeypatch.setattr(OwnedProcess, 'start', start)
    monkeypatch.setattr(insights, '_tool_preflight', control)
    try:
        with pytest.raises(worker.ProviderOwnershipError) as caught:
            worker.execute_task(task)
        assert len(held) == 1
        assert database.get_task(task.id).status.value == 'running'
        assert any(row['task_id'] == task.id for row in database.list_pipeline_target_reservations())
        assert worker._fail_stuck(task, caught.value) is False
        assert database.get_task(task.id).status.value == 'running'
        permit['close'] = True
        assert worker._fail_stuck(task, caught.value)
        assert database.get_task(task.id).status.value == 'failed'
        assert close_controls(task.id) == ''
        assert all(tree.process.poll() is not None for tree, _ in held)
        assert not case['factories'] and not case['deletes']
    finally:
        permit['close'] = True
        close_controls(task.id)
        for tree, original_close in held:
            original_close()
            tree.process.wait(timeout=10)
