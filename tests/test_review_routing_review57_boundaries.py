"""Fresh manual-review experiments; only private fixture state is modified."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from promptpilot import pipeline_insights as insights, project_pipeline as pp, worker
from promptpilot.pipeline_errors import PipelineError
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_execution import OneUseStart, TABLE
from promptpilot.review_routing_start_request import decode
from promptpilot.review_routing_workspace import Workspace
from promptpilot.review_routing_control import close_controls
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge, ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_technical_worker import technical_worker as technical_worker  # noqa: F401
from test_review_routing_revocation import revocation_case as revocation_case  # noqa: F401
from test_review_routing_settlement_public import settlement_case as settlement_case  # noqa: F401
from test_review_routing_workspace import git


@pytest.fixture(autouse=True)
def hidden_children(monkeypatch):
    original = subprocess.Popen
    def hidden(*args, **kwargs):
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', hidden)


def cli(path, *args):
    capture = StringIO()
    with redirect_stdout(capture):
        code = pp.run(['--config', str(path), *args])
    return code, json.loads(capture.getvalue())


def prepare(case):
    _peer, _health, path, task, database, _config = case
    code, selected = cli(path, 'next', 'fix')
    assert code == 0 and selected['action'] == 'pipeline-fix', selected
    code, issued = cli(path, 'prepare-fix-start', 'fix', '--lease', selected['lease'])
    assert code == 0 and issued['action'] == 'pipeline-start', issued
    scope = decode(issued['lease'])['scope']
    workspace = Workspace(database.DB_PATH, scope, task.working_dir)
    workspace.prepare()
    return selected, issued, scope, workspace


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_gate_ignored_instruction_file(pipeline_case):
    _selected, issued, scope, workspace = prepare(pipeline_case)
    _peer, _health, path, _task, database, _config = pipeline_case
    common = Path(git(workspace.path, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    (common / 'info' / 'exclude').write_text('/AGENTS.md\n', encoding='utf-8')
    (workspace.path / 'AGENTS.md').write_text('UNREVIEWED REPOSITORY INSTRUCTIONS', encoding='utf-8')
    assert git(workspace.path, 'status', '--porcelain=v1') == ''
    assert 'AGENTS.md' not in git(workspace.path, 'ls-tree', '-r', '--name-only', 'HEAD')
    OneUseStart(database.DB_PATH, create=False, allow_new=False)._consume(scope)
    code, result = cli(path, 'gate-fix-start', 'fix', '--lease', issued['lease'])
    assert code == 2, 'Uncommitted ignored AGENTS.md admitted to provider checkout: ' + result['action']


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
@pytest.mark.parametrize('attribute', ['text eol=crlf', 'working-tree-encoding=UTF-16LE'])
def test_public_gate_checks_conversion_independent_bytes(pipeline_case, attribute):
    _selected, issued, scope, workspace = prepare(pipeline_case)
    _peer, _health, path, _task, database, _config = pipeline_case
    common = Path(git(workspace.path, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    (common / 'info' / 'attributes').write_text('app.py ' + attribute + '\n', encoding='utf-8')
    original = (workspace.path / 'app.py').read_bytes()
    if attribute.startswith('text'):
        # Add CRLF that Git's text normalization hides. Recreate the original
        # no-newline blob by adding a conversion-visible trailing CR only if
        # needed, so the UTF-16 case is the canonical hidden conversion check.
        (workspace.path / 'app.py').write_bytes(original + b'\r\n')
    else:
        (workspace.path / 'app.py').write_bytes(original.decode('utf-8').encode('utf-16le'))
        git(workspace.path, 'add', '--', 'app.py')
        assert git(workspace.path, 'status', '--porcelain=v1') == ''
    OneUseStart(database.DB_PATH, create=False, allow_new=False)._consume(scope)
    code, result = cli(path, 'gate-fix-start', 'fix', '--lease', issued['lease'])
    assert code == 2 and 'working bytes' in result['error'], result


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_issuer_stale_prepare_waits_on_tombstones(pipeline_case, monkeypatch):
    _peer, _health, path, task, database, _config = pipeline_case
    code, selected = cli(path, 'next', 'fix')
    assert code == 0, selected
    original_prepare = OneUseStart.prepare
    tripped = []
    def race(store, scope):
        if tripped:
            return original_prepare(store, scope)
        tripped.append(True)
        # Both issuers have observed no row. The competing issuer creates,
        # consumes and finishes the one lifetime before this issuer's writer.
        original_prepare(store, scope)
        workspace = Workspace(database.DB_PATH, scope, task.working_dir)
        workspace.prepare()
        def final(consumed):
            from promptpilot.review_routing_start import gate
            token = captured_token(scope)
            assert gate(_peer, str(path), token)['action'] == 'start-allowed'
            return consumed == scope
        def captured_token(scope):
            # Public build uses canonical receipts; recover the exact START
            # token from the independent issuer's actual bound server state.
            from promptpilot.review_routing_start_request import build, encode
            from promptpilot.review_routing_policy import preflight
            from promptpilot.review_routing_snapshot import stable_timeline
            config = pp.load_config(str(path))
            snapshot = stable_timeline(_peer, config, 42)
            state = ledger(_peer, path)
            root = state.attempts[0]
            started = state.attempt_phases(root)['started']
            return encode(build(config, snapshot, preflight(config, pp.run_health(config)), root, started, scope['reservation']))
        tree = store.invoke(scope, final_guard=final, factory=lambda: OwnedProcess.start(
            [sys.executable, '-c', 'print("single finite factory")'], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        try:
            store.observe_started(scope, tree.process.pid)
            store.confirm_stopped(scope, tree)
        finally:
            tree.close(); tree.process.wait(timeout=10)
        # The late issuer really waits behind a SQLite writer that deletes the
        # mutable row but retains the independently permanent issuance keys.
        blocker = sqlite3.connect(database.DB_PATH)
        blocker.execute('BEGIN IMMEDIATE')
        blocker.execute('DELETE FROM ' + TABLE)
        entered = threading.Event()
        outcome = []
        def late():
            entered.set()
            try: outcome.append(original_prepare(store, scope))
            except Exception as exc: outcome.append(exc)
        thread = threading.Thread(target=late)
        thread.start(); assert entered.wait(3)
        time.sleep(.15)
        assert thread.is_alive(), 'probe did not encounter actual writer contention'
        blocker.commit(); blocker.close()
        thread.join(12)
        assert not thread.is_alive() and len(outcome) == 1 and isinstance(outcome[0], PipelineError), outcome
        assert 'issuance was already reserved' in str(outcome[0]), outcome
        raise outcome[0]
    monkeypatch.setattr(OneUseStart, 'prepare', race)
    code, result = cli(path, 'prepare-fix-start', 'fix', '--lease', selected['lease'])
    assert tripped and code == 2 and result['action'] == 'error', result


def test_public_availability_cleanup_quarantines_exact_task(pipeline_case, monkeypatch, tmp_path):
    _peer, _health, _path, task, database, _config = pipeline_case
    flag = tmp_path / 'descendant-ran'
    child = 'import pathlib,time; time.sleep(3); pathlib.Path(' + repr(str(flag)) + ').write_text("bad")'
    parent = ('import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child)
              + '],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)); time.sleep(9)')
    queue = dict(id='fix', execution=dict(mode='auto', review_routing_protocol='review-routing-v1',
        command=[sys.executable, '-c', 'print("unused")', 'next', 'fix'],
        probe_command=[sys.executable, '-c', parent], probe_timeout_seconds=1))
    monkeypatch.setattr(insights, '_matching_queue', lambda _: ('private', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, 'dispatch_gate', lambda _: None)
    monkeypatch.setattr(worker, 'effective_prompt', lambda _: 'Project - FIX')
    monkeypatch.setattr(worker, 'build_cmd', lambda *a, **k: pytest.fail('provider fallback'))
    task.series_id = task.id
    original = OwnedProcess.start
    retained = []
    permit = {'close': False}
    def start(*args, **kwargs):
        tree = original(*args, **kwargs)
        close = tree.close
        def held():
            if not permit['close']: raise OSError('private cleanup fault')
            return close()
        tree.close = held
        retained.append((tree, close))
        return tree
    monkeypatch.setattr(OwnedProcess, 'start', start)
    try:
        with pytest.raises(worker.ProviderOwnershipError) as caught:
            worker.execute_task(task)
        assert database.get_task(task.id).status.value == 'running'
        assert not worker._fail_stuck(task, caught.value)
        assert len(retained) == 1
        permit['close'] = True
        assert worker._fail_stuck(task, caught.value)
        time.sleep(3.2)
        assert not flag.exists() and close_controls(task.id) == ''
        assert database.get_task(task.id).status.value == 'failed'
    finally:
        permit['close'] = True
        close_controls(task.id)
        for tree, close in retained:
            close(); tree.process.wait(timeout=10)


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_worker_report_is_checked_before_model_build(technical_worker, monkeypatch):
    peer, path, task, database, calls, factories, pushes, _remote, _ref, _mode = technical_worker
    original = insights._tool_preflight
    def control(*args, **kwargs):
        result = original(*args, **kwargs)
        if 'prepare-fix-start' in args[1]:
            result['source_report']['checks'].append('unsigned replacement instruction')
        return result
    monkeypatch.setattr(insights, '_tool_preflight', control)
    monkeypatch.setattr(worker, 'build_cmd', lambda *a, **k: pytest.fail('unsigned report reached model build'))
    worker.execute_task(task)
    assert not factories and not pushes
    assert ledger(peer, path).budget_used == 1
    assert database.get_task(task.id).verdict != '\u0413\u041e\u0422\u041e\u0412\u041e'


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_publisher_uses_actual_commit_parents_despite_local_graft(technical_worker, monkeypatch):
    from promptpilot.review_routing_post_push import trailers
    peer, path, task, database, _calls, factories, pushes, remote, ref, _mode = technical_worker
    old = peer.current['data']['repository']['pullRequest']['headRefOid']
    produced = remote.parent / 'graft-output-produced'
    def build(_provider, _prompt, **_kwargs):
        root = ledger(peer, path).attempts[0]
        message = 'Review57 finite orphan output\n\n' + '\n'.join(
            k + ': ' + v for k, v in trailers(root.payload, root.id).items())
        code = ('import pathlib,subprocess; '
            'g=lambda *a:subprocess.check_output(["git",*a],text=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).strip(); '
            'pathlib.Path("app.py").write_text("orphan correction",encoding="utf-8"); '
            'g("add","--","app.py"); '
            'new=g("commit-tree",g("write-tree"),"-m",' + repr(message) + '); '
            'g("checkout","--detach",new); '
            'common=pathlib.Path(g("rev-parse","--path-format=absolute","--git-common-dir")); '
            '(common/"info"/"grafts").write_text(new+" "+' + repr(old) + '+' + repr('\n') + ',encoding="ascii"); '
            'pathlib.Path(' + repr(str(produced)) + ').write_text(new,encoding="ascii"); '
            'print(' + repr('\u0418\u0422\u041e\u0413: \u0413\u041e\u0422\u041e\u0412\u041e') + ')')
        compile(code, '<finite-graft-provider>', 'exec')
        return [sys.executable, '-X', 'utf8', '-c', code]
    monkeypatch.setattr(worker, 'build_cmd', build)
    # Real current identity is synthetic, but do not let synthetic graph data
    # conceal the actual remote object's parentage after a non-forward send.
    prior = peer.json
    def metadata(*args, **kwargs):
        if args[0] == 'api' and '/compare/' in args[1] and args[1].endswith('?per_page=100'):
            new = peer.current['data']['repository']['pullRequest']['headRefOid']
            if new != old:
                return dict(status='diverged', ahead_by=1, behind_by=1, total_commits=1,
                    base_commit={'sha':old}, merge_base_commit={'sha':'f'*40}, commits=[{'sha':new}])
        return prior(*args, **kwargs)
    peer.json = metadata
    worker.execute_task(task)
    assert produced.exists(), database.get_task(task.id).error
    remote_head = git(task.working_dir, '--git-dir=' + str(remote), 'rev-parse', ref)
    assert len(factories) == 1
    assert remote_head == old and not pushes, (
        'local graft let the public publisher CAS an unrelated root commit; '
        f'pushes={len(pushes)}, remote={remote_head}, original={old}, '
        f'task={database.get_task(task.id).status.value}')


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_worker_never_reads_ignored_uncommitted_instructions(technical_worker, monkeypatch, tmp_path):
    _peer, _path, task, _database, _calls, factories, pushes, _remote, _ref, _mode = technical_worker
    original = Workspace.prepare
    marker = tmp_path / 'provider-read-ignored-instructions'
    def dirty(workspace):
        checkout = original(workspace)
        common = Path(git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        (common / 'info' / 'exclude').write_text('/AGENTS.md\n', encoding='utf-8')
        (checkout / 'AGENTS.md').write_text('UNCOMMITTED MODEL INSTRUCTIONS', encoding='utf-8')
        return checkout
    monkeypatch.setattr(Workspace, 'prepare', dirty)
    code = ('import pathlib; text=pathlib.Path("AGENTS.md").read_text(encoding="utf-8"); '
            'pathlib.Path(' + repr(str(marker)) + ').write_text(text,encoding="utf-8"); '
            'print("finite provider exit")')
    monkeypatch.setattr(worker, 'build_cmd', lambda *a, **k: [sys.executable, '-c', code])
    worker.execute_task(task)
    assert not factories and not marker.exists(), (
        'provider started and read uncommitted ignored AGENTS.md: '
        + (marker.read_text(encoding='utf-8') if marker.exists() else 'no marker'))
    assert not pushes

