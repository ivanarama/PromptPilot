"""Retained audit material survives routine metadata and commit-date changes."""
import copy
import json
from pathlib import Path

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_baseline import verify
from promptpilot.review_routing_history import verified_depth
from promptpilot.review_routing_wire import context,parse
from test_review_routing_baseline import append
from test_review_routing_history import add_typed,history


def retained():
    state=history();add_typed(state,retain_history=True)
    return state


@pytest.mark.parametrize('change',['hold','closed','merged','draft','backdated-before-audit','backdated-after-audit'])
def test_retained_history_preserves_completed_round_without_granting_a_current_write(change):
    state=retained()
    publication=parse(state['edges'][-3]['node']['body'])
    if change=='hold':state['labels']=['hold']
    elif change in {'closed','merged'}:state['state']=change.upper()
    elif change=='draft':state['isDraft']=True
    else:
        index=2 if change=='backdated-before-audit' else len(state['edges'])-1
        state['edges'].insert(index,{'cursor':'LATE_COMMIT','node':{'__typename':'PullRequestCommit',
            'id':'LATE_COMMIT','commit':{'oid':'9'*40}}})
    result=verified_depth(state,trusted='owner')
    assert result['depth']==3 and result['mutation_authorized'] is False
    with pytest.raises(PipelineError):
        verify(state,publication['audit'],publication_context=context(publication),trusted='owner')


def test_equivalent_concurrent_publication_does_not_replace_the_original_audit():
    state=retained()
    payload=parse(state['edges'][-3]['node']['body'])
    payload['owner']='00000000-0000-4000-8000-000000000002'
    append(state,payload,'3003')
    result=verified_depth(state,trusted='owner')
    assert result['depth']==3 and '3003' in result['ledger'].ignored_ids


@pytest.mark.parametrize('change',['missing','reordered','bytes','independent'])
def test_retained_manifest_never_hides_changed_original_nodes_or_other_events(change):
    state=retained()
    if change=='missing':del state['edges'][0]
    elif change=='reordered':state['edges'][0],state['edges'][1]=state['edges'][1],state['edges'][0]
    elif change=='bytes':state['edges'][0]['node']['commit']['oid']='f'*40
    else:
        state['edges'].insert(2,{'cursor':'UNKNOWN_EVENT','node':{'__typename':'LabeledEvent',
            'id':'UNKNOWN_EVENT','label':{'name':'hold'}}})
    with pytest.raises(PipelineError):verified_depth(state,trusted='owner')


def test_new_history_manifest_is_bound_to_the_original_report_context():
    state=retained();publication=parse(state['edges'][-3]['node']['body'])
    changed=copy.deepcopy(publication)
    changed['audit']['history']['header']['labels']=['hold']
    assert context(changed)!=context(publication)


def test_shared_retained_audit_vectors_match_complete_history_and_ledger():
    data=json.loads((Path(__file__).parent/'fixtures/review-routing-retained-audit-v1.json').read_text(encoding='utf-8'))
    for case in data['cases']:
        if case['accepted']:
            result=verified_depth(case['snapshot'],trusted=case['trusted'])
            assert result['depth']==case['depth']==3
            assert result['ledger'].to_dict()==case['ledger']
        else:
            with pytest.raises(PipelineError):verified_depth(case['snapshot'],trusted=case['trusted'])
