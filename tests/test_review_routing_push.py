"""Real Git CAS against a local bare remote; not live GitHub pair acceptance."""
from contextlib import closing
import sqlite3
import subprocess
import sys

import pytest

from promptpilot import review_routing_start_request as starts
from promptpilot.pipeline_errors import PipelineError
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_execution import OneUseStart
from promptpilot.review_routing_post_push import trailers
from promptpilot.review_routing_push import Push,binding,prepare_expected,TABLE
from promptpilot.review_routing_workspace import Workspace
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case,selected,issue  # noqa: F401
from test_review_routing_start import metadata_launch
from test_review_routing_workspace import git


@pytest.fixture
def push_case(pipeline_case,tmp_path,monkeypatch):
    peer,_health,path,task,database,_config=pipeline_case
    issued=issue(pipeline_case,selected(pipeline_case));request=starts.decode(issued['lease'])
    scope=request['scope'];workspace=Workspace(database.DB_PATH,scope,task.working_dir)
    checkout=workspace.prepare();remote=tmp_path/'remote.git'
    git(tmp_path,'init','-q','--bare',str(remote))
    ref='refs/heads/'+request['identity']['head_ref']
    git(checkout,'push',str(remote),scope['source']['head']+':'+ref)
    store=OneUseStart(database.DB_PATH,create=False,allow_new=False)
    tree=metadata_launch(peer,str(path),issued['lease'],factory=lambda:OwnedProcess.start(
        [sys.executable,'-c','import time; time.sleep(60)'],stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
    try:
        store.observe_started(scope,tree.process.pid)
        with pytest.raises(PipelineError):store.verify_stopped(scope)
        (checkout/'app.py').write_text('corrected source',encoding='utf-8')
        git(checkout,'add','app.py')
        message='Correct the reviewed source\n\n'+'\n'.join(k+': '+v for k,v in trailers(request['attempt'],scope['attempt_id']).items())
        git(checkout,'commit','-qm',message)
        store.confirm_stopped(scope,tree)
        assert store.verify_stopped(scope)
        output=workspace.output(request['attempt']);value=binding(scope,request['identity'],output)
        original=workspace._git;pushes=[]
        def local_only(root,*args,**kwargs):
            if args and args[0]=='push':
                assert args[-2]=='https://github.com/owner/repo.git'
                assert args[-1]==output['new_head']+':'+ref
                assert '--force-with-lease='+ref+':'+scope['source']['head'] in args
                pushes.append(args)
                args=(*args[:-2],str(remote),args[-1])
            return original(root,*args,**kwargs)
        monkeypatch.setattr(workspace,'_git',local_only)
        publish=Push(database.DB_PATH,value)
        def readback():return git(tmp_path,'--git-dir='+str(remote),'rev-parse',ref)==output['new_head']
        def guard():
            store.verify_stopped(scope)
            assert workspace.output(request['attempt'])==output
            return True
        yield publish,prepare_expected(workspace,value),readback,guard,workspace,request,database,pushes,remote,ref
    finally:
        if store.status(scope)['state']!='stopped':store.confirm_stopped(scope,tree)
        tree.close();tree.process.wait(timeout=10)


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('lost_response',[False,True])
def test_actual_forward_cas_confirms_readback_and_never_pushes_twice(push_case,lost_response):
    publish,push,readback,guard,_workspace,_request,_database,pushes,_remote,_ref=push_case
    def send():
        push()
        if lost_response:raise OSError('response lost after real Git CAS')
    result=publish.publish(final_guard=guard,push=send,readback=readback)
    assert result==publish.require_confirmed() and len(pushes)==1
    again=publish.publish(final_guard=lambda:pytest.fail('repeated gate'),
                          push=lambda:pytest.fail('repeated push'),readback=readback)
    assert again==result and len(pushes)==1


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
def test_actual_remote_change_refuses_expected_head_and_preserves_unknown_receipt(push_case):
    publish,push,readback,guard,workspace,request,_database,pushes,remote,ref=push_case
    old=request['scope']['source']['head']
    other=git(workspace.path,'commit-tree',git(workspace.path,'rev-parse',old+'^{tree}'),'-p',old,'-m','concurrent writer')
    git(workspace.path,'push',str(remote),other+':'+ref)
    with pytest.raises(PipelineError,match='unknown'):
        publish.publish(final_guard=guard,push=push,readback=readback)
    assert len(pushes)==1 and publish.status()=='sending'
    assert git(workspace.path,'--git-dir='+str(remote),'rev-parse',ref)==other
    with pytest.raises(PipelineError):
        publish.publish(final_guard=lambda:pytest.fail('repeated final guard'),
                        push=lambda:pytest.fail('repeated failed CAS'),readback=readback)
    assert len(pushes)==1 and publish.status()=='sending'


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
def test_lost_publication_row_cannot_reconstruct_a_push_from_stopped_receipt(push_case):
    publish,push,readback,guard,_workspace,_request,database,pushes,_remote,_ref=push_case
    publish.prepare()
    with closing(sqlite3.connect(database.DB_PATH)) as connection:
        with connection:connection.execute('DELETE FROM '+TABLE)
    with pytest.raises(PipelineError,match='disappeared'):
        publish.publish(final_guard=guard,push=push,readback=readback)
    assert not pushes


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
def test_cancel_after_journal_writer_wait_refuses_before_real_push(push_case,monkeypatch):
    publish,push,readback,guard,_workspace,request,database,pushes,_remote,_ref=push_case
    original=publish._transition
    def waited(source,target,**kwargs):
        result=original(source,target,**kwargs)
        if target=='sending':database.request_cancel(request['scope']['task_id'])
        return result
    monkeypatch.setattr(publish,'_transition',waited)
    with pytest.raises(PipelineError):publish.publish(final_guard=guard,push=push,readback=readback)
    assert not pushes and publish.status()=='not-sent'
