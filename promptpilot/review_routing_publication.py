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
        if not callable(guard):
            raise PipelineError('routing publication requires a fresh external stage guard')
        self.seed=parse(render(payload))
        if self.seed['kind']!='publication':
            raise PipelineError('routing publication requires a full publication root')
        self.owner=self.seed['owner']
        self.context=context(self.seed)
        self.trusted=config['trusted_account']
        self.journal=journal
        self.guard=guard
        self.transport=Transport(gh,config,journal,self._guarded_snapshot)
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def _state(self):
        snapshot=self.transport._snapshot(self.seed)
        return self._decode_state(snapshot)

    def _decode_state(self,snapshot):
        history,deleted=records(snapshot,trusted=self.trusted,expected_pr=self.seed['pr'])
        # Historical slots survive a lower *current* limit. Current admission
        # is a distinct stage-guard decision; the global wire bound is16.
        ledger=reduce(history,deleted_at=deleted,max_attempts=16)
        return history,ledger

    def _guarded_snapshot(self,payload,*,write=False):
        snapshot=self.guard(payload,write=write)
        if not isinstance(snapshot,dict) or snapshot.get('pr_key')!=payload['pr']:
            raise PipelineError('routing stage guard returned another PR identity')
        if write:
            history,ledger=self._decode_state(snapshot)
            body=render(payload)
            if not any(render(item.payload)==body for item in history):
                # A known exact receipt is recovered by Transport, not posted.
                # Every genuinely new root must pass semantic admission on the
                # *last* coherent snapshot, after durable journal preparation.
                roots=[item for item in history if item.payload['kind']=='publication'
                       and (item.payload['identity']['head_sha'],item.payload['epoch'])==
                           (self.seed['identity']['head_sha'],self.seed['epoch'])]
                if payload['kind']=='publication':
                    if roots:
                        raise PipelineError('canonical publication root appeared before POST; recover its owner')
                    self._admit(ledger)
                else:
                    self._phase_admit(payload,history,ledger,roots)
        return snapshot

    def _phase_admit(self,payload,history,ledger,roots):
        if not roots:
            raise PipelineError('publication phase has no canonical root')
        root=min(roots,key=lambda item:item.order)
        if (context(root.payload)!=self.context or payload['context_sha256']!=self.context
                or payload['owner']!=self.owner):
            raise PipelineError('publication phase differs from its canonical context/owner')
        if any(proof.publication.id==root.id for proof in ledger.proofs):
            raise PipelineError('canonical publication already completed; do not POST another phase')
        view=resolve(_lease_nodes(history,root,purpose='review-publication'),
                     context_sha256=self.context,purpose='review-publication',now=self.clock())
        if payload['kind']=='lease':
            if payload['purpose']!='review-publication' or payload['root']!=root.id or payload['previous']!=view.active.id:
                raise PipelineError('publication renewal/takeover parent changed')
            now=self.clock()
            eligible=(self.owner==view.active.owner and now<view.expires_at
                      and view.expires_at-now<RENEW_WINDOW
                      or self.owner!=view.active.owner and now>=view.expires_at)
            if not eligible:
                raise PipelineError('publication renewal/takeover is no longer eligible')
            return
        if payload['kind'] not in {'claim','completion'} or payload['publication_id']!=root.id:
            raise PipelineError('unsupported publication phase or canonical root binding')
        claims=[item for item in history if item.payload['kind']=='claim'
                and item.payload['publication_id']==root.id and item.id not in ledger.ignored_ids]
        if payload['kind']=='claim' and claims:
            raise PipelineError('canonical claim appeared before POST; recover it')
        if payload['kind']=='completion' and (not claims or payload['claim_id']!=min(claims,key=lambda item:item.order).id):
            raise PipelineError('completion does not bind the fresh canonical earliest claim')
        view.require_owner(returned_id=payload['lease_id'],owner=self.owner,now=self.clock())

    def _publish(self,payload):
        return self.transport.publish(payload,owner=payload['owner'],
                                      phase=payload['kind']+':'+sha(payload))

    def _recover(self):
        for request in self.journal.pending_for(self.seed['pr']):
            # The saved request already exists, so Transport only reads and
            # confirms it. An invisible/conflicting result remains a barrier.
            self.transport.publish(parse(request['body']),owner=request['owner'],phase=request['phase'])

    def _admit(self,ledger):
        if ledger.resuming_publication_id is not None:
            raise PipelineError('incomplete resuming publication already consumed this engineering grant')
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
