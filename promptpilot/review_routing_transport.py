"""Write-ahead/exact-read-back transport for the future typed public routes.

The mandatory guard is supplied by the stage orchestrator. It must repeat
authenticated identity, complete coherent timeline, policy, epoch, lifecycle,
ownership and expiry gates for THIS phase. This class is not such a gate and
does not expose a CLI or dispatch an executor.
"""
from __future__ import annotations

from .pipeline_errors import PipelineError
from .review_routing_lease import decimal_id
from .review_routing_wire import records, render


class Transport:
    def __init__(self,gh,config,journal,guard):
        if not callable(guard):
            raise PipelineError('routing transport requires a fresh stage guard')
        self.gh=gh
        self.repository=config['repository']
        self.trusted=config['trusted_account']
        self.journal=journal
        self.guard=guard

    def _snapshot(self,payload,*,write=False):
        snapshot=self.guard(payload,write=write)
        if not isinstance(snapshot,dict) or snapshot.get('pr_key')!=payload['pr']:
            raise PipelineError('routing stage guard returned another PR identity')
        return snapshot

    def _matches(self,snapshot,payload,body):
        history,deleted=records(snapshot,trusted=self.trusted,expected_pr=payload['pr'])
        if deleted:
            raise PipelineError('routing deletion fence prevents receipt recovery/publication')
        return [record for record in history if render(record.payload)==body]

    def _exact(self,snapshot,payload,body,*,returned_id=None):
        matches=self._matches(snapshot,payload,body)
        if len(matches)!=1:
            raise PipelineError('routing POST has no unique authenticated exact-body receipt; do not repeat')
        record=matches[0]
        if returned_id is not None and record.id!=returned_id:
            raise PipelineError('routing POST returned ID differs from its exact server read-back')
        return record

    def publish(self,payload,*,owner,phase):
        """One network attempt, or read-only recovery of its exact receipt.

        Only the live prepare winner can record its final guard's definite
        pre-POST refusal. A crash or any failure after the network boundary
        stays unresolved; absent visibility never proves that it was not sent.
        """
        body=render(payload)  # Size/full encoding validated before I/O.
        target=payload['pr']['pr_number']
        snapshot=self._snapshot(payload)
        existing=self.journal.get(owner=owner,phase=phase)
        if existing is not None and existing['state']!='not-sent':
            if (existing['repository'],existing['pr_number'],existing['body'])!=(self.repository,target,body):
                raise PipelineError('routing recovery differs from its prepared request')
            expected_id=existing['returned_id']
            record=self._exact(snapshot,payload,body,returned_id=expected_id)
            self.journal.confirm(owner=owner,phase=phase,returned_id=record.id,body=body)
            return record
        matches=self._matches(snapshot,payload,body)
        if matches:
            # Local cache/database loss must not duplicate a surviving own
            # server phase. Bind/recheck its immutable exact receipt instead.
            record=self._exact(snapshot,payload,body)
            self.journal.prepare(owner=owner,phase=phase,repository=self.repository,number=target,body=body)
            self.journal.observe_returned_id(owner=owner,phase=phase,returned_id=record.id)
            record=self._exact(self._snapshot(payload),payload,body,returned_id=record.id)
            self.journal.confirm(owner=owner,phase=phase,returned_id=record.id,body=body)
            return record
        generation=self.journal.prepare(owner=owner,phase=phase,repository=self.repository,
                                        number=target,body=body,with_generation=True)
        if not generation:
            # Another process could have completed between get and prepare.
            # Repeat only the guarded READ path, never a network POST.
            return self.publish(payload,owner=owner,phase=phase)
        self.journal.arm(owner=owner,phase=phase,generation=generation,body=body)
        # All SQLite I/O, including the sending fence, precedes the final
        # guard. Only THIS controlled branch is known never to have sent.
        try:
            snapshot=self._snapshot(payload,write=True)
            matches=self._matches(snapshot,payload,body)
        except Exception:
            self.journal.refuse_before_post(owner=owner,phase=phase,generation=generation,body=body)
            raise
        if matches:
            # A delayed receipt can become visible during the write-ahead
            # transaction. The last fresh snapshot must suppress another POST.
            record=self._exact(snapshot,payload,body)
            self.journal.observe_returned_id(owner=owner,phase=phase,returned_id=record.id)
            record=self._exact(self._snapshot(payload),payload,body,returned_id=record.id)
            self.journal.confirm(owner=owner,phase=phase,returned_id=record.id,body=body)
            return record
        try:
            response=self.gh.json('api',f'repos/{self.repository}/issues/{target}/comments',
                                  '--method','POST','--input','-',
                                  input_value={'body':body})
            value=response['id']
            returned_id=decimal_id(str(value) if type(value) is int else value)
        except Exception:
            self.journal.ambiguous(owner=owner,phase=phase)
            # Read-only recovery can succeed even after response loss, but
            # absent/delayed visibility never triggers a second POST.
            try:
                record=self._exact(self._snapshot(payload),payload,body)
            except Exception as recovery:
                raise PipelineError('routing POST outcome is ambiguous; exact read-back required, no repeat') from recovery
            self.journal.confirm(owner=owner,phase=phase,returned_id=record.id,body=body)
            return record
        self.journal.observe_returned_id(owner=owner,phase=phase,returned_id=returned_id)
        record=self._exact(self._snapshot(payload),payload,body,returned_id=returned_id)
        self.journal.confirm(owner=owner,phase=phase,returned_id=record.id,body=body)
        return record
