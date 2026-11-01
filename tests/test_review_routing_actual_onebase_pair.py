"""Opt-in actual Go/checker/canonical-reader pair with a private GitHub peer.

This is reproducible paired A acceptance, not a production rollout or metrics.
Ordinary CI skips it unless an independently pinned OneBase binary and source
consumer tree are explicitly supplied by the paired-build operator.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from promptpilot import project_pipeline as pp,worker
from promptpilot.review_routing_policy import ROLES,consumer_tree
from test_review_routing_adapter import adapter as synthetic_adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case as pipeline_case  # noqa: F401
from test_review_routing_technical_worker import technical_worker as technical_worker  # noqa: F401
from test_review_routing_workspace import git


ACTUAL_HEALTH=pp.run_health
PEER='''
import json,os,sys
from pathlib import Path
path=Path(os.environ['ROUTING_ACTUAL_PAIR_STATE'])
state=json.loads(path.read_text(encoding='utf-8'));args=sys.argv[1:]
if args!=['api','graphql','--input','-']:raise SystemExit('private peer refuses non-GraphQL transport')
request=json.load(sys.stdin);query=request['query'];pr=state['pr']
def connection(nodes):
 return dict(totalCount=len(nodes),nodes=nodes,pageInfo=dict(hasNextPage=False,endCursor=None))
if 'PipelineHealthSnapshot' in query:
 comments=[dict(fullDatabaseId=e['node']['fullDatabaseId'],createdAt=e['node']['createdAt'],
  updatedAt=e['node']['lastEditedAt'] or e['node']['createdAt'],author=e['node']['author'],body=e['node']['body'])
  for e in pr['timelineItems']['edges'] if e['node']['__typename']=='IssueComment']
 pull=dict(id=pr['id'],number=pr['number'],title='private actual-pair fixture',body='',
  url='https://github.com/owner/repo/pull/42',createdAt='2026-10-29T00:00:00Z',
  updatedAt=pr['timelineItems']['updatedAt'],state=pr['state'],isDraft=pr['isDraft'],
  headRefOid=pr['headRefOid'],baseRefName=pr['baseRefName'],
  labels=connection(pr['labels']['nodes']),comments=connection(comments),
  commits=dict(nodes=[dict(commit=dict(id='COMMIT_'+pr['headRefOid'],oid=pr['headRefOid']))]))
 result=dict(data=dict(repository=dict(pullRequests=connection([pull]),issues=connection([]))))
 name='queue'
elif 'PipelineRoutingRepository' in query:
 result=dict(data=dict(repository=dict(id='R_BASE',nameWithOwner='owner/repo')));name='repository'
elif 'PipelineRoutingTimeline' in query:
 result=dict(data=dict(repository=dict(id='R_BASE',nameWithOwner='owner/repo',pullRequest=pr)));name='timeline'
else:raise SystemExit('private peer refuses unknown GraphQL query')
state['queries'].append(name)
temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(state),encoding='utf-8');os.replace(temporary,path)
print(json.dumps(result,ensure_ascii=False))
'''


@pytest.fixture
def adapter(synthetic_adapter,monkeypatch,tmp_path):  # noqa: F811
    binary_text=os.environ.get('PP_ONEBASE_PAIR_CHECKER')
    skills_text=os.environ.get('PP_ONEBASE_PAIR_SKILLS')
    digest=os.environ.get('PP_ONEBASE_PAIR_CHECKER_SHA256')
    if not binary_text and not skills_text and not digest:
        pytest.skip('explicit pinned actual OneBase paired build is unavailable')
    assert binary_text and skills_text and digest,'incomplete actual-pair qualification inputs'
    binary=Path(binary_text).resolve();source=Path(skills_text).resolve()
    assert binary.is_file() and hashlib.sha256(binary.read_bytes()).hexdigest()==digest
    initial={role:consumer_tree(source,role) for role in ROLES}
    peer,old_health,path,report,run=synthetic_adapter
    target=path.parent/'.claude/skills'
    # Preserve the synthetic setup in this exact private fixture. Never remove
    # or change an installed/source consumer tree to make compatibility pass.
    assert target.resolve().is_relative_to(tmp_path.resolve())
    preserved=tmp_path/'synthetic-readers-preserved';assert not preserved.exists()
    target.rename(preserved);target.mkdir()
    # The existing checker also checks UTF-8 contracts for TRIAGE and PLAN.
    # Copy their actual procedures too; do not disable that repository gate.
    for stage in [*ROLES.values(),'triage-issues','plan-approved']:
        assert (source/stage).is_dir() and not (source/stage).is_symlink()
        shutil.copytree(source/stage,target/stage)
    assert {role:consumer_tree(target,role) for role in ROLES}==initial
    state_path=tmp_path/'actual-pair-peer.json'
    script=tmp_path/'actual_pair_gh.py';script.write_text(PEER,encoding='utf-8')
    if os.name=='nt':
        compiler=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        assert compiler.is_file(),'actual Windows executable peer compiler unavailable'
        executable=tmp_path/'actual_pair_gh.exe'
        built=subprocess.run([str(compiler),'/nologo','/target:exe','/out:'+str(executable),
            str(Path(__file__).with_name('fake_gh_launcher.cs'))],capture_output=True,text=True,
            timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
        assert built.returncode==0,built.stdout+built.stderr
    else:
        executable=tmp_path/'actual_pair_gh'
        executable.write_text('#!'+sys.executable+'\n'+PEER,encoding='utf-8');executable.chmod(0o700)
    monkeypatch.setenv('GH_EXE',str(executable))
    monkeypatch.setenv('ROUTING_FAKE_PYTHON',sys.executable)
    monkeypatch.setenv('ROUTING_FAKE_SCRIPT',str(script))
    monkeypatch.setenv('ROUTING_ACTUAL_PAIR_STATE',str(state_path))
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL','file')
    raw=json.loads(path.read_text(encoding='utf-8'))
    raw['health_command']=[str(binary),'-json','-repo','owner/repo','-owner','owner',
        '-transport','graphql','-technical-handoff','-contract',str(target/'review-queue/SKILL.md')]
    path.write_text(json.dumps(raw),encoding='utf-8')
    evidence=[]
    def health(config,**kwargs):
        queries=json.loads(state_path.read_text(encoding='utf-8'))['queries'] if state_path.exists() else []
        current=copy.deepcopy(peer.current['data']['repository']['pullRequest'])
        current['timelineItems']['edges']+=copy.deepcopy(peer.comments)
        state_path.write_text(json.dumps(dict(pr=current,queries=queries)),encoding='utf-8')
        result=ACTUAL_HEALTH(config,**kwargs)
        evidence.append(result)
        return result
    monkeypatch.setattr(pp,'run_health',health)
    peer.actual_pair=dict(binary_sha256=digest,source_reader_sha256=initial,
                          state_path=state_path,evidence=evidence,fixture_root=tmp_path)
    return peer,old_health,path,report,run


@pytest.mark.parametrize('author_bridge',['real-source'],indirect=True)
@pytest.mark.parametrize('fault',[None,'lost-push-response','late-push-reader'])
def test_actual_go_and_all_four_canonical_readers_through_public_worker(technical_worker,fault):
    peer,path,task,database,calls,factories,pushes,remote,ref,mode=technical_worker
    mode['fault']=fault
    worker._execute_task_body(task)
    saved=database.get_task(task.id)
    history=ledger(peer,path);attempt=history.attempts[0]
    assert history.budget_used==len(history.attempts)==1 and len(factories)==1
    assert not peer.assignment_posts
    if fault=='late-push-reader':
        assert saved.status.value=='failed' and not pushes
        assert 'fix-done' not in history.attempt_phases(attempt)
    else:
        assert saved.status.value=='completed' and saved.verdict=='ГОТОВО',saved.error or saved.result
        assert len(pushes)==1
        done=history.attempt_phases(attempt)['fix-done']
        assert done.payload['details']['new_head']==git(task.working_dir,'--git-dir='+str(remote),'rev-parse',ref)
        health=pp.run_health(pp.load_config(str(path)),config_path=str(path))
        assert any(item['number']==42 and item['head']==done.payload['details']['new_head']
                   for item in health['content_review_candidates'])
        assert not health['technical_fix_candidates'] and not health['merge_executable']
    assert any('gate-fix-start' in command for command in calls)
    data=peer.actual_pair
    assert any(value.get('fix_execution_contract',{}).get('protocol')=='claimed-pipeline-fix-v1'
               for value in data['evidence'])
    queries=json.loads(data['state_path'].read_text(encoding='utf-8'))['queries']
    assert all(name in queries for name in ['queue','repository','timeline'])
    receipt=dict(binary_sha256=data['binary_sha256'],reader_sha256=data['source_reader_sha256'],
                 fault=fault,factories=len(factories),cas_sends=len(pushes),budget_used=history.budget_used,
                 task_status=saved.status.value,verdict=saved.verdict,query_count=len(queries),
                 github_transport='private synthetic peer',production_activation=False)
    (data['fixture_root']/'actual-pair-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
