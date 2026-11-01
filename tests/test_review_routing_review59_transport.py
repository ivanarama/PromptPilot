"""Independent local-only transport substitution probe."""
import pytest
from promptpilot import worker
from promptpilot.review_routing_workspace import Workspace
from promptpilot.review_routing_push import Push
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_technical_worker import technical_worker as technical_worker  # noqa: F401
from test_review_routing_workspace import git

ORIGINAL_GIT=Workspace._git

@pytest.mark.parametrize('setting',['url','pushurl'])
def test_git_literal_url_can_or_cannot_resolve_configured_remote(tmp_path,monkeypatch,setting):
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL','file')
    repo=tmp_path/'repo';repo.mkdir()
    bare=tmp_path/'remote.git'
    git(repo,'init','-q');git(repo,'config','user.name','Private fixture')
    git(repo,'config','user.email','private@example.invalid')
    git(repo,'init','-q','--bare',str(bare))
    (repo/'test.txt').write_text('private test',encoding='utf-8')
    git(repo,'add','test.txt');git(repo,'commit','-qm','private')
    url='https://github.com/owner/repo.git'
    git(repo,'config','remote.'+url+'.url',url)
    git(repo,'config','remote.'+url+'.'+setting,str(bare))
    # Only file transport is enabled by the private runner. No network possible.
    git(repo,'push','--porcelain',url,'HEAD:refs/heads/test')
    assert git(repo,'--git-dir='+str(bare),'rev-parse','refs/heads/test')==git(repo,'rev-parse','HEAD')

@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('setting',['url','pushurl'])
def test_public_cas_refuses_named_remote_substitution_after_writer_wait(technical_worker,monkeypatch,tmp_path,setting):
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL','file')
    peer,path,task,database,calls,factories,pushes,remote,ref,mode=technical_worker
    old=peer.current['data']['repository']['pullRequest']['headRefOid']
    diverted=tmp_path/'diverted.git'
    git(tmp_path,'init','-q','--bare',str(diverted))
    git(task.working_dir,'push',str(diverted),old+':'+ref)
    original_transition=Push._transition
    fixture_git=Workspace._git
    sends=[]
    def transition(push,source,target,**kw):
        value=original_transition(push,source,target,**kw)
        if target=='sending':
            key='remote.https://github.com/owner/repo.git.'
            git(task.working_dir,'config',key+'url','https://github.com/owner/repo.git')
            git(task.working_dir,'config',key+setting,str(diverted))
        return value
    def transport(workspace,root,*args,**kw):
        if args and args[0]=='push':
            sends.append(args)
            # Actual public argv and actual Git, with networking disabled.
            return ORIGINAL_GIT(workspace,root,*args,**kw)
        return fixture_git(workspace,root,*args,**kw)
    monkeypatch.setattr(Push,'_transition',transition)
    monkeypatch.setattr(Workspace,'_git',transport)
    worker.execute_task(task)
    actual=git(tmp_path,'--git-dir='+str(diverted),'rev-parse',ref)
    assert actual==old, 'public expected-repository CAS advanced a different bare remote; sends='+str(len(sends))
