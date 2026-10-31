"""Public claimed FIX author handoff with real CLI, scheduler and worker.

These compatible fake-peer tests do not accept the unimplemented pipeline
factory/post-push/settlement bridge or establish actual OneBase compatibility.
"""
import copy
from datetime import timezone
import json
from contextlib import redirect_stdout
from io import StringIO

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_fix_request as requests
from promptpilot.models import TaskCreate
from promptpilot.review_routing_history import verified_depth
from promptpilot.review_routing_snapshot import stable_timeline
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401


@pytest.fixture
def author_bridge(adapter, isolated_db, monkeypatch, tmp_path, request):
    peer, health, path, report, run = adapter
    if getattr(request, 'param', None) == 'fork':
        pr = peer.current['data']['repository']['pullRequest']
        pr['headRepository'] = {'id': 'R_FORK', 'nameWithOwner': 'contributor/repo'}
        pr['isCrossRepository'] = True
    code, selected = run('next', 'review')
    assert code == 0, selected
    code, completed = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0, completed
    health['fix_bridge_contract'] = copy.deepcopy(requests.BRIDGE)
    health['technical_fix_candidates'] = [{'stage': 'fix-pr', 'number': 42, 'head': 'a'*40, 'source': completed['source']}]
    task = isolated_db.create_task(TaskCreate(prompt='Project - FIX', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    assert task is not None
    monkeypatch.setenv('PP_TASK_ID', str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND', 'headless')
    monkeypatch.setenv('PP_PIPELINE_REPLICAS', '1')
    monkeypatch.setenv('PP_PIPELINE_LEASE_KEY_FILE', str(tmp_path/'lease.key'))
    peer.assignment_posts = []
    peer.assignees = [{'node_id': 'U_OTHER', 'login': 'other'}]
    peer.assignment_fault = None
    original = peer.json
    def fake(*args, input_value=None):
        if args == ('api', 'repos/owner/repo/pulls/42'):
            pr = peer.current['data']['repository']['pullRequest']
            return {'node_id': pr['id'], 'number': 42,
                    'base': {'ref': pr['baseRefName'], 'repo': {'node_id': pr['baseRepository']['id'], 'full_name': pr['baseRepository']['nameWithOwner']}},
                    'head': {'ref': pr['headRefName'], 'sha': pr['headRefOid'],
                             'repo': {'node_id': pr['headRepository']['id'], 'full_name': pr['headRepository']['nameWithOwner']}},
                    'assignees': copy.deepcopy(peer.assignees)}
        if args == ('api', 'repos/owner/repo/issues/42/assignees', '--method', 'POST', '--input', '-'):
            peer.assignment_posts.append(copy.deepcopy(input_value))
            if peer.assignment_fault not in {'ignored', 'unknown'}:
                author = peer.current['data']['repository']['pullRequest']['author']
                peer.assignees.append({'node_id': 'U_WRONG' if peer.assignment_fault == 'wrong-id' else author['id'],
                                       'login': author['login']})
            if peer.assignment_fault in {'lost-response', 'unknown'}:
                raise OSError('response lost')
            return {'assignees': copy.deepcopy(peer.assignees)}
        return original(*args, input_value=input_value)
    peer.json = fake
    return peer, health, path, task, run, isolated_db


def ledger(peer, path):
    config = pp.load_config(str(path))
    return verified_depth(stable_timeline(peer, config, 42), trusted='owner')['ledger']


def test_public_claimed_author_route_assigns_node_id_once_without_a_fake_process_start(author_bridge):
    peer, _health, path, _task, run, _db = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0 and selected['action'] == 'author-handoff', selected
    request = requests.decode(selected['lease'])
    assert request['attempt']['executor']['kind'] == 'author'
    for _ in range(2):
        code, result = run('complete', 'fix', '--lease', selected['lease'])
        assert code == 0 and result['action'] == 'handed-off', result
        assert result['provider_started'] is False
    state = ledger(peer, path)
    assert state.budget_used == 1 and len(state.attempts) == 1
    assert set(state.attempt_phases(state.attempts[0])) == {'assigned', 'handoff-done'}
    assert peer.assignment_posts == [{'assignees': [request['author']['login']]}]
    assert any(user['node_id'] == 'U_OTHER' for user in peer.assignees)
    code, waited = run('next', 'fix')
    assert code == 0 and waited['action'] == 'wait'
    assert 'started' not in state.attempt_phases(state.attempts[0])


@pytest.mark.parametrize('fault', ['ignored', 'wrong-id', 'unknown'])
def test_delivery_failure_is_a_spent_attempt_without_handoff_or_pipeline_fallback(author_bridge, fault):
    peer, _health, path, _task, run, _db = author_bridge
    peer.assignment_fault = fault
    code, selected = run('next', 'fix')
    assert code == 0, selected
    for _ in range(2):
        code, result = run('complete', 'fix', '--lease', selected['lease'])
        assert code == 2, result
    state = ledger(peer, path)
    assert state.budget_used == 1 and state.phases == ()
    assert len(peer.assignment_posts) == 1


def test_lost_assignment_response_recovers_only_by_authenticated_readback(author_bridge):
    peer, _health, _path, _task, run, _db = author_bridge
    peer.assignment_fault = 'lost-response'
    code, selected = run('next', 'fix')
    assert code == 0
    code, result = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 0 and result['action'] == 'handed-off', result
    assert len(peer.assignment_posts) == 1


@pytest.mark.parametrize('fault', ['actor', 'hold', 'author', 'fork', 'head-ref', 'reader', 'config',
                                  'peer', 'source', 'cancel', 'task-aba', 'reservation', 'expired', 'pipeline-policy'])
def test_last_public_gates_refuse_changes_without_assignment_or_new_root(author_bridge, fault):
    peer, health, path, task, run, database = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0, selected
    pr = peer.current['data']['repository']['pullRequest']
    if fault == 'actor':
        peer.actor = 'wrong'
    elif fault == 'hold':
        pr['labels']['nodes'] = [{'name': 'hold'}]
    elif fault == 'author':
        pr['author']['id'] = 'U_CHANGED'
    elif fault == 'fork':
        pr['headRepository'] = {'id': 'R_FORK', 'nameWithOwner': 'fork/repo'}
        pr['isCrossRepository'] = True
    elif fault == 'head-ref':
        pr['headRefName'] = 'replacement'
    elif fault == 'reader':
        (path.parent/'.claude/skills/fix-approved/SKILL.md').write_text('replaced', encoding='utf-8')
    elif fault in {'config', 'pipeline-policy'}:
        raw = json.loads(path.read_text(encoding='utf-8'))
        if fault == 'config':
            raw['technical_handoff']['max_extra_attempts'] = 1
        else:
            raw['technical_fix_execution'] = {'executor': 'pipeline'}
        path.write_text(json.dumps(raw), encoding='utf-8')
    elif fault == 'peer':
        health.pop('fix_bridge_contract')
    elif fault == 'source':
        health['technical_fix_candidates'][0]['source']['completion_id'] = '99'
    elif fault == 'cancel':
        database.request_cancel(task.id)
    elif fault in {'task-aba', 'expired'}:
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(database.DB_PATH)) as connection:
            with connection:
                if fault == 'task-aba':
                    connection.execute("UPDATE tasks SET started_at='2027-01-01T00:00:00+00:00' WHERE id=?", (task.id,))
                else:
                    connection.execute("UPDATE pipeline_target_reservations SET expires_at='2000-01-01T00:00:00+00:00'")
    else:
        database.release_pipeline_target_reservations(task.id)
    code, result = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 2, result
    assert peer.posts == ['publication', 'claim', 'completion'] and peer.assignment_posts == []


@pytest.mark.parametrize('purpose', [requests.PROGRESS, requests.START, 'promptpilot-fix-target-v1'])
def test_signed_other_purposes_cannot_be_used_as_admission(author_bridge, purpose):
    peer, _health, _path, _task, run, _db = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0
    value = requests.decode(selected['lease'])
    value['purpose'] = purpose
    value['attempt_id'] = '1003'
    code, result = run('complete', 'fix', '--lease', pp.encode_signed_lease(value))
    assert code == 2, result
    assert peer.assignment_posts == []


@pytest.mark.parametrize('remove_config', [False, True])
@pytest.mark.parametrize('head_changed', [False, True])
def test_off_or_removed_flag_with_existing_state_preserves_a_read_only_fence(author_bridge, remove_config, head_changed):
    peer, health, path, task, run, database = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0
    code, handed = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 0, handed
    before = list(peer.posts)
    raw = json.loads(path.read_text(encoding='utf-8'))
    if remove_config:
        raw.pop('technical_handoff')
    else:
        raw['technical_handoff']['enabled'] = False
    path.write_text(json.dumps(raw), encoding='utf-8')
    if head_changed:
        pr = peer.current['data']['repository']['pullRequest']
        pr['headRefOid'] = 'b'*40
        peer.comments.append({'cursor': 'OFF_HEAD', 'node': {'__typename': 'PullRequestCommit', 'id': 'OFF_HEAD', 'commit': {'oid': 'b'*40}}})
        for field in ['review_candidates', 'content_review_candidates']:
            health[field][0]['head'] = 'b'*40
    database.mark_completed(task.id, 'administrative handoff', exit_code=0, verdict='ПЕРЕДАНО АВТОРУ')
    # Test the manual public reader; it must not create a fresh typed journal
    # or borrow the finished administrative task's reservation.
    code, result = run('next', 'review')
    assert code == 0 and result['action'] == 'wait' and 'read-only' in result['reason'], result
    assert peer.posts == before and len(peer.assignment_posts) == 1


@pytest.mark.parametrize('checkpoint', ['root', 'assignment', 'assigned'])
@pytest.mark.parametrize('fault', ['hold', 'actor'])
def test_late_server_change_stops_the_next_write_and_never_changes_executor(author_bridge, checkpoint, fault):
    from promptpilot.review_routing_wire import parse
    peer, _health, path, _task, run, _db = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0
    original = peer.json
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        body = (kwargs.get('input_value') or {}).get('body')
        payload = parse(body) if body else None
        trigger = (checkpoint == 'root' and payload is not None and payload['kind'] == 'attempt'
                   or checkpoint == 'assigned' and payload is not None and payload.get('phase') == 'assigned'
                   or checkpoint == 'assignment' and args[1].endswith('/assignees'))
        if trigger:
            if fault == 'actor':
                peer.actor = 'wrong-actor'
            else:
                peer.current['data']['repository']['pullRequest']['labels']['nodes'] = [{'name': 'hold'}]
        return result
    peer.json = changed
    code, refused = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 2, refused
    state = ledger(peer, path)
    assert state.budget_used == 1 and 'handoff-done' not in state.attempt_phases(state.attempts[0])
    assert state.attempts[0].payload['executor']['kind'] == 'author'
    assert len(peer.assignment_posts) <= 1


@pytest.mark.parametrize('checkpoint', ['before-assignment', 'after-assignment'])
def test_crash_leaves_one_use_assignment_barrier_and_recovers_only_visible_delivery(author_bridge, monkeypatch, checkpoint):
    from promptpilot.review_routing_fix import FixGuard
    peer, _health, path, _task, run, _db = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0
    original = peer.json
    def crashed(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1].endswith('/assignees'):
            raise SystemExit('simulated process crash after assignment side effect')
        return result
    with monkeypatch.context() as crash:
        if checkpoint == 'before-assignment':
            def stop(*args, **kwargs):
                raise SystemExit('simulated process crash after durable assignment intent')
            crash.setattr(FixGuard, 'assignment_gate', stop)
        else:
            peer.json = crashed
        with pytest.raises(SystemExit):
            run('complete', 'fix', '--lease', selected['lease'])
    peer.json = original
    code, result = run('complete', 'fix', '--lease', selected['lease'])
    assert code == (2 if checkpoint == 'before-assignment' else 0), result
    assert len(peer.assignment_posts) == (0 if checkpoint == 'before-assignment' else 1)
    state = ledger(peer, path)
    assert state.budget_used == 1 and 'started' not in state.attempt_phases(state.attempts[0])


def test_manual_unassignment_and_lost_local_receipt_never_trigger_a_second_delivery(author_bridge):
    from contextlib import closing
    import sqlite3
    peer, _health, _path, _task, run, database = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0
    code, result = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 0, result
    peer.assignees = [{'node_id': 'U_OTHER', 'login': 'other'}]
    with closing(sqlite3.connect(database.DB_PATH)) as connection:
        with connection:
            connection.execute('DROP TABLE technical_fix_assignments_v1')
    code, refused = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 2, refused
    assert len(peer.assignment_posts) == 1


def test_two_private_scheduler_databases_share_one_canonical_executor_and_delivery(author_bridge, monkeypatch, tmp_path):
    peer, _health, path, first_task, run, database = author_bridge
    first_path = database.DB_PATH
    code, first = run('next', 'fix')
    assert code == 0
    second_path = tmp_path/'second-scheduler.db'
    with monkeypatch.context() as second_host:
        second_host.setattr(database, 'DB_PATH', second_path)
        database.init_db()
        database.create_task(TaskCreate(prompt='Project - FIX', working_dir=str(tmp_path)))
        task = database.get_next_runnable()
        assert task is not None
        second_host.setenv('PP_TASK_ID', str(task.id))
        second_host.setenv('PP_TASK_STARTED_AT', task.started_at.astimezone(timezone.utc).isoformat())
        code, second = run('next', 'fix')
        assert code == 0, second
        assert requests.decode(first['lease'])['attempt']['owner'] != requests.decode(second['lease'])['attempt']['owner']
        code, handed = run('complete', 'fix', '--lease', second['lease'])
        assert code == 0, handed
    assert database.DB_PATH == first_path
    assert database.get_task(first_task.id).status.value == 'running'
    code, refused = run('complete', 'fix', '--lease', first['lease'])
    assert code == 2 and 'another owner' in refused['error'], refused
    state = ledger(peer, path)
    assert state.budget_used == 1 and len(state.attempts) == 1 and len(peer.assignment_posts) == 1


def test_pause_does_not_revoke_a_claimed_administrative_task(author_bridge):
    peer, _health, _path, task, run, database = author_bridge
    original_timeout = task.task_timeout
    code, selected = run('next', 'fix')
    assert code == 0
    database.set_setting('worker_paused', '1')
    code, handed = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 0 and handed['action'] == 'handed-off', handed
    assert database.get_task(task.id).task_timeout == original_timeout
    assert len(peer.assignment_posts) == 1


@pytest.mark.parametrize('fault', ['hold', 'actor', 'cancel', 'token', 'expiry'])
def test_change_after_last_renewal_is_refused_before_the_first_attempt_post(author_bridge, monkeypatch, fault):
    from contextlib import closing
    import sqlite3
    peer, _health, _path, task, run, database = author_bridge
    code, selected = run('next', 'fix')
    assert code == 0
    original = requests.renew
    count = 0
    def after_wait(*args):
        nonlocal count
        result = original(*args)
        count += 1
        if count == 2:
            if fault == 'hold':
                peer.current['data']['repository']['pullRequest']['labels']['nodes'] = [{'name': 'hold'}]
            elif fault == 'actor':
                peer.actor = 'changed-after-db-wait'
            elif fault == 'cancel':
                database.request_cancel(task.id)
            else:
                with closing(sqlite3.connect(database.DB_PATH)) as connection:
                    with connection:
                        if fault == 'token':
                            connection.execute("UPDATE pipeline_target_reservations SET token=?", ('f'*32,))
                        else:
                            connection.execute("UPDATE pipeline_target_reservations SET expires_at='2000-01-01T00:00:00+00:00'")
        return result
    monkeypatch.setattr(requests, 'renew', after_wait)
    code, refused = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 2, refused
    assert peer.posts == ['publication', 'claim', 'completion'] and peer.assignment_posts == []


@pytest.mark.parametrize('author_bridge', [None, 'fork'], indirect=True)
@pytest.mark.parametrize('fault', [None, 'wait', 'empty', 'fallback', 'audit', 'merge', 'cleanup', 'unknown',
    'missing-protocol', 'v2', 'no-transport', 'progress', 'start', 'bad-hmac', 'machine', 'detached', 'worktree', 'herdr'])
def test_actual_worker_completes_administrative_handoff_with_zero_provider_calls(author_bridge, monkeypatch, fault):
    peer, _health, path, task, _run, database = author_bridge
    from promptpilot import pipeline_insights as insights, worker
    queue = {'id': 'fix', 'execution': {'mode': 'auto', 'command': ['pipelinectl', 'next', 'fix'],
                                       'review_routing_protocol': 'review-routing-v1'}}
    monkeypatch.setattr(insights, '_matching_queue', lambda _task: ('p', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, '_tool_available', lambda *args: (True, ''))
    monkeypatch.setattr(insights, 'load_providers', lambda: {})
    if fault in {'machine', 'detached', 'worktree'}:
        setattr(task, fault, 'remote' if fault == 'machine' else True)
    elif fault == 'herdr':
        monkeypatch.setattr(insights, 'load_providers', lambda: {'codex': {'executor': 'herdr'}, 'claude': {'executor': 'herdr'}})
    elif fault == 'no-transport':
        queue['execution'].pop('review_routing_protocol')
    monkeypatch.setattr(worker, 'load_providers', lambda: pytest.fail('author handoff loaded a provider'))
    monkeypatch.setattr(worker, 'build_cmd', lambda *args, **kwargs: pytest.fail('author handoff built provider argv'))
    monkeypatch.setattr(worker.OwnedProcess, 'start', lambda *args, **kwargs: pytest.fail('author handoff launched a provider'))
    calls = []
    def actual_cli(_execution, command, _directory, *, env_extra=None):
        calls.append(command)
        with monkeypatch.context() as environment:
            for name, value in (env_extra or {}).items():
                environment.setenv(name, value)
            stream = StringIO()
            with redirect_stdout(stream):
                code = pp.run(['--config', str(path), *command[1:]])
            result = json.loads(stream.getvalue())
            assert code == 0, result
            if command[-2:] == ['next', 'fix']:
                if fault in {'wait', 'empty', 'fallback', 'audit', 'merge', 'cleanup', 'unknown'}:
                    result['action'] = fault
                elif fault == 'missing-protocol':
                    result.pop('protocol')
                elif fault == 'v2':
                    result['protocol'] = 'review-routing-v2'
                elif fault in {'progress', 'start'}:
                    request = requests.decode(result['lease'])
                    request.update(purpose=requests.PROGRESS if fault == 'progress' else requests.START, attempt_id='1003')
                    result['lease'] = pp.encode_signed_lease(request)
                elif fault == 'bad-hmac':
                    body, signature = result['lease'].split('.')
                    signature = ('A' if signature[0] != 'A' else 'B')+signature[1:]
                    result['lease'] = body+'.'+signature
            return result
    monkeypatch.setattr(insights, '_tool_preflight', actual_cli)
    # The worker routes claimed series tasks. Keep its actual persistent row;
    # only the in-memory series selector is needed for this private test task.
    task.series_id = task.id
    monkeypatch.setattr(insights, 'dispatch_gate', lambda _task: None)
    monkeypatch.setattr(worker, 'effective_prompt', lambda _task: 'Project - FIX')
    worker._execute_task_body(task)
    saved = database.get_task(task.id)
    assert saved.status.value == 'completed'
    if fault is None:
        assert saved.verdict == 'ПЕРЕДАНО АВТОРУ'
        assert len(calls) == 2 and len(peer.assignment_posts) == 1
    else:
        assert saved.verdict == 'НУЖЕН ЧЕЛОВЕК' and peer.assignment_posts == []
        assert len(calls) <= 1
    assert insights.after_task_completed(task, saved.verdict) == []
