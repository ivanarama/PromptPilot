import json
import hashlib
import os
from pathlib import Path
import subprocess
import pytest
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_audit_history import retained
from test_review_routing_history import legacy_comment

@pytest.mark.parametrize('fault',['null','missing','wrong-type','empty','trusted'])
def test_disabled_public_review_fences_unavailable_protocol_actor(adapter,fault,tmp_path):
 peer,health,path,report,run=adapter
 state=retained()
 for edge in state['edges']:
  node=edge['node']
  if 'pp:review-routing-' in node.get('body',''):
   if fault=='null':node['author']=None
   elif fault=='missing':node.pop('author')
   elif fault=='wrong-type':node['author']='owner'
   elif fault=='empty':node['author']={}
   else:node['author']={'login':'owner'}
 peer.current['data']['repository']['pullRequest']['timelineItems']['edges']=state['edges']
 # Use the checker built here from the exact OneBase source, with this same
 # complete snapshot. No handcrafted capability dictionary contributes to election.
 binary=os.environ.get('PP_ONEBASE_PAIR_CHECKER')
 if not binary:
  # Ordinary CI exercises the real public coordinator with its existing
  # synthetic diagnostic peer; separately pinned builds exercise both CLIs.
  actual_health=dict(health)
 else:
  assert os.environ.get('PP_ONEBASE_PAIR_SKILLS')
  assert hashlib.sha256(Path(binary).read_bytes()).hexdigest()==os.environ.get('PP_ONEBASE_PAIR_CHECKER_SHA256')
 comments=[dict(id=int(e['node']['fullDatabaseId']),created_at=e['node']['createdAt'],
  updated_at=e['node']['createdAt'],user=e['node'].get('author'),body=e['node']['body'])
  for e in state['edges'] if e['node']['__typename']=='IssueComment']
 pull=dict(number=42,title='private joint unavailable actor probe',state='open',draft=False,
  created_at='2026-10-29T00:00:00Z',updated_at='2026-10-29T00:01:00Z',
  head=dict(sha=state['headRefOid']),base=dict(ref='main'),labels=[],comments=comments,routing_snapshot=state)
 prs=tmp_path/'prs.json';prs.write_text(json.dumps([pull]),encoding='utf-8')
 issues=tmp_path/'issues.json';issues.write_text('[]',encoding='utf-8')
 cmd=[binary or 'unavailable','-json','-repo','owner/repo','-owner','owner',
  '-prs',str(prs),'-issues',str(issues),'-contract',str(Path(os.environ.get('PP_ONEBASE_PAIR_SKILLS','.'))/'review-queue/SKILL.md')]
 if binary:
  native=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',timeout=30,
   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  if native.returncode:
   assert fault=='wrong-type',native.stderr
   # A malformed JSON actor is rejected at the native decoder, before election.
   return
  actual_health=json.loads(native.stdout)
 health.clear();health.update(actual_health)
 raw=json.loads(path.read_text(encoding='utf-8'))
 raw['technical_handoff']['enabled']=False
 path.write_text(json.dumps(raw),encoding='utf-8')
 code,result=run('next','review')
 gate_code,gate=(None,None)
 if result.get('action')=='fallback' and result.get('handoff'):
  gate_code,gate=run('gate-fallback','review','--lease',result['handoff']['lease'])
 (tmp_path/'independent-disabled-actor-receipt.json').write_text(json.dumps(dict(native_command=cmd,
  native_health=actual_health,code=code,result=result,gate_code=gate_code,gate=gate,posts=peer.posts),indent=2),encoding='utf-8')
 assert code!=0 or result['action'] in {'wait','error'},result
 assert peer.posts==[]
 if gate_code is not None:assert gate_code!=0,gate


@pytest.mark.parametrize('author',[None,{'login':'github-actions[bot]'},{'login':'foreign'}])
def test_disabled_public_review_keeps_ordinary_ghost_bot_and_foreign_marker(adapter,author):
 peer,_health,path,_report,run=adapter
 raw=json.loads(path.read_text(encoding='utf-8'))
 raw['technical_handoff']['enabled']=False
 path.write_text(json.dumps(raw),encoding='utf-8')
 body='<!-- pp:review-routing-v1 fake foreign marker -->' if author=={'login':'foreign'} else 'ordinary app: status'
 comment=legacy_comment('99999',body);comment['node']['author']=author
 peer.current['data']['repository']['pullRequest']['timelineItems']['edges'].append(comment)
 code,result=run('next','review')
 assert code==0 and result['action']=='fallback',result
 code,result=run('gate-fallback','review','--lease',result['handoff']['lease'])
 assert code==0 and result['action']=='validated',result
 assert peer.posts==[]


@pytest.mark.parametrize('author',[None,{},'owner'])
def test_disabled_previously_signed_legacy_lease_cannot_cross_new_unknown_typed_history(adapter,author):
 peer,_health,path,_report,run=adapter
 raw=json.loads(path.read_text(encoding='utf-8'));raw['technical_handoff']['enabled']=False
 path.write_text(json.dumps(raw),encoding='utf-8')
 code,result=run('next','review');assert code==0 and result['action']=='fallback',result
 lease=result['handoff']['lease']
 comment=legacy_comment('99999','<!-- pp:review-routing-v1 unknown retained history -->')
 comment['node']['author']=author
 peer.current['data']['repository']['pullRequest']['timelineItems']['edges'].append(comment)
 code,result=run('gate-fallback','review','--lease',lease)
 assert code!=0 and result['action']=='error',result
 assert peer.posts==[]
