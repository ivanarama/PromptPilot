"""Canonical pipeline START gate; installed worker factory not connected.

Only a fresh complete peer and actual trusted source/started history can reach
OneUseStart. This module never interprets process exit or expiry as settlement.
"""
from datetime import datetime,timezone

from . import db,project_pipeline as pp
from . import review_routing_pipeline_policy as policy
from . import review_routing_start_request as requests
from .pipeline_errors import PipelineError
from .review_routing_execution import OneUseStart
from .review_routing_fix import require_task
from .review_routing_history import verified_depth
from .review_routing_lease import resolve,decimal_id,owner_id
from .review_routing_proof import _lease_nodes
from .review_routing_snapshot import stable_timeline
from .review_routing_wire import _fields,_source,_integer,context,head40,records,sha


class StartGuard:
    decode_request=staticmethod(requests.decode)
    require_current=staticmethod(requests.require_current)

    def __init__(self,gh,config_path,token,*,clock=None):
        if not isinstance(config_path,str) or not config_path:
            raise PipelineError('pipeline START requires its actual configuration path')
        self.gh,self.config_path,self.token=gh,config_path,token
        self.request=self.decode_request(token)
        self.scope=self.request['scope']
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def now(self):
        value=self.clock()
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PipelineError('pipeline START clock must be timezone aware')
        return value.astimezone(timezone.utc)

    def state(self):
        request=self.decode_request(self.token)
        if request!=self.request:raise PipelineError('pipeline START signed binding changed')
        config=pp.load_config(self.config_path)
        health=pp.run_health(config,config_path=self.config_path)
        paired=policy.require(config,request['identity'],health=health)
        self.require_current(request,config)
        if paired['consumer_sha256']!=self.scope['reader_sha256']:
            raise PipelineError('pipeline START paired readers changed')
        expected=dict(stage='fix-pr',number=self.scope['pr']['pr_number'],head=self.scope['source']['head'],
            source=self.scope['source'],attempt_id=self.scope['attempt_id'],started_id=self.scope['started_id'],
            task_id=self.scope['task_id'],attempt_uuid=self.scope['attempt_uuid'])
        candidates=health.get('technical_fix_start_candidates')
        if not isinstance(candidates,list) or len(candidates)>4096:
            raise PipelineError('pipeline START lacks its bounded complete checker allowlist')
        seen=set()
        for item in candidates:
            _fields(item,'stage number head source attempt_id started_id task_id attempt_uuid')
            _source(item['source']);head40(item['head'])
            _integer(item['number'],1,2147483647);_integer(item['task_id'],1,9223372036854775807)
            decimal_id(item['attempt_id']);decimal_id(item['started_id']);owner_id(item['attempt_uuid'])
            if (item['stage']!='fix-pr' or item['number'] in seen
                    or item['head']!=item['source']['head'] or item['attempt_id']==item['started_id']):
                raise PipelineError('pipeline START checker target is invalid or duplicated')
            seen.add(item['number'])
        if expected not in candidates:raise PipelineError('pipeline START left its exact checker allowlist')
        snapshot=stable_timeline(self.gh,config,expected['number'])
        if snapshot['identity']!=request['identity'] or snapshot['author']!=request['author']:
            raise PipelineError('pipeline START full source identity or author changed')
        ledger=verified_depth(snapshot,trusted=config['trusted_account'])['ledger']
        root=next((row for row in ledger.attempts if row.id==self.scope['attempt_id']),None)
        proof=ledger.source_proof(self.scope['source'])
        if (root is None or root.payload!=request['attempt'] or proof is None or proof is not ledger.proofs[-1]
                or proof.outcome!='technical-handoff' or ledger.parking_id is not None
                or ledger.attempt_settlement(root) is not None
                or root.payload['attempt']>paired['current_limit']):
            raise PipelineError('pipeline START has no current canonical eligible pipeline attempt')
        phases=ledger.attempt_phases(root)
        started=phases.get('started')
        if (started is None or started.id!=self.scope['started_id']
                or started.payload['owner']!=self.scope['owner']
                or started.payload['lease_id']!=request['lease_id']
                or started.payload['details']!={'task_id':self.scope['task_id'],'attempt_uuid':self.scope['attempt_uuid']}
                or not {'assigned','handoff-done'} <= set(phases)
                or {'fix-done','barrier'} & set(phases)):
            raise PipelineError('pipeline START canonical started/executor context changed or is ended')
        source=proof.publication.payload
        pp.review_gate(snapshot,config,dict(head=self.scope['source']['head'],epoch=source['epoch'],anchor=source['anchor']))
        if set(snapshot['labels']) & {'hold','ship','needs-decision','manual'}:
            raise PipelineError('pipeline START stopped by current routing labels')
        history,_deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=self.scope['pr'])
        view=resolve(_lease_nodes(history,root,purpose='technical-attempt'),context_sha256=context(root.payload),
                     purpose='technical-attempt',now=self.now())
        view.require_owner(returned_id=view.active.id,owner=self.scope['owner'],now=self.now())
        pp.ensure_identity(self.gh,config)
        require_task(dict(target_reservation=self.scope['reservation']))
        return dict(config=config,health=health,view=view,snapshot=snapshot,root=root,ledger=ledger)

    def final(self,store,scope):
        if scope!=self.scope:raise PipelineError('pipeline START factory substituted its consumed scope')
        state=self.state()
        # All remote work precedes the local/config/readers/clock final fence.
        # No writer wait or remote call follows this last local consumption read.
        config=pp.load_config(self.config_path)
        paired=policy.require(config,self.request['identity'],health=state['health'])
        if paired['consumer_sha256']!=self.scope['reader_sha256']:
            raise PipelineError('pipeline START readers changed before its factory')
        if pp.load_config(self.config_path)!=config:
            raise PipelineError('pipeline START configuration changed during final reader checks')
        requests.require_current(self.request,config)
        store.verify_consumed(scope)
        state['view'].require_owner(returned_id=state['view'].active.id,owner=scope['owner'],now=self.now())
        requests.require_current(self.request,config)
        self._validated_expiry=state['view'].expires_at
        return True


def gate(gh,config_path,token):
    """Public read-only factory gate; requires the worker's consumed local scope."""
    from .review_routing_workspace import Workspace
    import hashlib
    guard=StartGuard(gh,config_path,token)
    store=OneUseStart(db.DB_PATH,create=False,allow_new=False)
    task=db.get_task(guard.scope['task_id'])
    if task is None or not task.working_dir:
        raise PipelineError('pipeline START source checkout is unavailable')
    workspace=Workspace(db.DB_PATH,guard.scope,task.working_dir)
    workspace.verify()
    guard.final(store,guard.scope)
    path=workspace.verify()
    store.verify_consumed(guard.scope)
    requests.require_current(guard.request,pp.load_config(config_path))
    if guard._validated_expiry<=guard.now():
        raise PipelineError('pipeline START lease expired during checkout verification')
    return dict(action='start-allowed',stage='fix',protocol='review-routing-v1',
        scope_sha256=sha(guard.scope),owner=guard.scope['owner'],
        workspace=str(path),workspace_receipt_sha256=hashlib.sha256(workspace.receipt.read_bytes()).hexdigest(),
        lease_expires_at=guard._validated_expiry.isoformat())
