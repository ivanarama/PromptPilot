"""Actual authenticated peer/journal/task progress bridge, not real paired acceptance."""
import copy
from datetime import timedelta

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_progress as progress
from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_post_push import trailers
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge, ledger  # noqa: F401


@pytest.fixture
def progress_case(author_bridge, monkeypatch):
    peer,health,path,task,run,database=author_bridge
    code,selected=run('next','fix');assert code==0,selected
    code,handed=run('complete','fix','--lease',selected['lease']);assert code==0,handed
    root=ledger(peer,path).attempts[0]
    peer.now+=timedelta(minutes=31)
    monkeypatch.setattr(progress,'_now',lambda:peer.now)
    pr=peer.current['data']['repository']['pullRequest']
    pr['headRefOid']='c'*40
    peer.comments.append({'cursor':'C_NEW_HEAD','node':{'__typename':'PullRequestCommit',
                         'id':'COMMIT_NEW_HEAD','commit':{'oid':'c'*40}}})
    health['fix_bridge_contract']=copy.deepcopy(progress.BRIDGE)
    health['technical_fix_candidates']=[]
    health['technical_fix_progress_candidates']=[dict(stage='fix-pr',number=42,head='a'*40,
        source=root.payload['source'],attempt_id=root.id,new_head='c'*40)]
    source=trailers(root.payload,root.id)
    message='Source-bound author correction\n\n'+'\n'.join(k+': '+v for k,v in source.items())
    original=peer.json
    head_repository=pr['headRepository']['nameWithOwner']
    graph=dict(status='ahead',ahead_by=1,behind_by=0,total_commits=1,
               base_commit={'sha':'a'*40},merge_base_commit={'sha':'a'*40},commits=[{'sha':'c'*40}])
    def api(*args,input_value=None):
        if args==('api','repos/'+head_repository+'/compare/'+'a'*40+'...'+'c'*40+'?per_page=100'):
            return copy.deepcopy(graph)
        if args==('api','repos/'+head_repository+'/commits/'+'c'*40):
            return dict(sha='c'*40,commit={'message':message})
        return original(*args,input_value=input_value)
    peer.json=api
    config=pp.load_config(str(path))
    return peer,health,path,task,run,database,root,config,graph


@pytest.mark.parametrize('author_bridge', [None, 'fork'], indirect=True)
def test_public_source_bound_progress_publishes_once_without_another_assignment_or_provider(progress_case):
    peer,health,path,_task,run,_database,root,config,_graph=progress_case
    code,selected=run('next','fix')
    assert code==0,selected
    assert selected['action']=='author-progress'
    before=len(peer.posts)
    for _ in range(2):
        code,result=run('complete','fix','--lease',selected['lease'])
        assert code==0,result
        assert result['action']=='fixed' and result['provider_started'] is False
        assert result['new_head']=='c'*40
    assert peer.posts[before:]==['lease','phase']
    assert len(peer.assignment_posts)==1
    state=ledger(peer,path)
    assert state.budget_used==1 and len(state.attempts)==1
    assert state.attempt_phases(root)['fix-done'].payload['details']['new_head']=='c'*40


def test_late_hold_blocks_progress_after_election_without_new_receipts(progress_case):
    peer,health,path,_task,_run,_database,_root,config,_graph=progress_case
    selected=progress.select(peer,config,config_path=str(path),health=health)
    before=list(peer.posts)
    peer.current['data']['repository']['pullRequest']['labels']['nodes']=[{'name':'hold'}]
    with pytest.raises(PipelineError):
        progress.complete(peer,config,selected['lease'],config_path=str(path))
    assert peer.posts==before and len(peer.assignment_posts)==1


@pytest.mark.parametrize('fault', [None, 'action-downgrade', 'wrong-head', 'missing-protocol'])
def test_actual_worker_checks_progress_envelope_without_loading_a_provider(progress_case,monkeypatch,fault):
    from contextlib import redirect_stdout
    from io import StringIO
    import json
    from promptpilot import pipeline_insights as insights, worker
    peer,_health,path,task,_run,database,root,_config,_graph=progress_case
    queue={'id':'fix','execution':{'mode':'auto','command':['pipelinectl','next','fix'],
                                  'review_routing_protocol':'review-routing-v1'}}
    monkeypatch.setattr(insights,'_matching_queue',lambda _task:('p',{'repository':'owner/repo'},queue))
    monkeypatch.setattr(insights,'_tool_available',lambda *args:(True,''))
    monkeypatch.setattr(insights,'load_providers',lambda:{})
    monkeypatch.setattr(worker,'load_providers',lambda:pytest.fail('progress loaded a provider'))
    monkeypatch.setattr(worker,'build_cmd',lambda *args,**kw:pytest.fail('progress built provider argv'))
    monkeypatch.setattr(worker.OwnedProcess,'start',lambda *args,**kw:pytest.fail('progress launched a provider'))
    calls=[]
    def cli(_execution,command,_directory,*,env_extra=None):
        calls.append(command)
        with monkeypatch.context() as environment:
            for name,value in (env_extra or {}).items(): environment.setenv(name,value)
            stream=StringIO()
            with redirect_stdout(stream): code=pp.run(['--config',str(path),*command[1:]])
            result=json.loads(stream.getvalue())
            assert code==0,result
            if command[-2:]==['next','fix']:
                if fault=='action-downgrade':result['action']='author-handoff'
                elif fault=='wrong-head':result['target']['new_head']='d'*40
                elif fault=='missing-protocol':result.pop('protocol')
            return result
    monkeypatch.setattr(insights,'_tool_preflight',cli)
    monkeypatch.setattr(insights,'dispatch_gate',lambda _task:None)
    monkeypatch.setattr(worker,'effective_prompt',lambda _task:'Project - FIX')
    task.series_id=task.id
    before=list(peer.posts)
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    assert saved.status.value=='completed'
    if fault is None:
        assert saved.verdict=='\u0413\u041e\u0422\u041e\u0412\u041e'
        assert len(calls)==2 and peer.posts[len(before):]==['lease','phase']
        assert ledger(peer,path).attempt_phases(root)['fix-done']
    else:
        assert saved.verdict=='\u041d\u0423\u0416\u0415\u041d \u0427\u0415\u041b\u041e\u0412\u0415\u041a'
        assert len(calls)==1 and peer.posts==before
    assert len(peer.assignment_posts)==1


@pytest.mark.parametrize('fault',['actor','reader','head','bridge','assignee','force-push'])
def test_public_progress_rechecks_late_source_and_authority_before_any_write(progress_case,fault):
    peer,health,path,_task,run,_database,_root,_config,_graph=progress_case
    code,selected=run('next','fix')
    assert code==0,selected
    before=list(peer.posts)
    pr=peer.current['data']['repository']['pullRequest']
    if fault=='actor':peer.actor='wrong'
    elif fault=='reader':(path.parent/'.claude/skills/fix-approved/SKILL.md').write_text('changed',encoding='utf-8')
    elif fault=='head':pr['headRefOid']='d'*40
    elif fault=='bridge':health.pop('fix_bridge_contract')
    elif fault=='assignee':peer.assignees=[]
    else:peer.comments.append({'cursor':'C_FORCE_PUSH','node':{'__typename':'HeadRefForcePushedEvent','id':'FORCE_PUSH_NEW'}})
    code,result=run('complete','fix','--lease',selected['lease'])
    assert code==2,result
    assert peer.posts==before and len(peer.assignment_posts)==1



@pytest.mark.parametrize('author_bridge',[None,'fork'],indirect=True)
def test_configured_pipeline_executor_never_retargets_an_existing_author_progress(progress_case):
    import json
    from promptpilot.review_routing_pipeline_policy import CONTRACT
    peer,health,path,_task,run,_database,root,_config,_graph=progress_case
    raw=json.loads(path.read_text(encoding='utf-8'))
    raw['technical_fix_execution']=dict(enabled=True,protocol='review-routing-v1',executor=dict(kind='pipeline',login='owner'))
    path.write_text(json.dumps(raw),encoding='utf-8')
    health['fix_execution_contract']=copy.deepcopy(CONTRACT)
    code,selected=run('next','fix')
    assert code==0 and selected['action']=='author-progress',selected
    code,result=run('complete','fix','--lease',selected['lease'])
    assert code==0 and result['executor']==root.payload['executor'] and result['provider_started'] is False,result
    assert ledger(peer,path).budget_used==1 and len(peer.assignment_posts)==1
