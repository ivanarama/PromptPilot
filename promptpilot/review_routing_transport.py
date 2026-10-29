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

    def _snapshot(self,payload):
        snapshot=self.guard(payload)
        if not isinstance(snapshot,dict) or snapshot.get('pr_key')!=payload['pr']:
            raise PipelineError('routing stage guard returned another PR identity')
        return snapshot

    def _exact(self,snapshot,payload,body,*,returned_id=None):
        history,_deleted=records(snapshot,trusted=self.trusted,expected_pr=payload['pr'])
        matches=[record for record in history if render(record.payload)==body]
        if len(matches)!=1:
            raise PipelineError('routing POST has no unique authenticated exact-body receipt; do not repeat')
        record=matches[0]
        if returned_id is not None and record.id!=returned_id:
            raise PipelineError('routing POST returned ID differs from its exact server read-back')
        return record

    def publish(self,payload,*,owner,phase):
        """One network attempt, or read-only recovery of its exact receipt.

        A failed gate after durable preparation can leave an unresolved local
        intent. That conservative barrier is deliberate: another process must
        not infer from absent visibility that a POST was never accepted.
        """
        body=render(payload)  # Size/full encoding validated before I/O.
        target=payload['pr']['pr_number']
        snapshot=self._snapshot(payload)
        existing=self.journal.get(owner=owner,phase=phase)
        if existing is not None:
            if (existing['repository'],existing['pr_number'],existing['body'])!=(self.repository,target,body):
                raise PipelineError('routing recovery differs from its prepared request')
            expected_id=existing['returned_id']
            record=self._exact(snapshot,payload,body,returned_id=expected_id)
            self.journal.confirm(owner=owner,phase=phase,returned_id=record.id,body=body)
            return record
        if not self.journal.prepare(owner=owner,phase=phase,repository=self.repository,number=target,body=body):
            # Another process could have completed between get and prepare.
            # Repeat only the guarded READ path, never a network POST.
            return self.publish(payload,owner=owner,phase=phase)
        # SQLite lock waits/encoding/identity reads may have crossed expiry or
        # a hold. The final stage guard runs after write-ahead commit.
        self._snapshot(payload)
        try:
            response=self.gh.json('api','-X','POST',f'repos/{self.repository}/issues/{target}/comments',
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
