"""Pure canonical CURRENT review and continuously carried ship facts.

An old review is never carried onto an unreviewed HEAD. Each intent/done link
binds a graph-verified merge and the committed review of its source; the final
HEAD has its own new canonical review. No feature flag, election, CI, budget,
policy or mutation permission is established by this module.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import re

from .pipeline_markers import has_review_again


class ShipAuthorizationError(ValueError):
    """Complete canonical facts were not proven; take the full-skill route."""


_SHA=re.compile(r'[0-9a-f]{40}\Z')
_INTENT=re.compile(
    r'(?m)^<!-- pp:base-sync-intent from=([0-9a-f]{40}) base=([0-9a-f]{40}) '
    r'review-comment=([0-9]+) claim=([0-9]+) completion=([0-9]+) '
    r'ship-event=([A-Za-z0-9_=-]+) previous=([0-9]+|none) -->$')
_DONE=re.compile(
    r'(?m)^<!-- pp:base-sync-done intent=([0-9]+) from=([0-9a-f]{40}) '
    r'to=([0-9a-f]{40}) base=([0-9a-f]{40}) previous=([0-9]+|none) '
    r'ship-event=([A-Za-z0-9_=-]+) -->$')
MAX_CHAIN_LINKS=64


def _protocol_body(body):
    """Keep protocol lines outside Markdown examples, as review-again does."""
    lines=[];fence=None;fence_length=0
    for line in body.split('\n'):
        text=line.lstrip(' ')
        delimiter=len(line)-len(text)<=3 and len(text)>=3 and text[0] in '`~'
        length=0
        if delimiter:
            while length<len(text) and text[length]==text[0]:length+=1
        if fence is not None:
            if (delimiter and text[0]==fence and length>=fence_length
                    and not text[length:].strip(' \t\r')):
                fence=None;fence_length=0
            continue
        if delimiter and length>=3 and (text[0]!='`' or '`' not in text[length:]):
            fence=text[0];fence_length=length
            continue
        lines.append(line)
    return '\n'.join(lines)


def _need(value,message):
    if not value:raise ShipAuthorizationError(message)


def _time(value):
    _need(isinstance(value,str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z',value),
        'missing/noncanonical UTC server timestamp')
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def _comment_id(node):
    value=node.get('fullDatabaseId')
    _need(isinstance(value,str) and re.fullmatch('[1-9][0-9]*',value),
          'missing canonical comment database identity')
    return int(value)


def _full_edges(snapshot):
    edges=snapshot['edges'];_need(isinstance(edges,list) and edges,'missing full server history')
    ids=set();cursors=set();comments=set()
    for edge in edges:
        node=edge['node'];identity=node['id'];cursor=edge['cursor']
        _need(isinstance(identity,str) and identity and isinstance(cursor,str) and cursor,
              'missing server node/cursor identity')
        _need(identity not in ids and cursor not in cursors,'duplicate server node/cursor')
        ids.add(identity);cursors.add(cursor)
        if node['__typename']=='IssueComment':
            value=_comment_id(node)
            _need(value not in comments,'duplicate server comment database identity')
            comments.add(value)
    return edges


def current_lineage_candidate(snapshot,trusted):
    """Cheap local chain candidate only, never a proof or permission.

    Traverse exact previous references from current to backwards. Ambiguity,
    cycles, missing links and the bounded size limit close this route. Every
    field is independently checked against the full timeline and graph below.
    """
    try:
        edges=_full_edges(snapshot);done={};by_head={}
        for i,e in enumerate(edges):
            n=e['node']
            if (n['__typename']!='IssueComment' or n.get('author',{}).get('login')!=trusted
                    or 'lastEditedAt' not in n or n['lastEditedAt'] is not None):continue
            matches=list(_DONE.finditer(_protocol_body(n.get('body') or '')))
            if len(matches)!=1:continue
            groups=matches[0].groups();identity=_comment_id(n)
            item={'intent_id':int(groups[0]),'source':groups[1],'head':groups[2],
                  'base':groups[3],'previous':groups[4],'ship_event_id':groups[5],
                  'done_id':identity,'done_index':i,'done_node':n}
            done[identity]=item;by_head.setdefault(item['head'],[]).append(item)
        last=by_head.get(snapshot.get('headRefOid'),[])
        if len(last)!=1:return None
        chain=[];seen=set();item=last[0]
        while True:
            if item['done_id'] in seen or len(chain)>=MAX_CHAIN_LINKS:return None
            seen.add(item['done_id']);chain.append(item)
            if item['previous']=='none':break
            previous=done.get(int(item['previous']))
            if previous is None or previous['head']!=item['source']:return None
            if len(by_head.get(previous['head'],[]))!=1:return None
            item=previous
        chain.reverse()
        return {'head':chain[-1]['head'],'hops':chain}
    except (KeyError,TypeError,ValueError,AttributeError,IndexError):return None


def _commit_identity(commit,head,source,base):
    """Authenticated Git REST graph; signature metadata is not authority."""
    _need(commit['sha']==head and [p['sha'] for p in commit['parents']]==[source,base]
          and isinstance(commit['tree']['sha'],str) and _SHA.fullmatch(commit['tree']['sha']),
          'canonical merge graph/tree identity differs')


def _ancestor(base,main,comparisons):
    if base==main:return
    value=comparisons[base]
    _need(value['status']=='ahead' and type(value['ahead_by']) is int and value['ahead_by']>0
          and type(value['behind_by']) is int and value['behind_by']==0 and value['base_commit']['sha']==base
          and value['merge_base_commit']['sha']==base and isinstance(value['commits'],list)
          and value['commits'] and value['commits'][-1]['sha']==main,
          'base is not proven ancestor of exact current protected main')


def resolve_current_ship_facts(pp,config,snapshot,pr,commit,main_ref,*,
                               cleanup_intent=None,commits=None,ancestry=None):
    """Return the current proof unchanged plus full canonical ship-chain facts.

    commits maps each server-derived merge HEAD to its full raw /git/commits
    response. ancestry maps earlier bases to full raw compare responses for
    base...current-main. Caller repeats stable timeline after these bounded
    reads. Merged cleanup does not call this live-label predicate.
    """
    try:
        return _resolve(pp,config,snapshot,pr,commit,main_ref,cleanup_intent,
                        {} if commits is None else commits,{} if ancestry is None else ancestry)
    except ShipAuthorizationError:raise
    except Exception as exc:
        raise ShipAuthorizationError('incomplete canonical facts: '+str(exc)) from exc


def _resolve(pp,config,snapshot,pr,commit,main_ref,cleanup,commits,ancestry):
    trusted=config['trusted_account'];repo=config['repository'];head=snapshot['headRefOid']
    _need(isinstance(trusted,str) and trusted and config['base_branch']=='main','unknown repository route')
    pp.validate_common(snapshot,config,{'head':head})
    _need(snapshot['labels'].count('ship')==1
          and not set(snapshot['labels'])&{'hold','manual','needs-decision','changes-requested'},
          'current label route is stopped')
    main=snapshot['baseRefOid']
    _need(all(isinstance(v,str) and _SHA.fullmatch(v) for v in (main,head)),'invalid current Git identity')
    _need(main_ref['ref']=='refs/heads/main' and main_ref['object']['type']=='commit'
          and main_ref['object']['sha']==main,'current protected main ref differs')
    _need(type(pr['number']) is int and pr['number']>0 and pr['state']=='open'
          and pr['merged'] is False and pr['draft'] is False
          and pr['base']['repo']['full_name']==repo and pr['base']['ref']=='main'
          and pr['base']['sha']==main and pr['head']['sha']==head
          and {x['name'] for x in pr['labels']}==set(snapshot['labels']),
          'live REST/GraphQL PR identity/state differs')
    edges=_full_edges(snapshot);candidate=current_lineage_candidate(snapshot,trusted)
    _need(candidate,'no unambiguous canonical current chain')
    chain=candidate['hops'];heads=[hop['head'] for hop in chain]
    _need(len(set(heads))==len(heads) and chain[0]['source'] not in heads,'cyclic source graph')
    info=pp.epoch(snapshot,trusted);pp.validate_epoch_safety(info,trusted)
    current=pp.proof(info,head,trusted)
    _need(current and current['outcome']=='reviewed','current HEAD has no canonical positive proof')
    by_comment={_comment_id(e['node']):(i,e['node']) for i,e in enumerate(edges)
                if e['node']['__typename']=='IssueComment'}
    def comment(identity):
        index,node=by_comment[identity]
        _need(node.get('author',{}).get('login')==trusted
              and 'lastEditedAt' in node and node['lastEditedAt'] is None,
              'untrusted/edited/incomplete canonical comment')
        _time(node['createdAt'])
        return index,node
    def canonical_proof(proof):
        r,rn=comment(proof['review_id']);c,cn=comment(proof['claim_id']);f,fn=comment(proof['completion_id'])
        _need(r<c<f and _time(rn['createdAt'])<=_time(cn['createdAt'])<=_time(fn['createdAt']),
              'canonical proof comment ordering differs')
        return r,c,f
    current_indices=canonical_proof(current)
    bound=len(edges)
    if cleanup is not None:
        _need(cleanup['number']==pr['number'] and cleanup['head']==head
              and cleanup['proof_sha256']==pp.digest(current)
              and hashlib.sha256(pr['body'].encode('utf-8')).hexdigest()==cleanup['body_sha256']
              and pp.same_repo_closing_issues(pr['body'],repo)==cleanup['issues'],
              'original cleanup proof/body/issues changed')
        bound=pp.exact_merge_intent_index(snapshot,config,cleanup)
        _need(_comment_id(edges[bound]['node'])==cleanup['id'],'cleanup server identity changed')
        comment(cleanup['id'])
        _need(current_indices[-1]<bound,'current proof did not complete before original cleanup')
    objects=dict(commits);objects[head]=commit
    allowed_nodes=set();marker_nodes=set();ship_id=chain[0]['ship_event_id'];facts=[]
    previous_done_index=-1;previous_done_time=None;source_anchor=None
    for n,hop in enumerate(chain):
        _need(hop['ship_event_id']==ship_id,'ship identity changed between chain links')
        _need(hop['previous']==('none' if n==0 else str(chain[n-1]['done_id'])),
              'canonical previous link differs')
        obj=objects[hop['head']];_commit_identity(obj,hop['head'],hop['source'],hop['base'])
        _ancestor(hop['base'],main,ancestry)
        listed=[(i,e['node']) for i,e in enumerate(edges)
                if e['node']['__typename']=='PullRequestCommit'
                and e['node']['commit']['oid']==hop['head']]
        _need(len(listed)==1,'canonical merge commit missing/duplicated in timeline')
        allowed_nodes.add(listed[0][1]['id'])
        ii,inn=comment(hop['intent_id']);di,dn=comment(hop['done_id'])
        _need(di==hop['done_index'] and previous_done_index<ii<di<bound,
              'canonical intent/done/cleanup order differs')
        im=list(_INTENT.finditer(_protocol_body(inn['body'])))
        dm=list(_DONE.finditer(_protocol_body(dn['body'])))
        _need(len(im)==len(dm)==1,'ambiguous canonical intent/done')
        ig=im[0].groups();dg=dm[0].groups()
        _need((ig[0],ig[1],ig[5],ig[6])==(hop['source'],hop['base'],ship_id,hop['previous'])
              and dg==(str(hop['intent_id']),hop['source'],hop['head'],hop['base'],hop['previous'],ship_id),
              'canonical intent/done binding differs')
        # GitHub orders merge commits by commit date. Remove only graph-proven
        # future to-commit edges while re-reading this source's complete proof.
        # Unrelated lifecycle edges and all fields of every server edge stay.
        future=set(heads[n:])
        prefix=[e for e in edges[:ii] if not (e['node']['__typename']=='PullRequestCommit'
                and e['node']['commit']['oid'] in future)]
        source_info=pp.epoch(dict(snapshot,headRefOid=hop['source'],edges=prefix),trusted)
        pp.validate_epoch_safety(source_info,trusted)
        source_proof=pp.proof(source_info,hop['source'],trusted)
        _need(source_proof and source_proof['outcome']=='reviewed'
              and (source_proof['review_id'],source_proof['claim_id'],source_proof['completion_id'])
                  ==tuple(map(int,ig[2:5])),'source has no exact committed canonical positive proof')
        sr,sc,sf=canonical_proof(source_proof)
        _need(previous_done_index<sr<sc<sf<ii,'source proof did not complete between previous done and intent')
        intent_time,done_time=_time(inn['createdAt']),_time(dn['createdAt'])
        _need(intent_time<=done_time,'intent/done timestamps differ from server ordering')
        if previous_done_time is not None:
            _need(previous_done_time<=_time(edges[sr]['node']['createdAt']),
                  'next source review predates its canonical merge done')
        if n==0:
            source_anchor=next(i for i,e in enumerate(edges) if e['node']['id']==source_info['anchor_id'])
        marker_nodes.update((inn['id'],dn['id']))
        facts.append({'source':hop['source'],'head':hop['head'],'base':hop['base'],
                      'intent_id':hop['intent_id'],'done_id':hop['done_id'],
                      'previous':hop['previous'],'source_proof':source_proof,'tree':obj['tree']['sha']})
        previous_done_index=di;previous_done_time=done_time
    _need(previous_done_index<current_indices[0]
          and previous_done_time<=_time(edges[current_indices[0]]['node']['createdAt']),
          'current review does not follow final canonical done')
    allowed_types={'PullRequestCommit','IssueComment','LabeledEvent','UnlabeledEvent'}
    for e in edges[source_anchor+1:]:
        node=e['node'];kind=node['__typename']
        _need(kind in allowed_types,'unsupported lifecycle event after original source')
        if kind=='PullRequestCommit':
            _need(node['id'] in allowed_nodes,'extra HEAD commit outside canonical chain')
        if kind=='IssueComment':
            _need('lastEditedAt' in node and node['lastEditedAt'] is None,'edited/incomplete full history')
            _time(node['createdAt'])
            _need(not (node.get('author',{}).get('login')==trusted
                       and has_review_again(node.get('body') or '')),
                  'trusted review-again invalidates original continuous ship')
            if re.search(r'(?m)^<!-- pp:base-sync-[^\n]* -->$',
                         _protocol_body(node.get('body') or '')):
                _need(node['id'] in marker_nodes,'extra/untrusted canonical lineage marker')
    transitions=[(i,e['node']) for i,e in enumerate(edges)
                 if e['node']['__typename'] in {'LabeledEvent','UnlabeledEvent'}
                 and e['node'].get('label',{}).get('name')=='ship']
    _need(transitions,'original trusted ship is absent')
    ship_index,ship=transitions[-1]
    _need(ship['id']==ship_id and ship['__typename']=='LabeledEvent'
          and ship.get('actor',{}).get('login')==trusted
          and source_anchor<ship_index<by_comment[chain[0]['intent_id']][0]
          and _time(ship['createdAt'])<=_time(by_comment[chain[0]['intent_id']][1]['createdAt']),
          'original trusted ship revoked/relabelled or outside source epoch')
    return {'verdict':'proven','protocol':'current-review-continuous-ship-chain-v1',
            'repository':repo,'number':pr['number'],'head':head,'main':main,
            'current_proof':current,'current_proof_sha256':pp.digest(current),
            'chain':facts,'original_ship_event':ship_id,'snapshot_sha256':pp.digest(snapshot),
            'mutation_authorized':False,'CI_identity_policy_budget_election_still_required':True}
