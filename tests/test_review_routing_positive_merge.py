"""Real next/complete MERGE and cleanup with a complete private GH transport."""
import copy
import json

import pytest

from promptpilot import project_pipeline as pp
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_positive import positive as positive, merged, rest_projection  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401


@pytest.fixture
def merge_case(positive):
    peer, health, path, run, _completed = positive
    code, inspected = run('inspect-routing-positive', '--number', '42', '--stage', 'merge')
    assert code == 0, inspected
    value = json.loads(path.read_text(encoding='utf-8'))
    value['required_checks'] = ['build', 'lint']
    path.write_text(json.dumps(value), encoding='utf-8')
    health.update(review_candidates=[], content_review_candidates=[],
                  merge_executable=[dict(number=42, head='a' * 40, stage='merge')],
                  positive_merge_candidates=[dict(number=42, head='a' * 40, stage='merge', source=inspected['source'])])
    pr = peer.current['data']['repository']['pullRequest']
    peer.native_posts = []
    peer.merge_sends = 0
    peer.merge_failure = None
    peer.ci_fault = None
    peer.merge_state = 'CLEAN'
    peer.issue_in_work = True
    peer.native_body = 'Fixes #7'
    peer.after_intent = None
    original = peer.json

    def rest_comment(edge):
        node = edge['node']
        return dict(id=int(node['fullDatabaseId']), node_id=node['id'], user=node['author'], body=node['body'],
                    created_at=node['createdAt'], updated_at=node['lastEditedAt'] or node['createdAt'],
                    issue_url='https://api.github.com/repos/owner/repo/issues/42')

    def comments():
        return [rest_comment(edge) for edge in [*pr['timelineItems']['edges'], *peer.comments]
                if edge['node']['__typename'] == 'IssueComment']

    def rest_pr():
        return dict(rest_projection(peer), title='positive typed fixture',
                    labels=copy.deepcopy(pr['labels']['nodes']), body=peer.native_body)

    def transport(*args, input_value=None):
        if args == ('api', 'repos/owner/repo/pulls/42'):
            return rest_pr()
        if args[:3] == ('pr', 'view', '42'):
            return dict(mergeStateStatus=peer.merge_state, mergeable='MERGEABLE', body=peer.native_body,
                        statusCheckRollup=[dict(name=name, conclusion='SUCCESS') for name in ['build', 'lint']])
        if args == ('api', 'repos/owner/repo/git/ref/heads/main'):
            return dict(object=dict(sha=pr['baseRefOid']))
        if args == ('api', 'repos/owner/repo/commits/' + 'a' * 40 + '/check-runs?per_page=100'):
            checks = [dict(id=20 + index, name=name, head_sha='a' * 40, app={'id': 15368},
                           status='completed', conclusion='success') for index, name in enumerate(['build', 'lint'])]
            if peer.ci_fault == 'failure':
                checks[0]['conclusion'] = 'failure'
            elif peer.ci_fault == 'head':
                checks[0]['head_sha'] = 'c' * 40
            elif peer.ci_fault == 'foreign-app':
                checks[0]['app']['id'] = 999
            return dict(total_count=len(checks), check_runs=checks)
        if args == ('api', 'repos/owner/repo/issues/42'):
            return dict(state=rest_pr()['state'], labels=copy.deepcopy(pr['labels']['nodes']))
        if args == ('api', 'repos/owner/repo/issues/7'):
            return dict(state='closed' if pr['state'] == 'MERGED' else 'open',
                        labels=[{'name': 'in-work'}] if peer.issue_in_work else [])
        if args == ('api', 'repos/owner/repo/issues/42/comments', '--method', 'POST', '--input', '-'):
            if '<!-- pp:review-routing-' in input_value['body']:
                return original(*args, input_value=input_value)
            ident = str(9000 + len(peer.native_posts))
            edge = dict(cursor='NATIVE_' + ident, node=dict(__typename='IssueComment', id='IC_' + ident,
                        fullDatabaseId=ident, createdAt=peer.now.isoformat().replace('+00:00', 'Z'),
                        lastEditedAt=None, author={'login': 'owner'}, body=input_value['body']))
            peer.comments.append(edge)
            peer.native_posts.append(input_value['body'])
            if 'pp:merge-cleanup-intent ' in input_value['body'] and peer.after_intent:
                peer.after_intent()
            return rest_comment(edge)
        if args == ('api', 'repos/owner/repo/pulls/42/merge', '--method', 'PUT', '--input', '-'):
            assert input_value == dict(merge_method='merge', sha='a' * 40)
            peer.merge_sends += 1
            if peer.merge_failure == 'unknown-open':
                raise pp.PipelineError('unknown delivery; original response unavailable')
            merged(peer)
            health['merge_executable'] = []
            health['positive_merge_candidates'] = []
            if peer.merge_failure == 'lost-accepted':
                raise pp.PipelineError('response lost after server accepted merge')
            return dict(merged=True, sha='b' * 40)
        return original(*args, input_value=input_value)

    def run_transport(*args, **_kwargs):
        if args[0] == 'api' and '--method' in args:
            assert args[:3] == ('api', '--method', 'DELETE')
            if args[3] == 'repos/owner/repo/issues/42/labels/ship':
                pr['labels']['nodes'] = [x for x in pr['labels']['nodes'] if x['name'] != 'ship']
                peer.comments.append(dict(cursor='UNSHIP', node=dict(__typename='UnlabeledEvent', id='UNSHIP',
                    createdAt=peer.now.isoformat().replace('+00:00', 'Z'), actor={'login': 'owner'}, label={'name': 'ship'})))
            else:
                assert args[3] == 'repos/owner/repo/issues/7/labels/in-work'
                peer.issue_in_work = False
            return ''
        assert args[:2] == ('api', '--paginate') and args[-2:] == ('--jq', '.[]'), args
        endpoint = args[2]
        if endpoint.startswith('repos/owner/repo/issues/comments?') or endpoint.startswith('repos/owner/repo/issues/42/comments?'):
            values = comments()
        else:
            assert endpoint == 'repos/owner/repo/pulls?state=open&per_page=100'
            values = [rest_pr()] if pr['state'] == 'OPEN' else []
        return '\n'.join(json.dumps(x) for x in values)

    peer.json = transport
    peer.run = run_transport
    return peer, health, path, run


def test_public_positive_merge_keeps_typed_proof_and_finishes_native_cleanup(merge_case):
    peer, _health, _path, run = merge_case
    code, selected = run('next', 'merge')
    assert code == 0 and selected['action'] == 'merge', selected
    request = pp.decode_signed_lease(selected['lease'])
    assert request['protocol'] == selected['protocol'] == 'positive-review-v1'
    assert request['ordinary']['proof']['kind'] == 'positive-review'
    code, completed = run('complete', 'merge', '--lease', selected['lease'])
    assert code == 0 and completed['action'] == 'completed', completed
    assert completed['number'] == 42 and completed['head'] == 'a' * 40
    assert peer.merge_sends == 1 and peer.issue_in_work is False
    assert len(peer.native_posts) == 2
    assert 'pp:merge-cleanup-intent ' in peer.native_posts[0]
    assert 'pp:merge-cleanup-done ' in peer.native_posts[1]
    assert not any('pp:head-reviewed' in body for body in peer.native_posts)
    code, empty = run('next', 'merge')
    assert code == 0 and empty['action'] == 'empty' and peer.merge_sends == 1, empty


def test_public_positive_merge_resolves_the_standard_relative_config_path(merge_case, monkeypatch):
    peer, _health, path, run = merge_case
    monkeypatch.chdir(path.parent)
    code, selected = run('--config', 'pipelinectl.json', 'next', 'merge')
    assert code == 0 and selected['action'] == 'merge', selected
    code, completed = run('--config', 'pipelinectl.json', 'complete', 'merge', '--lease', selected['lease'])
    assert code == 0 and completed['number'] == 42 and peer.merge_sends == 1, completed


@pytest.mark.parametrize('fault', ['signature', 'unsigned', 'expired', 'flag-off', 'config', 'reader',
                                   'hold', 'ship', 'actor', 'owner', 'ci-failure', 'ci-head', 'ci-foreign-app'])
def test_public_positive_completion_refuses_late_gate_changes_without_mutations(merge_case, fault):
    peer, health, path, run = merge_case
    code, selected = run('next', 'merge')
    assert code == 0 and selected['action'] == 'merge', selected
    token = selected['lease']
    pr = peer.current['data']['repository']['pullRequest']
    if fault == 'signature':
        token = token[:-1] + ('0' if token[-1] != '0' else '1')
    elif fault == 'unsigned':
        token = pp.encode_lease(pp.decode_signed_lease(token)['ordinary'])
    elif fault == 'expired':
        from datetime import timedelta
        peer.now += timedelta(seconds=1801)
    elif fault in {'flag-off', 'config'}:
        value = json.loads(path.read_text(encoding='utf-8'))
        if fault == 'flag-off':
            value['technical_handoff']['enabled'] = False
        else:
            value['merge_method'] = 'squash'
        path.write_text(json.dumps(value), encoding='utf-8')
    elif fault == 'reader':
        (path.parent / '.claude/skills/merge-shepherd/SKILL.md').write_text('other tree', encoding='utf-8')
    elif fault == 'hold':
        pr['labels']['nodes'].append({'name': 'hold'})
    elif fault == 'ship':
        pr['labels']['nodes'] = []
    elif fault == 'actor':
        peer.actor = 'someone'
    elif fault == 'owner':
        health['integration_owner'] = dict(number=99, head='c' * 40, stage='integration-review')
    else:
        peer.ci_fault = fault.removeprefix('ci-')
    code, refused = run('complete', 'merge', '--lease', token)
    assert code == 2 and refused['action'] == 'error', refused
    assert peer.merge_sends == 0 and peer.native_posts == []


@pytest.mark.parametrize('failure', ['lost-accepted', 'unknown-open'])
def test_public_unknown_merge_response_never_sends_again_after_cache_loss(merge_case, failure):
    peer, _health, path, run = merge_case
    code, selected = run('next', 'merge')
    assert code == 0 and selected['action'] == 'merge'
    peer.merge_failure = failure
    code, refused = run('complete', 'merge', '--lease', selected['lease'])
    assert code == 2 and refused['action'] == 'error' and peer.merge_sends == 1, refused
    # Only the disposable comment mirror is lost; the signing key and durable
    # independent send tombstone remain in their separate private directory.
    pp._merge_comment_index_path(pp.load_config(str(path))).unlink()
    code, recovered = run('next', 'merge')
    assert code == 0, recovered
    if failure == 'lost-accepted':
        assert recovered['action'] == 'cleanup'
        code, completed = run('complete', 'merge-cleanup', '--lease', recovered['lease'])
        assert code == 0 and completed['action'] == 'completed', completed
    else:
        assert recovered['action'] == 'wait' and 'never resend' in recovered['reason']
        code, repeated = run('complete', 'merge', '--lease', selected['lease'])
        assert code == 2 and repeated['action'] == 'error'
    assert peer.merge_sends == 1


def test_public_late_hold_after_intent_blocks_compare_and_merge(merge_case):
    peer, _health, _path, run = merge_case
    code, selected = run('next', 'merge')
    assert code == 0 and selected['action'] == 'merge'
    peer.after_intent = lambda: peer.current['data']['repository']['pullRequest']['labels']['nodes'].append({'name': 'hold'})
    code, refused = run('complete', 'merge', '--lease', selected['lease'])
    assert code == 2 and refused['action'] == 'error'
    assert peer.merge_sends == 0 and len(peer.native_posts) == 1


@pytest.mark.parametrize('fault', [None, 'no-direct', 'outer-version', 'target', 'unsigned', 'completion-target'])
def test_owned_scheduler_dispatch_uses_actual_cli_without_a_model(
        merge_case, isolated_db, monkeypatch, tmp_path, fault):
    from contextlib import redirect_stdout
    from io import StringIO
    from promptpilot import pipeline_insights as insights
    from promptpilot.models import TaskCreate

    peer, _health, path, _run = merge_case
    monkeypatch.setenv('PP_PIPELINE_LEASE_KEY_FILE', str(isolated_db.DB_PATH.parent / 'merge-lease.key'))
    task = isolated_db.create_task(TaskCreate(prompt='owned MERGE', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    assert task is not None
    queue = dict(id='merge', execution=dict(mode='auto', direct_complete=fault != 'no-direct',
                 command=['pipelinectl', '--config', str(path), 'next', 'merge']))
    monkeypatch.setattr(insights, '_matching_queue', lambda _: ('p', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, '_tool_available', lambda *_args: (True, ''))
    monkeypatch.setattr(insights, 'load_providers', lambda: {})
    calls = []

    def actual_cli(_execution, command, _directory, *, env_extra=None):
        calls.append(command)
        with monkeypatch.context() as environment:
            for name, value in (env_extra or {}).items():
                environment.setenv(name, value)
            stream = StringIO()
            with redirect_stdout(stream):
                code = pp.run(command[1:])
            result = json.loads(stream.getvalue())
        assert code == 0, result
        if command[-2:] == ['next', 'merge']:
            if fault == 'outer-version':
                result['protocol'] = 'positive-review-v2'
            elif fault == 'target':
                result['target']['number'] = 99
            elif fault == 'unsigned':
                result['lease'] = pp.encode_lease(pp.decode_signed_lease(result['lease'])['ordinary'])
        elif fault == 'completion-target':
            result['number'] = 99
        return result

    monkeypatch.setattr(insights, '_tool_preflight', actual_cli)
    route = insights.execution_route(task, 'fallback must not become a model task', str(tmp_path))
    if fault is None:
        assert route['action'] == 'complete_empty' and route['verdict'] == 'ГОТОВО', route
        assert len(calls) == 2 and peer.merge_sends == 1
        assert route['completion']['number'] == 42 and route['completion']['stage'] == 'merge-cleanup'
    else:
        assert route['action'] == 'block', route
        assert len(calls) == (2 if fault == 'completion-target' else 1)
        assert peer.merge_sends == (1 if fault == 'completion-target' else 0)


@pytest.mark.parametrize('fault', ['actor', 'hold', 'reader', 'expiry', 'flag', 'ci', 'body'])
def test_public_completion_rechecks_every_gate_after_receipt_writer_admission(
        merge_case, monkeypatch, fault):
    import sqlite3
    from datetime import timedelta

    peer, _health, path, run = merge_case
    code, selected = run('next', 'merge')
    assert code == 0 and selected['action'] == 'merge'
    original_connect = sqlite3.connect
    changed = []

    class Writer:
        def __init__(self, connection):
            self.connection = connection
            self.receipt_written = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, statement, *args):
            if statement.startswith('INSERT INTO receipts VALUES'):
                self.receipt_written = True
            return self.connection.execute(statement, *args)

        def commit(self):
            result = self.connection.commit()
            if self.receipt_written and not changed:
                changed.append(fault)
                if fault == 'actor':
                    peer.actor = 'someone'
                elif fault == 'hold':
                    peer.current['data']['repository']['pullRequest']['labels']['nodes'].append({'name': 'hold'})
                elif fault == 'reader':
                    (path.parent / '.claude/skills/tail-issues/SKILL.md').write_text('changed after writer admission', encoding='utf-8')
                elif fault == 'expiry':
                    peer.now += timedelta(seconds=1801)
                elif fault == 'flag':
                    value = json.loads(path.read_text(encoding='utf-8'))
                    value['technical_handoff']['enabled'] = False
                    path.write_text(json.dumps(value), encoding='utf-8')
                elif fault == 'body':
                    peer.native_body = 'Fixes #8'
                else:
                    peer.ci_fault = 'failure'
            return result

    def connect(filename, *args, **kwargs):
        connection = original_connect(filename, *args, **kwargs)
        return Writer(connection) if 'typed-merge-sends-v1' in str(filename) else connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    code, refused = run('complete', 'merge', '--lease', selected['lease'])
    assert code == 2 and refused['action'] == 'error', refused
    assert changed == [fault] and peer.merge_sends == 0 and len(peer.native_posts) == 1


def test_public_parked_technical_target_does_not_block_healthy_positive_merge(merge_case):
    peer, health, _path, run = merge_case
    parked = dict(number=99, head='c' * 40, stage='engineering-recovery')
    health.update(state='yellow', engineering_waiting=[parked],
                  findings=[dict(severity='yellow', code='routing_engineering_wait', pr=99)])
    original = peer.run

    def transport(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:3] == ('api', '--paginate', 'repos/owner/repo/pulls?state=open&per_page=100'):
            # REST label/priority order is not a competing execution allowlist.
            bad = dict(number=99, title='parked technical source', head={'sha': 'c' * 40},
                       base={'ref': 'main'}, labels=[{'name': 'ship'}, {'name': 'queue:p0'}])
            return json.dumps(bad) + '\n' + result
        return result

    peer.run = transport
    code, selected = run('next', 'merge')
    assert code == 0 and selected['action'] == 'merge' and selected['target']['number'] == 42, selected
    code, completed = run('complete', 'merge', '--lease', selected['lease'])
    assert code == 0 and completed['number'] == 42 and peer.merge_sends == 1, completed
