"""Public administrative recovery: actual worker/DB/journal and canonical peer.

Source delivery for the pipeline case is actual local Git CAS; peer/checker
metadata is synthetic. This does not replace real OneBase paired acceptance.
"""
from contextlib import redirect_stdout
import copy
from datetime import timedelta,timezone
from io import StringIO
import json

import pytest

from promptpilot import project_pipeline as pp,pipeline_insights as insights,worker
from promptpilot import review_routing_settlement as settlement
from promptpilot.models import TaskCreate
from promptpilot.review_routing_pipeline_policy import CONTRACT
from promptpilot.review_routing_request import decode as decode_review
from promptpilot.review_routing_stage_guard import StageGuard
from promptpilot.review_routing_publication import Publication
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_technical_worker import technical_worker as technical_worker  # noqa: F401
from test_review_routing_revocation import revocation_case as revocation_case  # noqa: F401


@pytest.fixture
def settlement_case(request,monkeypatch,author_bridge):
    kind=getattr(request,'param','pipeline')
    factories=[];pushes=[];deletes=[]
    if kind=='pipeline':
        peer,path,old_task,database,_calls,factories,pushes,_remote,_ref,mode=request.getfixturevalue('technical_worker')
        mode['fault']='late-done-actor'
        worker._execute_task_body(old_task)
        assert database.get_task(old_task.id).status.value=='failed' and len(pushes)==1
        peer.actor='owner'
        # Reach the original mutable checker fixture backing the real public
        # run_health adapter, not a new checker response invented by the test.
        _peer,health,_path,_old,_db,_config=request.getfixturevalue('pipeline_case')
    else:
        peer,database,_observer,_identity,deletes,_mode,_request,_root=request.getfixturevalue('revocation_case')
        _peer,health,path,old_task,_run,_db=request.getfixturevalue('author_bridge')
        pr=peer.current['data']['repository']['pullRequest'];pr['headRefOid']='c'*40
        peer.comments.append({'cursor':'AUTHOR_NEW_HEAD','node':{'__typename':'PullRequestCommit',
            'id':'AUTHOR_NEW_HEAD','commit':{'oid':'c'*40}}})
        health['fix_execution_contract']=copy.deepcopy(CONTRACT)
    state=ledger(peer,path);root=state.attempts[0]
    peer.now+=timedelta(minutes=31)
    monkeypatch.setattr(settlement,'_now',lambda:peer.now)
    new=peer.current['data']['repository']['pullRequest']['headRefOid']
    health['technical_fix_settlement_candidates']=[dict(stage='fix-pr',number=42,head=new,
        source=root.payload['source'],attempt_id=root.id)]
    health['technical_fix_candidates']=[];health['technical_fix_progress_candidates']=[]
    task=database.create_task(TaskCreate(prompt='Project - FIX',working_dir=old_task.working_dir))
    task=database.get_next_runnable();assert task is not None
    task.series_id=task.id
    monkeypatch.setenv('PP_TASK_ID',str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT',task.started_at.astimezone(timezone.utc).isoformat())
    queue={'id':'fix','execution':{'mode':'auto','command':['pipelinectl','--config',str(path),'next','fix'],
                                 'review_routing_protocol':'review-routing-v1'}}
    monkeypatch.setattr(insights,'_matching_queue',lambda _task:('p',{'repository':'owner/repo'},queue))
    monkeypatch.setattr(insights,'_tool_available',lambda *args:(True,''))
    monkeypatch.setattr(insights,'load_providers',lambda:{})
    monkeypatch.setattr(insights,'dispatch_gate',lambda _task:None)
    monkeypatch.setattr(worker,'effective_prompt',lambda _task:'Project - FIX')
    monkeypatch.setattr(worker,'load_providers',lambda:pytest.fail('settlement loaded a model provider'))
    monkeypatch.setattr(worker,'build_cmd',lambda *args,**kwargs:pytest.fail('settlement built model argv'))
    calls=[];fault={'value':None}
    def cli(command):
        stream=StringIO()
        with redirect_stdout(stream):code=pp.run(['--config',str(path),*command])
        return code,json.loads(stream.getvalue())
    def control(_execution,command,_directory,*,env_extra=None):
        calls.append(command)
        with monkeypatch.context() as environment:
            for name,value in (env_extra or {}).items():environment.setenv(name,value)
            code,value=cli(command[command.index('--config')+2:] if '--config' in command else command[1:])
        assert code==0 or value['action']=='error',value
        if command[-2:]==['next','fix'] and value.get('action')=='settlement':
            if fault['value']=='missing-protocol':value.pop('protocol',None)
            elif fault['value']=='wrong-head':value['target']['head']='d'*40
            elif fault['value']=='action-downgrade':value['action']='author-handoff'
        return value
    monkeypatch.setattr(insights,'_tool_preflight',control)
    return dict(peer=peer,health=health,path=path,old_task=old_task,task=task,database=database,root=root,
        new_head=new,factories=factories,pushes=pushes,deletes=deletes,calls=calls,cli=cli,fault=fault,kind=kind)


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('settlement_case',['pipeline','author'],indirect=True)
def test_public_worker_settles_old_executor_and_resumes_one_current_head_without_provider(settlement_case):
    case=settlement_case;peer=case['peer'];database=case['database'];task=case['task']
    before=list(peer.posts)
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    assert saved.status.value=='completed' and saved.verdict=='\u0413\u041e\u0422\u041e\u0412\u041e',saved.error or saved.result
    state=ledger(peer,case['path']);root=state.attempts[0]
    settled=state.attempt_settlement(root)
    assert settled is not None and state.resume_id is not None and state.budget_used==len(state.attempts)==1
    assert peer.posts[len(before):]==['lease','settlement','resume']
    assert len(case['factories'])==(1 if case['kind']=='pipeline' else 0)
    assert len(case['pushes'])==(1 if case['kind']=='pipeline' else 0)
    assert len(case['deletes'])==(1 if case['kind']=='author' else 0)
    assert 'fix-done' not in state.attempt_phases(root)
    assert next(entry for entry in state.resumes if entry.id==state.resume_id).payload['head']==case['new_head']


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('settlement_case',['pipeline'],indirect=True)
def test_public_resume_admits_exactly_one_new_review_and_preserves_spent_lifetime_budget(settlement_case,monkeypatch,tmp_path):
    case=settlement_case;peer=case['peer'];database=case['database'];task=case['task']
    worker._execute_task_body(task)
    saved=database.get_task(task.id);assert saved.verdict=='\u0413\u041e\u0422\u041e\u0412\u041e',saved.error or saved.result
    before=ledger(peer,case['path']);grant=before.resume_id
    database.release_pipeline_target_reservations(task.id)
    review=database.create_task(TaskCreate(prompt='Project - REVIEW',working_dir=task.working_dir))
    review=database.get_next_runnable();assert review is not None
    monkeypatch.setenv('PP_TASK_ID',str(review.id));monkeypatch.setenv('PP_TASK_STARTED_AT',review.started_at.astimezone(timezone.utc).isoformat())
    candidate=dict(stage='review',number=42,head=case['new_head'],review_depth=3)
    for field in ['review_candidates','content_review_candidates']:case['health'][field]=[candidate]
    case['health']['technical_fix_settlement_candidates']=[]
    # Advance the synthetic server and both real publication guards together.
    # Only time is injected; the public gate/reducer/transport are unchanged.
    for cls in [StageGuard,Publication]:
        original=cls.__init__
        def init(self,*args,_original=original,**kwargs):return _original(self,*args,**dict(kwargs,clock=lambda:peer.now))
        monkeypatch.setattr(cls,'__init__',init)
    code,selected=case['cli'](['next','review'])
    assert code==0 and selected['action']=='audit',selected
    request=decode_review(selected['lease'])
    assert request['resume_id']==grant and request['head']==case['new_head'] and request['budget_used']==1
    report=copy.deepcopy(before.proofs[-1].publication.payload['report']);report['head']=case['new_head']
    path=tmp_path/'new-head-report.json';path.write_text(json.dumps(report),encoding='utf-8')
    code,result=case['cli'](['complete','review','--lease',selected['lease'],'--report',str(path)])
    assert code==0 and result['action']=='completed',result
    after=ledger(peer,case['path'])
    assert after.budget_used==1 and len(after.attempts)==1 and after.parking_id is None
    assert after.proofs[-1].publication.payload['resume_id']==grant
    code,repeated=case['cli'](['next','review'])
    assert code==0 and repeated['action']=='wait',repeated


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('settlement_case',['author'],indirect=True)
@pytest.mark.parametrize('fault',['missing-protocol','wrong-head','action-downgrade'])
def test_public_worker_rejects_administrative_settlement_downgrade_with_zero_providers(settlement_case,fault):
    case=settlement_case;case['fault']['value']=fault
    before=list(case['peer'].posts)
    worker._execute_task_body(case['task'])
    saved=case['database'].get_task(case['task'].id)
    assert saved.status.value=='completed' and saved.verdict!='\u0413\u041e\u0422\u041e\u0412\u041e'
    assert case['peer'].posts==before and not case['deletes'] and not case['factories']


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('settlement_case',['author'],indirect=True)
@pytest.mark.parametrize('fault',['actor','reader','head'])
def test_actual_settlement_post_rechecks_source_after_writer_wait(settlement_case,monkeypatch,fault):
    from promptpilot.review_routing_journal import Journal
    from promptpilot.review_routing_wire import parse
    case=settlement_case;peer=case['peer'];original=Journal.arm
    def arm(journal,*args,**kwargs):
        result=original(journal,*args,**kwargs)
        if parse(kwargs['body'])['kind']=='settlement':
            if fault=='actor':peer.actor='changed-after-settlement-writer'
            elif fault=='reader':(case['path'].parent/'.claude/skills/fix-approved/SKILL.md').write_text('changed',encoding='utf-8')
            else:
                peer.current['data']['repository']['pullRequest']['headRefOid']='d'*40
                peer.comments.append({'cursor':'LATE_HEAD','node':{'__typename':'PullRequestCommit',
                    'id':'LATE_HEAD','commit':{'oid':'d'*40}}})
        return result
    monkeypatch.setattr(Journal,'arm',arm)
    before=list(peer.posts)
    worker._execute_task_body(case['task'])
    saved=case['database'].get_task(case['task'].id)
    assert saved.verdict!='\u0413\u041e\u0422\u041e\u0412\u041e' and not case['factories']
    state=ledger(peer,case['path'])
    assert state.budget_used==1 and state.attempt_settlement(state.attempts[0]) is None
    assert peer.posts[len(before):]==['lease'] and len(case['deletes'])==1
