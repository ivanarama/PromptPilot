"""Immutable audit-prefix binding, not authentication or mutation authority.

The public stage must separately prove actor, current identity/epoch, paired
policy, canonical proof/ownership and TTL. A matching prefix does not grant
permission to publish. Only this publication's immutable suffix is tolerated.
"""
from __future__ import annotations

import hashlib
import json
import re

from .pipeline_errors import PipelineError
from .review_routing_wire import audit_binding, context, hash64, records, parse
from .review_routing_lease import timestamp


MAX_BYTES=8388608


def _hash(value):
    digest=hashlib.sha256();size=0
    encoder=json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    try:
        for chunk in encoder.iterencode(value):
            raw=chunk.encode('utf-8');size+=len(raw)
            if size+1>MAX_BYTES:
                raise PipelineError('routing audit baseline exceeds bounded size')
            digest.update(raw)
    except (TypeError,ValueError,UnicodeError,RecursionError) as exc:
        raise PipelineError('routing audit baseline is not bounded canonical JSON') from exc
    digest.update(b'\n')
    return digest.hexdigest()


def _edges(snapshot):
    if not isinstance(snapshot,dict) or not isinstance(snapshot.get('edges'),list):
        raise PipelineError('routing audit baseline needs the complete typed snapshot')
    cursors=set();ids=set()
    for edge in snapshot['edges']:
        try:
            cursor=edge['cursor'];node=edge['node'];ident=node['id']
        except (KeyError,TypeError) as exc:
            raise PipelineError('routing audit boundary has incomplete server identity') from exc
        if (not isinstance(cursor,str) or not cursor or cursor in cursors
                or not isinstance(ident,str) or not ident or ident in ids):
            raise PipelineError('routing audit boundary has missing/duplicate server identity')
        cursors.add(cursor);ids.add(ident)
        if node.get('__typename')=='IssueComment':
            body=node.get('body')
            if not isinstance(body,str):
                raise PipelineError('routing comment body is unavailable')
            if 'author' not in node:
                raise PipelineError('routing comment actor projection is unavailable')
            author=node['author']
            if author is None:
                if re.search(r'(^|[^A-Za-z0-9_])pp:',body):
                    raise PipelineError('routing protocol comment actor is unresolved')
            elif (not isinstance(author,dict) or not isinstance(author.get('login'),str)
                  or not author['login'] or author['login']!=author['login'].strip()
                  or any(ord(char)<32 or ord(char)==127 for char in author['login'])):
                raise PipelineError('routing comment actor projection is malformed')
            timestamp(node.get('createdAt'))
            if 'lastEditedAt' not in node:
                raise PipelineError('routing comment edit status is unavailable')
            if node['lastEditedAt'] is not None:
                timestamp(node['lastEditedAt'])
    return snapshot['edges']


def _projection(snapshot,edges):
    result=dict(snapshot,edges=edges)
    # Normal base advancement is checked separately by the public gate. It
    # must not invalidate reviewed content; timeline ABA events remain hashed.
    result.pop('baseRefOid',None)
    result.pop('updatedAt',None)
    return result


def capture(snapshot,*,retain_history=False):
    edges=_edges(snapshot)
    boundary=edges[-1] if edges else None
    binding={'snapshot_sha256':_hash(_projection(snapshot,edges)),
             'last_edge_cursor':boundary['cursor'] if boundary else None,
             'last_edge_node_id':boundary['node']['id'] if boundary else None,
             'base_ref_oid':snapshot.get('baseRefOid')}
    if retain_history:
        header=_projection(snapshot,[]);header.pop('edges')
        binding['history']={'protocol':'audit-history-v1','header':header,
                            'edges':[{'cursor':edge['cursor'],'node_id':edge['node']['id']} for edge in edges]}
    return audit_binding(binding)


def historical(snapshot,binding,*,identity,author,publication_index,publication_context,trusted):
    """Recover actual retained audit material, never invent old metadata.

    Only later commit-date insertions may be absent from the retained manifest.
    Missing or edited original nodes and other intervening events stay unknown.
    Live publication still uses verify() with its strict complete prefix fence.
    """
    audit_binding(binding)
    edges=_edges(snapshot)
    history=binding.get('history')
    if history is None:
        # Development records predating retained metadata can only be checked
        # when the original projection is still available without guessing.
        from .pr_identity import PRIdentityV1
        original=dict(snapshot,identity=identity,headRefOid=identity['head_sha'],
                      identity_sha256=PRIdentityV1(**identity).sha256,
                      edges=edges[:publication_index])
    else:
        header=history['header']
        if (header['identity']!=identity or header['author']!=author
                or header['headRefOid']!=identity['head_sha']
                or header['pr_key']!=snapshot['pr_key']):
            raise PipelineError('retained audit header differs from its publication')
        from .pr_identity import PRIdentityV1
        if header['identity_sha256']!=PRIdentityV1(**identity).sha256:
            raise PipelineError('retained audit identity digest differs')
        manifest={(edge['cursor'],edge['node_id']) for edge in history['edges']}
        retained=[]
        for edge in edges[:publication_index]:
            key=edge['cursor'],edge['node']['id']
            if key in manifest:retained.append(edge)
            elif edge['node']['__typename']!='PullRequestCommit':
                node=edge['node']
                data=(parse(node.get('body') or '') if node['__typename']=='IssueComment'
                      and (node.get('author') or {}).get('login')==trusted
                      and 'pp:review-routing-' in (node.get('body') or '') else {})
                bound=(context(data) if data.get('kind')=='publication' else data.get('context_sha256'))
                own=(data.get('kind') in {'publication','claim','completion'}
                     or data.get('kind')=='lease' and data['purpose']=='review-publication')
                if not own or bound!=publication_context:
                    raise PipelineError('historical audit has an unexplained independent event')
        if [{'cursor':edge['cursor'],'node_id':edge['node']['id']} for edge in retained]!=history['edges']:
            raise PipelineError('historical audit original nodes are missing or reordered')
        original=dict(header,edges=retained,baseRefOid=binding['base_ref_oid'])
    observed=capture(original)
    if any(observed[key]!=binding[key] for key in
           ('snapshot_sha256','last_edge_cursor','last_edge_node_id')):
        raise PipelineError('historical publication audit prefix is unavailable or changed')
    return original


def verify(snapshot,binding,*,publication_context,trusted):
    """Check the signed prefix and only known own-publication suffix records.

    Canonical reducer/owner gates remain mandatory after this structural check.
    Never erase arbitrary comments, labels or pp:* records to make a hash fit.
    """
    audit_binding(binding)
    hash64(publication_context)
    edges=_edges(snapshot)
    boundary=binding['last_edge_cursor']
    end=0
    if boundary is not None:
        matches=[index for index,edge in enumerate(edges)
                 if (edge['cursor'],edge['node']['id'])==
                    (boundary,binding['last_edge_node_id'])]
        if len(matches)!=1:
            raise PipelineError('routing signed audit boundary is missing or changed')
        end=matches[0]+1
    if _hash(_projection(snapshot,edges[:end]))!=binding['snapshot_sha256']:
        raise PipelineError('routing signed audit prefix changed')
    suffix=edges[end:]
    history,deleted=records({'edges':suffix},trusted=trusted,expected_pr=snapshot.get('pr_key'))
    if deleted or len(history)!=len(suffix):
        raise PipelineError('routing audit suffix contains an independent event/comment')
    for record in history:
        data=record.payload
        if data['kind']=='publication':
            bound=context(data)
        elif data['kind'] in {'lease','claim','completion'}:
            bound=data['context_sha256']
            if data['kind']=='lease' and data['purpose']!='review-publication':
                raise PipelineError('routing audit suffix contains another lease purpose')
        else:
            raise PipelineError('routing audit suffix contains another operation')
        if bound!=publication_context:
            raise PipelineError('routing audit suffix differs from its publication context')
    return {'verified':True,'mutation_authorized':False}
