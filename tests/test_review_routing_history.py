"""A marker count is not a verified historical REVIEW transaction."""
import copy

import pytest

from promptpilot import project_pipeline as pp
from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_baseline import capture
from promptpilot.review_routing_history import verified_depth
from promptpilot.review_routing_wire import context,outcome
from test_review_routing_baseline import snapshot,append,root


def legacy_comment(ident,body):
    return {'cursor':'C'+ident,'node':{'__typename':'IssueComment','id':'IC_'+ident,
        'fullDatabaseId':ident,'createdAt':'2026-10-29T00:00:00Z','lastEditedAt':None,
        'author':{'login':'owner'},'body':body}}


def anchor(state,head,ident):
    state['headRefOid']=head
    state['edges'].append({'cursor':'C'+ident,'node':{'__typename':'PullRequestCommit',
        'id':ident,'commit':{'oid':head}}})


def legacy(state,head,start):
    anchor(state,head,'ANCHOR_'+str(start))
    epoch=pp.epoch(state,'owner')['hash']
    review,claim,completion=map(str,range(start,start+3))
    body=f'Reviewed-SHA: {head}\nOutcome-Label: changes-requested\n<!-- pp:review pp:tail=0 -->'
    state['edges']+=[legacy_comment(review,body),
        legacy_comment(claim,f'<!-- pp:review-claim {head} review-comment={review} epoch-sha256={epoch} -->'),
        legacy_comment(completion,f'<!-- pp:head-reviewed {head} review-comment={review} claim={claim} epoch-sha256={epoch} -->')]


def history():
    state=snapshot();head=state['headRefOid']
    legacy(state,'1'*40,100);legacy(state,'2'*40,110)
    anchor(state,head,'CURRENT')
    return state


def add_typed(state,*,depth=2,epoch=None,anchor_id=None):
    data=root(state);info=pp.epoch(state,'owner')
    data.update(depth=depth,epoch=epoch or info['hash'],anchor=anchor_id or info['anchor_id'],audit=capture(state))
    data['outcome']=outcome(data['report'],depth=data['depth'],budget_used=data['budget_used'],max_attempts=data['max_attempts'])
    common={'protocol':data['protocol'],'pr':data['pr'],'owner':data['owner'],
            'context_sha256':context(data),'publication_id':'3000','lease_id':'3000'}
    append(state,data,'3000');append(state,dict(common,kind='claim'),'3001')
    append(state,dict(common,kind='completion',claim_id='3001'),'3002')


def test_two_verified_legacy_rounds_then_typed_completion_have_depth_three():
    state=history()
    assert verified_depth(state,trusted='owner')['depth']==2
    add_typed(state)
    result=verified_depth(state,trusted='owner')
    assert result['depth']==3 and [item['kind'] for item in result['completed']]==['legacy','legacy','typed']
    assert result['ledger'].budget_used==0 and result['mutation_authorized'] is False
    anchor(state,'c'*40,'AFTER_FIX')
    assert verified_depth(state,trusted='owner')['depth']==3


@pytest.mark.parametrize('change',['missing-review','missing-claim','foreign-review','wrong-epoch','edited','duplicate-id','deletion'])
def test_unknown_or_forged_history_refuses_instead_of_resetting_depth(change):
    state=history();first=next(edge for edge in state['edges'] if edge['node'].get('fullDatabaseId')=='100')
    if change=='missing-review':state['edges'].remove(first)
    elif change=='missing-claim':
        state['edges']=[edge for edge in state['edges'] if edge['node'].get('fullDatabaseId')!='101']
    elif change=='foreign-review':first['node']['author']['login']='contributor'
    elif change=='wrong-epoch':
        complete=next(edge for edge in state['edges'] if edge['node'].get('fullDatabaseId')=='102')
        # Still a well-formed marker, with an unrelated epoch.
        complete['node']['body']=f'<!-- pp:head-reviewed {"1"*40} review-comment=100 claim=101 epoch-sha256={"f"*64} -->'
    elif change=='edited':first['node']['lastEditedAt']='2026-10-29T00:00:01Z'
    elif change=='duplicate-id':
        twin=copy.deepcopy(first);twin['cursor']='OTHER_CURSOR';twin['node']['id']='OTHER_NODE';state['edges'].append(twin)
    else:state['edges'].append({'cursor':'DELETED','node':{'__typename':'CommentDeletedEvent','id':'DELETED','createdAt':'2026-10-29T00:00:01Z'}})
    with pytest.raises(PipelineError):verified_depth(state,trusted='owner')


def test_duplicate_canonical_completion_counts_one_review_not_one_more_round():
    state=history()
    source=next(edge for edge in state['edges'] if edge['node'].get('fullDatabaseId')=='112')
    # Duplicate receipt in the same epoch before its next HEAD anchor.
    state['edges'].insert(len(state['edges'])-1,legacy_comment('113',source['node']['body']))
    assert verified_depth(state,trusted='owner')['depth']==2


def test_typed_declared_depth_must_match_independently_verified_preceding_rounds():
    state=history();add_typed(state,depth=3)
    with pytest.raises(PipelineError,match='contradicts verified history'):
        verified_depth(state,trusted='owner')


def test_untrusted_completion_prose_does_not_change_depth():
    state=history();extra=legacy_comment('999','<!-- pp:head-reviewed '+'a'*40+' review-comment=1 claim=2 epoch-sha256='+'f'*64+' -->')
    extra['node']['author']['login']='contributor';state['edges'].append(extra)
    assert verified_depth(state,trusted='owner')['depth']==2


@pytest.mark.parametrize('change',['epoch','anchor'])
def test_typed_proof_requires_a_real_historical_server_epoch(change):
    state=history()
    add_typed(state,**({'epoch':'f'*64} if change=='epoch' else {'anchor_id':'NO_SUCH_SERVER_ANCHOR'}))
    with pytest.raises(PipelineError,match='historical server epoch/anchor'):
        verified_depth(state,trusted='owner')


@pytest.mark.parametrize('change',['head','review-again'])
def test_typed_epoch_must_stay_the_same_through_completion(change):
    state=history();add_typed(state)
    if change=='head':
        event={'cursor':'MID_HEAD','node':{'__typename':'HeadRefForcePushedEvent','id':'MID_HEAD',
                                        'afterCommit':{'oid':'b'*40}}}
    else:event=legacy_comment('4000','pp:review-again')
    state['edges'].insert(len(state['edges'])-2,event)
    with pytest.raises(PipelineError):verified_depth(state,trusted='owner')


def test_legacy_and_typed_completion_cannot_count_twice_for_one_epoch():
    state=snapshot();legacy(state,state['headRefOid'],100);add_typed(state,depth=1)
    with pytest.raises(PipelineError,match='same HEAD epoch'):
        verified_depth(state,trusted='owner')


@pytest.mark.parametrize('valid',[True,False])
def test_unfinished_typed_root_needs_a_real_epoch_but_does_not_add_a_round(valid):
    state=history();add_typed(state,**({} if valid else {'epoch':'f'*64}))
    del state['edges'][-2:]
    if valid:assert verified_depth(state,trusted='owner')['depth']==2
    else:
        with pytest.raises(PipelineError,match='historical server epoch/anchor'):
            verified_depth(state,trusted='owner')
