"""Verified legacy/typed review depth, not checker counters or mutation authority."""
from __future__ import annotations

from . import project_pipeline as pp
from .pipeline_errors import PipelineError
from .review_routing_baseline import _edges
from .review_routing_lease import decimal_id
from .review_routing_proof import reduce
from .review_routing_wire import records


def verified_depth(snapshot,*,trusted):
    """Count canonical completed transactions over the whole lifetime PR.

    Old regex completion references alone cannot make the next audit a third
    round. Unknown/deleted/edited proof history refuses instead of resetting
    depth. Existing legacy serialization and its signed digest stay untouched.
    """
    edges=_edges(snapshot)
    by_id={};positions={}
    for index,edge in enumerate(edges):
        node=edge['node']
        if node['__typename']=='CommentDeletedEvent':
            raise PipelineError('routing review depth has deleted/unknown proof history')
        if node['__typename']!='IssueComment':continue
        value=node.get('fullDatabaseId')
        ident=decimal_id(str(value) if type(value) is int else value)
        if ident in by_id:raise PipelineError('routing review depth has duplicate server comment IDs')
        by_id[ident]=node;positions[ident]=index
        if ((node.get('author') or {}).get('login')==trusted and node.get('lastEditedAt') is not None):
            raise PipelineError('routing review depth has edited trusted history')
    legacy=[];seen_reviews=set()
    for ident,node in by_id.items():
        if (node.get('author') or {}).get('login')!=trusted:continue
        match=pp.COMPLETE.fullmatch((node.get('body') or '').strip())
        if match is None:continue
        head,review_id,claim_id,epoch=match.groups()
        decimal_id(review_id);decimal_id(claim_id)
        if review_id not in by_id or claim_id not in by_id:
            raise PipelineError('routing legacy completion references missing proof history')
        # Reconstruct the exact server prefix at this completion. Reusing the
        # current HEAD/epoch for older rounds would silently discard proofs.
        index=positions[ident]
        prefix=dict(snapshot,headRefOid=head,edges=edges[:index+1])
        info=pp.epoch(prefix,trusted)
        pp.validate_epoch_safety(info,trusted)
        established=pp.proof(info,head,trusted)
        if (info['hash']!=epoch or established is None
                or str(established['review_id'])!=review_id
                or str(established['claim_id'])!=claim_id):
            raise PipelineError('routing legacy completion is not its canonical claim-bound proof')
        if review_id not in seen_reviews:
            seen_reviews.add(review_id)
            legacy.append({'kind':'legacy','completion_id':str(established['completion_id']),
                           'review_id':review_id,'head':head,'epoch':epoch,
                           'outcome':established['outcome'],'index':positions[str(established['completion_id'])]})
    history,deleted=records(snapshot,trusted=trusted,expected_pr=snapshot['pr_key'])
    ledger=reduce(history,deleted_at=deleted,max_attempts=16)
    typed=[{'kind':'typed','completion_id':proof.completion.id,'publication_id':proof.publication.id,
            'head':proof.source['head'],'epoch':proof.source['epoch'],'outcome':proof.outcome,
            'index':positions[proof.completion.id]} for proof in ledger.proofs]
    completed=sorted(legacy+typed,key=lambda item:item['index'])
    # A typed root's declared depth is immutable reviewed context, and must
    # agree with independently verified transactions preceding that root.
    for proof in ledger.proofs:
        prior=sum(item['index']<positions[proof.publication.id] for item in completed)
        if proof.publication.payload['depth']!=prior:
            raise PipelineError('routing typed publication depth contradicts verified history')
    return {'depth':len(completed),'completed':completed,'ledger':ledger,'mutation_authorized':False}
