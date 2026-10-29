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
    if os.name=='nt':pytest.skip('executable shebang peer runs on POSIX; in-process CLI/worker tests cover Windows')
    root,config,health=peers(tmp_path)
    response=page();response['data']['repository']['pullRequest']['timelineItems']['edges']=history()['edges']
    candidate={'number':42,'head':'a'*40,'stage':'review','review_depth':2}
    health.update(review_candidates=[candidate],content_review_candidates=[candidate],merge_executable=[],integration_owner=None)
    state=tmp_path/'peer.json'
    state.write_text(json.dumps({'page':response,'health':health,'comments':[],'posts':[],'calls':[]}),encoding='utf-8')
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
                       GH_EXE=str(executable),ROUTING_FAKE_PEER=str(state),PYTHONDONTWRITEBYTECODE='1')
    def run(*args):
        result=subprocess.run([sys.executable,'-X','utf8','-m','promptpilot.project_pipeline','--config',str(path),*args],
            cwd=Path(__file__).resolve().parents[1],env=environment,capture_output=True,text=True,encoding='utf-8',timeout=90)
        return result.returncode,json.loads(result.stdout)
    return state,report,run


def test_real_subprocess_cli_third_round_and_completed_old_head_readback(cli_peer):
    state,report,run=cli_peer
    code,selected=run('next','review');assert code==0 and selected['action']=='audit',selected
    code,completed=run('complete','review','--lease',selected['lease'],'--report',str(report))
    assert code==0 and completed['action']=='completed',completed
    before=json.loads(state.read_text(encoding='utf-8'));assert len(before['posts'])==3
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
