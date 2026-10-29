"""Exercise the immutable phase coordinator through its guarded transport."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_journal import Journal
from promptpilot.review_routing_publication import Publication
from promptpilot.review_routing_wire import parse, render


HISTORY=json.loads((Path(__file__).parent/'fixtures/review-routing-wire-v1.json').read_text(encoding='utf-8'))['records']
PAYLOAD=HISTORY[0]['payload']
CONFIG={'repository':'owner/repo','trusted_account':'owner'}
OWNER2='00000000-0000-4000-8000-000000000002'


class Server:
    def __init__(self,*,lose_kind=None,visible=True):
        self.now=datetime(2026,10,29,tzinfo=timezone.utc)
        self.comments=[]
        self.posts=[]
        self.lose_kind=lose_kind
        self.visible=visible
        self.hold=False

    def json(self,*args,input_value=None):
        assert args==('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-')
        payload=parse(input_value['body'])
        ident=str(1000+len(self.comments))
        self.comments.append({'cursor':'C'+ident,'node':{'__typename':'IssueComment','fullDatabaseId':ident,
             'createdAt':self.now.isoformat().replace('+00:00','Z'),'lastEditedAt':None,
             'author':{'login':'owner'},'body':input_value['body']}})
        self.posts.append(payload['kind'])
        if payload['kind']==self.lose_kind:
            raise OSError('response lost after acceptance')
        return {'id':ident}

    def guard(self,payload,*,write=False):
        if self.hold:
            raise PipelineError('stage stopped by hold')
        return {'pr_key':payload['pr'],'edges':copy.deepcopy(self.comments) if self.visible else []}


def transaction(server,tmp_path,*,owner=None):
    seed=copy.deepcopy(PAYLOAD)
    if owner is not None:
        seed['owner']=owner
    return Publication(server,CONFIG,Journal(tmp_path),server.guard,seed,clock=lambda:server.now)


def test_three_immutable_phases_complete_once_and_cache_loss_does_not_repeat(tmp_path):
    server=Server()
    result=transaction(server,tmp_path/'first').run()
    assert result['action']=='completed' and result['outcome']=='technical-handoff'
    assert result['source']['publication_id']=='1000'
    assert result['source']['claim_id']=='1001' and result['source']['completion_id']=='1002'
    assert result['mutation_authorized'] is False
    assert server.posts==['publication','claim','completion']
    assert transaction(server,tmp_path/'cache-lost').run()==result
    assert len(server.posts)==3
    assert all('pp:head-reviewed' not in item['node']['body'] for item in server.comments)


@pytest.mark.parametrize('kind',['publication','claim','completion'])
def test_response_loss_in_each_phase_is_read_back_without_a_second_post(tmp_path,kind):
    server=Server(lose_kind=kind)
    assert transaction(server,tmp_path).run()['action']=='completed'
    assert server.posts==['publication','claim','completion']


def test_delayed_receipt_remains_a_barrier_then_recovers_the_same_phase(tmp_path):
    server=Server(lose_kind='publication',visible=False)
    for _ in range(2):
        with pytest.raises(PipelineError):
            transaction(server,tmp_path).run()
    assert server.posts==['publication']
    server.visible=True
    assert transaction(server,tmp_path).run()['action']=='completed'
    assert server.posts==['publication','claim','completion']


def test_live_other_owner_waits_and_expired_owner_takeover_keeps_the_same_root(tmp_path):
    server=Server()
    server.json('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-',
                input_value={'body':render(PAYLOAD)})
    assert transaction(server,tmp_path/'second',owner=OWNER2).run()['action']=='wait'
    assert server.posts==['publication']
    server.now+=timedelta(minutes=30)
    result=transaction(server,tmp_path/'takeover',owner=OWNER2).run()
    assert result['action']=='completed' and result['source']['publication_id']=='1000'
    assert server.posts==['publication','lease','claim','completion']
    assert transaction(server,tmp_path/'old-owner').run()==result
    assert len(server.posts)==4


def test_expired_same_uuid_does_not_renew_or_restart_a_publication(tmp_path):
    server=Server()
    server.json('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-',
                input_value={'body':render(PAYLOAD)})
    server.now+=timedelta(minutes=30)
    result=transaction(server,tmp_path).run()
    assert result['action']=='wait' and 'new signed request UUID' in result['reason']
    assert server.posts==['publication']


def test_renewal_in_last_five_minutes_uses_the_same_owner_and_continues(tmp_path):
    server=Server()
    server.json('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-',
                input_value={'body':render(PAYLOAD)})
    server.now+=timedelta(minutes=26)
    result=transaction(server,tmp_path).run()
    assert result['action']=='completed'
    assert server.posts==['publication','lease','claim','completion']
    assert parse(server.comments[1]['node']['body'])['owner']==PAYLOAD['owner']


def test_late_hold_after_first_accepted_post_prevents_claim_and_completion(tmp_path):
    server=Server()
    post=server.json

    def hold_after_post(*args,**kwargs):
        result=post(*args,**kwargs)
        server.hold=True
        return result

    server.json=hold_after_post
    with pytest.raises(PipelineError,match='hold'):
        transaction(server,tmp_path).run()
    assert server.posts==['publication']


def history_server(count):
    server=Server()
    server.now+=timedelta(minutes=4)
    for item in HISTORY[:count]:
        server.comments.append({'cursor':'C'+item['id'],'node':{'__typename':'IssueComment',
             'fullDatabaseId':item['id'],'createdAt':item['created_at'],'lastEditedAt':None,
             'author':{'login':'owner'},'body':render(item['payload'])}})
    return server


@pytest.mark.parametrize('count,edit,reason',[
    (4,{},'post-push'),
    (8,{'budget_used':0},'lifetime budget'),
    (19,{},'engineering resume'),
])
def test_invalid_new_publication_is_refused_before_poisoning_server_history(tmp_path,count,edit,reason):
    from promptpilot.review_routing import inspect_report
    from promptpilot.review_routing_wire import outcome
    server=history_server(count)
    seed=copy.deepcopy(HISTORY[8 if count<19 else 16]['payload'])
    if count==19:
        seed['identity']['head_sha']='d'*40
        seed['report']['head']='d'*40
        seed['report_sha256']=inspect_report(seed['report'],'d'*40)['report_sha256']
        seed['epoch']='f'*64
    seed.update(edit)
    seed['outcome']=outcome(seed['report'],depth=seed['depth'],budget_used=seed['budget_used'],max_attempts=seed['max_attempts'])
    writer=Publication(server,CONFIG,Journal(tmp_path),server.guard,seed,clock=lambda:server.now)
    with pytest.raises(PipelineError,match=reason):
        writer.run()
    assert server.posts==[]


def test_next_review_after_completed_fix_preserves_spent_slot_and_exact_new_head(tmp_path):
    server=history_server(8)
    writer=Publication(server,CONFIG,Journal(tmp_path),server.guard,HISTORY[8]['payload'],clock=lambda:server.now)
    result=writer.run()
    assert result['action']=='completed' and result['source']['head']==HISTORY[8]['payload']['identity']['head_sha']
    assert server.posts==['publication','claim','completion']


def test_incomplete_resumed_publication_cannot_reuse_the_grant_in_another_epoch(tmp_path):
    from test_review_routing_proof import history, next_review, resume
    grant=resume('1018','1011',head='d'*40)
    existing=next_review(history()+[grant],head='d'*40,blocking=[],resume_id=grant.id)[:-1]
    server=Server()
    server.now+=timedelta(minutes=4)
    for item in existing:
        server.comments.append({'cursor':'C'+item.id,'node':{'__typename':'IssueComment',
             'fullDatabaseId':item.id,'createdAt':item.created_at,'lastEditedAt':None,
             'author':{'login':'owner'},'body':render(item.payload)}})
    seed=copy.deepcopy(existing[-2].payload)
    seed['epoch']='f'*64
    seed['anchor']='OTHER_ANCHOR'
    writer=Publication(server,CONFIG,Journal(tmp_path),server.guard,seed,clock=lambda:server.now)
    with pytest.raises(PipelineError,match='resum|incomplete'):
        writer.run()
    assert server.posts==[]


def test_final_fresh_snapshot_rechecks_semantic_admission_before_root_post(tmp_path):
    server=history_server(3)
    seed=copy.deepcopy(HISTORY[8]['payload'])
    seed['budget_used']=0
    original=server.guard

    def late_attempt(payload,*,write=False):
        if write and not any(item['node']['fullDatabaseId']=='4500' for item in server.comments):
            server.comments.append({'cursor':'C4500','node':{'__typename':'IssueComment',
                 'fullDatabaseId':'4500','createdAt':'2026-10-29T00:03:00Z','lastEditedAt':None,
                 'author':{'login':'owner'},'body':render(HISTORY[3]['payload'])}})
        return original(payload,write=write)

    writer=Publication(server,CONFIG,Journal(tmp_path),late_attempt,seed,clock=lambda:server.now)
    with pytest.raises(PipelineError,match='budget|post-push'):
        writer.run()
    assert server.posts==[]


@pytest.mark.parametrize('change_report',[False,True])
def test_final_snapshot_canonical_root_prevents_another_root_before_post(tmp_path,change_report):
    from promptpilot.review_routing import inspect_report
    server=history_server(3)
    seed=copy.deepcopy(HISTORY[8]['payload']);seed['budget_used']=0
    competing=copy.deepcopy(seed);competing['owner']=OWNER2
    if change_report:
        competing['report']['change']+=' different audit'
        competing['report_sha256']=inspect_report(competing['report'],competing['identity']['head_sha'])['report_sha256']
    original=server.guard

    def late_root(payload,*,write=False):
        if write and not any(item['node']['fullDatabaseId']=='4500' for item in server.comments):
            server.comments.append({'cursor':'C4500','node':{'__typename':'IssueComment',
                 'fullDatabaseId':'4500','createdAt':'2026-10-29T00:03:00Z','lastEditedAt':None,
                 'author':{'login':'owner'},'body':render(competing)}})
        return original(payload,write=write)

    writer=Publication(server,CONFIG,Journal(tmp_path),late_root,seed,clock=lambda:server.now)
    with pytest.raises(PipelineError,match='root appeared'):
        writer.run()
    assert server.posts==[]
