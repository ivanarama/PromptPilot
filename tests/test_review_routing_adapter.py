"""Actual pipelinectl.run entrypoints with an authenticated local fake peer."""
from datetime import datetime,timedelta,timezone
import json

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_request as requests
from promptpilot.review_routing_wire import render
from test_review_routing_history import history,legacy,anchor
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401


@pytest.fixture
def adapter(setup_guard,monkeypatch,capsys,tmp_path):
    peer,config,health,path,_root,_request,seed,_make=setup_guard
    peer.now=datetime.now(timezone.utc)
    monkeypatch.setenv('PP_PIPELINE_STATE_DIR',str(tmp_path/'s'))
    peer.current['data']['repository']['pullRequest']['timelineItems']['edges']=history()['edges']
    candidate={'stage':'review','number':42,'head':seed['identity']['head_sha'],'review_depth':2}
    health.update(review_candidates=[candidate],content_review_candidates=[candidate],merge_executable=[],integration_owner=None)
    monkeypatch.setattr(pp,'GitHub',lambda:peer)
    peer.graphql_page=lambda owner,name,number,cursor:peer.json('api','graphql')
    report=tmp_path/'report.json';report.write_text(json.dumps(seed['report']),encoding='utf-8')
    def run(*args):
        result=pp.run(['--config',str(path),*args])
        output=json.loads(capsys.readouterr().out)
        return result,output
    return peer,health,path,report,run


def test_third_round_reaches_actual_cli_audit_and_typed_completion(adapter):
    peer,_health,_path,report,run=adapter
    code,selected=run('next','review')
    assert code==0 and selected['action']=='audit' and selected['review_depth']==2
    request=requests.decode(selected['lease'])
    assert request['purpose']==requests.AUDIT and request['depth']==2 and peer.posts==[]
    code,completed=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0 and completed['action']=='completed' and completed['outcome']=='technical-handoff'
    assert peer.posts==['publication','claim','completion'] and completed['mutation_authorized'] is False
    code,repeated=run('next','review')
    assert code==0 and repeated['action']=='wait' and repeated['source']==completed['source']
    assert peer.posts==['publication','claim','completion']


def test_recovery_cli_reuses_server_report_with_new_uuid_and_no_report_argument(adapter):
    peer,_health,_path,report,run=adapter
    _code,selected=run('next','review');original=requests.decode(selected['lease'])
    # Complete the first POST only, as if the publishing process had stopped.
    from promptpilot.review_routing import inspect_file
    from promptpilot.review_routing_wire import outcome
    value=json.loads(inspect_file(str(report),original['head'])['canonical_record'])
    seed={'protocol':'review-routing-v1','kind':'publication','pr':original['pr_key'],'owner':original['owner_uuid'],
          'identity':original['identity'],'author':original['author'],'epoch':original['epoch'],'anchor':original['anchor'],
          'audit':original['audit'],'depth':2,'budget_used':0,'max_attempts':2,'resume_id':None,
          'report':value,'report_sha256':inspect_file(str(report),original['head'])['report_sha256'],
          'outcome':outcome(value,depth=2,budget_used=0)}
    peer.json('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-',input_value={'body':render(seed)})
    peer.comments[0]['node']['createdAt']=(peer.now-timedelta(minutes=31)).isoformat().replace('+00:00','Z')
    code,recovery=run('next','review')
    assert code==0,recovery
    recovered=requests.decode(recovery['lease'])
    assert code==0 and recovery['action']=='recover' and recovered['owner_uuid']!=original['owner_uuid']
    assert recovered['audit']==original['audit'] and peer.posts==['publication']
    code,completed=run('complete','review','--lease',recovery['lease'])
    assert code==0 and completed['action']=='completed' and completed['source']['publication_id']=='1000'
    assert peer.posts==['publication','lease','claim','completion']


def test_recovery_refuses_a_replacement_report(adapter):
    _peer,_health,_path,report,run=adapter
    code,selected=run('next','review');assert code==0
    request=requests.decode(selected['lease'])
    request['purpose']=requests.RECOVERY
    request['recovery']={'publication_id':'1000','context_sha256':'a'*64}
    code,result=run('complete','review','--lease',requests.encode(request),'--report',str(report))
    assert code==2 and 'substitute' in result['error']


def test_actual_first_round_remains_legacy_when_opted_in(adapter):
    peer,health,_path,_report,run=adapter
    from test_review_routing_baseline import snapshot
    state=snapshot();legacy(state,'1'*40,100);anchor(state,'a'*40,'CURRENT')
    peer.current['data']['repository']['pullRequest']['timelineItems']['edges']=state['edges']
    health['review_candidates'][0]['review_depth']=1
    code,selected=run('next','review')
    assert code==0 and selected['action']=='audit'
    assert 'protocol' not in pp.decode_signed_lease(selected['lease']) and peer.posts==[]


def test_backdated_later_commit_does_not_erase_completed_legacy_rounds(adapter):
    peer,_health,_path,report,run=adapter
    edges=peer.current['data']['repository']['pullRequest']['timelineItems']['edges']
    edges.insert(2,{'cursor':'LATE_OLD_DATE','node':{'__typename':'PullRequestCommit',
        'id':'LATE_OLD_DATE','commit':{'oid':'9'*40}}})
    code,selected=run('next','review')
    assert code==0 and selected['action']=='audit',selected
    assert selected['review_depth']==2
    code,completed=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0 and completed['outcome']=='technical-handoff',completed
    assert peer.posts==['publication','claim','completion']


@pytest.mark.parametrize('change',['missing-contract','red','reader','closed'])
def test_actual_entrypoints_fail_closed_without_a_compatible_current_peer(adapter,change):
    peer,health,path,_report,run=adapter
    if change=='missing-contract':health.pop('review_routing_contract')
    elif change=='red':health['state']='red'
    elif change=='reader':
        entry=path.parent/'.claude/skills/fix-approved/SKILL.md';entry.write_text('old reader',encoding='utf-8')
    else:peer.current['data']['repository']['pullRequest']['state']='CLOSED'
    code,result=run('next','review')
    assert code==2 and result['action']=='error' and peer.posts==[]


ENVELOPE_FAULTS={
    'missing-protocol':('audit',None,True),'v2':('audit','review-routing-v2',True),
    'empty-lease':('audit','review-routing-v1',True),'no-transport':('audit',None,False),
    'merge-no-protocol':('merge',None,True),'merge-v2':('merge','review-routing-v2',True),
    'cleanup-no-protocol':('cleanup',None,True),'cleanup-v2':('cleanup','review-routing-v2',True),
    'wait-no-protocol':('wait',None,True),'empty-v2':('empty','review-routing-v2',True),
    'error-no-protocol':('error',None,True),'unknown-v2':('unexpected','review-routing-v2',True),
    'generic-merge':('merge',None,False),'generic-cleanup':('cleanup','review-routing-v2',False),
}


@pytest.mark.parametrize('envelope_fault',[None,*ENVELOPE_FAULTS])
@pytest.mark.parametrize('recover',[False,True])
def test_single_lane_worker_uses_owned_request_and_recovers_without_model(
        adapter,isolated_db,monkeypatch,tmp_path,recover,envelope_fault):
    from contextlib import redirect_stdout
    from io import StringIO
    from promptpilot import pipeline_insights as insights
    from promptpilot.models import TaskCreate

    peer,_health,path,report,_run=adapter
    monkeypatch.setenv('PP_PIPELINE_LEASE_KEY_FILE',str(isolated_db.DB_PATH.parent/'pipeline-lease.key'))
    task=isolated_db.create_task(TaskCreate(prompt='Project - REVIEW',working_dir=str(tmp_path)))
    task=isolated_db.get_next_runnable();assert task is not None
    if recover:
        # Make a valid prior owner publication through the actual audit CLI,
        # stopping its fake server after the accepted root POST.
        original=peer.json
        def stop_after_root(*args,**kwargs):
            result=original(*args,**kwargs)
            if kwargs.get('input_value') is not None:raise OSError('response lost')
            return result
        peer.json=stop_after_root
        # A read-back succeeds but the next phase must not be posted here.
        from promptpilot import review_routing_adapter as typed
        original_run=typed.Publication.run
        def root_only(self,**kwargs):
            self._publish(self.seed)
            return {'action':'wait','reason':'original publishing process ended'}
        monkeypatch.setattr(typed.Publication,'run',root_only)
        with redirect_stdout(StringIO()):
            selected=pp.next_review(peer,pp.load_config(str(path)),config_path=str(path))
            pp.complete_review(peer,pp.load_config(str(path)),selected['lease'],str(report),config_path=str(path))
        monkeypatch.setattr(typed.Publication,'run',original_run);peer.json=original
        peer.comments[0]['node']['createdAt']=(peer.now-timedelta(minutes=31)).isoformat().replace('+00:00','Z')
    queue={'id':'review','execution':{'mode':'auto','command':['pipelinectl','next','review'],
                                    'review_routing_protocol':'review-routing-v1'}}
    if envelope_fault is not None and not ENVELOPE_FAULTS[envelope_fault][2]:
        queue['execution'].pop('review_routing_protocol')
    monkeypatch.setattr(insights,'_matching_queue',lambda _task:('p',{'repository':'owner/repo'},queue))
    monkeypatch.setattr(insights,'_tool_available',lambda *args:(True,''))
    monkeypatch.setattr(insights,'load_providers',lambda:{})
    calls=[]
    def actual_cli(_execution,command,_directory,*,env_extra=None):
        calls.append(command)
        with monkeypatch.context() as environment:
            for name,value in (env_extra or {}).items():environment.setenv(name,value)
            stream=StringIO()
            with redirect_stdout(stream):code=pp.run(['--config',str(path),*command[1:]])
            result=json.loads(stream.getvalue())
            assert code==0,result
            if envelope_fault is not None and command[-2:]==['next','review']:
                action,protocol,_transport=ENVELOPE_FAULTS[envelope_fault]
                result['action']=action
                if protocol is None:result.pop('protocol',None)
                else:result['protocol']=protocol
                if envelope_fault=='empty-lease':result['lease']=''
            return result
    monkeypatch.setattr(insights,'_tool_preflight',actual_cli)
    route=insights.execution_route(task,'fallback skill',str(tmp_path))
    if envelope_fault is not None:
        assert route['action']=='block' and len(calls)==1,route
        return
    assert route['action']==('complete_empty' if recover else 'prompt'),route
    if recover:
        assert route['completion']['action']=='completed' and len(calls)==2
        assert '--report' not in calls[-1] and route['mode']=='tool'
    else:
        assert route['pipeline_replicas']==1 and route['review_routing_protocol']=='review-routing-v1'
        assert route['pipeline_target_reservation']['task_id']==task.id and len(calls)==1


def test_completion_rechecks_current_content_membership_after_the_audit(adapter):
    peer,health,_path,report,run=adapter
    code,selected=run('next','review');assert code==0
    health.update(review_candidates=[],content_review_candidates=[])
    code,refused=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==2 and refused['action']=='error',refused
    assert peer.posts==[]


@pytest.mark.parametrize('receipt',['visible','invisible','wrong-returned-id','wrong-actor'])
def test_public_next_confirms_visible_old_pending_receipt_then_can_audit_new_head(adapter,receipt):
    from promptpilot.pipeline_errors import PipelineError
    from promptpilot.review_routing_wire import parse
    from promptpilot.review_routing_adapter import _journal
    peer,health,path,report,run=adapter
    value=json.loads(report.read_text(encoding='utf-8'));value['blocking']=[]
    report.write_text(json.dumps(value),encoding='utf-8')
    code,selected=run('next','review');assert code==0
    original=peer.json;blocked=False
    def lost_completion(*args,**kwargs):
        nonlocal blocked
        if len(args)>1 and args[1]=='graphql' and blocked:raise PipelineError('temporary receipt read unavailable')
        result=original(*args,**kwargs)
        if kwargs.get('input_value') is not None and parse(kwargs['input_value']['body'])['kind']=='completion':
            blocked=True;raise OSError('completion response lost')
        return result
    peer.json=lost_completion
    code,failed=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==2 and failed['action']=='error' and len(peer.posts)==3
    peer.json=original
    peer.current['data']['repository']['pullRequest']['headRefOid']='c'*40
    peer.comments.append({'cursor':'AFTER_REVIEW','node':{'__typename':'PullRequestCommit','id':'AFTER_REVIEW','commit':{'oid':'c'*40}}})
    health['review_candidates'][0]['head']='c'*40
    health['review_candidates'][0]['review_depth']=3
    journal=_journal(pp.load_config(str(path)))
    key=requests.decode(selected['lease'])['pr_key']
    if receipt=='invisible':peer.comments=[edge for edge in peer.comments if edge['node'].get('fullDatabaseId')!='1002']
    elif receipt=='wrong-returned-id':
        row=journal.pending_for(key)[0]
        journal.observe_returned_id(owner=row['owner'],phase=row['phase'],returned_id='99999')
    elif receipt=='wrong-actor':peer.actor='other'
    code,current=run('next','review')
    if receipt!='visible':
        assert current['action'] in {'wait','error'} and journal.pending_for(key),current
        assert peer.posts==['publication','claim','completion']
        return
    assert code==0 and current['action']=='audit' and current['target']['head']=='c'*40,current
    assert journal.pending_for(key)==[]
    assert peer.posts==['publication','claim','completion']


def test_current_health_depth_must_agree_with_verified_history_before_model(adapter):
    peer,health,_path,_report,run=adapter
    health['review_candidates'][0]['review_depth']=3
    code,refused=run('next','review')
    assert code==2 and refused['action']=='error' and peer.posts==[]
