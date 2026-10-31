"""Real subprocess CLI against a private executable GitHub/checker peer.

These are compatible fake-peer REVIEW tests, not actual Go or full FIX-chain
acceptance. No provider, credentials or real GitHub endpoint is used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_review_routing_history import history
from test_review_routing_policy import peers
from test_review_routing_snapshot import page
from test_review_routing_wire import publication


PEER='''
from datetime import datetime,timezone
import copy,json,os,sys
from pathlib import Path
path=Path(os.environ['ROUTING_FAKE_PEER']);state=json.loads(path.read_text())
args=sys.argv[1:];state['calls'].append(args[:2]);failure=False
if args==['api','user']:result={'login':'owner'}
elif args==['api','repos/owner/repo']:result={'full_name':'owner/repo','node_id':'R_BASE'}
elif args[:2]==['api','graphql']:
 result=copy.deepcopy(state['page']);result['data']['repository']['pullRequest']['timelineItems']['edges']+=state['comments']
elif args==['api','repos/owner/repo/pulls/42']:
 pr=state['page']['data']['repository']['pullRequest']
 result={'node_id':pr['id'],'number':42,
   'base':{'ref':pr['baseRefName'],'repo':{'node_id':pr['baseRepository']['id'],'full_name':pr['baseRepository']['nameWithOwner']}},
   'head':{'ref':pr['headRefName'],'sha':pr['headRefOid'],'repo':{'node_id':pr['headRepository']['id'],'full_name':pr['headRepository']['nameWithOwner']}},
   'assignees':state.get('assignees',[])}
elif args==['api','repos/owner/repo/issues/42/assignees','--method','POST','--input','-']:
 assignment=json.load(sys.stdin);state.setdefault('assignments',[]).append(assignment)
 author=state['page']['data']['repository']['pullRequest']['author']
 state['assignees']=[{'node_id':author['id'],'login':author['login']}];result={'assignees':state['assignees']}
elif args==['api','repos/owner/repo/issues/42/comments','--method','POST','--input','-']:
 body=json.load(sys.stdin)['body'];ident=str(1000+len(state['comments']))
 state['comments'].append({'cursor':'C'+ident,'node':{'__typename':'IssueComment','id':'IC_'+ident,'fullDatabaseId':ident,
   'createdAt':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'lastEditedAt':None,'author':{'login':'owner'},'body':body}})
 state['posts'].append(body);result={'id':ident}
 if state.get('move_after_root') and len(state['posts'])==1:
  state['page']['data']['repository']['pullRequest']['headRefOid']='c'*40
  state['comments'].append({'cursor':'NEW_HEAD','node':{'__typename':'PullRequestCommit','id':'NEW_HEAD','commit':{'oid':'c'*40}}})
  failure=True
else:raise SystemExit('unexpected fake API request')
temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(state));os.replace(temporary,path)
if failure:raise SystemExit('response lost after accepted root and later HEAD change')
print(json.dumps(result))
'''


@pytest.fixture
def cli_peer(tmp_path):
    root,config,health=peers(tmp_path)
    response=page();response['data']['repository']['pullRequest']['timelineItems']['edges']=history()['edges']
    candidate={'number':42,'head':'a'*40,'stage':'review','review_depth':2}
    health.update(review_candidates=[candidate],content_review_candidates=[candidate],merge_executable=[],integration_owner=None)
    state=tmp_path/'peer.json'
    state.write_text(json.dumps({'page':response,'health':health,'comments':[],'posts':[],'calls':[]}),encoding='utf-8')
    script=tmp_path/'fake_gh.py';script.write_text(PEER,encoding='utf-8')
    if os.name=='nt':
        compiler=Path(os.environ.get('SystemRoot', r'C:\Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            pytest.skip('Windows executable peer requires the .NET Framework C# compiler')
        executable=tmp_path/'gh.exe'
        built=subprocess.run([str(compiler),'/nologo','/target:exe','/out:'+str(executable),
            str(Path(__file__).with_name('fake_gh_launcher.cs'))],capture_output=True,text=True,
            timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert built.returncode==0,built.stdout+built.stderr
    else:
        executable=tmp_path/'gh';executable.write_text('#!'+sys.executable+'\n'+PEER,encoding='utf-8');executable.chmod(0o700)
    checker=tmp_path/'checker.py'
    checker.write_text("import json,os\nfrom pathlib import Path\nprint(json.dumps(json.loads(Path(os.environ['ROUTING_FAKE_PEER']).read_text())['health']))\n",encoding='utf-8')
    config.update(repository='owner/repo',trusted_account='owner',
                  health_command=[sys.executable,str(checker),'-contract',str(root/'review-queue/SKILL.md'),'-json'])
    path=tmp_path/'pipelinectl.json';path.write_text(json.dumps(config),encoding='utf-8')
    report=tmp_path/'report.json';report.write_text(json.dumps(publication()['report']),encoding='utf-8')
    environment=dict(os.environ)
    for name in list(environment):
        if name.startswith('PP_') or name in {'GH_TOKEN','GITHUB_TOKEN','GH_EXE'}:environment.pop(name,None)
    environment.update(PP_DATA_DIR=str(tmp_path/'d'),PP_PIPELINE_STATE_DIR=str(tmp_path/'s'),
                       GH_EXE=str(executable),ROUTING_FAKE_PEER=str(state),PYTHONDONTWRITEBYTECODE='1',
                       PYTHONIOENCODING='utf-8',
                       ROUTING_FAKE_PYTHON=sys.executable,ROUTING_FAKE_SCRIPT=str(script))
    def run(*args):
        result=subprocess.run([sys.executable,'-X','utf8','-m','promptpilot.project_pipeline','--config',str(path),*args],
            cwd=Path(__file__).resolve().parents[1],env=environment,capture_output=True,text=True,encoding='utf-8',timeout=90,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        return result.returncode,json.loads(result.stdout)
    run.environment=environment
    return state,report,run


@pytest.mark.skipif(os.name != 'nt', reason='Windows executable pipe bridge')
def test_windows_executable_peer_preserves_canonical_utf8_and_stderr(cli_peer, tmp_path):
    from promptpilot.review_routing_wire import render
    _state, _report, run = cli_peer
    script = tmp_path/'echo_bytes.py'
    script.write_text("import sys\n"
                      "body = sys.stdin.buffer.read()\n"
                      "sys.stdout.buffer.write(body)\n"
                      "sys.stderr.buffer.write(body)\n"
                      "raise SystemExit(7)\n", encoding='utf-8')
    environment = dict(run.environment, ROUTING_FAKE_SCRIPT=str(script),
                       PYTHONIOENCODING='ascii:surrogateescape')
    # Includes canonical Cyrillic evidence and characters outside Windows
    # single-byte code pages. Do not hide pipe corruption with ASCII escapes.
    body = (render(publication()) + '\n中文 🚀 "quoted"\r\n').encode('utf-8')
    result = subprocess.run([environment['GH_EXE'], '--input', '-'], input=body,
                            env=environment, capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 7
    assert result.stdout == body
    assert result.stderr == body

    # Reproduce CI's Python 3.10 Windows default: the peer must parse stdin as
    # UTF-8 even when UTF-8 mode was disabled in its inherited environment.
    # The old launcher silently changed this JSON through a legacy code page.
    script.write_text("import json,sys\n"
                      "print(json.dumps({'value': json.load(sys.stdin), 'utf8': sys.flags.utf8_mode}))\n",
                      encoding='utf-8')
    environment.pop('PYTHONIOENCODING')
    environment['PYTHONUTF8'] = '0'
    expected = {'body': body.decode('utf-8')}
    result = subprocess.run([environment['GH_EXE'], '--input', '-'],
                            input=json.dumps(expected, ensure_ascii=False).encode('utf-8'),
                            env=environment, capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'value': expected, 'utf8': 1}


def test_real_subprocess_cli_third_round_and_completed_old_head_readback(cli_peer):
    state,report,run=cli_peer
    code,selected=run('next','review');assert code==0 and selected['action']=='audit',selected
    code,completed=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0 and completed['action']=='completed',completed
    before=json.loads(state.read_text(encoding='utf-8'));assert len(before['posts'])==3
    from promptpilot.review_routing_wire import parse, render
    record = parse(before['posts'][0])
    assert record['report'] == json.loads(report.read_text(encoding='utf-8'))
    assert render(record).encode('utf-8') == before['posts'][0].encode('utf-8')
    before['page']['data']['repository']['pullRequest']['labels']['nodes']=[{'name':'hold'}]
    state.write_text(json.dumps(before),encoding='utf-8')
    code,recovered=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0 and recovered==completed,recovered
    assert len(json.loads(state.read_text(encoding='utf-8'))['posts'])==3


def test_real_subprocess_lost_response_after_head_change_is_not_reposted(cli_peer):
    state,report,run=cli_peer
    value=json.loads(state.read_text(encoding='utf-8'));value['move_after_root']=True
    state.write_text(json.dumps(value),encoding='utf-8')
    code,selected=run('next','review');assert code==0,selected
    for _ in range(2):
        code,refused=run('complete','review','--lease',selected['lease'],'--report',str(report))
        assert code==2 and refused['action']=='error',refused
    assert len(json.loads(state.read_text(encoding='utf-8'))['posts'])==1


def test_real_subprocess_claimed_author_handoff_uses_executable_peer_and_no_process_start(cli_peer, isolated_db, tmp_path):
    from datetime import timezone
    from promptpilot.models import TaskCreate
    from promptpilot.review_routing_fix_request import BRIDGE
    from promptpilot.review_routing_wire import parse
    state,report,run=cli_peer
    code,selected=run('next','review');assert code==0,selected
    code,completed=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0,completed
    value=json.loads(state.read_text(encoding='utf-8'))
    value['health']['fix_bridge_contract']=BRIDGE
    value['health']['technical_fix_candidates']=[{'stage':'fix-pr','number':42,'head':'a'*40,'source':completed['source']}]
    state.write_text(json.dumps(value),encoding='utf-8')
    isolated_db.create_task(TaskCreate(prompt='Project - FIX',working_dir=str(tmp_path)))
    task=isolated_db.get_next_runnable();assert task is not None
    run.environment.update(PP_DATA_DIR=str(isolated_db.DB_PATH.parent),PP_TASK_ID=str(task.id),
        PP_TASK_STARTED_AT=task.started_at.astimezone(timezone.utc).isoformat(),
        PP_PIPELINE_REPLICAS='1',PP_PROVIDER_OWNERSHIP_KIND='headless')
    code,selected=run('next','fix');assert code==0 and selected['action']=='author-handoff',selected
    for _ in range(2):
        code,result=run('complete','fix','--lease',selected['lease'])
        assert code==0 and result['action']=='handed-off' and result['provider_started'] is False,result
    value=json.loads(state.read_text(encoding='utf-8'))
    assert len(value['assignments'])==1
    records=[parse(body) for body in value['posts']]
    assert [entry['kind'] for entry in records]==['publication','claim','completion','attempt','phase','phase']
    assert [entry['phase'] for entry in records if entry['kind']=='phase']==['assigned','handoff-done']
