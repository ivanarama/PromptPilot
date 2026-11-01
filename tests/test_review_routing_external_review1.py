"""Public regressions from OneBase PR2452 canonical review 6555284057."""
import json

import pytest

from promptpilot import review_routing_wire as wire
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_history import legacy_comment,history,add_typed


@pytest.mark.parametrize('body,override',[
    ('pp:review-again',True),
    ('Example:\n```text\npp:review-again\n```',False),
    ('Example:\n~~~text\npp:review-again\n~~~',False),
    ('Example:\n````text\npp:review-again\n```',False),
    ('```text\npp:review-again\n```\npp:review-again',True),
])
def test_public_completed_review_ignores_markdown_override_examples(adapter,body,override):
    peer,health,_path,report,run=adapter
    value=json.loads(report.read_text(encoding='utf-8'));value['blocking']=[]
    report.write_text(json.dumps(value),encoding='utf-8')
    code,selected=run('next','review');assert code==0 and selected['action']=='audit'
    code,result=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0 and result['outcome']=='reviewed',result
    for field in ['review_candidates','content_review_candidates']:
        health[field][0]['review_depth']=3
    peer.comments.append(legacy_comment('99999',body))
    before=list(peer.posts)
    code,result=run('next','review')
    assert code==0 and (result['action']=='audit')==override,result
    assert peer.posts==before


@pytest.mark.parametrize('field',[None,'depth','budget_used'])
def test_public_unfinished_recovery_requires_original_history_counters(adapter,field):
    peer,_health,_path,_report,run=adapter
    state=history();add_typed(state)
    state['edges']=state['edges'][:-2]
    if field:
        for edge in state['edges']:
            node=edge['node']
            if not node.get('body','').startswith('<!-- pp:review-routing-v1 '):continue
            payload=wire.parse(node['body']);assert payload['kind']=='publication'
            payload[field]+=1;node['body']=wire.render(payload)
    peer.current['data']['repository']['pullRequest']['timelineItems']['edges']=state['edges']
    code,result=run('next','review')
    if field is None:assert code==0 and result['action']=='recover',result
    else:assert code!=0 and result['action']=='error',result
    assert peer.posts==[]


def test_public_legacy_history_keeps_fenced_example_between_review_and_claim(adapter):
    peer,_health,_path,_report,run=adapter
    state=history()
    for index,edge in enumerate(state['edges']):
        if edge['node'].get('body','').startswith('Reviewed-SHA:'):
            state['edges'].insert(index+1,legacy_comment('99999','Example:\n```text\npp:review-again\n```'))
            break
    peer.current['data']['repository']['pullRequest']['timelineItems']['edges']=state['edges']
    code,result=run('next','review')
    assert code==0 and result['action']=='audit' and result['review_depth']==2,result
    assert peer.posts==[]
