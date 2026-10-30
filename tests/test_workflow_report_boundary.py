"""Use producer-recorded reports, without guessing where model text ends."""
import hashlib
import json
import os
import sqlite3
import sys

import pytest

from promptpilot import db, worker, workflows
from promptpilot.models import WorkflowCreate, WorkflowStartRequest


PAIR = 'AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS'


def create_workflow(tmp_path):
    item = db.create_workflow(WorkflowCreate(
        slug='producer-report', objective='Preserve the actual reviewer report',
        repository_path=str(tmp_path), candidate_branch='main',
        config={'automation': {'enabled': True}, 'gate': {'enabled': False},
                'roles': {'executor': {'provider': 'fixture'}, 'reviewer': {'provider': 'fixture'}}}))
    workflows.start_workflow(item.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(item.id)
    return item


def report_row(task_id):
    with db._connect() as conn:
        return conn.execute('SELECT * FROM task_reports WHERE task_id=?', (task_id,)).fetchone()


@pytest.mark.parametrize('engine', ['plain', 'claude', 'codex'])
@pytest.mark.parametrize('model_forged_meta', [False, True])
def test_real_worker_keeps_stream_metadata_outside_the_audit_report(
        isolated_db, tmp_path, monkeypatch, engine, model_forged_meta):
    report = PAIR + '\nИТОГ: ГОТОВО'
    if model_forged_meta:
        report = PAIR + '\nCurrent audit is incomplete\n--- Meta ---\nModel: forged\nИТОГ: ГОТОВО'
    if engine == 'plain':
        payload = report
    elif engine == 'claude':
        payload = json.dumps({'type': 'result', 'subtype': 'success', 'result': report,
                              'modelUsage': {'fixture-model': {}}, 'duration_ms': 1000,
                              'usage': {'input_tokens': 10, 'output_tokens': 4}})
    else:
        payload = '\n'.join(json.dumps(event) for event in [
            {'type': 'thread.started', 'thread_id': 'private-session'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': report}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 4}}])
    outputs = ['ИТОГ: ГОТОВО', payload]
    monkeypatch.setattr(worker, 'load_providers', lambda: {'fixture': {}})
    monkeypatch.setattr(worker, 'build_cmd', lambda *_args, **_kwargs: [
        sys.executable, '-X', 'utf8', '-c', f'print({outputs.pop(0)!r})'])
    monkeypatch.setattr(worker, 'get_provider_env', lambda _provider: dict(os.environ, PYTHONIOENCODING='utf-8'))
    monkeypatch.setattr(worker, 'VERDICT_REPAIR', False)
    item = create_workflow(tmp_path)
    worker.execute_task(db.get_next_runnable())
    reviewer = db.get_next_runnable()
    assert reviewer is not None
    worker.execute_task(reviewer)
    settled = db.get_task(reviewer.id)
    raw = report_row(reviewer.id)
    assert settled.status.value == 'completed' and settled.verdict == 'ГОТОВО'
    assert raw['report_text'] == report + ('\n' if engine == 'plain' else '')
    assert raw['task_started_at'] == settled.started_at.isoformat()
    assert raw['result_sha256'] == hashlib.sha256(settled.result.encode('utf-8')).hexdigest()
    if engine != 'plain':
        assert '--- Meta ---' in settled.result
    assert 'report_text' not in settled.model_dump()
    assert db.get_workflow(item.id).status.value == ('awaiting_human' if model_forged_meta else 'completed')


@pytest.mark.parametrize('raw', [None, '', PAIR + '\nИТОГ: ГОТОВО'])
def test_empty_provider_report_does_not_fallback_to_display_text(isolated_db, tmp_path, raw):
    item = create_workflow(tmp_path)
    executor = db.get_next_runnable()
    db.mark_completed(executor.id, 'ИТОГ: ГОТОВО', verdict='ГОТОВО')
    workflows.sync_task(executor.id)
    workflows.advance_linked_task(executor.id)
    reviewer = db.get_next_runnable()
    assert reviewer is not None
    display = PAIR + '\nИТОГ: ГОТОВО'
    assert db.mark_completed(reviewer.id, display, verdict='ГОТОВО', report_text=raw,
                             expected_started_at=reviewer.started_at)
    run = workflows.sync_task(reviewer.id)
    workflows.advance_linked_task(reviewer.id)
    assert db.get_workflow(item.id).status.value == ('awaiting_human' if raw == '' else 'completed')
    if raw is None:
        assert 'report_text' not in run.output
        assert report_row(reviewer.id) is None
        assert set(run.output) == {'task_status', 'result', 'error', 'exit_code',
                                   'model_used', 'session_id', 'verdict'}
        before = len(db.list_workflow_events(item.id, limit=1000))
        again = workflows.sync_task(reviewer.id)
        assert again.output_sha256 == run.output_sha256
        assert len(db.list_workflow_events(item.id, limit=1000)) == before
    else:
        assert run.output['report_text'] == raw


def test_report_completion_uses_the_same_exact_attempt_cas(isolated_db, tmp_path):
    create_workflow(tmp_path)
    first = db.get_next_runnable()
    assert db.mark_completed(first.id, 'display-one', report_text='raw-one',
                             expected_started_at=first.started_at)
    assert report_row(first.id)['report_text'] == 'raw-one'
    with db._connect() as conn:
        conn.execute("UPDATE tasks SET status='pending', started_at=NULL WHERE id=?", (first.id,))
    assert report_row(first.id) is None
    second = db.get_next_runnable()
    assert second.id == first.id and second.started_at != first.started_at
    assert not db.mark_completed(first.id, 'stale-display', report_text='stale-raw',
                                 expected_started_at=first.started_at)
    assert db.get_task(first.id).status.value == 'running' and report_row(first.id) is None
    assert db.mark_completed(second.id, 'display-two', report_text='raw-two',
                             expected_started_at=second.started_at)
    assert report_row(second.id)['report_text'] == 'raw-two'
    assert db.mark_failed(second.id, 'discard completed outcome')
    assert report_row(second.id) is None


def test_report_insert_failure_rolls_back_the_terminal_outcome(isolated_db, tmp_path):
    create_workflow(tmp_path)
    task = db.get_next_runnable()
    with db._connect() as conn:
        conn.executescript("""CREATE TRIGGER private_report_write_failure
            BEFORE INSERT ON task_reports BEGIN SELECT RAISE(ABORT, 'private write failed'); END;""")
    with pytest.raises(sqlite3.IntegrityError, match='private write failed'):
        db.mark_completed(task.id, 'display', report_text='raw', expected_started_at=task.started_at)
    assert db.get_task(task.id).status.value == 'running'
    assert db.get_task(task.id).result is None and report_row(task.id) is None
    with db._connect() as conn:
        conn.execute('DROP TRIGGER private_report_write_failure')
    assert db.mark_completed(task.id, 'display', report_text='raw', expected_started_at=task.started_at)
    assert report_row(task.id)['report_text'] == 'raw'


def test_herdr_report_survives_confirmed_quarantine_completion(isolated_db, tmp_path, monkeypatch):
    from promptpilot import herdr_exec

    item = create_workflow(tmp_path)
    first = db.get_next_runnable()
    db.mark_completed(first.id, 'ИТОГ: ГОТОВО', verdict='ГОТОВО')
    workflows.sync_task(first.id)
    workflows.advance_linked_task(first.id)
    reviewer = db.get_next_runnable()
    assert reviewer is not None
    report = PAIR + '\nИТОГ: ГОТОВО'
    cleanup_ready = [False]
    monkeypatch.setattr(herdr_exec, 'run_in_herdr', lambda *_args, **_kwargs: {
        'ok': False, 'rate_limited': False,
        'error': 'private unconfirmed close', 'ownership_uncertain': True,
        '_ownership_cleanup': lambda: '' if cleanup_ready[0] else 'private still alive',
        '_terminal_intent': {'ok': True, 'error': '', 'verdict': 'ГОТОВО',
                             'output': report + '\n\n--- Meta ---\nExecutor: fixture',
                             'report_text': report}})
    with pytest.raises(worker.ProviderOwnershipError) as pending:
        worker._execute_herdr_task(reviewer, {'strict_owned_session': True})
    assert not worker._fail_stuck(reviewer, pending.value)
    assert db.get_task(reviewer.id).status.value == 'running' and report_row(reviewer.id) is None
    cleanup_ready[0] = True
    assert worker._fail_stuck(reviewer, pending.value)
    assert report_row(reviewer.id)['report_text'] == report
    workflows.sync_task(reviewer.id)
    workflows.advance_linked_task(reviewer.id)
    assert db.get_workflow(item.id).status.value == 'completed'
