"""Fresh authenticated guard for the internal typed REVIEW publication path.

The public CLI/executor bridge is not enabled by importing this module. Each
write must pass this guard AND Publication's canonical phase admission, then
the final-before-POST callback after reducer/matching/SQLite work.
"""
from __future__ import annotations

from datetime import datetime,timezone
import copy

from . import project_pipeline as pp
from . import review_routing_request as requests
from .pipeline_errors import PipelineError
from .review_routing_baseline import verify
from .review_routing_lease import RENEW_WINDOW,resolve
from .review_routing_policy import preflight
from .review_routing_proof import _lease_nodes,reduce
from .review_routing_snapshot import stable_timeline
from .review_routing_wire import context,parse,records,render,sha


class StageGuard:
    def __init__(self,gh,config_path,request_token,payload,*,clock=None):
        if not isinstance(config_path,str) or not config_path:
            raise PipelineError('typed REVIEW requires its effective configuration path')
        self.gh=gh;self.config_path=config_path;self.token=request_token
        self.request=requests.decode(request_token)
        self.seed=parse(render(payload))
        if self.seed['kind']!='publication':
            raise PipelineError('typed REVIEW guard requires a complete publication root')
        self.context=context(self.seed)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self._ticket=None
        request=self.request
        bindings={'pr':'pr_key','identity':'identity','author':'author','epoch':'epoch',
                  'anchor':'anchor','audit':'audit','depth':'depth','budget_used':'budget_used',
                  'resume_id':'resume_id'}
        if any(self.seed[name]!=request[bound] for name,bound in bindings.items()):
            raise PipelineError('typed REVIEW publication differs from its signed audit scope')
        if self.seed['owner']!=request['owner_uuid']:
            raise PipelineError('typed REVIEW publication uses another request UUID')
        recovery=request['recovery']
        if recovery is None and self.seed['max_attempts']!=request['current_limit']:
            raise PipelineError('typed REVIEW new audit uses another current attempt limit')
        if recovery is not None and recovery['context_sha256']!=self.context:
            raise PipelineError('typed REVIEW recovery differs from its canonical context')

    def _phase(self,payload,*,write):
        value=parse(render(payload))
        if value['pr']!=self.request['pr_key']:
            raise PipelineError('typed REVIEW phase belongs to another PR')
        if value['kind']=='publication':
            if context(value)!=self.context:
                raise PipelineError('typed REVIEW phase changes its immutable report/context')
        elif (value['kind'] not in {'claim','completion','lease'}
              or value['context_sha256']!=self.context
              or value['kind']=='lease' and value['purpose']!='review-publication'):
            raise PipelineError('typed REVIEW request cannot publish another operation')
        if write and value['owner']!=self.request['owner_uuid']:
            raise PipelineError('typed REVIEW phase borrows another owner UUID')
        return value

    def _base(self,snapshot):
        before=self.request['audit']['base_ref_oid'];after=snapshot['baseRefOid']
        if before==after:return
        # Normal forward movement of main does not change reviewed content.
        # A divergent/replaced base is not silently erased from the binding.
        comparison=self.gh.json('api',f'repos/{self.request["repository"]}/compare/{before}...{after}')
        if (not isinstance(comparison,dict) or comparison.get('status')!='ahead'
                or (comparison.get('base_commit') or {}).get('sha')!=before
                or (comparison.get('merge_base_commit') or {}).get('sha')!=before):
            raise PipelineError('typed REVIEW integration base did not advance from its signed base')

    def _historical_receipt(self,snapshot,value,config):
        """Observe an exact old receipt without granting another WRITE ticket.

        A later HEAD/hold/close must not turn a visible accepted POST into an
        unresolved local send. This exception requires an actual unique body
        in a fully verified lifetime history; invisible requests still refuse.
        """
        from .review_routing_history import verified_depth

        request=self.request
        current=dict(snapshot['identity']);original=dict(request['identity'])
        current.pop('head_sha');original.pop('head_sha')
        if (current!=original or snapshot['author']!=request['author']
                or snapshot['pr_key']!=request['pr_key'] or not snapshot['labelsComplete']):
            raise PipelineError('typed REVIEW old receipt belongs to a changed PR identity/author')
        verified_depth(snapshot,trusted=config['trusted_account'])
        history,deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=request['pr_key'])
        if deleted or sum(render(record.payload)==render(value) for record in history)!=1:
            raise PipelineError('typed REVIEW historical read requires a unique exact existing receipt')

    def __call__(self,payload,*,write=False):
        return self._check(payload,write=write)

    def _check(self,payload,*,write=False,_after_waits=False):
        self._ticket=None
        value=self._phase(payload,write=write)
        # Reauthenticate the original signed scope and reload the effective
        # config on EVERY phase. run_health may fast-forward/reload it again.
        request=requests.decode(self.token)
        if request!=self.request:
            raise PipelineError('typed REVIEW signed request changed')
        config=pp.load_config(self.config_path)
        health=pp.run_health(config,config_path=self.config_path)
        policy=preflight(config,health)
        requests.require_current(request,config,write=False)
        if (policy.get('enabled') is not True
                or policy['consumer_sha256']!=request['reader_sha256']
                or policy['current_limit']!=request['current_limit']):
            raise PipelineError('typed REVIEW paired policy changed or was disabled')
        if write:
            from .fallback_handoff import validate_health
            validate_health(health)
            if not pp.content_review_elected(health,{'number':request['number'],'head':request['head'],
                                                      'review_depth':request['depth']}):
                raise PipelineError('typed REVIEW target left the current exact content allowlist')
        snapshot=stable_timeline(self.gh,config,request['number'])
        try:
            pp.review_gate(snapshot,config,request)
            if (snapshot['identity']!=request['identity'] or snapshot['author']!=request['author']
                    or snapshot['identity_sha256']!=request['identity_sha256']):
                raise PipelineError('typed REVIEW current full identity/author changed')
            verify(snapshot,request['audit'],publication_context=self.context,trusted=config['trusted_account'])
            self._base(snapshot)
        except PipelineError as original:
            if write:raise
            try:self._historical_receipt(snapshot,value,config)
            except PipelineError:raise original
        history,deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=request['pr_key'])
        reduce(history,deleted_at=deleted,max_attempts=16)
        roots=[item for item in history if item.payload['kind']=='publication'
               and (item.payload['identity']['head_sha'],item.payload['epoch'])==(request['head'],request['epoch'])]
        root=min(roots,key=lambda item:item.order) if roots else None
        if root is not None and context(root.payload)!=self.context:
            raise PipelineError('typed REVIEW canonical publication has another context')
        recovery=request['recovery']
        if recovery is not None and (root is None or root.id!=recovery['publication_id']):
            raise PipelineError('typed REVIEW recovery root is missing or no longer canonical')
        # Mutation phases never substitute server/public UUIDs for the signed
        # local request. Pure read-back may inspect the exact old-owner receipt.
        view=None
        if root is not None:
            view=resolve(_lease_nodes(history,root,purpose='review-publication'),
                         context_sha256=self.context,purpose='review-publication',now=self.clock())
        pp.ensure_identity(self.gh,config)
        renewed=pp.renew_lease_target_reservation(request,config) if write and not _after_waits else None
        requests.require_current(request,config,write=write)
        if write:
            self._ticket={'snapshot':snapshot,'snapshot_sha256':sha(snapshot),'body':render(value),
                          'health':copy.deepcopy({name:health[name] for name in
                              ['state','findings','review_routing_contract']}),
                          'view':view,'reservation':renewed}
        return snapshot

    def final_before_post(self,payload,snapshot):
        # One-use ticket from this exact fresh WRITE read. All parsing and
        # matching are before the final clock read; no DB/network work follows.
        ticket=self._ticket;self._ticket=None
        if (ticket is None or snapshot is not ticket['snapshot']
                or sha(snapshot)!=ticket['snapshot_sha256'] or render(payload)!=ticket['body']):
            raise PipelineError('typed REVIEW final guard has no matching fresh write snapshot')
        # Publication's journal may have waited after the initial guard. Renew
        # first, then obtain another authenticated actor/checker/history/lease
        # view without moving that write wait behind the refreshed snapshot.
        renewed=pp.renew_lease_target_reservation(self.request,pp.load_config(self.config_path))
        fresh=self._check(payload,write=True,_after_waits=True)
        ticket=self._ticket;self._ticket=None
        ticket['reservation']=renewed
        from .review_routing_publication import Publication
        Publication(self.gh,pp.load_config(self.config_path),None,self,self.seed,
                    clock=self.clock).require_admission(payload,fresh,require_new=True)
        requests.verify_local(self.request)
        # Actor reads, reservation renewal and semantic/exact matching may
        # have taken arbitrarily long. Re-read the LOCAL effective config and
        # all four actual consumer trees after them. The previously complete
        # authenticated checker contract is comparison evidence, not cached
        # permission. All this I/O remains before the last expiry check.
        config=pp.load_config(self.config_path)
        policy=preflight(config,ticket['health'])
        if (policy.get('enabled') is not True
                or policy['consumer_sha256']!=self.request['reader_sha256']
                or policy['current_limit']!=self.request['current_limit']):
            raise PipelineError('typed REVIEW final paired policy changed or was disabled')
        renewed=ticket['reservation']
        expiry=None
        if renewed is not None:
            try:expiry=datetime.fromisoformat(renewed['expires_at'])
            except (KeyError,TypeError,ValueError) as exc:
                raise PipelineError('typed REVIEW renewed task reservation expiry is invalid') from exc
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise PipelineError('typed REVIEW renewed task reservation expiry is not timezone aware')
        requests.require_current(self.request,config,write=True)
        now=self.clock()
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise PipelineError('typed REVIEW final clock must be timezone aware')
        current=int(now.timestamp())
        if self.request['issued_at']>current+60 or self.request['expires_at']<=current:
            raise PipelineError('typed REVIEW request expired or is not yet valid before POST')
        if expiry is not None and now>=expiry:
            raise PipelineError('typed REVIEW task reservation expired before POST')
        view=ticket['view']
        if payload['kind']=='publication':
            if view is not None:raise PipelineError('typed REVIEW canonical root already exists')
        elif view is None:
            raise PipelineError('typed REVIEW phase has no canonical server lease')
        elif payload['kind']=='lease':
            if payload['previous']!=view.active.id or payload['root']!=view.root.id:
                raise PipelineError('typed REVIEW server lease parent changed')
            same=self.request['owner_uuid']==view.active.owner
            if not (same and view.active.time<=now<view.expires_at and view.expires_at-now<RENEW_WINDOW
                    or not same and now>=view.expires_at):
                raise PipelineError('typed REVIEW renewal/takeover no longer eligible before POST')
        else:
            view.require_owner(returned_id=payload['lease_id'],owner=self.request['owner_uuid'],now=now)
