"""Finite technical lease renewal by the original in-memory producer only.

An old START is a lookup, never permission to launch again. A heartbeat cannot
take over an expired owner, publish code, settle an executor or refund a slot.
"""
import copy
from datetime import datetime,timedelta,timezone
import os
import secrets
import time

from . import db,project_pipeline as pp
from . import review_routing_start_request as starts
from . import review_routing_pipeline_policy as policy
from .pipeline_errors import PipelineError
from .review_routing_execution import OneUseStart
from .review_routing_lease import RENEW_WINDOW
from .review_routing_start import StartGuard
from .review_routing_wire import context,parse,render,sha

PURPOSE='review-routing-v1-fix-heartbeat'
NONCE_ENV='PP_TECHNICAL_EXECUTION_NONCE'


def _now():return datetime.now(timezone.utc)


def validate(value):return starts._validate_structure(value,purpose=PURPOSE)


def decode(token):
    # Reuse the exact bounded canonical signed envelope, with a closed purpose.
    if not isinstance(token,str) or len(token)>65536:
        raise PipelineError('invalid signed technical heartbeat')
    value=validate(pp.decode_signed_lease(token))
    if token.split('.')[0]!=pp.encode_lease(value):
        raise PipelineError('noncanonical technical heartbeat')
    return value


def require_current(value,config,*,write=True):
    return starts._require_current(value,config,write=write,validator=validate)


def deadline(scope,config):
    from .fix_handoff import lease_seconds
    task=db.get_task(scope['task_id'])
    if task is None or task.started_at is None:
        raise PipelineError('technical heartbeat lost its actual producer task')
    limit=lease_seconds(config)
    if type(task.task_timeout) is int and task.task_timeout>0:
        limit=min(limit,task.task_timeout)
    return int(task.started_at.astimezone(timezone.utc).timestamp())+limit


class HeartbeatGuard(StartGuard):
    decode_request=staticmethod(decode)
    require_current=staticmethod(require_current)

    def __init__(self,gh,config_path,token,nonce,*,clock=None):
        super().__init__(gh,config_path,token,clock=clock or _now)
        self.nonce=nonce
        self.store=OneUseStart(db.DB_PATH,create=False,allow_new=False)
        self._ticket=None

    def live(self,config):
        require_current(self.request,config)
        self.store.verify_live(self.scope,self.nonce)
        if self.now().timestamp()>=deadline(self.scope,config):
            raise PipelineError('technical producer finite deadline expired')

    def state(self):
        state=super().state()
        self.live(state['config'])
        return state

    def _check(self,payload,*,write=False):
        self._ticket=None
        value=parse(render(payload))
        seed=self.request['attempt']
        if (value['kind']!='lease' or value['purpose']!='technical-attempt'
                or value['pr']!=seed['pr'] or value['owner']!=seed['owner']
                or value['context_sha256']!=context(seed) or value['root']!=self.scope['attempt_id']):
            raise PipelineError('technical heartbeat may only renew its exact existing owner')
        state=self.state()
        view=state['view'];now=self.now()
        view.require_owner(returned_id=view.active.id,owner=seed['owner'],now=now)
        if write:
            if value['previous']!=view.active.id or not timedelta(0)<view.expires_at-now<RENEW_WINDOW:
                raise PipelineError('technical heartbeat cannot take over, revive or renew outside its window')
            self._ticket=dict(snapshot=state['snapshot'],snapshot_sha256=sha(state['snapshot']),body=render(value))
        return state['snapshot']

    def __call__(self,payload,*,write=False):return self._check(payload,write=write)

    def final_before_post(self,payload,snapshot):
        ticket,self._ticket=self._ticket,None
        if (ticket is None or snapshot is not ticket['snapshot']
                or sha(snapshot)!=ticket['snapshot_sha256'] or render(payload)!=ticket['body']):
            raise PipelineError('technical heartbeat lacks its one-use final write ticket')
        # Repeat all remote guards AFTER journal writer waits, then finish on
        # current config/readers, private live proof and actual expiry clock.
        self._check(payload,write=True)
        self._ticket=None
        state=self.state()
        config=pp.load_config(self.config_path)
        paired=policy.require(config,self.request['identity'],health=state['health'])
        if paired['consumer_sha256']!=self.scope['reader_sha256'] or pp.load_config(self.config_path)!=config:
            raise PipelineError('technical heartbeat final reader/config binding changed')
        self.live(config)
        view=state['view'];now=self.now()
        view.require_owner(returned_id=payload['previous'],owner=payload['owner'],now=now)
        if not timedelta(0)<view.expires_at-now<RENEW_WINDOW:
            raise PipelineError('technical heartbeat lease changed before POST')


def prepare(gh,config_path,start_token,*,nonce=None):
    original=starts.decode(start_token)
    config=pp.load_config(config_path)
    starts.require_current(original,config,write=False)
    producer=nonce if nonce is not None else os.environ.get(NONCE_ENV)
    OneUseStart(db.DB_PATH,create=False,allow_new=False).verify_live(original['scope'],producer)
    now=int(time.time());end=min(now+300,deadline(original['scope'],config))
    if end<=now:raise PipelineError('technical producer finite deadline expired')
    value=copy.deepcopy(original)
    value.update(purpose=PURPOSE,issued_at=now,expires_at=end,nonce=secrets.token_hex(16))
    token=pp.encode_signed_lease(validate(value))
    guard=HeartbeatGuard(gh,config_path,token,producer)
    guard.state()
    return dict(action='heartbeat-prepared',stage='fix',protocol='review-routing-v1',lease=token,
                scope_sha256=sha(original['scope']),provider_started=False)


def renew(gh,config_path,token,*,nonce=None):
    from .review_routing_adapter import _journal,_recover_visible_pending
    from .review_routing_transport import Transport
    from .review_routing_wire import records
    producer=nonce if nonce is not None else os.environ.get(NONCE_ENV)
    guard=HeartbeatGuard(gh,config_path,token,producer)
    state=guard.state();view=state['view']
    if view.expires_at-guard.now()>=RENEW_WINDOW:
        return dict(action='heartbeat-unchanged',stage='fix',protocol='review-routing-v1',
                    scope_sha256=sha(guard.scope),lease_expires_at=view.expires_at.isoformat(),provider_started=False)
    config=state['config'];journal=_journal(config)
    history,_deleted=records(state['snapshot'],trusted=config['trusted_account'],expected_pr=guard.scope['pr'])
    if _recover_visible_pending(gh,config,state['snapshot'],history,journal,journal.pending_for(guard.scope['pr'])):
        raise PipelineError('technical heartbeat preserves an unresolved publication barrier')
    payload=dict(protocol='review-routing-v1',kind='lease',pr=guard.scope['pr'],owner=guard.scope['owner'],
        context_sha256=guard.scope['context_sha256'],purpose='technical-attempt',root=guard.scope['attempt_id'],
        previous=view.active.id)
    receipt=Transport(gh,config,journal,guard).publish(payload,owner=guard.scope['owner'],phase='heartbeat:'+sha(payload))
    current=guard.state()['view']
    current.require_owner(returned_id=receipt.id,owner=guard.scope['owner'],now=guard.now())
    return dict(action='heartbeat-renewed',stage='fix',protocol='review-routing-v1',
        scope_sha256=sha(guard.scope),lease_expires_at=current.expires_at.isoformat(),provider_started=False)
