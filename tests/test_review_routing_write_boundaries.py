import json
from pathlib import Path
from datetime import timezone
from contextlib import redirect_stdout
from io import StringIO

import pytest

from test_review_routing_stage_guard import setup_guard  # noqa: F401
from test_review_routing_adapter import adapter as _adapter_fixture
from promptpilot import project_pipeline as pp

adapter=_adapter_fixture


def test_completed_typed_review_wait_is_no_work_in_worker(adapter, isolated_db, monkeypatch, tmp_path):
    from promptpilot import pipeline_insights as insights
    from promptpilot.models import TaskCreate
    peer, _health, path, report, run = adapter
    code, selected = run('next', 'review')
    assert code == 0
    code, completed = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and completed['action'] == 'completed'
    isolated_db.create_task(TaskCreate(prompt='Project - REVIEW', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    queue = {'id': 'review', 'execution': {'mode': 'auto', 'command': ['pipelinectl', 'next', 'review'],
             'review_routing_protocol': 'review-routing-v1'}}
    monkeypatch.setattr(insights, '_matching_queue', lambda task: ('p', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, '_tool_available', lambda *args: (True, ''))
    monkeypatch.setattr(insights, 'load_providers', lambda: {})
    responses = []
    def actual_cli(execution, command, directory, *, env_extra=None):
        with monkeypatch.context() as environment:
            for key, value in (env_extra or {}).items():
                environment.setenv(key, value)
            stream = StringIO()
            with redirect_stdout(stream):
                code = pp.run(['--config', str(path), *command[1:]])
            response = json.loads(stream.getvalue())
            assert code == 0, response
            responses.append(response)
            return response
    monkeypatch.setattr(insights, '_tool_preflight', actual_cli)
    route = insights.execution_route(task, 'fallback', str(tmp_path))
    assert responses[0]['action'] == 'wait', responses
    assert route['action'] == 'complete_empty', route


def test_off_retains_fence_when_the_only_typed_root_is_edited(adapter, monkeypatch):
    from promptpilot.review_routing_publication import Publication
    peer, health, path, report, run = adapter
    def root_only(self):
        self._publish(self.seed)
        return {'action': 'wait', 'reason': 'finite simulated stop after root'}
    monkeypatch.setattr(Publication, 'run', root_only)
    code, selected = run('next', 'review')
    assert code == 0
    code, stopped = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and peer.posts == ['publication'], stopped
    root = peer.comments[0]['node']
    root.update(body='Redacted routing record', lastEditedAt='2026-10-30T09:00:00Z')
    pr = peer.current['data']['repository']['pullRequest']
    pr['headRefOid'] = 'b' * 40
    peer.comments.append({'cursor': 'NEW', 'node': {'__typename': 'PullRequestCommit', 'id': 'NEW', 'commit': {'oid': 'b' * 40}}})
    health['review_candidates'][0]['head'] = 'b' * 40
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['technical_handoff']['enabled'] = False
    path.write_text(json.dumps(raw), encoding='utf-8')
    code, result = run('next', 'review')
    assert code == 0, result
    assert result['action'] == 'wait', result


def test_cancelled_claimed_review_cannot_publish(adapter, isolated_db, monkeypatch, tmp_path):
    from promptpilot.models import TaskCreate
    peer, _health, path, report, run = adapter
    isolated_db.create_task(TaskCreate(prompt='Project - REVIEW', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    monkeypatch.setenv('PP_TASK_ID', str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND', 'headless')
    code, selected = run('next', 'review')
    assert code == 0 and selected['action'] == 'audit', selected
    assert isolated_db.request_cancel(task.id)
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 2 and peer.posts == [], (result, peer.posts)


def test_review_hold_after_database_renewal_prevents_post(adapter, isolated_db, monkeypatch, tmp_path):
    from promptpilot.models import TaskCreate
    peer, _health, path, report, run = adapter
    isolated_db.create_task(TaskCreate(prompt='Project - REVIEW', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    monkeypatch.setenv('PP_TASK_ID', str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND', 'headless')
    code, selected = run('next', 'review')
    assert code == 0 and selected['action'] == 'audit', selected
    original = pp.renew_lease_target_reservation
    def after_wait(*args, **kwargs):
        result = original(*args, **kwargs)
        peer.current['data']['repository']['pullRequest']['labels']['nodes'] = [{'name': 'hold'}]
        return result
    monkeypatch.setattr(pp, 'renew_lease_target_reservation', after_wait)
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 2, result
    assert peer.posts == [], peer.posts


@pytest.mark.parametrize('change',['cancel','attempt','reservation-token','reservation-delete'])
def test_local_claim_changes_after_final_remote_checks_prevent_post(adapter,isolated_db,monkeypatch,tmp_path,change):
    from promptpilot.models import TaskCreate
    from promptpilot.review_routing_publication import Publication
    peer,_health,_path,report,run=adapter
    isolated_db.create_task(TaskCreate(prompt='Project - REVIEW',working_dir=str(tmp_path)))
    task=isolated_db.get_next_runnable()
    monkeypatch.setenv('PP_TASK_ID',str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT',task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND','headless')
    code,selected=run('next','review')
    assert code==0 and selected['action']=='audit',selected
    original=Publication.require_admission
    def changed_after_remote(self,payload,snapshot,**kwargs):
        result=original(self,payload,snapshot,**kwargs)
        if kwargs.get('require_new'):
            if change=='cancel':
                assert isolated_db.request_cancel(task.id)
            else:
                with isolated_db._connect(immediate=True) as connection:
                    if change=='attempt':
                        connection.execute("UPDATE tasks SET started_at='2001-01-01T00:00:00+00:00' WHERE id=?",(task.id,))
                    elif change=='reservation-token':
                        connection.execute("UPDATE pipeline_target_reservations SET token='another-owner' WHERE task_id=?",(task.id,))
                    else:
                        connection.execute('DELETE FROM pipeline_target_reservations WHERE task_id=?',(task.id,))
        return result
    monkeypatch.setattr(Publication,'require_admission',changed_after_remote)
    code,result=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==2 and peer.posts==[],(result,peer.posts)


def test_off_known_local_transaction_survives_absent_remote_marker(adapter,monkeypatch):
    peer,_health,path,report,run=adapter
    code,selected=run('next','review');assert code==0
    code,result=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0 and result['action']=='completed',result
    # Simulate a peer losing marker bodies without a usable deletion event.
    # The existing local transaction is independent refusal evidence.
    peer.comments=[]
    raw=json.loads(path.read_text(encoding='utf-8'))
    raw.pop('technical_handoff')
    path.write_text(json.dumps(raw),encoding='utf-8')
    code,result=run('next','review')
    assert code==0 and result['action']=='wait',result


def test_off_history_probe_is_read_only_and_bound_to_repository_pr(tmp_path):
    from promptpilot.review_routing_journal import Journal
    from test_review_routing_wire import publication
    from promptpilot.review_routing_wire import render
    missing=tmp_path/'absent'
    assert Journal.has_existing_history(missing,'owner/repo',42) is False
    assert not missing.exists()
    directory=tmp_path/'existing'
    journal=Journal(directory)
    value=publication()
    journal.prepare(owner=value['owner'],phase='publication',repository='owner/repo',number=42,body=render(value))
    before=journal.path.read_bytes()
    assert Journal.has_existing_history(directory,'owner/repo',42) is True
    assert Journal.has_existing_history(directory,'owner/repo',43) is False
    assert Journal.has_existing_history(directory,'other/repo',42) is False
    assert journal.path.read_bytes()==before
    assert {p.name for p in Path(directory).iterdir()}=={'review-routing-journal.sqlite3'}
