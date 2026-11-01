"""Declared checks through public workflow API and real gate processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from promptpilot import gate_execution, workflows
from test_workflow_gate_serial import gating, journal, post
from test_workflow_pause_race import request


def check_file(root, source="print('declared check passed')"):
    (root/'check.py').write_text(source, encoding='utf-8')
    return dict(argv=[sys.executable, 'check.py'], inputs=['check.py'], repeat='per-attempt')


def test_declared_public_execution_is_argv_once(isolated_db, tmp_path, monkeypatch):
    spec = check_file(tmp_path, "from pathlib import Path; Path('effects').write_text('x'); print('PASS')")
    item = gating(isolated_db, tmp_path, commands=[spec, spec])
    seen = []
    original = workflows.run_owned

    def observe(argv, **kwargs):
        seen.append(argv)
        assert argv == [str(Path(sys.executable).resolve()), 'check.py']
        return original(argv, **kwargs)

    monkeypatch.setattr(workflows, 'run_owned', observe)
    response = post(item)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'reviewing'
    assert (tmp_path/'effects').read_text() == 'x'
    assert len(seen) == 1
    assert post(item).status_code == 200
    assert len(seen) == 1
    row = journal(isolated_db, item.id)[0]
    assert row['state'] == 'APPLIED'
    scope = json.loads(row['scope_json'])
    assert scope['inputs']['check.py']['sha256']
    assert scope['commands'] == [spec]


@pytest.mark.parametrize('fault', ['missing-input', 'missing-program', 'directory'])
def test_all_commands_preflight_before_any_spawn(isolated_db, tmp_path, monkeypatch, fault):
    good = check_file(tmp_path)
    bad = dict(good)
    if fault == 'missing-input':
        bad['inputs'] = ['absent.py']
    elif fault == 'missing-program':
        bad['argv'] = ['promptpilot-intentionally-absent-program-145']
    else:
        (tmp_path/'directory').mkdir()
        bad['inputs'] = ['directory']
    item = gating(isolated_db, tmp_path, commands=[good, bad])
    monkeypatch.setattr(workflows, 'run_owned', lambda *a, **kw: pytest.fail('invalid scope entered factory'))
    response = post(item)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'awaiting_human'
    events = isolated_db.list_workflow_events(item.id, limit=100)
    assert not any(x.event_type == 'gate.passed' for x in events)


def test_modified_declared_input_does_not_apply_stale_pass(isolated_db, tmp_path, monkeypatch):
    item = gating(isolated_db, tmp_path, commands=[check_file(tmp_path)])
    original = workflows.run_owned

    def change_after_exit(*args, **kwargs):
        result = original(*args, **kwargs)
        (tmp_path/'check.py').write_text("print('different input')", encoding='utf-8')
        return result

    monkeypatch.setattr(workflows, 'run_owned', change_after_exit)
    response = post(item)
    assert response.status_code == 200, response.text
    assert response.json()['status'] != 'reviewing'
    assert journal(isolated_db, item.id)[0]['state'] == 'UNKNOWN'
    assert not any(x.event_type == 'gate.passed' for x in isolated_db.list_workflow_events(item.id, limit=100))


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory junction boundary')
@pytest.mark.parametrize('replace_after_exit', [False, True])
def test_junction_input_cannot_escape_or_apply_pass(isolated_db, tmp_path, monkeypatch, replace_after_exit):
    repo, outside = tmp_path/'repo', tmp_path/'outside'
    repo.mkdir(); outside.mkdir()
    (outside/'check.py').write_text("from pathlib import Path; Path('outside-effect').write_text('ran')", encoding='utf-8')
    link = repo/'linked'

    def junction():
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True, text=True, errors='replace', timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stdout+result.stderr

    if replace_after_exit:
        link.mkdir()
        (link/'check.py').write_text("print('repository input')", encoding='utf-8')
    else:
        junction()
    try:
        command = dict(argv=[sys.executable, 'linked/check.py'], inputs=['linked/check.py'], repeat='per-attempt')
        item = gating(isolated_db, repo, commands=[command])
        original = workflows.run_owned

        def execute(*args, **kwargs):
            if not replace_after_exit:
                pytest.fail('junction input entered the process factory')
            result = original(*args, **kwargs)
            # Move only the known private fixture directory before substituting
            # a junction. The target directory is preserved by final cleanup.
            assert link.resolve().is_relative_to(tmp_path.resolve())
            link.rename(repo/'original-input')
            junction()
            return result

        monkeypatch.setattr(workflows, 'run_owned', execute)
        response = post(item)
        assert response.status_code == 200, response.text
        assert response.json()['status'] != 'reviewing'
        assert not (repo/'outside-effect').exists()
        assert not any(x.event_type == 'gate.passed' for x in isolated_db.list_workflow_events(item.id, limit=100))
        if replace_after_exit:
            assert journal(isolated_db, item.id)[0]['state'] == 'UNKNOWN'
    finally:
        # rmdir unlinks this junction itself; never recurse into its target.
        if link.exists() and link.lstat().st_file_attributes & 0x400:
            link.rmdir()


def test_disabled_declared_gate_does_not_read_missing_inputs(isolated_db, tmp_path, monkeypatch):
    spec = dict(argv=['absent-command'], inputs=['missing.py'], repeat='per-attempt')
    item = gating(isolated_db, tmp_path, commands=[spec], enabled=False)
    monkeypatch.setattr(workflows, 'run_owned', lambda *a, **kw: pytest.fail('disabled check spawned'))
    assert post(item).json()['status'] == 'reviewing'


@pytest.mark.parametrize('change', [
    {'repeat': 'always'}, {'repeat': None}, {'inputs': ['../outside']},
    {'inputs': ['/absolute']}, {'inputs': ['a//b']}, {'inputs': ['a', 'a']},
    {'inputs': ['C:/private']}, {'argv': []}, {'argv': ['bad\0program']}, {'shell': True},
])
def test_invalid_declared_contract_rejected_at_public_create(isolated_db, tmp_path, change):
    command = dict(argv=['python', 'check.py'], inputs=['check.py'], repeat='per-attempt')
    command.update(change)
    response = request('POST', '/api/workflows', json={
        'slug': 'invalid-declared', 'objective': 'Reject invalid gate contract',
        'repository_path': str(tmp_path), 'candidate_branch': 'feature/declared',
        'config': {'gate': {'commands': [command]}},
    })
    assert response.status_code == 422, response.text


def test_declared_setup_validation_and_api_round_trip(isolated_db, tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    spec = check_file(tmp_path)
    response = request('POST', '/api/workflows/validate-setup', json={
        'repository_path': str(tmp_path), 'candidate_branch': 'feature/declared',
        'gate_commands': [spec],
    })
    assert response.status_code == 200, response.text
    assert not any(x['status'] == 'error' and x['code'].startswith('gate') for x in response.json()['checks'])
    created = request('POST', '/api/workflows', json={
        'slug': 'declared-roundtrip', 'objective': 'Persist explicit argv and inputs',
        'repository_path': str(tmp_path), 'candidate_branch': 'feature/declared',
        'config': {'gate': {'commands': [spec, 'git status --porcelain']}},
    })
    assert created.status_code == 201, created.text
    data = created.json()
    loaded = request('GET', '/api/workflows/'+data['id'])
    assert loaded.json()['config']['gate']['commands'] == [spec, 'git status --porcelain']


def test_stage_inputs_must_be_deliverables_public_plan(isolated_db, tmp_path):
    created = request('POST', '/api/workflows', json={
        'slug': 'declared-plan', 'objective': 'Check declared stage deliverables',
        'repository_path': str(tmp_path), 'candidate_branch': 'feature/declared',
        'config': {'planning': {'enabled': True}},
    })
    assert created.status_code == 201, created.text
    item = created.json()
    stage = dict(code='S1', title='Check', objective='Run the declared check',
                 acceptance_gates=[check_file(tmp_path)], deliverables=[])
    response = request('PUT', '/api/workflows/'+item['id']+'/plan', json={
        'expected_version': item['state_version'], 'stages': [stage],
    })
    assert response.status_code == 422 and 'deliverables' in response.text


def test_legacy_snapshot_retains_old_shape(isolated_db, tmp_path):
    item = gating(isolated_db, tmp_path)
    with isolated_db._connect() as conn:
        saved = gate_execution.snapshot(conn, item)
    assert 'inputs' not in saved and 'programs' not in saved and 'validation_errors' not in saved
    assert isinstance(saved['commands'][0], str)


def test_real_editor_roundtrip(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for real JavaScript editor check')
    source = Path(__file__).parents[1]
    result = subprocess.run([node, 'tests/declared_gate_editor.cjs'], cwd=source,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
