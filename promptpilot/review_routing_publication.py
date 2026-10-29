"""Versioned immutable REVIEW publication/recovery coordinator.

This internal coordinator is not a public route or an authorization gate.
The required stage guard must freshly authenticate the signed request, actor,
coherent identity/epoch, paired readers, effective policy, canonical ownership
and TTL before every write, including after journal I/O. No labels, legacy
success markers, ship, executor or tool are emitted here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .pipeline_errors import PipelineError
from .review_routing_lease import RENEW_WINDOW, resolve
from .review_routing_proof import _lease_nodes, reduce
from .review_routing_transport import Transport
from .review_routing_wire import context, parse, records, render, sha


class Publication:
    def __init__(self,gh,config,journal,guard,payload,*,clock=None):
        self.seed=parse(render(payload))
        if self.seed['kind']!='publication':
            raise PipelineError('routing publication requires a full publication root')
        self.owner=self.seed['owner']
        self.context=context(self.seed)
        self.trusted=config['trusted_account']
        self.journal=journal
        self.guard=guard
        self.transport=Transport(gh,config,journal,guard)
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def _state(self):
        snapshot=self.transport._snapshot(self.seed)
        history,deleted=records(snapshot,trusted=self.trusted,expected_pr=self.seed['pr'])
        # Historical slots survive a lower *current* limit. Current admission
        # is a distinct stage-guard decision; the global wire bound is16.
        ledger=reduce(history,deleted_at=deleted,max_attempts=16)
        return history,ledger

    def _publish(self,payload):
        return self.transport.publish(payload,owner=payload['owner'],
                                      phase=payload['kind']+':'+sha(payload))

    def _recover(self):
        for request in self.journal.pending_for(self.seed['pr']):
            # The saved request already exists, so Transport only reads and
            # confirms it. An invisible/conflicting result remains a barrier.
            self.transport.publish(parse(request['body']),owner=request['owner'],phase=request['phase'])

    def _admit(self,ledger):
        if self.seed['budget_used']!=ledger.budget_used:
            raise PipelineError('routing publication has a stale lifetime budget')
        resume=self.seed['resume_id']
        if ledger.parking_id is not None:
            grant=next((item for item in ledger.resumes if item.id==ledger.resume_id),None)
            if (grant is None or resume!=grant.id
                    or grant.payload['head']!=self.seed['identity']['head_sha']):
                raise PipelineError('routing publication requires the exact current engineering resume')
        elif resume is not None:
            raise PipelineError('routing publication cannot reuse an inactive engineering resume')
        if ledger.attempts:
            previous=ledger.attempts[-1]
            done=ledger.attempt_phases(previous).get('fix-done')
            if done is None:
                raise PipelineError('routing publication must wait for completed post-push provenance')
            if done.payload['details']['new_head']!=self.seed['identity']['head_sha'] and resume is None:
                raise PipelineError('routing publication HEAD differs from completed FIX')

    def run(self,*,max_steps=12):
        if type(max_steps) is not int or not 1<=max_steps<=32:
            raise PipelineError('invalid bounded publication action limit')
        self._recover()
        for _step in range(max_steps):
            history,ledger=self._state()
            roots=[item for item in history if item.payload['kind']=='publication'
                   and (item.payload['identity']['head_sha'],item.payload['epoch'])==
                       (self.seed['identity']['head_sha'],self.seed['epoch'])]
            if not roots:
                self._admit(ledger)
                self._publish(self.seed)
                continue
            root=min(roots,key=lambda item:item.order)
            if context(root.payload)!=self.context:
                raise PipelineError('routing publication recovery has another immutable report/context')
            completed=next((proof for proof in ledger.proofs if proof.publication.id==root.id),None)
            if completed is not None:
                return {'action':'completed','outcome':completed.outcome,'source':completed.source,
                        'mutation_authorized':False}
            now=self.clock()
            view=resolve(_lease_nodes(history,root,purpose='review-publication'),
                         context_sha256=self.context,purpose='review-publication',now=now)
            common={'protocol':self.seed['protocol'],'pr':self.seed['pr'],'owner':self.owner,
                    'context_sha256':self.context}
            if now>=view.expires_at:
                if self.owner==view.active.owner:
                    return {'action':'wait','reason':'expired owner needs a new signed request UUID',
                            'root':root.id,'mutation_authorized':False}
                self._publish(dict(common,kind='lease',purpose='review-publication',
                                   root=root.id,previous=view.active.id))
                continue
            if self.owner!=view.active.owner:
                return {'action':'wait','reason':'canonical publication has another live owner',
                        'root':root.id,'mutation_authorized':False}
            view.require_owner(returned_id=view.active.id,owner=self.owner,now=now)
            if view.expires_at-now<RENEW_WINDOW:
                self._publish(dict(common,kind='lease',purpose='review-publication',
                                   root=root.id,previous=view.active.id))
                continue
            claims=[item for item in history if item.payload['kind']=='claim'
                    and item.payload['publication_id']==root.id and item.id not in ledger.ignored_ids]
            if not claims:
                self._publish(dict(common,kind='claim',publication_id=root.id,lease_id=view.active.id))
                continue
            claim=min(claims,key=lambda item:item.order)
            self._publish(dict(common,kind='completion',publication_id=root.id,
                               claim_id=claim.id,lease_id=view.active.id))
        raise PipelineError('routing publication action limit reached; recover known phases before continuing')
