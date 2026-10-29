"""Authenticated guard through real typed reads, policy trees and journal."""
import copy
from datetime import datetime,timedelta,timezone
import json

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_request as requests
from promptpilot import review_routing_policy as policy
from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing import inspect_report
from promptpilot.review_routing_journal import Journal
from promptpilot.review_routing_publication import Publication
from promptpilot.review_routing_snapshot import stable_timeline
from promptpilot.review_routing_stage_guard import StageGuard
from promptpilot.review_routing_wire import context,parse,render,sha
from test_review_routing_policy import peers
from test_review_routing_snapshot import page
from test_review_routing_wire import publication


class Peer:
    def __init__(self):
        self.now=datetime(2026,10,29,12,tzinfo=timezone.utc)
        self.current=page();self.actor='owner';self.posts=[];self.comments=[]
        self.compare_status='ahead';self.calls=[]
    def json(self,*args,input_value=None):
        self.calls.append(args[:2])
        if args==('api','user'):return {'login':self.actor}
        if args==('api','repos/owner/repo'):return {'full_name':'owner/repo','node_id':'R_BASE'}
        if args[1]=='graphql':
            assert input_value is None and 'mutation' not in ' '.join(args)
            result=copy.deepcopy(self.current)
            result['data']['repository']['pullRequest']['timelineItems']['edges']+=copy.deepcopy(self.comments)
            return result
        if args[1].startswith('repos/owner/repo/compare/'):
            before=args[1].rsplit('/',1)[1].split('...')[0]
            return {'status':self.compare_status,'base_commit':{'sha':before},'merge_base_commit':{'sha':before}}
        assert args==('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-')
        data=parse(input_value['body']);ident=str(1000+len(self.comments))
        self.comments.append({'cursor':'C'+ident,'node':{'__typename':'IssueComment','id':'IC_'+ident,
            'fullDatabaseId':ident,'createdAt':self.now.isoformat().replace('+00:00','Z'),
            'lastEditedAt':None,'author':{'login':'owner'},'body':input_value['body']}})
        self.posts.append(data['kind'])
        return {'id':ident}


@pytest.fixture
def setup_guard(tmp_path,monkeypatch):
    monkeypatch.setenv('PP_DATA_DIR',str(tmp_path/'private-data'))
    monkeypatch.setenv('PP_PIPELINE_REPLICAS','1')
    for name in ['PP_PIPELINE_LEASE_KEY_FILE','PP_TASK_ID','PP_TASK_STARTED_AT','PP_PROVIDER_OWNERSHIP_KIND']:
        monkeypatch.delenv(name,raising=False)
    root,raw,health=peers(tmp_path)
    raw.update(repository='owner/repo',trusted_account='owner')
    path=tmp_path/'pipelinectl.json';path.write_text(json.dumps(raw),encoding='utf-8')
    config=pp.load_config(str(path));peer=Peer()
    monkeypatch.setattr(pp,'run_health',lambda *args,**kwargs:copy.deepcopy(health))
    monkeypatch.setattr(requests.time,'time',lambda:int(peer.now.timestamp()))
    state=stable_timeline(peer,config,42)
    req=requests.build(config,state,policy.preflight(config,health),depth=2,budget_used=0,
                       now=int(peer.now.timestamp()))
    seed=publication()
    seed.update(identity=state['identity'],pr=state['pr_key'],author=state['author'],epoch=req['epoch'],
        anchor=req['anchor'],audit=req['audit'],depth=2,budget_used=0,max_attempts=2,resume_id=None,owner=req['owner_uuid'])
    seed['report']['head']=req['head']
    seed['report_sha256']=inspect_report(seed['report'],req['head'])['report_sha256']
    token=requests.encode(req)
    def make(payload=None,request_token=None):
        return StageGuard(peer,str(path),request_token or token,payload or seed,clock=lambda:peer.now)
    return peer,config,health,path,root,req,seed,make


def test_authenticated_three_phase_publication_and_expired_receipt_recovery(setup_guard,tmp_path):
    peer,config,_h,_p,_r,req,seed,make=setup_guard
    first=Publication(peer,config,Journal(tmp_path/'first'),make(),seed,clock=lambda:peer.now).run()
    assert first['action']=='completed' and peer.posts==['publication','claim','completion']
    peer.now+=timedelta(seconds=req['expires_at']-req['issued_at']+1)
    assert Publication(peer,config,Journal(tmp_path/'lost-cache'),make(),seed,clock=lambda:peer.now).run()==first
    assert peer.posts==['publication','claim','completion']


@pytest.mark.parametrize('change',['actor','hold','draft','closed','author','head-branch','reader','config','red','deleted-prefix'])
def test_fresh_gate_changes_stop_before_any_write(setup_guard,tmp_path,change):
    peer,config,health,path,root,_req,seed,make=setup_guard
    pr=peer.current['data']['repository']['pullRequest']
    if change=='actor':peer.actor='someone'
    elif change=='hold':pr['labels']['nodes']=[{'name':'hold'}]
    elif change=='draft':pr['isDraft']=True
    elif change=='closed':pr['state']='CLOSED'
    elif change=='author':pr['author']['id']='U_CHANGED'
    elif change=='head-branch':pr['headRefName']='other-branch'
    elif change=='reader':(root/'fix-approved/SKILL.md').write_text('changed',encoding='utf-8')
    elif change=='config':
        raw=json.loads(path.read_text(encoding='utf-8'));raw['technical_handoff']['max_extra_attempts']=1
        path.write_text(json.dumps(raw),encoding='utf-8')
    elif change=='red':health['state']='red'
    else:pr['timelineItems']['edges']=[]
    with pytest.raises(PipelineError):
        Publication(peer,config,Journal(tmp_path/change),make(),seed,clock=lambda:peer.now).run()
    assert peer.posts==[]


def test_config_and_actor_are_read_again_after_root_post(setup_guard,tmp_path):
    peer,config,_h,_p,_r,_req,seed,make=setup_guard
    original=peer.json
    def changed_actor(*args,**kwargs):
        result=original(*args,**kwargs)
        if kwargs.get('input_value') is not None:peer.actor='someone'
        return result
    peer.json=changed_actor
    with pytest.raises(PipelineError,match='authenticated'):
        Publication(peer,config,Journal(tmp_path/'journal'),make(),seed,clock=lambda:peer.now).run()
    assert peer.posts==['publication']


def test_normal_forward_base_movement_is_proven_but_divergence_refuses(setup_guard):
    peer,_config,_h,_p,_r,_req,seed,make=setup_guard
    peer.current['data']['repository']['pullRequest']['baseRefOid']='c'*40
    assert make()(seed)['baseRefOid']=='c'*40
    assert any('/compare/' in call[1] for call in peer.calls)
    peer.compare_status='diverged'
    with pytest.raises(PipelineError,match='did not advance'):make()(seed)


def test_request_expiry_during_final_snapshot_hash_does_not_send(setup_guard,tmp_path,monkeypatch):
    from promptpilot import review_routing_stage_guard as stages
    peer,config,_h,_p,_r,req,seed,make=setup_guard
    gate=make();original=stages.sha;count=0
    def delayed_hash(value):
        nonlocal count
        result=original(value);count+=1
        if count==2:peer.now=datetime.fromtimestamp(req['expires_at'],timezone.utc)
        return result
    monkeypatch.setattr(stages,'sha',delayed_hash)
    journal=Journal(tmp_path/'journal')
    with pytest.raises(PipelineError,match='expired'):
        Publication(peer,config,journal,gate,seed,clock=lambda:peer.now).run()
    assert peer.posts==[] and journal.pending_for(seed['pr'])==[]


def test_final_snapshot_ticket_is_one_use_and_cannot_bind_mutated_snapshot(setup_guard):
    _peer,_config,_h,_p,_r,_req,seed,make=setup_guard
    gate=make();state=gate(seed,write=True)
    changed=copy.deepcopy(state)
    with pytest.raises(PipelineError,match='matching fresh'):gate.final_before_post(seed,changed)
    with pytest.raises(PipelineError,match='matching fresh'):gate.final_before_post(seed,state)
    state=gate(seed,write=True);state['labels'].append('hold')
    with pytest.raises(PipelineError,match='matching fresh'):gate.final_before_post(seed,state)


def test_claim_lease_expires_during_matching_even_with_live_request(setup_guard,tmp_path):
    peer,config,_h,_p,_r,_req,seed,make=setup_guard
    journal=Journal(tmp_path/'journal');tx=Publication(peer,config,journal,make(),seed,clock=lambda:peer.now)
    original=tx.transport._matches
    def delayed(snapshot,payload,body):
        matches=original(snapshot,payload,body)
        if payload['kind']=='claim' and tx.guard._ticket is not None:
            peer.now+=timedelta(minutes=30)
        return matches
    tx.transport._matches=delayed
    with pytest.raises(PipelineError,match='not currently active'):tx.run()
    assert peer.posts==['publication']
    assert journal.pending_for(seed['pr'])==[]


def test_wrong_report_context_and_operation_cannot_use_a_valid_request(setup_guard):
    _peer,_config,_h,_p,_r,_req,seed,make=setup_guard
    changed=copy.deepcopy(seed);changed['owner']='00000000-0000-4000-8000-000000000002'
    with pytest.raises(PipelineError,match='another request UUID'):make(payload=changed)
    gate=make()
    other=copy.deepcopy(seed);other['anchor']='different'
    with pytest.raises(PipelineError,match='immutable report/context'):gate(other,write=True)
    claim={'protocol':seed['protocol'],'kind':'claim','pr':seed['pr'],
           'owner':'00000000-0000-4000-8000-000000000002','context_sha256':context(seed),
           'publication_id':'1000','lease_id':'1000'}
    with pytest.raises(PipelineError,match='another owner UUID'):gate(claim,write=True)


def test_new_recovery_scope_preserves_old_root_limit_and_takes_over_after_expiry(setup_guard,tmp_path):
    peer,config,health,path,_r,_req,seed,_make=setup_guard
    peer.json('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-',
              input_value={'body':render(seed)})
    peer.now+=timedelta(minutes=30)
    raw=json.loads(path.read_text(encoding='utf-8'));raw['technical_handoff']['max_extra_attempts']=1
    path.write_text(json.dumps(raw),encoding='utf-8');current=pp.load_config(str(path))
    state=stable_timeline(peer,current,42)
    request=requests.build(current,state,policy.preflight(current,health),depth=seed['depth'],
        budget_used=seed['budget_used'],now=int(peer.now.timestamp()),
        recovery={'publication_id':'1000','context_sha256':context(seed),'audit':seed['audit']})
    recovered=copy.deepcopy(seed);recovered['owner']=request['owner_uuid']
    assert recovered['max_attempts']==2 and request['current_limit']==1
    gate=StageGuard(peer,str(path),requests.encode(request),recovered,clock=lambda:peer.now)
    result=Publication(peer,current,Journal(tmp_path/'recovery'),gate,recovered,clock=lambda:peer.now).run()
    assert result['action']=='completed' and result['source']['publication_id']=='1000'
    assert peer.posts==['publication','lease','claim','completion']
    assert parse(peer.comments[0]['node']['body'])['max_attempts']==2


@pytest.mark.parametrize('when',['actor','renewal','matching'])
@pytest.mark.parametrize('what',['disabled','reader'])
def test_late_local_policy_change_stops_the_first_post(setup_guard,tmp_path,monkeypatch,when,what):
    peer,config,_health,path,root,_req,seed,make=setup_guard
    changed=False
    def change():
        nonlocal changed
        if changed:return
        changed=True
        if what=='disabled':
            raw=json.loads(path.read_text(encoding='utf-8'));raw['technical_handoff']['enabled']=False
            path.write_text(json.dumps(raw),encoding='utf-8')
        else:
            entry=root/'fix-approved/SKILL.md'
            entry.write_text(entry.read_text(encoding='utf-8')+'\nnew policy\n',encoding='utf-8')
    tx=Publication(peer,config,Journal(tmp_path/'journal'),make(),seed,clock=lambda:peer.now)
    if when=='actor':
        original_guard_call=StageGuard.__call__
        def track_write(self,payload,*,write=False):
            self._test_in_write=write
            try:return original_guard_call(self,payload,write=write)
            finally:self._test_in_write=False
        monkeypatch.setattr(StageGuard,'__call__',track_write)
        original=peer.json
        def after_actor(*args,**kwargs):
            result=original(*args,**kwargs)
            if args==('api','user') and getattr(tx.guard,'_test_in_write',False):change()
            return result
        peer.json=after_actor
    elif when=='renewal':
        original=pp.renew_lease_target_reservation
        def after_renewal(*args,**kwargs):
            result=original(*args,**kwargs);change();return result
        monkeypatch.setattr(pp,'renew_lease_target_reservation',after_renewal)
    else:
        original=tx.transport._matches
        def after_matching(*args,**kwargs):
            result=original(*args,**kwargs)
            if tx.guard._ticket is not None:change()
            return result
        tx.transport._matches=after_matching
    with pytest.raises(PipelineError):tx.run()
    assert changed and peer.posts==[]
    assert tx.journal.pending_for(seed['pr'])==[]


def historical_guard(setup_guard):
    """Actual two legacy transactions, rather than a synthetic declared depth."""
    from test_review_routing_history import history
    peer,config,health,path,_root,_req,seed,_make=setup_guard
    peer.current['data']['repository']['pullRequest']['timelineItems']['edges']=history()['edges']
    state=stable_timeline(peer,config,42)
    request=requests.build(config,state,policy.preflight(config,health),depth=2,budget_used=0,
                           now=int(peer.now.timestamp()))
    seed=copy.deepcopy(seed)
    seed.update(epoch=request['epoch'],anchor=request['anchor'],audit=request['audit'],owner=request['owner_uuid'])
    token=requests.encode(request)
    return peer,config,seed,lambda:StageGuard(peer,str(path),token,seed,clock=lambda:peer.now)


@pytest.mark.parametrize('change',['head','hold','closed'])
def test_exact_completed_receipt_survives_later_head_or_admission_change(setup_guard,tmp_path,change):
    peer,config,seed,make=historical_guard(setup_guard)
    original=Publication(peer,config,Journal(tmp_path/'first'),make(),seed,clock=lambda:peer.now).run()
    pr=peer.current['data']['repository']['pullRequest']
    if change=='head':
        pr['headRefOid']='c'*40
        peer.comments.append({'cursor':'NEW_HEAD','node':{'__typename':'PullRequestCommit',
                              'id':'NEW_HEAD','commit':{'oid':'c'*40}}})
    elif change=='hold':pr['labels']['nodes']=[{'name':'hold'}]
    else:pr['state']='CLOSED'
    assert Publication(peer,config,Journal(tmp_path/'lost-cache'),make(),seed,clock=lambda:peer.now).run()==original
    assert original['mutation_authorized'] is False
    with pytest.raises(PipelineError):make()(seed,write=True)
    assert peer.posts==['publication','claim','completion']


def test_lost_post_response_can_confirm_old_root_without_continuing_on_new_head(setup_guard,tmp_path):
    peer,config,seed,make=historical_guard(setup_guard)
    journal=Journal(tmp_path/'journal');original=peer.json
    def response_lost(*args,**kwargs):
        result=original(*args,**kwargs)
        if kwargs.get('input_value') is not None:
            peer.current['data']['repository']['pullRequest']['headRefOid']='c'*40
            peer.comments.append({'cursor':'NEW_HEAD','node':{'__typename':'PullRequestCommit',
                                  'id':'NEW_HEAD','commit':{'oid':'c'*40}}})
            raise OSError('response lost after accepted root')
        return result
    peer.json=response_lost
    with pytest.raises(PipelineError):
        Publication(peer,config,journal,make(),seed,clock=lambda:peer.now).run()
    row=journal.get(owner=seed['owner'],phase='publication:'+sha(seed))
    assert row['state']=='done' and row['returned_id']=='1000'
    assert peer.posts==['publication']


@pytest.mark.parametrize('change',['actor','head-ref','author','edited','missing-legacy','duplicate-receipt'])
def test_old_receipt_does_not_bypass_identity_or_history_validation(setup_guard,tmp_path,change):
    peer,config,seed,make=historical_guard(setup_guard)
    Publication(peer,config,Journal(tmp_path/'first'),make(),seed,clock=lambda:peer.now).run()
    pr=peer.current['data']['repository']['pullRequest'];pr['labels']['nodes']=[{'name':'hold'}]
    if change=='actor':peer.actor='other'
    elif change=='head-ref':pr['headRefName']='other'
    elif change=='author':pr['author']['id']='U_OTHER'
    elif change=='edited':peer.comments[0]['node']['lastEditedAt']=peer.now.isoformat()
    elif change=='missing-legacy':
        pr['timelineItems']['edges']=[edge for edge in pr['timelineItems']['edges'] if edge['node'].get('fullDatabaseId')!='100']
    else:
        twin=copy.deepcopy(peer.comments[0]);twin.update(cursor='DUPLICATE')
        twin['node'].update(id='IC_DUPLICATE',fullDatabaseId='9999');peer.comments.append(twin)
    with pytest.raises(PipelineError):
        Publication(peer,config,Journal(tmp_path/'recovery'),make(),seed,clock=lambda:peer.now).run()
    assert peer.posts==['publication','claim','completion']


def test_invisible_ambiguous_root_stays_a_barrier_after_head_change(setup_guard,tmp_path):
    peer,config,seed,make=historical_guard(setup_guard)
    journal=Journal(tmp_path/'journal');original=peer.json
    def invisible(*args,**kwargs):
        result=original(*args,**kwargs)
        if kwargs.get('input_value') is not None:
            peer.comments.clear()
            peer.current['data']['repository']['pullRequest']['headRefOid']='c'*40
            raise OSError('root accepted but receipt not visible')
        return result
    peer.json=invisible
    tx=Publication(peer,config,journal,make(),seed,clock=lambda:peer.now)
    for _ in range(2):
        with pytest.raises(PipelineError):tx.run()
    row=journal.get(owner=seed['owner'],phase='publication:'+sha(seed))
    assert row['state']=='ambiguous' and row['returned_id'] is None
    assert peer.posts==['publication']
