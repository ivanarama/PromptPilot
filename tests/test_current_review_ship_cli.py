"""Real parser/public CLI, actual2448 snapshot, real proof/epoch/intent readers.

All GH operations below are an in-memory boundary double; no network or real
repository mutations. The original pending intent is parsed through the real
durable comment index, never replaced with a fabricated proof or intent.
"""
import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from promptpilot import project_pipeline as fixed

CAPTURE=Path(__file__).parent/'fixtures/current-review-ship-2448.json'
REPO='ivanarama/onebase';NUMBER=2448


class ActualGitHub:
    def __init__(self):
        raw=CAPTURE.read_bytes()
        assert hashlib.sha256(raw).hexdigest()=='50c3442173d1de7f760a3aa7e7a0464f1a3d88277046f4b598bc6737e362678c'
        self.cap=json.loads(raw)
        self.snapshot=self.cap['stable_full_snapshot']
        self.calls=[];self.mutations=[];self.timeout_seconds=120
        self.clock=0;self.identity='ivanarama';self.owner_head=None
        self.revoke_during_graph_read=False;self.unknown_put=False
        self.commits={self.cap['commit']['sha']:self.cap['commit']};self.comparisons={}

    def now(self):
        self.clock+=1
        return f'2026-11-06T16:00:{self.clock:02d}Z'

    def health(self,*args,**kwargs):
        head=self.snapshot['headRefOid']
        owner={'number':NUMBER,'head':self.owner_head or head,'stage':'integration-merge-ready','review_depth':2}
        return {'state':'yellow','integration_owner':owner,'findings':[
            {'severity':'yellow','code':'single_flight_barrier','pr':NUMBER}],
            'merge_executable':[owner],'review_candidates':[],'content_review_candidates':[]}

    def graphql_page(self,owner,name,number,cursor):
        assert (owner,name,number,cursor)==('ivanarama','onebase',NUMBER,None)
        s=self.snapshot
        pr={k:copy.deepcopy(s[k]) for k in ('headRefOid','baseRefOid','baseRefName','state','isDraft')}
        pr['labels']={'nodes':[{'name':v} for v in s['labels']], 'pageInfo':{'hasNextPage':False}}
        pr['timelineItems']={'edges':copy.deepcopy(s['edges']), 'updatedAt':s['updatedAt'],
                             'pageInfo':{'hasNextPage':False,'endCursor':None}}
        return {'data':{'repository':{'pullRequest':pr}}}

    def comments(self):
        result=[]
        for edge in self.snapshot['edges']:
            n=edge['node']
            if n['__typename']=='IssueComment':
                result.append({'id':int(n['fullDatabaseId']),'body':n['body'],
                    'user':copy.deepcopy(n['author']),'created_at':n['createdAt'],
                    'updated_at':n.get('lastEditedAt') or n['createdAt'],
                    'issue_url':f'https://api.github.com/repos/{REPO}/issues/{NUMBER}'})
        return result

    def append(self,kind,**node):
        self.snapshot['edges'].append({'cursor':f'test-cursor-{len(self.snapshot["edges"])}',
            'node':{'__typename':kind,'id':f'test-node-{len(self.snapshot["edges"])}',**node}})
        self.snapshot['updatedAt']=node['createdAt']

    def run(self,*args,**kwargs):
        if args[:2]==('api','--paginate'):
            assert args[2].startswith(f'repos/{REPO}/issues/')
            return '\n'.join(json.dumps(c) for c in self.comments())
        if args[:3]==('api','--method','DELETE'):
            assert args[3]==f'repos/{REPO}/issues/{NUMBER}/labels/ship'
            self.mutations.append(('DELETE',args[3],None))
            self.snapshot['labels'].remove('ship')
            self.cap['pr']['labels']=[x for x in self.cap['pr']['labels'] if x['name']!='ship']
            self.append('UnlabeledEvent',actor={'login':'ivanarama'},label={'name':'ship'},createdAt=self.now())
            return ''
        raise AssertionError(('unexpected gh run',args))

    def json(self,*args,input_value=None):
        self.calls.append(args)
        if args[:2]==('pr','view'):
            return copy.deepcopy(self.cap['status'])
        assert args[0]=='api'
        route=args[1]
        method=args[args.index('--method')+1] if '--method' in args else 'GET'
        if method!='GET':self.mutations.append((method,route,copy.deepcopy(input_value)))
        if route=='user':return {'login':self.identity}
        if route==f'repos/{REPO}/pulls/{NUMBER}' and method=='GET':
            return copy.deepcopy(self.cap['pr'])
        if route.startswith(f'repos/{REPO}/git/commits/'):
            if self.revoke_during_graph_read:
                self.revoke_during_graph_read=False
                self.append('UnlabeledEvent',actor={'login':'ivanarama'},label={'name':'ship'},createdAt=self.now())
                self.snapshot['labels'].remove('ship')
                self.cap['pr']['labels']=[v for v in self.cap['pr']['labels'] if v['name']!='ship']
            return copy.deepcopy(self.commits[route.rsplit('/',1)[1]])
        if route.startswith(f'repos/{REPO}/compare/'):
            return copy.deepcopy(self.comparisons[route.rsplit('/',1)[1]])
        if route==f'repos/{REPO}/git/ref/heads/main':
            return {'ref':'refs/heads/main','object':{'type':'commit','sha':self.snapshot['baseRefOid']}}
        if route==f'repos/{REPO}/issues/{NUMBER}' and method=='GET':
            return {'labels':copy.deepcopy(self.cap['pr']['labels'])}
        if route==f'repos/{REPO}/pulls/{NUMBER}/merge' and method=='PUT':
            assert input_value=={'merge_method':'merge','sha':self.snapshot['headRefOid']}
            self.snapshot['state']='MERGED'
            self.cap['pr'].update(state='closed',merged=True,merge_commit_sha='f'*40)
            self.append('MergedEvent',commit={'oid':'f'*40},createdAt=self.now())
            if self.unknown_put:raise fixed.PipelineError('transport lost response after exact CAS send')
            return {'merged':True,'sha':'f'*40}
        if route==f'repos/{REPO}/issues/{NUMBER}/comments' and method=='POST':
            assert 'pp:merge-cleanup-done' in input_value['body']
            created=self.now()
            self.append('IssueComment',fullDatabaseId='999999995',author={'login':'ivanarama'},
                        lastEditedAt=None,body=input_value['body'],createdAt=created)
            return self.comments()[-1]
        raise AssertionError(('unexpected gh JSON',args,input_value))


@pytest.fixture
def cli(tmp_path,monkeypatch,capsys):
    gh=ActualGitHub()
    config=tmp_path/'pipelinectl.json'
    names=['build','test-windows','lint','vuln','postgres-integration','smoke',
           'Smoke ubuntu-latest / private','Smoke windows-latest / private']
    config.write_text(json.dumps({'repository':REPO,'trusted_account':'ivanarama',
        'health_command':['project-health','-json'],
        'base_branch':'main','ready_owner_merge':True,'base_sync_carry':False,
        'base_sync_merge':False,'required_checks':names}),encoding='utf-8')
    monkeypatch.setenv('PP_PIPELINE_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('PP_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.delenv('PP_PIPELINE_EXCLUDED_NUMBERS',raising=False)
    monkeypatch.delenv('PP_PIPELINE_REPLICAS',raising=False)
    def invoke(module,*args):
        monkeypatch.setattr(module,'GitHub',lambda:gh)
        monkeypatch.setattr(module,'run_health',gh.health)
        code=module.run(['--config',str(config),*args])
        captured=capsys.readouterr()
        return code,json.loads(captured.out),captured.err
    return gh,invoke


def test_actual_public_cli_uses_original_intent_and_exact_head_cas(cli):
    gh,invoke=cli
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    lease=fixed.decode_lease(result['lease'])
    assert lease['intent']==gh.cap['intent'] and lease['proof']==gh.cap['proof']
    assert fixed.digest(lease['proof'])==gh.cap['intent']['proof_sha256']
    assert gh.mutations==[]
    code,done,_=invoke(fixed,'complete','merge','--lease',result['lease'])
    assert code==0 and done['action']=='completed',done
    assert [m for m in gh.mutations if m[0]=='PUT']==[
        ('PUT',f'repos/{REPO}/pulls/{NUMBER}/merge',{'merge_method':'merge','sha':lease['head']})]
    posts=[m for m in gh.mutations if m[0]=='POST']
    assert len(posts)==1 and 'pp:merge-cleanup-done' in posts[0][2]['body']
    assert not any('pp:merge-cleanup-intent' in m[2]['body'] for m in posts)
    assert 'ship' not in gh.snapshot['labels']
    # The same old lease cannot send the merge again, and merged cleanup did
    # not require the live ship label that its own first cleanup removed.
    code,again,_=invoke(fixed,'complete','merge','--lease',result['lease'])
    assert code==2 and len([m for m in gh.mutations if m[0]=='PUT'])==1


@pytest.mark.parametrize('fault', ['revoked','foreign-relabel','parents','tree','extra-head',
    'force-push','restore','base-change','deleted-comment','source-edit','broken-done',
    'broken-source-intent','review-again-gap','current-proof','current-completion','ci',
    'body','hold','manual','missing-edit-state','different-pr',
    'commit-identity','partial-commit','in-flight-revoke'])
def test_real_cli_rejects_changed_source_or_current_facts_without_writes(cli,fault):
    gh,invoke=cli
    edges=gh.snapshot['edges']
    if fault in ('revoked','foreign-relabel'):
        kind='UnlabeledEvent' if fault=='revoked' else 'LabeledEvent'
        node={'__typename':kind,'id':'injected-node','createdAt':'2026-11-06T13:21:00Z',
              'actor':{'login':'ivanarama' if fault=='revoked' else 'foreign'},'label':{'name':'ship'}}
        edges.insert(8,{'cursor':'injected-cursor','node':node})
    elif fault=='parents':gh.cap['commit']['parents'].reverse()
    elif fault=='tree':gh.cap['commit']['tree']['sha']='not-a-Git-tree'
    elif fault in ('extra-head','force-push','restore','base-change','deleted-comment'):
        kinds={'extra-head':'PullRequestCommit','force-push':'HeadRefForcePushedEvent',
               'restore':'HeadRefRestoredEvent','base-change':'BaseRefChangedEvent','deleted-comment':'CommentDeletedEvent'}
        node={'__typename':kinds[fault],'id':'injected-node'}
        if fault=='extra-head':node['commit']={'oid':'f'*40}
        edges.insert(8,{'cursor':'injected-cursor','node':node})
    elif fault=='source-edit':edges[2]['node']['lastEditedAt']='2026-11-06T13:10:00Z'
    elif fault=='broken-done':edges[9]['node']['body']=edges[9]['node']['body'].replace('previous=none','previous=1')
    elif fault=='broken-source-intent':edges[7]['node']['body']=edges[7]['node']['body'].replace('review-comment=6685215803','review-comment=1')
    elif fault=='review-again-gap':
        node=copy.deepcopy(edges[7]['node'])
        node.update(id='injected-node',fullDatabaseId='999999991',body='pp:review-again',createdAt='2026-11-06T13:21:00Z')
        edges.insert(8,{'cursor':'injected-cursor','node':node})
    elif fault=='current-proof':edges[10]['node']['author']['login']='foreign'
    elif fault=='current-completion':edges[12]['node']['body']=edges[12]['node']['body'].replace('claim=6686603049','claim=1')
    elif fault=='ci':
        next(c for c in gh.cap['status']['statusCheckRollup'] if c['name']=='build')['conclusion']='FAILURE'
    elif fault=='body':gh.cap['status']['body']+='\nchanged';gh.cap['pr']['body']+='\nchanged'
    elif fault in ('hold','manual'):
        gh.snapshot['labels'].append(fault);gh.cap['pr']['labels'].append({'name':fault})
    elif fault=='missing-edit-state':del edges[7]['node']['lastEditedAt']
    elif fault=='different-pr':gh.cap['pr']['number']=9999
    elif fault=='commit-identity':gh.cap['commit']['sha']='f'*40
    elif fault=='partial-commit':del gh.cap['commit']['tree']
    elif fault=='in-flight-revoke':gh.revoke_during_graph_read=True
    code,result,_=invoke(fixed,'next','merge')
    assert code==2 or result['action'] in ('fallback','wait'),result
    assert not result.get('lease') and gh.mutations==[]


@pytest.mark.parametrize('fault',['owner','identity','ci','body','late-revoke','late-proof'])
def test_completion_rechecks_real_gates_before_any_mutation(cli,fault):
    gh,invoke=cli
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    if fault=='owner':gh.owner_head='f'*40
    elif fault=='identity':gh.identity='foreign'
    elif fault=='ci':next(c for c in gh.cap['status']['statusCheckRollup'] if c['name']=='build')['conclusion']='FAILURE'
    elif fault=='body':gh.cap['status']['body']+='\nchanged';gh.cap['pr']['body']+='\nchanged'
    elif fault=='late-revoke':
        gh.append('UnlabeledEvent',actor={'login':'ivanarama'},label={'name':'ship'},createdAt=gh.now())
        gh.snapshot['labels'].remove('ship');gh.cap['pr']['labels']=[v for v in gh.cap['pr']['labels'] if v['name']!='ship']
    elif fault=='late-proof':gh.snapshot['edges'][12]['node']['body']+='\nchanged'
    code,denied,_=invoke(fixed,'complete','merge','--lease',result['lease'])
    assert code==2,denied
    assert gh.mutations==[]


def test_unknown_put_is_not_automatically_resent_and_cleanup_uses_original_intent(cli):
    gh,invoke=cli
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge'
    gh.unknown_put=True
    code,error,_=invoke(fixed,'complete','merge','--lease',result['lease'])
    assert code==2 and 'transport lost response' in error['error']
    assert len(gh.mutations)==1 and gh.mutations[0][0]=='PUT'
    code,recovery,_=invoke(fixed,'next','merge')
    assert code==0 and recovery['action']=='cleanup'
    code,done,_=invoke(fixed,'complete','merge-cleanup','--lease',recovery['lease'])
    assert code==0 and done['action']=='completed'
    assert len([m for m in gh.mutations if m[0]=='PUT'])==1
    assert len([m for m in gh.mutations if m[0]=='POST'])==1


def test_source_sticky_ship_may_precede_old_review_with_new_current_review(cli):
    gh,invoke=cli
    edges=gh.snapshot['edges']
    ship=edges.pop(6)
    ship['node']['createdAt']='2026-11-06T12:49:00Z'
    edges.insert(2,ship)
    # Source anchor is followed by ship before old review/claim/completion;
    # that full old proof is committed before base-sync intent. CURRENT HEAD
    # still has its own exact committed review. No old review is carried.
    assert edges[1]['node']['commit']['oid']=='b2fb8719bf779cfc49143128d2b595fffed8ad00'
    assert edges[2]['node']['id']=='LE_lADOSKrn-s8AAAABbeuQnM8AAAAIOvco1A'
    assert edges[3]['node']['fullDatabaseId']=='6685215803'
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    lease=fixed.decode_lease(result['lease'])
    assert lease['proof']==gh.cap['proof'] and lease['intent']==gh.cap['intent']
    assert gh.mutations==[]
    code,done,_=invoke(fixed,'complete','merge','--lease',result['lease'])
    assert code==0 and done['action']=='completed',done
    assert len([m for m in gh.mutations if m[0]=='PUT'])==1


@pytest.mark.parametrize('author',['ivanarama','foreign'])
@pytest.mark.parametrize('example',['prose','inline','backticks','tildes','unclosed'])
def test_lineage_discussion_and_quoted_examples_do_not_revoke_ship(cli,author,example):
    gh,invoke=cli
    marker=gh.snapshot['edges'][9]['node']['body']
    bodies={
        'prose':'Audit note: checked pp:base-sync-intent and pp:base-sync-done provenance.',
        'inline':'Example: `'+marker+'`',
        'backticks':'```html\n'+marker+'\n```',
        'tildes':'   ~~~~html\n'+marker+'\n   ~~~~',
        'unclosed':'```html\n'+marker,
    }
    gh.append('IssueComment',fullDatabaseId='999999997',author={'login':author},
              lastEditedAt=None,body=bodies[example],createdAt=gh.now())
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    lease=fixed.decode_lease(result['lease'])
    assert lease['intent']==gh.cap['intent'] and lease['proof']==gh.cap['proof']
    assert gh.mutations==[]


@pytest.mark.parametrize('author',['ivanarama','foreign'])
@pytest.mark.parametrize('marker',['extra-done','unknown-version'])
def test_real_extra_lineage_markers_still_close_gate(cli,author,marker):
    gh,invoke=cli
    body=(gh.snapshot['edges'][9]['node']['body'] if marker=='extra-done'
          else '<!-- pp:base-sync-unknown-v99 value=unrecognized -->')
    gh.append('IssueComment',fullDatabaseId='999999997',author={'login':author},
              lastEditedAt=None,body=body,createdAt=gh.now())
    code,result,_=invoke(fixed,'next','merge')
    assert code==2 or result['action'] in ('fallback','wait'),result
    assert not result.get('lease') and gh.mutations==[]


def unsigned_object(tmp_path,parents,tree,message):
    """Real unsigned Git objects in an isolated test repository, no project commit."""
    binary='C:/Program Files/Git/mingw64/bin/git.exe'
    if not Path(binary).is_file():binary='git'
    repo=tmp_path/'synthetic-git-fixture'
    if not repo.exists():
        repo.mkdir()
        subprocess.run([binary,'init','--quiet'],cwd=repo,check=True,capture_output=True)
    raw=('tree '+tree+'\n'+''.join('parent '+p+'\n' for p in parents)
         +'author Fixture <fixture@example.invalid> 1793974000 +0000\n'
         +'committer Fixture <fixture@example.invalid> 1793974000 +0000\n\n'+message+'\n').encode()
    head=subprocess.check_output([binary,'hash-object','-w','-t','commit','--stdin'],cwd=repo,input=raw).decode().strip()
    assert subprocess.check_output([binary,'cat-file','commit',head],cwd=repo)==raw
    return {'sha':head,'tree':{'sha':tree},'parents':[{'sha':p} for p in parents],
            'verification':{'verified':False,'reason':'unsigned','signature':None,'payload':None}}


def advance_main(gh,tmp_path):
    old=gh.snapshot['baseRefOid']
    obj=unsigned_object(tmp_path,[old],gh.cap['commit']['tree']['sha'],'synthetic accepted main descendant')
    gh.snapshot['baseRefOid']=obj['sha'];gh.cap['pr']['base']['sha']=obj['sha']
    gh.comparisons[old+'...'+obj['sha']]={'status':'ahead','ahead_by':1,'behind_by':0,
        'base_commit':{'sha':old},'merge_base_commit':{'sha':old},'commits':[{'sha':obj['sha']}]}
    return obj


def refresh_current_proof_and_intent(gh):
    trusted='ivanarama';head=gh.snapshot['headRefOid']
    info=fixed.epoch(gh.snapshot,trusted)
    proof=fixed.proof(info,head,trusted)
    assert proof and proof['outcome']=='reviewed'
    gh.cap['proof']=proof
    cleanup=next(e['node'] for e in reversed(gh.snapshot['edges'])
                 if e['node']['__typename']=='IssueComment' and 'pp:merge-cleanup-intent' in e['node']['body'])
    cleanup['body']=fixed.intent_body(head,proof,gh.cap['pr']['body'],[])
    rest=next(c for c in gh.comments() if c['id']==int(cleanup['fullDatabaseId']))
    gh.cap['intent']=fixed.parse_merge_intent(rest,{'repository':REPO,'trusted_account':trusted})
    assert gh.cap['intent']


def add_unsigned_second_hop(gh,tmp_path):
    source=gh.snapshot['headRefOid'];oldbase=gh.snapshot['baseRefOid']
    main=advance_main(gh,tmp_path)['sha']
    obj=unsigned_object(tmp_path,[source,main],gh.cap['commit']['tree']['sha'],'synthetic second base-sync')
    head=obj['sha'];ship='LE_lADOSKrn-s8AAAABbeuQnM8AAAAIOvco1A'
    gh.snapshot['edges'].pop() # Explicit synthetic case: old unexecuted cleanup marker is absent.
    def comment(identity,body,at):
        gh.append('IssueComment',fullDatabaseId=str(identity),author={'login':'ivanarama'},
                  lastEditedAt=None,body=body,createdAt=at)
    comment(7000000000,f'<!-- pp:base-sync-intent from={source} base={main} review-comment=6686599236 '
            f'claim=6686603049 completion=6686610358 ship-event={ship} previous=6686048754 -->','2026-11-06T14:10:00Z')
    gh.append('PullRequestCommit',commit={'oid':head},createdAt='2026-11-06T14:11:00Z')
    gh.snapshot['headRefOid']=head;gh.cap['pr']['head']['sha']=head
    gh.commits[head]=obj;gh.cap['commit']=obj
    comment(7000000001,f'<!-- pp:base-sync-done intent=7000000000 from={source} to={head} base={main} '
            f'previous=6686048754 ship-event={ship} -->','2026-11-06T14:12:00Z')
    epoch=fixed.epoch(gh.snapshot,'ivanarama')['hash']
    comment(7000000002,f'Reviewed-SHA: {head}\nOutcome-Label: reviewed\n<!-- pp:review pp:tail=0 -->','2026-11-06T14:13:00Z')
    comment(7000000003,f'<!-- pp:review-claim {head} review-comment=7000000002 epoch-sha256={epoch} -->','2026-11-06T14:14:00Z')
    comment(7000000004,f'<!-- pp:head-reviewed {head} review-comment=7000000002 claim=7000000003 epoch-sha256={epoch} -->','2026-11-06T14:15:00Z')
    comment(7000000005,'placeholder pp:merge-cleanup-intent','2026-11-06T14:16:00Z')
    refresh_current_proof_and_intent(gh)
    return oldbase,main,head


def test_valid_unsigned_merge_with_new_current_proof_is_authorized(cli,tmp_path):
    gh,invoke=cli
    old=gh.snapshot['headRefOid'];obj=unsigned_object(tmp_path,
        [p['sha'] for p in gh.cap['commit']['parents']],gh.cap['commit']['tree']['sha'],'synthetic unsigned dirty base-sync')
    head=obj['sha'];old_epoch=fixed.epoch(gh.snapshot,'ivanarama')['hash']
    gh.snapshot['headRefOid']=head;gh.cap['pr']['head']['sha']=head
    gh.commits={head:obj};gh.cap['commit']=obj
    for edge in gh.snapshot['edges']:
        node=edge['node']
        if node['__typename']=='PullRequestCommit' and node['commit']['oid']==old:node['commit']['oid']=head
    epoch=fixed.epoch(gh.snapshot,'ivanarama')['hash']
    for edge in gh.snapshot['edges']:
        node=edge['node']
        if node['__typename']=='IssueComment':node['body']=node['body'].replace(old,head).replace(old_epoch,epoch)
    refresh_current_proof_and_intent(gh)
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    lease=fixed.decode_lease(result['lease'])
    assert lease['proof']==gh.cap['proof'] and lease['head']==head
    code,done,_=invoke(fixed,'complete','merge','--lease',result['lease'])
    assert code==0 and done['action']=='completed',done


def test_signature_metadata_is_not_an_extra_authorization_gate(cli):
    gh,invoke=cli
    gh.cap['commit']['verification']={'verified':False,'reason':'unknown_key',
        'signature':'different format/header order','payload':'not an authority field'}
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    assert fixed.decode_lease(result['lease'])['proof']==gh.cap['proof'] and gh.mutations==[]


def test_proven_advanced_main_preserves_original_intent_and_current_proof(cli,tmp_path):
    gh,invoke=cli
    original=copy.deepcopy(gh.cap['intent']);current=copy.deepcopy(gh.cap['proof'])
    advance_main(gh,tmp_path)
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    lease=fixed.decode_lease(result['lease'])
    assert lease['intent']==original and lease['proof']==current


def test_two_hop_chain_uses_each_source_proof_same_ship_and_new_final_proof(cli,tmp_path):
    gh,invoke=cli
    _old,main,head=add_unsigned_second_hop(gh,tmp_path)
    code,result,_=invoke(fixed,'next','merge')
    assert code==0 and result['action']=='merge',result
    lease=fixed.decode_lease(result['lease'])
    assert lease['head']==head and lease['proof']==gh.cap['proof'] and lease['intent']==gh.cap['intent']
    assert gh.mutations==[]
    code,done,_=invoke(fixed,'complete','merge','--lease',result['lease'])
    assert code==0 and done['action']=='completed',done
    assert len([m for m in gh.mutations if m[0]=='PUT'])==1


@pytest.mark.parametrize('fault',['previous-missing','previous-cycle','previous-head','intent-previous',
    'source-proof','source-incomplete','ship-identity','revocation','foreign-relabel','graph',
    'unrelated-ancestor','wrong-main-ancestor','missing-ancestry','override-between-links','final-proof'])
def test_two_hop_chain_or_ancestry_fault_never_sends(cli,tmp_path,fault):
    gh,invoke=cli
    old,main,head=add_unsigned_second_hop(gh,tmp_path)
    by_id={int(e['node']['fullDatabaseId']):e['node'] for e in gh.snapshot['edges'] if e['node']['__typename']=='IssueComment'}
    if fault=='previous-missing':by_id[7000000001]['body']=by_id[7000000001]['body'].replace('previous=6686048754','previous=1')
    elif fault=='previous-cycle':by_id[7000000001]['body']=by_id[7000000001]['body'].replace('previous=6686048754','previous=7000000001')
    elif fault=='previous-head':by_id[6686048754]['body']=by_id[6686048754]['body'].replace('to=8f8a3c60d201be67945962fa0028058199ba30d2','to='+'f'*40)
    elif fault=='intent-previous':by_id[7000000000]['body']=by_id[7000000000]['body'].replace('previous=6686048754','previous=none')
    elif fault=='source-proof':by_id[6686599236]['body']=by_id[6686599236]['body'].replace('Outcome-Label: reviewed','Outcome-Label: changes-requested')
    elif fault=='source-incomplete':by_id[6686610358]['body']+='\nchanged'
    elif fault=='ship-identity':by_id[7000000001]['body']=by_id[7000000001]['body'].replace('LE_lADOSKrn-s8AAAABbeuQnM8AAAAIOvco1A','OTHER_SHIP')
    elif fault in ('revocation','foreign-relabel'):
        index=next(i for i,e in enumerate(gh.snapshot['edges']) if e['node'] is by_id[7000000000])
        gh.snapshot['edges'].insert(index,{'cursor':'fault-cursor','node':{'__typename':'UnlabeledEvent' if fault=='revocation' else 'LabeledEvent',
            'id':'fault-node','actor':{'login':'ivanarama' if fault=='revocation' else 'foreign'},'label':{'name':'ship'},'createdAt':'2026-11-06T14:09:00Z'}})
    elif fault=='graph':gh.commits[head]['parents'].reverse()
    elif fault=='unrelated-ancestor':gh.comparisons[old+'...'+main]['status']='diverged'
    elif fault=='wrong-main-ancestor':gh.comparisons[old+'...'+main]['commits'][-1]['sha']='f'*40
    elif fault=='missing-ancestry':gh.comparisons[old+'...'+main]={}
    elif fault=='override-between-links':
        index=next(i for i,e in enumerate(gh.snapshot['edges']) if e['node'] is by_id[7000000000])+1
        n=copy.deepcopy(by_id[7000000000]);n.update(id='fault-node',fullDatabaseId='7000000010',body='pp:review-again',createdAt='2026-11-06T14:10:30Z')
        gh.snapshot['edges'].insert(index,{'cursor':'fault-cursor','node':n})
    elif fault=='final-proof':by_id[7000000004]['body']+='\nchanged'
    code,result,_=invoke(fixed,'next','merge')
    assert code==2 or result['action'] in ('fallback','wait'),result
    assert not result.get('lease') and gh.mutations==[]
