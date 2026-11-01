"""Public administrative settlement and one exact-current-HEAD REVIEW resume.

This route never launches a provider, changes source or refunds a spent slot.
It independently proves the old executor, delivery, full current PR identity,
canonical lease/history, actual task and four paired readers at every write.
"""
import copy
from datetime import datetime,timezone
import secrets
import time
from uuid import uuid4

from . import db,project_pipeline as pp
from . import review_routing_settlement_request as requests
from . import review_routing_pipeline_policy as execution_policy
from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing_assignment import read_assignment
from .review_routing_fix import require_task
from .review_routing_history import verified_depth
from .review_routing_lease import RENEW_WINDOW,resolve,decimal_id
from .review_routing_policy import preflight
from .review_routing_post_push import verify
from .review_routing_proof import _lease_nodes
from .review_routing_retirement import Retirement
from .review_routing_revocation import Revocation
from .review_routing_snapshot import stable_timeline
from .review_routing_wire import _fields,_source,context,head40,parse,records,render,sha

REASON='Trusted observer verified the old executor and delivery; review the exact current HEAD'


def _now():return datetime.now(timezone.utc)


def capability(config,health):
    paired=preflight(config,health)
    if paired.get('enabled') is not True or health.get('fix_execution_contract')!=execution_policy.CONTRACT:
        raise PipelineError('technical settlement requires the complete paired execution contract')
    return paired


def candidates(health):
    values=health.get('technical_fix_settlement_candidates',[])
    if not isinstance(values,list) or len(values)>4096:raise PipelineError('settlement checker allowlist is incomplete or oversized')
    seen=set()
    for item in values:
        _fields(item,'stage number head source attempt_id')
        _source(item['source']);head40(item['head']);decimal_id(item['attempt_id'])
        if (pp._fix_candidate_key(item)!=('fix-pr',item['number'],item['head'])
                or item['number'] in seen or item['head']==item['source']['head']):
            raise PipelineError('settlement checker target duplicates or changes its exact new-head source')
        seen.add(item['number'])
    return values


def source(gh,config,health,item):
    paired=capability(config,health)
    snapshot=stable_timeline(gh,config,item['number'])
    if snapshot['headRefOid']!=item['head']:raise PipelineError('settlement checker current HEAD is stale')
    ledger=verified_depth(snapshot,trusted=config['trusted_account'])['ledger']
    root=next((entry for entry in ledger.attempts if entry.id==item['attempt_id']),None)
    proof=ledger.source_proof(item['source'])
    if (root is None or root is not ledger.attempts[-1] or root.payload['source']!=item['source']
            or proof is None or proof is not ledger.proofs[-1] or proof.outcome!='technical-handoff'
            or PRIdentityV1(**dict(snapshot['identity'],head_sha=item['source']['head'])).sha256!=item['source']['identity_sha256']
            or snapshot['author']!=proof.publication.payload['author']):
        raise PipelineError('settlement lacks its latest canonical unfinished source/executor/full identity')
    phases=ledger.attempt_phases(root)
    if 'fix-done' in phases:raise PipelineError('settlement cannot replace successful source provenance')
    settled=ledger.attempt_settlement(root)
    if ledger.parking_id is not None and (settled is None or ledger.parking_id!=settled.id):
        raise PipelineError('settlement cannot replace another engineering parking')
    if ledger.resuming_publication_id is not None:
        raise PipelineError('settlement resume was already consumed by a REVIEW publication')
    info=pp.epoch(snapshot,config['trusted_account'])
    pp.review_gate(snapshot,config,dict(head=item['head'],epoch=info['hash'],anchor=info['anchor_id']))
    if set(snapshot['labels']) & {'hold','ship','needs-decision','manual'}:
        raise PipelineError('settlement stopped by current routing labels')
    history,_deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=root.payload['pr'])
    view=resolve(_lease_nodes(history,root,purpose='technical-attempt'),context_sha256=context(root.payload),
                 purpose='technical-attempt',now=_now())
    if root.payload['executor']['kind']=='pipeline':
        execution_policy.require(config,snapshot['identity'])
        observer=Retirement(db.DB_PATH,root,phases)
        observed=observer.observe()
    else:
        if 'started' in phases or 'handoff-done' not in phases:
            raise PipelineError('author settlement has no completed administrative delivery')
        observer=Revocation(db.DB_PATH,root,snapshot['author'])
        observed=observer.observe()
        assignees=read_assignment(gh,dict(repository=config['repository'],number=item['number'],
            identity=snapshot['identity'],author=snapshot['author']))
        if observed['confirmed'] and (snapshot['author']['id'],snapshot['author']['login']) in assignees:
            raise PipelineError('bound author was reassigned after confirmed revocation; preserve the manual change')
    pp.ensure_identity(gh,config)
    return dict(config=config,health=health,paired=paired,snapshot=snapshot,ledger=ledger,root=root,
                phases=phases,settled=settled,view=view,observer=observer,observed=observed)


class SettlementGuard:
    def __init__(self,gh,config_path,token):
        if not isinstance(config_path,str) or not config_path:raise PipelineError('settlement requires its actual configuration path')
        self.gh,self.config_path,self.token=gh,config_path,token
        self.request=requests.decode(token);self.seed=self.request['attempt'];self._ticket=None

    def item(self):
        return dict(stage='fix-pr',number=self.request['identity']['pr_number'],head=self.request['identity']['head_sha'],
                    source=self.seed['source'],attempt_id=self.request['attempt_id'])

    def state(self,*,allow_pending=False):
        if requests.decode(self.token)!=self.request:raise PipelineError('settlement signed binding changed')
        config=pp.load_config(self.config_path)
        health=pp.run_health(config,config_path=self.config_path)
        requests.require_current(self.request,config)
        if self.item() not in candidates(health):raise PipelineError('settlement left its fresh exact checker allowlist')
        state=source(self.gh,config,health,self.item())
        if (state['snapshot']['identity']!=self.request['identity'] or state['snapshot']['author']!=self.request['author']
                or state['root'].payload!=self.seed or state['paired']['consumer_sha256']!=self.request['reader_sha256']
                or sha(state['observed']['evidence'])!=self.request['evidence_sha256']):
            raise PipelineError('settlement source, executor evidence, author or readers changed')
        if (self.seed['executor']['kind']=='pipeline'
                and state['observed']['evidence']['publication']['outcome']=='sending' and not allow_pending):
            raise PipelineError('old executor publication remains unknown; independent exact readback required')
        require_task(self.request);requests.verify_local(self.request)
        return state

    def finish_local(self,state):
        requests.verify_local(self.request)
        config=pp.load_config(self.config_path)
        paired=capability(config,state['health'])
        if paired['consumer_sha256']!=self.request['reader_sha256'] or pp.load_config(self.config_path)!=config:
            raise PipelineError('settlement final readers/config changed')
        requests.require_current(self.request,config)

    def _confirmed(self,state):
        observed=state['observed']
        if self.seed['executor']['kind']=='pipeline':
            if not observed['retired']:raise PipelineError('old pipeline executor/publication is not retired')
        elif not observed['confirmed']:raise PipelineError('old author delivery is not independently revoked')

    def _check(self,payload,*,write=False):
        self._ticket=None;value=parse(render(payload))
        state=self.state();view=state['view'];now=_now();owner=self.request['owner']
        if value['pr']!=self.seed['pr']:raise PipelineError('settlement substituted its lifetime PR')
        kind=value['kind']
        if kind=='lease':
            if (value['owner']!=owner or value['context_sha256']!=context(self.seed)
                    or value['purpose']!='technical-attempt' or value['root']!=self.request['attempt_id']):
                raise PipelineError('settlement cannot acquire another executor/source lease')
            if write:
                same=owner==view.active.owner
                if (value['previous']!=view.active.id or not (
                        same and view.active.time<=now<view.expires_at and view.expires_at-now<RENEW_WINDOW
                        or not same and now>=view.expires_at)):
                    raise PipelineError('settlement canonical lease is active elsewhere or cannot be revived')
        elif kind=='settlement':
            self._confirmed(state)
            expected=self.settlement_payload(state)
            if value!=expected:raise PipelineError('settlement changes its observed executor, delivery, HEAD or canonical receipts')
        elif kind=='resume':
            self._confirmed(state)
            settled=state['settled']
            if (settled is None or value!=self.resume_payload(settled)
                    or settled.payload['evidence_sha256']!=self.request['evidence_sha256']):
                raise PipelineError('settlement resume changes its exact parking/current HEAD or executor evidence')
        else:raise PipelineError('administrative settlement cannot publish content, starts or source changes')
        if write:
            if kind!='lease':view.require_owner(returned_id=view.active.id,owner=owner,now=now)
            self._ticket=dict(snapshot=state['snapshot'],snapshot_sha256=sha(state['snapshot']),body=render(value))
        return state['snapshot']

    def __call__(self,payload,*,write=False):return self._check(payload,write=write)

    def final_before_post(self,payload,snapshot):
        ticket,self._ticket=self._ticket,None
        if (ticket is None or snapshot is not ticket['snapshot'] or sha(snapshot)!=ticket['snapshot_sha256']
                or render(payload)!=ticket['body']):raise PipelineError('settlement lacks its one-use final write ticket')
        requests.renew(self.request,pp.load_config(self.config_path))
        self._check(payload,write=True);self._ticket=None
        state=self.state();self.finish_local(state)
        view=state['view'];now=_now();owner=self.request['owner']
        if payload['kind']=='lease':
            same=owner==view.active.owner
            if (payload['previous']!=view.active.id or not (
                    same and view.active.time<=now<view.expires_at and view.expires_at-now<RENEW_WINDOW
                    or not same and now>=view.expires_at)):
                raise PipelineError('settlement canonical lease changed before POST')
        else:view.require_owner(returned_id=payload['lease_id'] if payload['kind']=='settlement' else view.active.id,
                                owner=owner,now=now)

    def revocation_gate(self):
        state=self.state();self.finish_local(state)
        state['view'].require_owner(returned_id=state['view'].active.id,owner=self.request['owner'],now=_now())
        return True

    def settlement_payload(self,state):
        phases=state['phases'];started=phases.get('started');barrier=phases.get('barrier')
        return dict(protocol='review-routing-v1',kind='settlement',pr=self.seed['pr'],owner=self.request['owner'],
            context_sha256=context(self.seed),attempt_id=self.request['attempt_id'],lease_id=state['view'].active.id,
            source=self.seed['source'],barrier_id=barrier.id if barrier else None,started_id=started.id if started else None,
            observed_head=self.request['identity']['head_sha'],reason=REASON,
            executor_outcome='pipeline-stopped' if self.seed['executor']['kind']=='pipeline' else 'author-unassigned',
            evidence_sha256=self.request['evidence_sha256'])

    def resume_payload(self,settled):
        return dict(protocol='review-routing-v1',kind='resume',pr=self.seed['pr'],
                    head=self.request['identity']['head_sha'],parking_id=settled.id,
                    previous=self.request['attempt_id'],reason=REASON)

    def confirm_pending(self):
        state=self.state(allow_pending=True)
        observed=state['observed']
        if self.seed['executor']['kind']!='pipeline' or observed['evidence']['publication']['outcome']!='sending':return self.token
        def readback(value):
            fresh=self.state(allow_pending=True)
            if value!=fresh['observed']['publication'] or value['new_head']!=fresh['snapshot']['headRefOid']:return False
            verify(self.gh,fresh['config'],fresh['snapshot'],self.seed,self.request['attempt_id'],value['new_head'])
            self.finish_local(fresh)
            return True
        updated=state['observer'].confirm_publication(readback)
        value=copy.deepcopy(self.request);value['evidence_sha256']=sha(updated['evidence']);value['nonce']=secrets.token_hex(16)
        return requests.encode(value)


def select(gh,config,*,config_path,health):
    values=candidates(health)
    if not values:return None
    capability(config,health)
    task=pp._pipeline_task_id(required=True);started=pp._pipeline_task_started_at(required=True)
    if pp._provider_ownership_kind(required=True)!='headless':raise PipelineError('settlement requires a local headless administrative task')
    owned=[row for row in db.list_pipeline_target_reservations(repository=config['repository']) if row['task_id']==task]
    if owned:
        if len(owned)!=1:raise PipelineError('settlement task has ambiguous existing target reservations')
        own=owned[0]
        values=[item for item in values if (own['stage'],own['number'],own['head'])==pp._fix_candidate_key(item)]
        if not values:raise PipelineError('settlement claimed target changed; no retargeting')
    waiting=None
    for item in values:
        if item['number'] in pp.admission_exclusions():continue
        try:state=source(gh,config,health,item)
        except PipelineError as exc:
            waiting=dict(action='wait',stage='fix',protocol='review-routing-v1',provider_started=False,
                number=item['number'],reason='settlement preserves this target engineering barrier: '+str(exc)[:500])
            continue
        view=state['view']
        if _now()<view.expires_at:
            waiting=dict(action='wait',stage='fix',protocol='review-routing-v1',provider_started=False,
                reason='settlement waits for the prior canonical owner lease',retry_at=view.expires_at.isoformat())
            continue
        reservation=db.reserve_pipeline_target(config['repository'],'fix-pr',item['number'],item['head'],task,
            config['target_reservation_ttl_seconds'],task_started_at=started,ownership_kind='headless',strict_lifetime=True)
        if reservation is None:
            waiting=dict(action='wait',stage='fix',protocol='review-routing-v1',provider_started=False,
                         reason='settlement target belongs to another live task')
            continue
        now=int(time.time())
        value=dict(version=1,protocol='review-routing-v1',purpose=requests.PURPOSE,repository=config['repository'],
            identity=state['snapshot']['identity'],author=state['snapshot']['author'],attempt=state['root'].payload,
            attempt_id=state['root'].id,owner=str(uuid4()),config_sha256=pp.digest(config),
            reader_sha256=state['paired']['consumer_sha256'],pipeline_replicas=pp._configured_replica_count(),
            target_reservation=reservation,evidence_sha256=sha(state['observed']['evidence']),issued_at=now,
            expires_at=now+min(1800,config['target_reservation_ttl_seconds']),nonce=secrets.token_hex(16))
        guard=SettlementGuard(gh,config_path,requests.encode(value))
        token=guard.confirm_pending()
        SettlementGuard(gh,config_path,token).state()
        return dict(action='settlement',stage='fix',protocol='review-routing-v1',lease=token,target=item,
                    provider_started=False,mutation_authorized=False)
    return waiting


def complete(gh,config_path,token):
    from .review_routing_adapter import _journal,_recover_visible_pending
    from .review_routing_transport import Transport
    guard=SettlementGuard(gh,config_path,token);state=guard.state()
    config=state['config'];journal=_journal(config)
    history,_deleted=records(state['snapshot'],trusted=config['trusted_account'],expected_pr=guard.seed['pr'])
    if _recover_visible_pending(gh,config,state['snapshot'],history,journal,journal.pending_for(guard.seed['pr'])):
        raise PipelineError('settlement preserves an unresolved POST barrier')
    transport=Transport(gh,config,journal,guard)
    def publish(payload):return transport.publish(payload,owner=guard.request['owner'],phase='settlement:'+sha(payload))
    settled=state['settled']
    if settled is not None and state['ledger'].resume_id is not None:
        grant=next(entry for entry in state['ledger'].resumes if entry.id==state['ledger'].resume_id)
        if grant.payload==guard.resume_payload(settled):
            guard._confirmed(state)
            if settled.payload['evidence_sha256']!=guard.request['evidence_sha256']:
                raise PipelineError('existing settlement differs from independently observed executor evidence')
            guard.finish_local(state)
            return _result(guard,settled,grant)
    view=state['view']
    if view.active.owner!=guard.request['owner'] or view.expires_at-_now()<RENEW_WINDOW:
        if view.active.owner!=guard.request['owner'] and _now()<view.expires_at:
            return dict(action='wait',stage='fix',protocol='review-routing-v1',provider_started=False,
                        reason='another canonical settlement owner remains active')
        publish(dict(protocol='review-routing-v1',kind='lease',pr=guard.seed['pr'],owner=guard.request['owner'],
            context_sha256=context(guard.seed),purpose='technical-attempt',root=guard.request['attempt_id'],previous=view.active.id))
    state=guard.state()
    if guard.seed['executor']['kind']=='pipeline':state['observer'].retire(state['observed']['evidence'])
    else:state['observer'].revoke(gh,guard.request['identity'],final_guard=guard.revocation_gate)
    state=guard.state()
    settled=state['settled'] or publish(guard.settlement_payload(state))
    if settled.payload['evidence_sha256']!=guard.request['evidence_sha256']:
        raise PipelineError('canonical settlement differs from independently observed old executor')
    grant=publish(guard.resume_payload(settled))
    return _result(guard,settled,grant)


def _result(guard,settled,grant):
    return dict(action='resumed',stage='fix',protocol='review-routing-v1',provider_started=False,
        attempt_id=guard.request['attempt_id'],source=guard.seed['source'],executor=guard.seed['executor'],
        new_head=guard.request['identity']['head_sha'],settlement_id=settled.id,resume_id=grant.id,
        mutation_authorized=False,reason='old executor/delivery verified; one independent current-HEAD REVIEW resumed')
