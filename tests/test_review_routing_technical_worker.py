"""Public worker, real owned subprocess and real local Git CAS.

GitHub/checker are authenticated synthetic peers; live OneBase acceptance is
separate. No installed source, credentials or public repository is changed.
"""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import sys

import pytest

from promptpilot import project_pipeline as pp,pipeline_insights as insights,worker
from promptpilot.review_routing_execution import OneUseStart
from promptpilot.review_routing_heartbeat import NONCE_ENV
from promptpilot.review_routing_post_push import trailers
from promptpilot.review_routing_push import Push
from promptpilot.review_routing_workspace import Workspace
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_workspace import git


@pytest.fixture
def technical_worker(pipeline_case,monkeypatch,tmp_path):
    peer,health,path,task,database,_config=pipeline_case
    queue={'id':'fix','execution':{'mode':'auto','command':['pipelinectl','--config',str(path),'next','fix'],
                                 'review_routing_protocol':'review-routing-v1'}}
    monkeypatch.setattr(insights,'_matching_queue',lambda _task:('p',{'repository':'owner/repo'},queue))
    monkeypatch.setattr(insights,'_tool_available',lambda *args:(True,''))
    monkeypatch.setattr(insights,'load_providers',lambda:{})
    monkeypatch.setattr(insights,'dispatch_gate',lambda _task:None)
    monkeypatch.setattr(worker,'effective_prompt',lambda _task:'Project - FIX')
    monkeypatch.setattr(worker,'load_providers',lambda:{})
    monkeypatch.setattr(worker,'get_provider_env',lambda *args,**kwargs:os.environ.copy())
    monkeypatch.setattr(worker,'_start_owned_with_spawn_retry',lambda *args,**kwargs:pytest.fail('technical FIX used generic spawn retry'))
    monkeypatch.setattr(worker,'_repair_verdict',lambda *args,**kwargs:pytest.fail('technical FIX invoked a repair model'))
    task.series_id=task.id
    calls=[];factories=[];pushes=[];mode={'fault':None};source=task.working_dir
    remote=tmp_path/'actual-remote.git'
    git(tmp_path,'init','-q','--bare',str(remote))
    pr=peer.current['data']['repository']['pullRequest'];old=pr['headRefOid'];ref='refs/heads/'+pr['headRefName']
    git(source,'push',str(remote),old+':'+ref)
    original_api=peer.json
    def api(*args,input_value=None):
        new=pr['headRefOid']
        if new!=old and args==('api','repos/owner/repo/compare/'+old+'...'+new+'?per_page=100'):
            return dict(status='ahead',ahead_by=1,behind_by=0,total_commits=1,
                base_commit={'sha':old},merge_base_commit={'sha':old},commits=[{'sha':new}])
        if new!=old and args==('api','repos/owner/repo/commits/'+new):
            return dict(sha=new,commit={'message':git(source,'show','-s','--format=%B',new)})
        return original_api(*args,input_value=input_value)
    peer.json=api
    original_git=Workspace._git
    original_transition=Push._transition
    def transition(push,source,target,**kwargs):
        result=original_transition(push,source,target,**kwargs)
        if target=='sending':
            if mode['fault']=='late-push-actor':peer.actor='changed-after-journal-wait'
            elif mode['fault']=='late-push-reader':
                (path.parent/'.claude/skills/fix-approved/SKILL.md').write_text('changed',encoding='utf-8')
        if target=='confirmed' and mode['fault']=='late-done-actor':peer.actor='changed-after-cas-confirmation'
        return result
    monkeypatch.setattr(Push,'_transition',transition)
    def local_git(workspace,root,*args,**kwargs):
        if args and args[0]=='push':
            assert args[-2]=='https://github.com/owner/repo.git'
            assert '--force-with-lease='+ref+':'+old in args
            pushes.append(args)
            new=args[-1].split(':')[0]
            result=original_git(workspace,root,*args[:-2],str(remote),args[-1],**kwargs)
            pr['headRefOid']=new
            peer.comments.append({'cursor':'ACTUAL_CAS_HEAD','node':{'__typename':'PullRequestCommit',
                'id':'ACTUAL_CAS_HEAD','commit':{'oid':new}}})
            state=ledger(peer,path);attempt=state.attempts[0]
            health['technical_fix_progress_candidates']=[dict(stage='fix-pr',number=42,head=old,
                source=attempt.payload['source'],attempt_id=attempt.id,new_head=new)]
            if mode['fault']=='lost-push-response':raise OSError('lost response after actual CAS')
            return result
        return original_git(workspace,root,*args,**kwargs)
    monkeypatch.setattr(Workspace,'_git',local_git)
    def cli(execution,command,directory,*,env_extra=None):
        calls.append(command)
        with monkeypatch.context() as environment:
            for name,value in (env_extra or {}).items():environment.setenv(name,value)
            stream=StringIO()
            with redirect_stdout(stream):code=pp.run(command[1:])
            value=json.loads(stream.getvalue())
            if command[-2:]==['next','fix']:
                if mode['fault']=='missing-protocol':value.pop('protocol',None)
                elif mode['fault']=='wrong-head':value['target']['head']='e'*40
                elif mode['fault']=='action-downgrade':value['action']='author-handoff'
            assert code==0 or value['action']=='error',value
            return value
    monkeypatch.setattr(insights,'_tool_preflight',cli)
    def build(_provider,prompt,**kwargs):
        state=ledger(peer,path);attempt=state.attempts[0]
        assert NONCE_ENV not in prompt and 'pipeline-start' not in prompt
        message='Actual worker correction\n\n'+'\n'.join(k+': '+v for k,v in trailers(attempt.payload,attempt.id).items())
        if mode['fault']=='rate-limit':code="import sys; print('rate limit',file=sys.stderr); sys.exit(1)"
        else:
            code=("import pathlib,subprocess; pathlib.Path('app.py').write_text('corrected by owned worker',encoding='utf-8'); "
                "subprocess.run(['git','-c','maintenance.auto=false','-c','gc.auto=0','add','--','app.py'],check=True); "
                "subprocess.run(['git','-c','maintenance.auto=false','-c','gc.auto=0','commit','-qm',"+repr(message)+"],check=True); "
                "print("+repr('\u0418\u0422\u041e\u0413: \u0413\u041e\u0422\u041e\u0412\u041e')+")")
        return [sys.executable,'-X','utf8','-c',code]
    monkeypatch.setattr(worker,'build_cmd',build)
    original_start=worker.OwnedProcess.start
    def factory(args,**kwargs):
        if args and args[0]==sys.executable:
            factories.append(args)
            assert NONCE_ENV not in kwargs['env'] and 'PP_PIPELINE_LEASE_KEY_FILE' not in kwargs['env']
            if mode['fault']=='ambiguous-spawn':raise OSError('ambiguous actual factory boundary')
        return original_start(args,**kwargs)
    monkeypatch.setattr(worker.OwnedProcess,'start',factory)
    return peer,path,task,database,calls,factories,pushes,remote,ref,mode


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('fault',[None,'lost-push-response'])
def test_public_worker_one_owned_launch_one_cas_and_one_machine_fix_done(technical_worker,fault):
    peer,path,task,database,calls,factories,pushes,remote,ref,mode=technical_worker
    mode['fault']=fault
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    assert saved.status.value=='completed' and saved.verdict=='\u0413\u041e\u0422\u041e\u0412\u041e',saved.error or saved.result
    assert len(factories)==len(pushes)==1
    state=ledger(peer,path);root=state.attempts[0]
    assert state.budget_used==len(state.attempts)==1 and not peer.assignment_posts
    done=state.attempt_phases(root)['fix-done']
    assert done.payload['details']['new_head']==git(task.working_dir,'--git-dir='+str(remote),'rev-parse',ref)
    assert any('gate-fix-start' in command for command in calls)


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('fault',['missing-protocol','wrong-head','action-downgrade','rate-limit','ambiguous-spawn'])
def test_public_worker_never_falls_back_or_relaunches_a_consumed_technical_start(technical_worker,fault):
    peer,path,task,database,_calls,factories,pushes,_remote,_ref,mode=technical_worker
    mode['fault']=fault
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    assert saved.status.value in {'failed','completed'}
    assert saved.verdict!='\u0413\u041e\u0422\u041e\u0412\u041e' and not pushes
    state=ledger(peer,path)
    if fault in {'rate-limit','ambiguous-spawn'}:
        assert len(factories)==1 and state.budget_used==1
        scope=json.loads(next(iter(_rows(database.DB_PATH)))['scope_json'])
        status=OneUseStart(database.DB_PATH,create=False,allow_new=False).status(scope)
        assert status['state']==('stopped' if fault=='rate-limit' else 'unknown')
        with pytest.raises(pp.PipelineError):
            OneUseStart(database.DB_PATH,create=False,allow_new=False).invoke(scope,
                final_guard=lambda _scope:True,factory=lambda:pytest.fail('second provider'))
        assert 'fix-done' not in state.attempt_phases(state.attempts[0])
    else:assert not factories and state.budget_used==0


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('fault',['late-push-actor','late-push-reader','late-done-actor'])
def test_public_worker_rechecks_authority_after_cas_journal_wait_and_before_fix_done(technical_worker,fault):
    peer,path,task,database,_calls,factories,pushes,_remote,_ref,mode=technical_worker
    mode['fault']=fault
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    assert saved.status.value=='failed' and len(factories)==1
    assert len(pushes)==(1 if fault=='late-done-actor' else 0)
    # Reader drift intentionally makes source-health unusable, not the
    # immutable ledger itself; inspect using the unchanged canonical parser.
    state=ledger(peer,path)
    assert state.budget_used==1 and 'fix-done' not in state.attempt_phases(state.attempts[0])


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
def test_stopped_publisher_can_finish_same_owner_lease_without_renewing_execution_or_pushing_again(technical_worker,monkeypatch):
    from datetime import timedelta
    from promptpilot.review_routing_publish import PublicationGuard
    from promptpilot.review_routing_worker import Execution
    peer,path,task,database,_calls,factories,pushes,_remote,_ref,_mode=technical_worker
    cleanup=Execution.cleanup;advanced={'value':False}
    def stop(execution):
        value=cleanup(execution)
        if execution.stopped and not advanced['value']:
            peer.now+=timedelta(minutes=26);advanced['value']=True
        return value
    monkeypatch.setattr(Execution,'cleanup',stop)
    init=PublicationGuard.__init__
    def publisher(self,*args,**kwargs):return init(self,*args,**dict(kwargs,clock=lambda:peer.now))
    monkeypatch.setattr(PublicationGuard,'__init__',publisher)
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    assert saved.status.value=='completed' and saved.verdict=='\u0413\u041e\u0422\u041e\u0412\u041e',saved.error or saved.result
    assert len(factories)==len(pushes)==1 and peer.posts.count('lease')==1
    state=ledger(peer,path);root=state.attempts[0]
    assert state.budget_used==1 and state.attempt_phases(root)['fix-done'].payload['lease_id']!=root.id


def _rows(path):
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory=sqlite3.Row
        return connection.execute('SELECT * FROM technical_fix_starts_v1').fetchall()
