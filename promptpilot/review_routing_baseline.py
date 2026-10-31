"""Immutable audit-prefix binding, not authentication or mutation authority.

The public stage must separately prove actor, current identity/epoch, paired
policy, canonical proof/ownership and TTL. A matching prefix does not grant
permission to publish. Only this publication's immutable suffix is tolerated.
"""
from __future__ import annotations

import hashlib
import json

from .pipeline_errors import PipelineError
from .review_routing_wire import audit_binding, context, hash64, records


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
    return snapshot['edges']


def _projection(snapshot,edges):
    result=dict(snapshot,edges=edges)
    # Normal base advancement is checked separately by the public gate. It
    # must not invalidate reviewed content; timeline ABA events remain hashed.
    result.pop('baseRefOid',None)
    result.pop('updatedAt',None)
    return result


def capture(snapshot):
    edges=_edges(snapshot)
    boundary=edges[-1] if edges else None
    binding={'snapshot_sha256':_hash(_projection(snapshot,edges)),
             'last_edge_cursor':boundary['cursor'] if boundary else None,
             'last_edge_node_id':boundary['node']['id'] if boundary else None,
             'base_ref_oid':snapshot.get('baseRefOid')}
    return audit_binding(binding)


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
