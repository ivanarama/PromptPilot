"""Positive consumers through the real CLI and complete typed transport."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from promptpilot.review_routing_wire import sha
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401


def rest_projection(peer):
    pr = peer.current['data']['repository']['pullRequest']
    event = next((edge['node'] for edge in [*pr['timelineItems']['edges'], *peer.comments]
                  if edge['node']['__typename'] == 'MergedEvent'), None)
    return dict(number=pr['number'], node_id=pr['id'], merged=pr['state'] == 'MERGED',
                state='closed' if pr['state'] == 'MERGED' else 'open',
                head=dict(sha=pr['headRefOid'], ref=pr['headRefName'],
                          repo=dict(node_id=pr['headRepository']['id'], full_name=pr['headRepository']['nameWithOwner'])),
                base=dict(ref=pr['baseRefName'], repo=dict(node_id=pr['baseRepository']['id'], full_name=pr['baseRepository']['nameWithOwner'])),
                user=dict(node_id=pr['author']['id'], login=pr['author']['login']),
                merge_commit_sha='b' * 40, merged_at=event['createdAt'] if event else None)


@pytest.fixture
def positive(adapter):
    peer, health, path, report, run = adapter
    value = json.loads(report.read_text(encoding='utf-8'))
    value['blocking'] = []
    value['tail'] = [dict(kind='issue', title='Follow-up invariant', text='Preserve the source on restart'),
                     dict(kind='discard', text='No separate change is warranted')]
    report.write_text(json.dumps(value), encoding='utf-8')
    code, selected = run('next', 'review')
    assert code == 0 and selected['action'] == 'audit'
    code, completed = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and completed['outcome'] == 'reviewed', completed
    health['positive_review_contract'] = dict(protocol='positive-review-v1', snapshot='complete', purposes=['merge', 'tail'])
    pr = peer.current['data']['repository']['pullRequest']
    pr['labels']['nodes'] = [{'name': 'ship'}]
    peer.comments.append({'cursor': 'SHIP', 'node': {'__typename': 'LabeledEvent', 'id': 'SHIP',
                          'createdAt': peer.now.isoformat().replace('+00:00', 'Z'),
                          'actor': {'login': 'owner'}, 'label': {'name': 'ship'}}})
    original = peer.json

    def transport(*args, **kwargs):
        if args == ('api', 'repos/owner/repo/pulls/42'):
            return rest_projection(peer)
        return original(*args, **kwargs)

    peer.json = transport
    return peer, health, path, run, completed


def merged(peer):
    pr = peer.current['data']['repository']['pullRequest']
    pr['state'] = 'MERGED'
    peer.comments.append({'cursor': 'MERGED', 'node': {'__typename': 'MergedEvent', 'id': 'MERGED',
                          'createdAt': peer.now.isoformat().replace('+00:00', 'Z'), 'commit': {'oid': 'b' * 40}}})


def inspect(run, stage='merge'):
    return run('inspect-routing-positive', '--number', '42', '--stage', stage)


def test_public_positive_context_retains_actual_typed_source_and_tail(positive):
    peer, _health, _path, run, completed = positive
    code, result = inspect(run)
    assert code == 0, result
    assert result['source']['kind'] == 'positive-review'
    assert result['source']['source'] == completed['source']
    assert result['source']['nodes'] == dict(publication='IC_1000', claim='IC_1001', completion='IC_1002')
    assert result['source_sha256'] == sha(result['source']) and result['mutation_authorized'] is False
    assert peer.posts == ['publication', 'claim', 'completion']
    assert all('pp:head-reviewed' not in edge['node'].get('body', '') for edge in peer.comments)
    merged(peer)
    code, tail = inspect(run, 'tail')
    assert code == 0, tail
    assert tail['source'] == result['source'] and tail['merge_sha'] == 'b' * 40
    assert tail['merge_event'] == 'MERGED'
    assert tail['tail'][0] == dict(kind='issue', title='Follow-up invariant', text='Preserve the source on restart',
                                  index=1, item_sha256=sha(dict(kind='issue', title='Follow-up invariant', text='Preserve the source on restart')))
    assert peer.posts == ['publication', 'claim', 'completion']


@pytest.mark.parametrize('fault', ['no-capability', 'flag-off', 'reader', 'no-ship', 'foreign-ship',
                                   'hold', 'head', 'author', 'fork', 'edited', 'deleted', 'ABA', 'override'])
def test_public_positive_merge_context_refuses_stale_or_incomplete_authority(positive, fault):
    peer, health, path, run, _completed = positive
    pr = peer.current['data']['repository']['pullRequest']
    if fault == 'no-capability':
        health.pop('positive_review_contract')
    elif fault == 'flag-off':
        value = json.loads(path.read_text(encoding='utf-8'))
        value['technical_handoff']['enabled'] = False
        path.write_text(json.dumps(value), encoding='utf-8')
    elif fault == 'reader':
        (path.parent / '.claude/skills/tail-issues/SKILL.md').write_text('old consumer', encoding='utf-8')
    elif fault == 'no-ship':
        pr['labels']['nodes'] = []
    elif fault == 'foreign-ship':
        peer.comments[-1]['node']['actor']['login'] = 'someone'
    elif fault == 'hold':
        pr['labels']['nodes'].append({'name': 'hold'})
    elif fault == 'head':
        pr['headRefOid'] = 'c' * 40
    elif fault == 'author':
        pr['author']['id'] = 'CHANGED'
    elif fault == 'fork':
        pr['headRepository']['id'] = 'CHANGED'
    elif fault == 'edited':
        peer.comments[0]['node']['lastEditedAt'] = '2026-11-01T00:00:00Z'
    elif fault == 'deleted':
        peer.comments.append({'cursor': 'DELETED', 'node': {'__typename': 'CommentDeletedEvent', 'id': 'DELETED',
                              'createdAt': peer.now.isoformat().replace('+00:00', 'Z')}})
    elif fault == 'ABA':
        for index, head in enumerate(['c' * 40, 'a' * 40]):
            peer.comments.append({'cursor': f'ABA{index}', 'node': {'__typename': 'HeadRefForcePushedEvent',
                                  'id': f'ABA{index}', 'createdAt': peer.now.isoformat().replace('+00:00', 'Z'),
                                  'afterCommit': {'oid': head}}})
    else:
        peer.comments.append({'cursor': 'OVERRIDE', 'node': {'__typename': 'IssueComment', 'id': 'IC_9000',
                              'fullDatabaseId': '9000', 'createdAt': peer.now.isoformat().replace('+00:00', 'Z'),
                              'lastEditedAt': None, 'author': {'login': 'owner'}, 'body': 'pp:review-again'}})
    code, result = inspect(run)
    assert code == 2 and result['action'] == 'error', result
    assert peer.posts == ['publication', 'claim', 'completion']


@pytest.mark.parametrize('fault', ['not-merged', 'no-tail', 'restore', 'second-merge', 'post-merge-override'])
def test_public_tail_context_requires_exact_unsuperseded_merge(positive, fault):
    peer, _health, _path, run, _completed = positive
    if fault != 'not-merged':
        merged(peer)
    if fault == 'no-tail':
        peer.current['data']['repository']['pullRequest']['labels']['nodes'].append({'name': 'no-tail'})
    elif fault == 'restore':
        peer.comments.append({'cursor': 'RESTORE', 'node': {'__typename': 'HeadRefRestoredEvent', 'id': 'RESTORE',
                              'createdAt': peer.now.isoformat().replace('+00:00', 'Z')}})
    elif fault == 'second-merge':
        peer.comments.append(copy.deepcopy(peer.comments[-1]))
        peer.comments[-1]['cursor'] = 'SECOND_MERGE'
        peer.comments[-1]['node']['id'] = 'SECOND_MERGE'
    elif fault == 'post-merge-override':
        peer.comments.append({'cursor': 'OVERRIDE', 'node': {'__typename': 'IssueComment', 'id': 'IC_9000',
                              'fullDatabaseId': '9000', 'createdAt': peer.now.isoformat().replace('+00:00', 'Z'),
                              'lastEditedAt': None, 'author': {'login': 'owner'}, 'body': 'pp:review-again'}})
    code, result = inspect(run, 'tail')
    assert code == 2 and result['action'] == 'error', result
    assert peer.posts == ['publication', 'claim', 'completion']


def test_public_tail_allows_one_terminal_branch_delete(positive):
    peer, _health, _path, run, _completed = positive
    merged(peer)
    peer.comments.append({'cursor': 'DELETE', 'node': {'__typename': 'HeadRefDeletedEvent', 'id': 'DELETE',
                          'createdAt': peer.now.isoformat().replace('+00:00', 'Z')}})
    code, result = inspect(run, 'tail')
    assert code == 0 and result['merge_sha'] == 'b' * 40, result


@pytest.mark.parametrize('fault', ['number', 'node-id', 'author', 'repository', 'missing-merged-at', 'merge-time'])
def test_public_tail_refuses_partial_or_conflicting_rest_merge_identity(positive, fault):
    peer, _health, _path, run, _completed = positive
    merged(peer)
    original = peer.json

    def transport(*args, **kwargs):
        result = original(*args, **kwargs)
        if args == ('api', 'repos/owner/repo/pulls/42'):
            if fault == 'number':
                result['number'] = 99
            elif fault == 'node-id':
                result['node_id'] = 'OTHER_PR'
            elif fault == 'author':
                result['user']['node_id'] = 'OTHER_AUTHOR'
            elif fault == 'repository':
                result['head']['repo']['node_id'] = 'OTHER_REPOSITORY'
            elif fault == 'missing-merged-at':
                result.pop('merged_at')
            else:
                result['merged_at'] = '2026-01-01T00:00:00Z'
        return result

    peer.json = transport
    code, result = inspect(run, 'tail')
    assert code == 2 and result['action'] == 'error', result


def export_vector(name, value):
    directory = os.environ.get('PP_POSITIVE_GOLDEN_DIR')
    if directory is None:
        return
    path = Path(directory)
    assert path.is_absolute() and path.is_dir()
    (path / (name + '.json')).write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))


@pytest.mark.parametrize('name,stage,accepted', [
    ('positive-open', 'merge', True), ('positive-merged', 'tail', True),
    ('terminal-delete', 'tail', True), ('tail-before-merge', 'tail', False),
    ('post-merge-restore', 'tail', False), ('second-merge', 'tail', False),
    ('head-ABA', 'merge', False), ('new-head', 'merge', False),
    ('author-changed', 'merge', False), ('fork-changed', 'merge', False),
    ('edited-publication', 'merge', False), ('deleted-history', 'merge', False),
    ('override', 'merge', False), ('post-merge-override', 'tail', False),
    ('missing-merge-time', 'tail', False), ('missing-merge-commit', 'tail', False),
    ('positive-after-merge', 'tail', False), ('two-terminal-deletes', 'tail', False),
    ('fenced-override', 'merge', True), ('post-merge-fenced-override', 'tail', True),
])
def test_shared_positive_vectors_are_measured_through_public_cli(positive, name, stage, accepted):
    from promptpilot import project_pipeline as pp
    from promptpilot.review_routing_snapshot import stable_timeline

    peer, _health, path, run, _completed = positive
    pr = peer.current['data']['repository']['pullRequest']
    if stage == 'tail' and name != 'tail-before-merge':
        merged(peer)
    at = peer.now.isoformat().replace('+00:00', 'Z')
    if name in {'terminal-delete', 'post-merge-restore'}:
        kind = 'HeadRefDeletedEvent' if name == 'terminal-delete' else 'HeadRefRestoredEvent'
        peer.comments.append(dict(cursor='LIFECYCLE', node=dict(__typename=kind, id='LIFECYCLE', createdAt=at)))
    elif name == 'second-merge':
        peer.comments.append(dict(cursor='SECOND_MERGE', node=dict(__typename='MergedEvent', id='SECOND_MERGE', createdAt=at, commit={'oid': 'b' * 40})))
    elif name == 'missing-merge-time':
        peer.comments[-1]['node'].pop('createdAt')
    elif name == 'missing-merge-commit':
        peer.comments[-1]['node'].pop('commit')
    elif name == 'positive-after-merge':
        event = peer.comments.pop()
        peer.comments.insert(0, event)
    elif name == 'two-terminal-deletes':
        for index in range(2):
            peer.comments.append(dict(cursor=f'DELETE{index}', node=dict(__typename='HeadRefDeletedEvent', id=f'DELETE{index}', createdAt=at)))
    elif name == 'head-ABA':
        for index, head in enumerate(['c' * 40, 'a' * 40]):
            peer.comments.append(dict(cursor=f'ABA{index}', node=dict(__typename='HeadRefForcePushedEvent', id=f'ABA{index}', createdAt=at, afterCommit={'oid': head})))
    elif name == 'new-head':
        pr['headRefOid'] = 'c' * 40
    elif name == 'author-changed':
        pr['author']['id'] = 'OTHER_AUTHOR'
    elif name == 'fork-changed':
        pr['headRepository']['id'] = 'OTHER_REPOSITORY'
        pr['headRepository']['nameWithOwner'] = 'other/repo'
        pr['isCrossRepository'] = True
    elif name == 'edited-publication':
        peer.comments[0]['node']['lastEditedAt'] = at
    elif name == 'deleted-history':
        peer.comments.append(dict(cursor='DELETED', node=dict(__typename='CommentDeletedEvent', id='DELETED', createdAt=at)))
    elif name in {'override', 'post-merge-override', 'fenced-override', 'post-merge-fenced-override'}:
        body = '```text\npp:review-again\n```' if 'fenced' in name else 'pp:review-again'
        peer.comments.append(dict(cursor='OVERRIDE', node=dict(__typename='IssueComment', id='IC_9000', fullDatabaseId='9000',
            createdAt=at, lastEditedAt=None, author={'login': 'owner'}, body=body)))
    snapshot = stable_timeline(peer, pp.load_config(str(path)), 42)
    code, result = inspect(run, stage)
    assert (code == 0) is accepted, result
    value = dict(name=name, stage=stage, accepted=accepted, snapshot=snapshot,
                 source=result['source'] if accepted else None,
                 merge_sha=result['merge_sha'] if accepted else None)
    export_vector(name, value)


def test_shared_technical_source_never_becomes_positive(adapter):
    from promptpilot import project_pipeline as pp
    from promptpilot.review_routing_snapshot import stable_timeline

    peer, health, path, report, run = adapter
    code, selected = run('next', 'review')
    assert code == 0 and selected['action'] == 'audit'
    code, result = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 0 and result['outcome'] == 'technical-handoff'
    health['positive_review_contract'] = dict(protocol='positive-review-v1', snapshot='complete', purposes=['merge', 'tail'])
    snapshot = stable_timeline(peer, pp.load_config(str(path)), 42)
    code, result = inspect(run)
    assert code == 2 and result['action'] == 'error'
    export_vector('technical-is-not-positive', dict(name='technical-is-not-positive', stage='merge', accepted=False,
                  snapshot=snapshot, source=None, merge_sha=None))


def test_retained_positive_vectors_replay_through_public_transport(adapter):
    peer, health, _path, _report, run = adapter
    directory = Path(__file__).parent / 'fixtures'
    raw = (directory / 'positive-review-v1.json').read_bytes()
    vectors = json.loads(raw)
    origin = json.loads((directory / 'positive-review-origin.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(raw).hexdigest() == origin['sha256']
    assert vectors['source_head'] == origin['source_head'] and len(vectors['cases']) == origin['case_count'] == 21
    assert vectors['authority'] == 'unverified-offline-input'
    health['positive_review_contract'] = dict(protocol='positive-review-v1', snapshot='complete', purposes=['merge', 'tail'])
    original = peer.json

    def transport(*args, **kwargs):
        if args == ('api', 'repos/owner/repo/pulls/42'):
            return rest_projection(peer)
        return original(*args, **kwargs)

    peer.json = transport
    pr = peer.current['data']['repository']['pullRequest']
    for fixture in vectors['cases']:
        state = fixture['snapshot']
        identity = state['identity']
        pr.update(id=identity['pr_id'], number=identity['pr_number'],
                  headRefOid=state['headRefOid'], baseRefOid=state['baseRefOid'], baseRefName=state['baseRefName'],
                  headRefName=identity['head_ref'], isCrossRepository=identity['cross_repository'],
                  headRepository=dict(id=identity['head_repository_id'], nameWithOwner=identity['head_repository']),
                  baseRepository=dict(id=identity['base_repository_id'], nameWithOwner=identity['base_repository']),
                  author=copy.deepcopy(state['author']), state=state['state'], isDraft=state['isDraft'],
                  labels=dict(nodes=[dict(name=name) for name in state['labels']], pageInfo=dict(hasNextPage=False)))
        pr['timelineItems'].update(updatedAt=state['updatedAt'], edges=copy.deepcopy(state['edges']))
        peer.comments.clear()
        code, result = inspect(run, fixture['stage'])
        assert (code == 0) is fixture['accepted'], (fixture['name'], result)
        if fixture['accepted']:
            assert result['source'] == fixture['source'] and result['merge_sha'] == fixture['merge_sha']
    assert peer.posts == []
