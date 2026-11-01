"""Trusted stopped-output publication: one expected-HEAD CAS then fix-done."""
import copy
import secrets
import time

from . import db,project_pipeline as pp
from . import review_routing_start_request as starts
from . import review_routing_pipeline_policy as policy
from .pipeline_errors import PipelineError
from .review_routing_execution import OneUseStart
from .review_routing_lease import RENEW_WINDOW
from .review_routing_heartbeat import deadline
from .review_routing_post_push import verify
from .review_routing_progress import _source_gate,candidates as progress_candidates
from .review_routing_push import Push,binding,prepare_expected
from .review_routing_start import StartGuard
from .review_routing_wire import _fields,context,head40,parse,render,sha
from .review_routing_workspace import Workspace

PURPOSE='review-routing-v1-fix-publication'


def _base(value):
    _fields(value,starts.FIELDS+' new_head tree')
    for key in ['new_head','tree']:head40(value[key])
    if value['new_head']==value['scope']['source']['head']:
        raise PipelineError('technical publication requires a changed forward HEAD')
    base={key:part for key,part in value.items() if key not in {'new_head','tree'}}
    return starts._validate_structure(base,purpose=PURPOSE)


def decode(token):
    if not isinstance(token,str) or len(token)>65536:raise PipelineError('invalid technical publication token')
    value=pp.decode_signed_lease(token);_base(value)
    if token.split('.')[0]!=pp.encode_lease(value):raise PipelineError('noncanonical technical publication token')
    return value


def require_current(value,config,*,write=True):
    return starts._require_current(_base(value),config,write=write,
        validator=lambda base:starts._validate_structure(base,purpose=PURPOSE))


def _workspace(scope):
    task=db.get_task(scope['task_id'])
    if task is None or not task.working_dir:raise PipelineError('technical publication lost its source checkout')
    return Workspace(db.DB_PATH,scope,task.working_dir)


class PublicationGuard(StartGuard):
    decode_request=staticmethod(decode)
    require_current=staticmethod(require_current)
    forbidden_phases={'barrier'}

    def __init__(self,gh,config_path,token,*,clock=None):
        super().__init__(gh,config_path,token,clock=clock)
        self.store=OneUseStart(db.DB_PATH,create=False,allow_new=False)
        self.workspace=_workspace(self.scope)
        self.output={key:self.request[key] for key in ['new_head','tree']}
        self.push=Push(db.DB_PATH,binding(self.scope,self.request['identity'],self.output))
        self._ticket=None

    def checker_gate(self,health):
        # Selection is checked against the actual coherent snapshot below.
        # The complete paired contract is always checked by StartGuard first.
        pass

    def identity_gate(self,snapshot):
        current=snapshot['headRefOid']
        expected=dict(self.request['identity'],head_sha=current)
        if (current not in {self.scope['source']['head'],self.request['new_head']}
                or snapshot['identity']!=expected or snapshot['author']!=self.request['author']):
            raise PipelineError('technical publication source identity or output HEAD changed')

    def source_gate(self,snapshot,config,proof):
        if snapshot['headRefOid']==self.scope['source']['head']:
            return super().source_gate(snapshot,config,proof)
        before,after=_source_gate(snapshot,config,proof,self.request)
        if before!=after:
            graph=self.gh.json('api',f'repos/{config["repository"]}/compare/{before}...{after}')
            if (not isinstance(graph,dict) or graph.get('status')!='ahead'
                    or (graph.get('base_commit') or {}).get('sha')!=before
                    or (graph.get('merge_base_commit') or {}).get('sha')!=before):
                raise PipelineError('technical publication integration base did not move forward')

    def state(self):
        state=super().state()
        self.store.verify_stopped(self.scope)
        if self.now().timestamp()>=deadline(self.scope,state['config']):
            raise PipelineError('technical publication finite producer deadline expired')
        snapshot=state['snapshot']
        if snapshot['headRefOid']==self.scope['source']['head']:
            super().checker_gate(state['health'])
            if 'fix-done' in state['ledger'].attempt_phases(state['root']):
                raise PipelineError('technical publication completed receipt lost its new HEAD')
            state['details']=None
        else:
            expected=dict(stage='fix-pr',number=self.scope['pr']['pr_number'],head=self.scope['source']['head'],
                source=self.scope['source'],attempt_id=self.scope['attempt_id'],new_head=self.request['new_head'])
            if expected not in progress_candidates(state['health']):
                raise PipelineError('technical publication left its fresh exact progress allowlist')
            state['details']=verify(self.gh,state['config'],snapshot,self.request['attempt'],
                                    self.scope['attempt_id'],self.request['new_head'])
        return state

    def finish_local(self,state):
        # Complete all SQLite waits before the last config/readers/clock fence.
        self.store.verify_stopped(self.scope)
        config=pp.load_config(self.config_path)
        paired=policy.require(config,self.request['identity'],health=state['health'])
        if paired['consumer_sha256']!=self.scope['reader_sha256'] or pp.load_config(self.config_path)!=config:
            raise PipelineError('technical publication final readers/config changed')
        require_current(self.request,config)
        now=self.now()
        if now.timestamp()>=deadline(self.scope,config):raise PipelineError('technical publication deadline expired before mutation')
        state['view'].require_owner(returned_id=state['view'].active.id,owner=self.scope['owner'],now=self.now())

    def final_push(self):
        if self.workspace.output(self.request['attempt'])!=self.output:
            raise PipelineError('technical publication private output changed')
        state=self.state()
        if state['snapshot']['headRefOid']!=self.scope['source']['head']:
            raise PipelineError('technical CAS expected source has already changed')
        self.finish_local(state)
        return True

    def readback(self):
        state=self.state()
        if state['snapshot']['headRefOid']!=self.request['new_head'] or state['details'] is None:return False
        self.finish_local(state)
        return True

    def _check(self,payload,*,write=False):
        self._ticket=None
        value=parse(render(payload));seed=self.request['attempt']
        if (value['pr']!=self.scope['pr']
                or value['owner']!=self.scope['owner'] or value['context_sha256']!=context(seed)
                or value['kind'] not in {'phase','lease'}):
            raise PipelineError('technical publication changes its exact source owner or purpose')
        state=self.state()
        if value['kind']=='lease':
            if value['purpose']!='technical-attempt' or value['root']!=self.scope['attempt_id']:
                raise PipelineError('technical publisher may only finish its same existing owner lease')
            if write:
                if 'fix-done' in state['ledger'].attempt_phases(state['root']):
                    raise PipelineError('technical publisher cannot renew a completed attempt')
                view=state['view'];now=self.now()
                view.require_owner(returned_id=value['previous'],owner=value['owner'],now=now)
                if view.expires_at-now>=RENEW_WINDOW:raise PipelineError('technical publisher renewal is outside its window')
        else:
            if value['phase']!='fix-done' or value['attempt_id']!=self.scope['attempt_id']:
                raise PipelineError('technical publication may only publish its exact fix-done')
            self.push.require_confirmed()
            if state['details'] is None or value['details']!=state['details']:
                raise PipelineError('technical FIX lacks independently verified new HEAD/source trailers')
            state['view'].require_owner(returned_id=value['lease_id'],owner=value['owner'],now=self.now())
        if write:self._ticket=dict(snapshot=state['snapshot'],snapshot_sha256=sha(state['snapshot']),body=render(value))
        return state['snapshot']

    def __call__(self,payload,*,write=False):return self._check(payload,write=write)

    def final_before_post(self,payload,snapshot):
        ticket,self._ticket=self._ticket,None
        if (ticket is None or snapshot is not ticket['snapshot'] or sha(snapshot)!=ticket['snapshot_sha256']
                or render(payload)!=ticket['body']):raise PipelineError('technical FIX lacks its one-use final write ticket')
        self._check(payload,write=True)
        self._ticket=None
        state=self.state()
        if payload['kind']=='phase':self.push.require_confirmed()
        self.finish_local(state)
        view=state['view'];now=self.now()
        view.require_owner(returned_id=payload['lease_id'] if payload['kind']=='phase' else payload['previous'],
                           owner=payload['owner'],now=now)
        if payload['kind']=='lease' and view.expires_at-now>=RENEW_WINDOW:
            raise PipelineError('technical publisher lease changed before POST')


def prepare(gh,config_path,start_token):
    original=starts.decode(start_token)
    config=pp.load_config(config_path)
    starts.require_current(original,config,write=False)
    scope=original['scope']
    OneUseStart(db.DB_PATH,create=False,allow_new=False).verify_stopped(scope)
    output=_workspace(scope).output(original['attempt'])
    now=int(time.time());end=min(now+300,deadline(scope,config))
    if end<=now:raise PipelineError('technical publication producer deadline expired')
    value=copy.deepcopy(original)
    value.update(purpose=PURPOSE,issued_at=now,expires_at=end,nonce=secrets.token_hex(16),**output)
    _base(value);token=pp.encode_signed_lease(value)
    PublicationGuard(gh,config_path,token).state()
    return dict(action='publication-prepared',stage='fix',protocol='review-routing-v1',lease=token,
                scope_sha256=sha(scope),new_head=output['new_head'],provider_started=False)


def publish(gh,config_path,token):
    from .review_routing_adapter import _journal,_recover_visible_pending
    from .review_routing_transport import Transport
    from .review_routing_wire import records
    guard=PublicationGuard(gh,config_path,token)
    state=guard.state();config=state['config'];journal=_journal(config)
    history,_deleted=records(state['snapshot'],trusted=config['trusted_account'],expected_pr=guard.scope['pr'])
    if _recover_visible_pending(gh,config,state['snapshot'],history,journal,journal.pending_for(guard.scope['pr'])):
        raise PipelineError('technical publication preserves an unresolved POST barrier')
    def completed(state):
        existing=state['ledger'].attempt_phases(state['root']).get('fix-done')
        if existing is None:return None
        guard.push.require_confirmed()
        if existing.payload['details']!=state['details']:
            raise PipelineError('technical FIX existing completion differs from independently verified output')
        guard.finish_local(state)
        return _result(guard,existing)
    existing=completed(state)
    if existing is not None:return existing
    transport=Transport(gh,config,journal,guard)
    def renew_owner(state):
        view=state['view']
        if view.expires_at-guard.now()<RENEW_WINDOW:
            payload=dict(protocol='review-routing-v1',kind='lease',pr=guard.scope['pr'],owner=guard.scope['owner'],
                context_sha256=guard.scope['context_sha256'],purpose='technical-attempt',root=guard.scope['attempt_id'],
                previous=view.active.id)
            transport.publish(payload,owner=guard.scope['owner'],phase='pipeline-publish-lease:'+sha(payload))
    renew_owner(state)
    send=prepare_expected(guard.workspace,guard.push.value)
    guard.push.publish(final_guard=guard.final_push,push=send,readback=guard.readback)
    state=guard.state()
    existing=completed(state)
    if existing is not None:return existing
    renew_owner(state)
    state=guard.state()
    payload=dict(protocol='review-routing-v1',kind='phase',phase='fix-done',pr=guard.scope['pr'],
        owner=guard.scope['owner'],context_sha256=guard.scope['context_sha256'],attempt_id=guard.scope['attempt_id'],
        lease_id=state['view'].active.id,details=state['details'])
    receipt=transport.publish(payload,owner=guard.scope['owner'],phase='pipeline-fix-done:'+sha(payload))
    return _result(guard,receipt)


def _result(guard,receipt):
    return dict(action='fixed',stage='fix',protocol='review-routing-v1',attempt_id=guard.scope['attempt_id'],
                source=guard.scope['source'],executor=guard.scope['executor'],new_head=guard.request['new_head'],
                fix_done_id=receipt.id,provider_started=False,mutation_authorized=False)
