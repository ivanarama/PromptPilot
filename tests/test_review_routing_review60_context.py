"""Partial actor projections and impossible retained headers never reset depth."""
import copy
import hashlib

import pytest

from promptpilot import review_routing_wire as wire
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_audit_history import retained
from test_review_routing_history import legacy_comment


@pytest.mark.parametrize('fault',['missing','null','wrong-type'])
def test_public_review_cannot_forget_typed_history_with_an_unresolved_actor(adapter,fault):
    peer,_health,_path,report,run=adapter
    code,selected=run('next','review');assert code==0
    code,_completed=run('complete','review','--lease',selected['lease'],'--report',str(report));assert code==0
    for edge in peer.comments:
        if fault=='missing':del edge['node']['author']
        elif fault=='null':edge['node']['author']=None
        else:edge['node']['author']='owner'
    before=list(peer.posts)
    code,result=run('next','review')
    assert code==2 and result['action']=='error' and peer.posts==before


@pytest.mark.parametrize('author',[None,{'login':'github-actions[bot]'}])
def test_known_unrelated_ghost_and_bot_comments_do_not_erase_or_park_review(adapter,author):
    peer,_health,_path,_report,run=adapter
    comment=legacy_comment('99999','ordinary unrelated app: status');comment['node']['author']=author
    peer.current['data']['repository']['pullRequest']['timelineItems']['edges'].append(comment)
    code,selected=run('next','review')
    assert code==0 and selected['action']=='audit' and selected['review_depth']==2


@pytest.mark.parametrize('field,value',[
    ('labelsComplete',False),('state','INVALID_STATE'),('isDraft','false'),
    ('labels',{'unexpected':True}),('baseRefName','other-base'),
])
def test_public_review_rejects_a_correctly_hashed_impossible_original_header(adapter,field,value):
    peer,_health,_path,_report,run=adapter
    state=retained();old_context=new_context=None
    for edge in state['edges']:
        node=edge['node']
        if not node.get('body','').startswith('<!-- pp:review-routing-v1 '):continue
        payload=wire.parse(node['body'])
        if payload['kind']=='publication':
            old_context=wire.context(payload)
            header=payload['audit']['history']['header'];header[field]=copy.deepcopy(value)
            payload['audit']['snapshot_sha256']=wire.sha(dict(header,edges=state['edges'][:10]))
            new_context=wire.sha({key:item for key,item in payload.items() if key!='owner'})
        elif payload.get('context_sha256')==old_context:payload['context_sha256']=new_context
        raw=wire.canonical(payload)
        node['body']=f"<!-- pp:review-routing-v1 kind={payload['kind']} sha256={hashlib.sha256(raw).hexdigest()} -->\n"+raw.decode('utf-8')
    peer.current['data']['repository']['pullRequest']['timelineItems']['edges']=state['edges']
    code,result=run('next','review')
    assert code==2 and result['action']=='error' and peer.posts==[]
