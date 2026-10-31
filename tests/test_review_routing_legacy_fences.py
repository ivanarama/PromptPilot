import copy
from datetime import timezone
import json

import pytest

from conftest import isolated_db as isolated_db
from test_review_routing_adapter import adapter as adapter
from test_review_routing_stage_guard import setup_guard as setup_guard
from promptpilot import project_pipeline as pp
from promptpilot.models import TaskCreate
from promptpilot.review_routing_publication import Publication


def test_expired_claimed_review_does_not_revive_reservation(adapter, isolated_db, monkeypatch, tmp_path):
    peer, _health, _path, report, run = adapter
    isolated_db.create_task(TaskCreate(prompt='Private REVIEW', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    monkeypatch.setenv('PP_TASK_ID', str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND', 'headless')
    code, selected = run('next', 'review')
    assert code == 0 and selected['action'] == 'audit', selected
    with isolated_db._connect(immediate=True) as connection:
        connection.execute("UPDATE pipeline_target_reservations SET expires_at='2000-01-01T00:00:00+00:00'")
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 2 and peer.posts == [], (code, result, peer.posts)


def root_only(self, **kwargs):
    self._publish(self.seed)
    return {'action': 'wait', 'reason': 'private finite root-only fixture'}


def test_old_off_fallback_gate_refuses_a_new_typed_transaction(adapter, monkeypatch):
    peer, _health, path, report, run = adapter
    raw = json.loads(path.read_text(encoding='utf-8'))
    off = copy.deepcopy(raw)
    off['technical_handoff']['enabled'] = False
    path.write_text(json.dumps(off), encoding='utf-8')
    code, legacy = run('next', 'review')
    assert code == 0 and legacy['action'] == 'fallback', legacy
    path.write_text(json.dumps(raw), encoding='utf-8')
    code, selected = run('next', 'review')
    assert code == 0 and selected['action'] == 'audit', selected
    monkeypatch.setattr(Publication, 'run', root_only)
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and peer.posts == ['publication'], result
    path.write_text(json.dumps(off), encoding='utf-8')
    code, fence = run('next', 'review')
    assert code == 0 and fence['action'] == 'wait', fence
    code, result = run('gate-fallback', 'review', '--lease', legacy['handoff']['lease'])
    assert code == 2, (code, result)


def test_enabled_integration_route_retains_pending_typed_fence(adapter, monkeypatch):
    peer, health, _path, report, run = adapter
    code, selected = run('next', 'review')
    assert code == 0, selected
    monkeypatch.setattr(Publication, 'run', root_only)
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and peer.posts == ['publication'], result
    target = {'number': 42, 'head': 'a'*40, 'stage': 'integration-review'}
    health.update(review_candidates=[target], content_review_candidates=[], integration_owner=target,
                  findings=[{'severity': 'yellow', 'code': 'single_flight_barrier', 'pr': 42}])
    code, result = run('next', 'review')
    assert code != 0 or result['action'] in {'wait', 'empty', 'error'}, (code, result)


def test_semantic_canonical_claim_is_rechecked_after_final_renewal(adapter, monkeypatch):
    from promptpilot.review_routing_wire import parse, render
    peer, _health, _path, report, run = adapter
    code, selected = run('next', 'review')
    assert code == 0, selected
    original = pp.renew_lease_target_reservation
    original_json = peer.json
    def claim_id_gap(*args, **kwargs):
        result = original_json(*args, **kwargs)
        body = (kwargs.get('input_value') or {}).get('body')
        if body is not None and parse(body)['kind'] == 'claim':
            peer.comments[-1]['cursor'] = 'C1500'
            peer.comments[-1]['node'].update(id='IC_1500', fullDatabaseId='1500')
            return {'id': '1500'}
        return result
    peer.json = claim_id_gap
    count = 0
    injected = False
    def after_wait(*args, **kwargs):
        nonlocal count, injected
        result = original(*args, **kwargs)
        count += 1
        # Two renewals for each root/claim/completion. Change the canonical
        # earliest claim after the completion's final DB wait.
        if count == 6:
            assert peer.posts == ['publication', 'claim']
            competing = copy.deepcopy(peer.comments[1])
            competing['cursor'] = 'EARLIER_CLAIM'
            competing['node'].update(id='IC_EARLIER', fullDatabaseId='1400')
            assert parse(competing['node']['body'])['kind'] == 'claim'
            # Same server second, smaller numeric ID wins.
            assert render(parse(competing['node']['body'])) == competing['node']['body']
            peer.comments.insert(1, competing)
            injected = True
        return result
    monkeypatch.setattr(pp, 'renew_lease_target_reservation', after_wait)
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert injected and code == 2 and peer.posts == ['publication', 'claim'], (code, result, peer.posts)


def test_worker_does_not_dispatch_full_skill_around_pending_typed_root(adapter, isolated_db, monkeypatch, tmp_path):
    from contextlib import redirect_stdout
    from io import StringIO
    from promptpilot import pipeline_insights as insights
    peer, health, path, report, run = adapter
    monkeypatch.setenv('PP_PIPELINE_LEASE_KEY_FILE', str(isolated_db.DB_PATH.parent/'pipeline-lease.key'))
    code, selected = run('next', 'review')
    assert code == 0, selected
    monkeypatch.setattr(Publication, 'run', root_only)
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and peer.posts == ['publication'], result
    target = {'number': 42, 'head': 'a'*40, 'stage': 'integration-review'}
    health.update(review_candidates=[target], content_review_candidates=[], integration_owner=target,
                  findings=[{'severity': 'yellow', 'code': 'single_flight_barrier', 'pr': 42}])
    isolated_db.create_task(TaskCreate(prompt='Private REVIEW', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    queue = {'id': 'review', 'execution': {'mode': 'auto', 'command': ['pipelinectl', 'next', 'review'],
                                         'review_routing_protocol': 'review-routing-v1'}}
    monkeypatch.setattr(insights, '_matching_queue', lambda _: ('p', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, '_tool_available', lambda *args: (True, ''))
    monkeypatch.setattr(insights, 'load_providers', lambda: {})
    def actual_cli(_execution, command, _directory, *, env_extra=None):
        with monkeypatch.context() as environment:
            for name, value in (env_extra or {}).items():
                environment.setenv(name, value)
            stream = StringIO()
            with redirect_stdout(stream):
                code = pp.run(['--config', str(path), *command[1:]])
            result = json.loads(stream.getvalue())
            assert code == 0, result
            return result
    monkeypatch.setattr(insights, '_tool_preflight', actual_cli)
    route = insights.execution_route(task, 'full repository skill', str(tmp_path))
    (tmp_path/'integration-route-summary.json').write_text(json.dumps({name: route.get(name) for name in
        ['action', 'mode', 'reason', 'fallback_reason', 'target']}, ensure_ascii=False), encoding='utf-8')
    assert route['action'] != 'prompt', (route['action'], route.get('mode'), route.get('target'))


@pytest.mark.parametrize('change', ['edit', 'delete'])
def test_off_remote_uncertainty_fences_without_a_local_journal(adapter, monkeypatch, tmp_path, change):
    peer, health, path, report, run = adapter
    code, selected = run('next', 'review')
    assert code == 0, selected
    monkeypatch.setattr(Publication, 'run', root_only)
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and peer.posts == ['publication'], result
    if change == 'edit':
        peer.comments[0]['node'].update(body='Redacted', lastEditedAt='2026-10-30T09:00:00Z')
    else:
        peer.comments = [{'cursor': 'DELETION', 'node': {'__typename': 'CommentDeletedEvent', 'id': 'DELETION',
                                                       'createdAt': '2026-10-30T09:00:00Z'}}]
    pr = peer.current['data']['repository']['pullRequest']
    pr['headRefOid'] = 'b'*40
    peer.comments.append({'cursor': 'NEW_HEAD', 'node': {'__typename': 'PullRequestCommit', 'id': 'NEW_HEAD',
                                                       'commit': {'oid': 'b'*40}}})
    health['review_candidates'][0]['head'] = 'b'*40
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw.pop('technical_handoff')
    path.write_text(json.dumps(raw), encoding='utf-8')
    # Simulate a fresh host; preserve all earlier artifacts and journals.
    fresh = tmp_path/'fresh-state'
    monkeypatch.setenv('PP_PIPELINE_STATE_DIR', str(fresh))
    code, result = run('next', 'review')
    assert code == 0 and result['action'] == 'wait', result
    assert not fresh.exists()


def test_old_legacy_fix_admission_and_owner_tokens_refuse_typed_history(isolated_db,monkeypatch,tmp_path):
    from test_parallel_fix_election import CONFIG,HEAD_A,_setup_handoff
    from promptpilot import fix_handoff
    _setup_handoff(isolated_db,monkeypatch,tmp_path,[{'stage':'review','number':10,'head':HEAD_A}])
    elected=pp.next_fix(None,dict(CONFIG))
    admitted=fix_handoff.gate(None,dict(CONFIG),elected['handoff']['lease'])
    assert admitted['action']=='validated'
    history={'edges':[{'node':{'__typename':'IssueComment','author':{'login':'owner'},
                               'body':'<!-- pp:review-routing-v1: hidden typed transaction -->',
                               'lastEditedAt':None}}]}
    monkeypatch.setattr(pp,'stable_timeline',lambda *_:history)
    with pytest.raises(pp.PipelineError,match='existing or uncertain trusted history'):
        fix_handoff.gate(None,dict(CONFIG),elected['handoff']['lease'])
    with pytest.raises(pp.PipelineError,match='existing or uncertain trusted history'):
        fix_handoff.gate(None,dict(CONFIG),admitted['ownership_lease'],ownership_only=True)


def test_old_legacy_tool_review_cannot_publish_over_pending_local_v1(adapter,monkeypatch,tmp_path):
    from promptpilot.review_routing_adapter import journal_directory
    from promptpilot.review_routing_journal import Journal
    from promptpilot.review_routing_wire import render
    from test_review_routing_wire import publication
    peer,health,path,report,run=adapter
    raw=json.loads(path.read_text(encoding='utf-8'))
    off=copy.deepcopy(raw);off['technical_handoff']['enabled']=False
    path.write_text(json.dumps(off),encoding='utf-8')
    pr=peer.current['data']['repository']['pullRequest']
    pr['timelineItems']['edges']=[edge for edge in pr['timelineItems']['edges']
        if edge['node'].get('fullDatabaseId') not in {'110','111','112'}
        and edge['node'].get('id')!='ANCHOR_110']
    health['review_candidates'][0]['review_depth']=1
    code,old=run('next','review')
    assert code==0 and old['action']=='audit',old
    # An independently durable pending typed transaction appears after this
    # legitimate first/second-round lease. It must fence even invisible wire.
    config=pp.load_config(str(path));pending=publication()
    journal=Journal(journal_directory(config))
    journal.prepare(owner=pending['owner'],phase='publication',repository='owner/repo',
                    number=42,body=render(pending))
    legacy_report=tmp_path/'legacy-review.json'
    legacy_report.write_text(json.dumps({'change':'Checked change','checks':['private check'],
                                        'blocking':[],'tail':[]}),encoding='utf-8')
    def forbidden_post(*_args,**_kwargs):
        raise AssertionError('legacy completion must not enter a publication factory')
    monkeypatch.setattr(pp,'post_comment',forbidden_post)
    code,result=run('complete','review','--lease',old['lease'],'--report',str(legacy_report))
    assert code==2 and peer.posts==[],result
    assert 'existing or uncertain trusted history' in result['error']
