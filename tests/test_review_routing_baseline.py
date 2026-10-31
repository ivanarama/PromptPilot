"""Audit prefix survives only its own verified immutable publication suffix."""
import copy

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot import review_routing_baseline as baseline
from promptpilot import review_routing_wire as wire
from test_review_routing_snapshot import Reader,page,CONFIG
from promptpilot.review_routing_snapshot import stable_timeline
from test_review_routing_wire import publication


def snapshot():
    return stable_timeline(Reader([page(),page()]),CONFIG,42)


def root(state):
    payload=publication()
    payload['audit']=baseline.capture(state)
    return payload


def append(state,payload,ident):
    state['edges'].append({'cursor':'C'+ident,'node':{'__typename':'IssueComment','id':'IC_'+ident,
        'fullDatabaseId':ident,'createdAt':'2026-10-29T00:00:01Z','lastEditedAt':None,
        'author':{'login':'owner'},'body':wire.render(payload)}})


def test_signed_prefix_survives_only_its_own_publication_phases_and_normal_base_advance():
    state=snapshot();payload=root(state);binding=payload['audit'];ctx=wire.context(payload)
    assert binding['snapshot_sha256']==wire.sha({key:value for key,value in state.items() if key not in {'baseRefOid','updatedAt'}})
    append(state,payload,'1000')
    common={'protocol':payload['protocol'],'pr':payload['pr'],'owner':payload['owner'],
            'context_sha256':ctx,'publication_id':'1000','lease_id':'1000'}
    append(state,dict(common,kind='claim'),'1001')
    append(state,dict(common,kind='completion',claim_id='1001'),'1002')
    state['baseRefOid']='c'*40
    state['updatedAt']='2026-10-29T00:20:00Z'
    assert baseline.verify(state,binding,publication_context=ctx,trusted='owner')=={
        'verified':True,'mutation_authorized':False}
    edited=copy.deepcopy(payload);edited['audit']['last_edge_node_id']='OTHER_NODE'
    assert wire.context(edited)!=ctx


@pytest.mark.parametrize('field,value',[
    ('headRefOid','c'*40),('labels',['hold']),('state','CLOSED'),('isDraft',True),
    ('author',{'id':'OTHER','login':'owner'}),
    ('identity',{'different':'identity'}),
])
def test_changed_audit_header_is_never_hidden_by_own_phase_filtering(field,value):
    state=snapshot();payload=root(state);binding=payload['audit']
    append(state,payload,'1000');state[field]=value
    with pytest.raises(PipelineError,match='prefix changed'):
        baseline.verify(state,binding,publication_context=wire.context(payload),trusted='owner')


@pytest.mark.parametrize('edit',['body','author','cursor','id','deleted','duplicate'])
def test_changed_prefix_or_boundary_cannot_be_replaced_by_a_current_snapshot(edit):
    state=snapshot();payload=root(state);binding=payload['audit'];first=state['edges'][0]
    if edit=='body':first['node']['commit']['oid']='c'*40
    elif edit=='author':first['node']['author']={'login':'other'}
    elif edit=='cursor':first['cursor']='OTHER'
    elif edit=='id':first['node']['id']='OTHER'
    elif edit=='deleted':state['edges']=[]
    else:state['edges'].append(copy.deepcopy(first))
    with pytest.raises(PipelineError):
        baseline.verify(state,binding,publication_context=wire.context(payload),trusted='owner')


@pytest.mark.parametrize('kind',['comment','untrusted-wire','label','another-context','another-purpose'])
def test_independent_suffix_is_not_masked_as_an_own_projection(kind):
    state=snapshot();payload=root(state);binding=payload['audit'];ctx=wire.context(payload)
    if kind=='another-context':
        changed=copy.deepcopy(payload);changed['anchor']='OTHER_ANCHOR'
        append(state,changed,'1000')
    elif kind=='another-purpose':
        append(state,dict(protocol=payload['protocol'],pr=payload['pr'],owner=payload['owner'],
            kind='lease',purpose='technical-attempt',context_sha256=ctx,root='1000',previous='1000'),'1001')
    else:
        append(state,payload,'1000')
        node=state['edges'][-1]['node']
        if kind=='comment':node['body']='Please review another thing'
        elif kind=='untrusted-wire':node['author']['login']='contributor'
        else:node.update(__typename='LabeledEvent',label={'name':'ship'})
    with pytest.raises(PipelineError,match='suffix'):
        baseline.verify(state,binding,publication_context=ctx,trusted='owner')


def test_empty_boundary_is_explicit_and_cannot_hide_an_independent_head_event():
    state=snapshot();head=state['edges'].pop();payload=root(state);binding=payload['audit']
    assert binding['last_edge_cursor'] is None and binding['last_edge_node_id'] is None
    state['edges'].append(head)
    with pytest.raises(PipelineError,match='suffix'):
        baseline.verify(state,binding,publication_context=wire.context(payload),trusted='owner')


def test_partial_audit_binding_and_oversized_snapshot_are_refused(monkeypatch):
    payload=publication();payload['audit']['last_edge_cursor']=None
    with pytest.raises(PipelineError,match='both cursor'):
        wire.render(payload)
    monkeypatch.setattr(baseline,'MAX_BYTES',256)
    with pytest.raises(PipelineError,match='bounded size'):
        baseline.capture(snapshot())
