from copy import deepcopy

import pytest

from promptpilot.owner_waits import select, receipt


def observation():
    candidates = [dict(number=n, updated_at='2026-11-03T00:00:00Z', labels=['needs-decision']) for n in [2279, 2367]]
    return dict(cache=dict(complete=True, available=True, stale=False, invalidated=False, age_seconds=1),
                queues=[dict(id='owner', membership_complete=True, backlog=2, admission_items=candidates)])


def records(data, context):
    context.setdefault('heads', {str(c['number']): 'a'*40 for c in data['queues'][0]['admission_items']})
    return [receipt(c, context, head='a'*40, reason='Full lint exceeded the limit',
                    evidence='immutable actual FIX handoff', now=100) for c in data['queues'][0]['admission_items']]


def test_finite_unchanged_inbox_waits_never_block_other_owner_actions():
    data = observation(); context = dict(environment='profile/tool/resource fingerprints')
    result = select(data, records(data, context), context, now=101)
    assert result['action'] == 'fallback' and len(result['excluded']) == 2
    assert result['negative_only'] is True and result['scope'] == 'owner-inbox'
    assert 'eligible' not in result
    assert not {'ship', 'approved', 'lease', 'handoff'} & result.keys()
    assert select(data, records(data, context), context, now=701)['action'] == 'fallback'


def test_one_wait_does_not_hide_another_candidate():
    data = observation(); context = dict(environment='one')
    result = select(data, records(data, context)[1:], context, now=101)
    assert result['action'] == 'fallback' and 'eligible' not in result
    assert result['excluded'][0]['number'] == 2367


def test_reviewed_delivery_pr_outside_inbox_is_not_excluded_by_positive_list():
    data = observation(); context = dict(environment='one')
    memo = records(data, context)
    # The same complete needs-decision inbox exists both with and without a
    # reviewed PR waiting for ship. It cannot decide the whole election domain.
    context['heads']['2474'] = 'b' * 40
    memo = records(data, context)
    result = select(data, memo, context, now=101)
    assert result['action'] == 'fallback' and 'eligible' not in result
    assert {item['number'] for item in result['excluded']} == {2279, 2367}
    assert 2474 not in {item['number'] for item in result['excluded']}


def test_empty_decision_inbox_does_not_prove_delivery_is_empty():
    data = observation()
    data['queues'][0].update(backlog=0, admission_items=[])
    result = select(data, [], dict(environment='one', heads={}), now=101)
    assert result['action'] == 'fallback' and result['excluded'] == []
    assert 'eligible' not in result


@pytest.mark.parametrize('change', ['comment', 'label', 'new_candidate', 'environment', 'duplicate', 'future'])
def test_changed_inputs_release_negative_observation(change):
    data = observation(); context = dict(environment='one'); memo = records(data, context)
    if change == 'comment': data['queues'][0]['admission_items'][0]['updated_at'] = '2026-11-03T01:00:00Z'
    if change == 'label': data['queues'][0]['admission_items'][0]['labels'].append('queue:p0')
    if change == 'new_candidate':
        data['queues'][0]['admission_items'].append(dict(number=2400, updated_at='now', labels=[]));data['queues'][0]['backlog'] += 1
    if change == 'environment': context['environment'] = 'new successful lint'
    if change == 'duplicate': memo.append(deepcopy(memo[0]))
    if change == 'future': memo[0]['observed_at'] = 150
    assert select(data, memo, context, now=101)['action'] == 'fallback'


@pytest.mark.parametrize('change', ['stale', 'incomplete', 'truncated', 'unknown', 'bad_record'])
def test_uncertainty_is_never_a_proof_that_the_whole_queue_is_waiting(change):
    data = observation(); context = dict(environment='one'); memo = records(data, context)
    if change == 'stale': data['cache']['stale'] = True
    if change == 'incomplete': data['cache']['complete'] = False
    if change == 'truncated': data['queues'][0]['membership_complete'] = False
    if change == 'unknown': data['cache'].pop('age_seconds')
    if change == 'bad_record': memo[0]['expires_at'] = 100000000
    assert select(data, memo, context, now=101)['action'] != 'wait'


def test_cannot_create_permanent_wait():
    data = observation()
    with pytest.raises(ValueError):
        receipt(data['queues'][0]['admission_items'][0], dict(environment='x', heads={'2279':'a'*40}),
                head='a'*40, reason='wait', evidence='log', now=100, seconds=601)
