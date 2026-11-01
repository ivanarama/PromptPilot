"""Actual private Git worktrees; no network, provider or installed-source writes."""
import copy
import shlex

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.process_tree import run_owned
from promptpilot.review_routing_workspace import Workspace
from test_review_routing_execution import start_scope as start_scope  # noqa: F401


def git(root,*args):
    result=run_owned(['git','-c','maintenance.auto=false','-c','gc.auto=0','-C',str(root),*args],
        capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stderr
    return result.stdout.strip()


@pytest.fixture
def workspace_case(request,tmp_path):
    database,scope=request.getfixturevalue('start_scope')
    repo=tmp_path/'base-checkout';repo.mkdir()
    git(repo,'init','-q');git(repo,'config','user.name','Workspace fixture')
    git(repo,'config','user.email','workspace@example.invalid')
    git(repo,'remote','add','origin','https://github.com/owner/repo.git')
    (repo/'app.py').write_text('reviewed source',encoding='utf-8')
    git(repo,'add','app.py');git(repo,'commit','-qm','reviewed source')
    head=git(repo,'rev-parse','HEAD')
    scope['source']['head']=head;scope['reservation']['head']=head
    (repo/'app.py').write_text('operator uncommitted changes',encoding='utf-8')
    (repo/'operator-notes.txt').write_text('retain me',encoding='utf-8')
    workspace=Workspace(database.DB_PATH,scope,repo)
    workspace._fixture_database=database.DB_PATH
    return workspace,repo,scope


def test_private_checkout_is_clean_source_bound_idempotent_and_preserves_base(workspace_case):
    workspace,repo,scope=workspace_case
    index=(repo/'.git/index').read_bytes()
    path=workspace.prepare()
    receipt=workspace.receipt.read_bytes()
    assert path!=repo and (path/'app.py').read_text(encoding='utf-8')=='reviewed source'
    assert workspace.prepare()==path and workspace.receipt.read_bytes()==receipt
    assert git(path,'rev-parse','HEAD')==scope['source']['head']
    assert (repo/'.git/index').read_bytes()==index
    assert (repo/'app.py').read_text(encoding='utf-8')=='operator uncommitted changes'
    assert (repo/'operator-notes.txt').read_text(encoding='utf-8')=='retain me'


@pytest.mark.parametrize('fault',['bytes','head','scope','remote'])
def test_workspace_drift_is_preserved_and_refused_before_factory(workspace_case,fault):
    workspace,repo,scope=workspace_case
    path=workspace.prepare()
    if fault=='bytes':(path/'app.py').write_text('unexpected edits',encoding='utf-8')
    elif fault=='head':
        (path/'app.py').write_text('other commit',encoding='utf-8')
        git(path,'add','app.py');git(path,'commit','-qm','other commit')
    elif fault=='scope':
        changed=copy.deepcopy(scope);changed['config_sha256']='e'*64
        workspace=Workspace(workspace._fixture_database,changed,repo)
    else:git(repo,'remote','set-url','origin','https://github.com/foreign/repo.git')
    with pytest.raises(PipelineError):workspace.verify()
    assert path.exists() and (repo/'operator-notes.txt').read_text(encoding='utf-8')=='retain me'
    if fault=='bytes':assert (path/'app.py').read_text(encoding='utf-8')=='unexpected edits'


def test_existing_checkout_without_receipt_is_an_engineering_artifact(workspace_case):
    workspace,_repo,_scope=workspace_case
    workspace._directories(create=True)
    workspace.path.mkdir();artifact=workspace.path/'retain.txt';artifact.write_text('do not reset')
    with pytest.raises(PipelineError):workspace.prepare()
    assert artifact.read_text()=='do not reset' and not workspace.receipt.exists()


def test_base_checkout_hook_never_runs_during_private_creation(workspace_case,tmp_path):
    workspace,repo,_scope=workspace_case
    flag=tmp_path/'hook-executed'
    hook=repo/'.git/hooks/post-checkout'
    hook.write_text('#!/bin/sh\necho unsafe > '+shlex.quote(flag.as_posix())+'\n',encoding='utf-8')
    hook.chmod(0o700)
    workspace.prepare()
    assert not flag.exists()
