"""Independent finite public-entrypoint probes for exact PR 227 HEAD."""
from contextlib import redirect_stdout
from io import StringIO
import json
import subprocess
import sys
import time

import pytest

from promptpilot import pipeline_insights as insights, project_pipeline as pp, worker
from promptpilot.review_routing_execution import OneUseStart
from promptpilot.review_routing_start_request import decode
from promptpilot.review_routing_workspace import Workspace
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_revocation import revocation_case as revocation_case  # noqa: F401
from test_review_routing_settlement_public import settlement_case as settlement_case  # noqa: F401
from test_review_routing_workspace import git


def cli(path, *args):
    stream = StringIO()
    with redirect_stdout(stream):
        code = pp.run(['--config', str(path), *args])
    return code, json.loads(stream.getvalue())


def test_public_fix_availability_probe_owns_descendants(pipeline_case, monkeypatch, tmp_path):
    _peer, _health, _path, task, database, _config = pipeline_case
    flag = tmp_path / 'late-probe-child'
    child = 'import pathlib,time; time.sleep(2.5); pathlib.Path(' + repr(str(flag)) + ').write_text("survived")'
    parent = ('import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child)
              + '],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,'
                'creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)); time.sleep(15)')
    queue = dict(id='fix', execution=dict(mode='auto', review_routing_protocol='review-routing-v1',
        command=[sys.executable, '-c', 'print("unused")', 'next', 'fix'],
        probe_command=[sys.executable, '-c', parent], probe_timeout_seconds=1))
    monkeypatch.setattr(insights, '_matching_queue', lambda _: ('private', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, 'dispatch_gate', lambda _: None)
    monkeypatch.setattr(worker, 'effective_prompt', lambda _: 'Project - FIX')
    monkeypatch.setattr(worker, 'build_cmd', lambda *a, **kw: pytest.fail('provider fallback'))
    task.series_id = task.id
    worker.execute_task(task)
    assert database.get_task(task.id).status.value == 'completed'
    time.sleep(3)
    assert not flag.exists(), 'availability probe descendant executed after the public FIX task was completed'


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_start_rejects_tracked_bytes_hidden_by_clean_filter(pipeline_case):
    _peer, _health, path, task, database, _config = pipeline_case
    code, selection = cli(path, 'next', 'fix')
    assert code == 0 and selection['action'] == 'pipeline-fix', selection
    code, prepared = cli(path, 'prepare-fix-start', 'fix', '--lease', selection['lease'])
    assert code == 0, prepared
    scope = decode(prepared['lease'])['scope']
    workspace = Workspace(database.DB_PATH, scope, task.working_dir)
    checkout = workspace.prepare()
    common = git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    from pathlib import Path
    attributes = Path(common) / 'info' / 'attributes'
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text('app.py filter=reviewmask\n', encoding='utf-8')
    git(checkout, 'config', 'filter.reviewmask.clean', 'git show HEAD:app.py')
    (checkout / 'app.py').write_text('unreviewed executable source hidden by clean filter', encoding='utf-8')
    git(checkout, 'add', '--', 'app.py')
    assert git(checkout, 'status', '--porcelain=v1') == ''
    assert git(checkout, 'ls-files', '-v', 'app.py') == 'H app.py'
    OneUseStart(database.DB_PATH, create=False, allow_new=False)._consume(scope)
    code, result = cli(path, 'gate-fix-start', 'fix', '--lease', prepared['lease'])
    assert code == 2, 'public source gate admitted different tracked bytes: ' + str(result.get('action'))


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_public_start_cannot_reconstruct_local_issuance_deleted_after_prepare(pipeline_case):
    _peer, _health, path, task, database, _config = pipeline_case
    code, selection = cli(path, 'next', 'fix')
    assert code == 0, selection
    code, prepared = cli(path, 'prepare-fix-start', 'fix', '--lease', selection['lease'])
    assert code == 0, prepared
    scope = decode(prepared['lease'])['scope']
    Workspace(database.DB_PATH, scope, task.working_dir).prepare()
    from test_review_routing_execution import update
    update(database.DB_PATH, 'DELETE FROM technical_fix_starts_v1')
    code, result = cli(path, 'gate-fix-start', 'fix', '--lease', prepared['lease'])
    assert code == 2 and result['action'] == 'error', result
    code, result = cli(path, 'prepare-fix-start', 'fix', '--lease', selection['lease'])
    assert code == 2 and result['action'] == 'error', result


@pytest.mark.parametrize('settlement_case', ['author'], indirect=True)
@pytest.mark.parametrize('fault', ['cancel', 'reservation-token'])
def test_public_author_settlement_rechecks_local_owner_before_delete(settlement_case, monkeypatch, fault):
    from promptpilot.review_routing_revocation import Revocation
    case = settlement_case
    original = Revocation._schema
    tripped = []
    def after_wait(observer, connection, *, create=False):
        result = original(observer, connection, create=create)
        if create and not tripped:
            tripped.append(True)
            if fault == 'cancel':
                connection.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                    ('cancel_task:' + str(case['task'].id), '1'))
            else:
                connection.execute('UPDATE pipeline_target_reservations SET token=? WHERE task_id=?',
                    ('f' * 32, case['task'].id))
        return result
    monkeypatch.setattr(Revocation, '_schema', after_wait)
    worker.execute_task(case['task'])
    assert tripped and not case['deletes'] and not case['factories']
    assert case['database'].get_task(case['task'].id).verdict != '\u0413\u041e\u0422\u041e\u0412\u041e'


def test_overlapping_public_start_issuers_cannot_recreate_a_spent_missing_row(pipeline_case, monkeypatch):
    """Interleave two NEW issuers at the pre-POST boundary, without threads."""
    from promptpilot.review_routing_transport import Transport
    from promptpilot.process_tree import OwnedProcess
    from test_review_routing_start import metadata_launch
    from test_review_routing_execution import update
    peer, _health, path, _task, database, _config = pipeline_case
    code, selection = cli(path, 'next', 'fix')
    assert code == 0 and selection['action'] == 'pipeline-fix', selection
    original = Transport.publish
    interleaved = []
    factories = []
    def launch(prepared):
        scope = decode(prepared['lease'])['scope']
        def factory():
            factories.append(True)
            return OwnedProcess.start([sys.executable, '-c', 'print("finite review child")'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tree = metadata_launch(peer, str(path), prepared['lease'], factory=factory)
        try:
            tree.process.wait(timeout=10)
            OneUseStart(database.DB_PATH, create=False, allow_new=False).confirm_stopped(scope, tree)
        finally:
            tree.close()
            tree.process.wait(timeout=10)
        return scope
    def pause_new_issuer(transport, payload, **kwargs):
        if payload.get('kind') == 'phase' and payload.get('phase') == 'started' and not interleaved:
            interleaved.append(True)
            # Issuer A has already computed can_prepare=True. Issuer B runs
            # to completion while A is suspended before its START publication.
            code, first = cli(path, 'prepare-fix-start', 'fix', '--lease', selection['lease'])
            assert code == 0, first
            scope = launch(first)
            assert OneUseStart(database.DB_PATH).status(scope)['state'] == 'stopped'
            # Simulate loss of the exact local issuance, retaining server
            # START, established-store marker, task and reservation.
            update(database.DB_PATH, 'DELETE FROM technical_fix_starts_v1')
        return original(transport, payload, **kwargs)
    monkeypatch.setattr(Transport, 'publish', pause_new_issuer)
    code, second = cli(path, 'prepare-fix-start', 'fix', '--lease', selection['lease'])
    if code == 0:
        launch(second)
    assert factories == [True], 'overlapping public issuers rebuilt lost spent issuance and invoked a second factory'


@pytest.mark.parametrize('author_bridge', ['real-source'], indirect=True)
def test_actual_public_workspace_gate_cannot_launch_twice_after_overlapping_issuance(pipeline_case, monkeypatch):
    import test_review_routing_start as launch_seam
    _peer, _health, _path, task, database, _config = pipeline_case
    def public_launch(peer, config_path, token, *, factory):
        scope = decode(token)['scope']
        workspace = Workspace(database.DB_PATH, scope, task.working_dir)
        workspace.prepare()
        def gate(consumed):
            assert consumed == scope
            code, result = cli(config_path, 'gate-fix-start', 'fix', '--lease', token)
            assert code == 0 and result['action'] == 'start-allowed', result
            return True
        return OneUseStart(database.DB_PATH, create=False, allow_new=False).invoke(
            scope, final_guard=gate, factory=factory)
    monkeypatch.setattr(launch_seam, 'metadata_launch', public_launch)
    test_overlapping_public_start_issuers_cannot_recreate_a_spent_missing_row(pipeline_case, monkeypatch)
