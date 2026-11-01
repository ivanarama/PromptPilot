"""Pipeline admission and prepared START metadata issuer; worker not connected.

Canonical metadata does not prove a process launch or grant a second factory.
The worker must consume the separately prepared START with its final gate.
"""
import copy
from datetime import datetime,timezone
from uuid import UUID,uuid4

from . import db,project_pipeline as pp
from . import review_routing_pipeline_request as requests
from . import review_routing_pipeline_policy as policy
from .pipeline_errors import PipelineError
from .review_routing_execution import OneUseStart
from .review_routing_fix import candidates,require_task
from .review_routing_history import verified_depth
from .review_routing_lease import resolve
from .review_routing_proof import _lease_nodes
from .review_routing_snapshot import stable_timeline
from .review_routing_wire import context,parse,records,render,sha


class AdmissionGuard:
    def __init__(self,gh,config_path,token):
        if not isinstance(config_path,str) or not config_path:
            raise PipelineError('pipeline admission requires its actual configuration path')
        self.gh,self.config_path,self.token=gh,config_path,token
        self.request=requests.decode(token);self.seed=self.request['attempt'];self._ticket=None
        self.occurrence=dict(task_id=self.request['target_reservation']['task_id'],
                             attempt_uuid=str(UUID(hex=self.request['nonce'],version=4)))

    def state(self,*,write=False):
        request=requests.decode(self.token)
        if request!=self.request:raise PipelineError('pipeline admission signed scope changed')
        config=pp.load_config(self.config_path)
        health=pp.run_health(config,config_path=self.config_path)
        paired=policy.require(config,request['identity'],health=health)
        requests.require_current(request,config,write=write)
        if (paired['consumer_sha256']!=request['reader_sha256']
                or paired['current_limit']!=request['current_limit']):
            raise PipelineError('pipeline admission readers or lifetime limit changed')
        expected=dict(stage='fix-pr',number=request['number'],head=request['head'],source=self.seed['source'])
        if write and expected not in candidates(health):
            raise PipelineError('pipeline admission left its fresh exact checker allowlist')
        snapshot=stable_timeline(self.gh,config,request['number'])
        if snapshot['identity']!=request['identity'] or snapshot['author']!=request['author']:
            raise PipelineError('pipeline admission source identity, fork or author changed')
        ledger=verified_depth(snapshot,trusted=config['trusted_account'])['ledger']
        proof=ledger.source_proof(self.seed['source'])
        if (proof is None or proof is not ledger.proofs[-1] or proof.outcome!='technical-handoff'
                or context(proof.publication.payload)!=self.seed['context_sha256']
                or ledger.parking_id is not None or self.seed['attempt']>paired['current_limit']):
            raise PipelineError('pipeline admission lacks its current eligible canonical technical source')
        source=proof.publication.payload
        pp.review_gate(snapshot,config,dict(head=request['head'],epoch=source['epoch'],anchor=source['anchor']))
        if set(snapshot['labels']) & {'hold','ship','needs-decision','manual'}:
            raise PipelineError('pipeline admission stopped by current routing labels')
        roots=[root for root in ledger.attempts if root.payload['source']==self.seed['source']]
        root=roots[0] if roots else None
        view=None
        if root is not None:
            if root.payload!=self.seed or ledger.attempt_settlement(root) is not None:
                raise PipelineError('pipeline admission canonical root/executor changed or is settled')
            phases=ledger.attempt_phases(root)
            if {'fix-done','barrier'} & set(phases):raise PipelineError('pipeline admission is ended or ambiguous')
            started=phases.get('started')
            if started is not None and started.payload['details']!=self.occurrence:
                raise PipelineError('pipeline admission canonical START belongs to another task/UUID')
            history,_deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=self.seed['pr'])
            view=resolve(_lease_nodes(history,root,purpose='technical-attempt'),
                         context_sha256=context(root.payload),purpose='technical-attempt',now=datetime.now(timezone.utc))
            view.require_owner(returned_id=view.active.id,owner=self.seed['owner'],now=datetime.now(timezone.utc))
        pp.ensure_identity(self.gh,config)
        return dict(config=config,health=health,paired=paired,snapshot=snapshot,ledger=ledger,root=root,view=view)

    def __call__(self,payload,*,write=False,after_waits=False):
        self._ticket=None
        value=parse(render(payload))
        if (value['kind'] not in {'attempt','phase'} or value['pr']!=self.seed['pr']
                or value['owner']!=self.seed['owner'] or value['kind']=='attempt' and value!=self.seed):
            raise PipelineError('pipeline admission cannot publish a different operation')
        state=self.state(write=write);root,view=state['root'],state['view']
        if value['kind']=='phase':
            if (root is None or value['attempt_id']!=root.id or value['context_sha256']!=context(root.payload)
                    or value['phase'] not in {'assigned','handoff-done','started'}
                    or value['details']!=(self.occurrence if value['phase']=='started' else {})):
                raise PipelineError('pipeline admission phase changes its canonical executor/start context')
        if write:
            ledger=state['ledger']
            if value['kind']=='attempt':
                admission=ledger.next_attempt_admission(self.seed['source'],max_attempts=state['paired']['current_limit'])
                if (root is not None or not admission['eligible'] or self.seed['attempt']!=ledger.budget_used+1
                        or self.seed['previous']!=(ledger.attempts[-1].id if ledger.attempts else None)
                        or self.seed['series']!=(ledger.attempts[0].payload['series'] if ledger.attempts
                                               else ledger.source_proof(self.seed['source']).completion.id)):
                    raise PipelineError('pipeline admission lifetime source/series changed before reservation')
            else:
                phases=ledger.attempt_phases(root)
                prerequisite={'handoff-done':'assigned','started':'handoff-done'}.get(value['phase'])
                if value['phase'] in phases or prerequisite and prerequisite not in phases:
                    raise PipelineError('pipeline admission phase already exists or lacks its prerequisite')
                view.require_owner(returned_id=value['lease_id'],owner=self.seed['owner'],now=datetime.now(timezone.utc))
            if not after_waits:
                require_task(self.request);requests.renew(self.request,state['config'])
            requests.require_current(self.request,state['config'])
            self._ticket=dict(snapshot=state['snapshot'],digest=sha(state['snapshot']),body=render(value),
                              health=copy.deepcopy(state['health']),view=view)
        return state['snapshot']

    def final_before_post(self,payload,snapshot):
        ticket,self._ticket=self._ticket,None
        if (ticket is None or ticket['snapshot'] is not snapshot or ticket['digest']!=sha(snapshot)
                or ticket['body']!=render(payload)):
            raise PipelineError('pipeline admission has no one-use final write ticket')
        require_task(self.request)
        config=pp.load_config(self.config_path)
        renewed=requests.renew(self.request,config)
        self(payload,write=True,after_waits=True)
        ticket,self._ticket=self._ticket,None
        requests.verify_local(self.request)
        config=pp.load_config(self.config_path)
        paired=policy.require(config,self.request['identity'],health=ticket['health'])
        if (paired['consumer_sha256']!=self.request['reader_sha256']
                or paired['current_limit']!=self.request['current_limit'] or pp.load_config(self.config_path)!=config):
            raise PipelineError('pipeline admission final readers/configuration changed')
        requests.require_current(self.request,config)
        now=datetime.now(timezone.utc)
        if datetime.fromisoformat(renewed['expires_at'])<=now:
            raise PipelineError('pipeline admission reservation expired before publication')
        if ticket['view'] is not None:
            ticket['view'].require_owner(returned_id=payload['lease_id'],owner=self.seed['owner'],now=now)


def select(gh,config,*,config_path,health):
    ordered=candidates(health)
    if not ordered:return None
    task=pp._pipeline_task_id(required=True)
    own=next((row for row in db.list_pipeline_target_reservations(repository=config['repository'])
              if row['task_id']==task),None)
    if own is not None:
        ordered=[item for item in ordered if (own['stage'],own['number'],own['head'])==pp._fix_candidate_key(item)]
        if not ordered:raise PipelineError('pipeline admission reservation target changed; no retargeting')
    for item in ordered:
        if item['number'] in pp.admission_exclusions():continue
        snapshot=stable_timeline(gh,config,item['number'])
        if snapshot['identity']['cross_repository']:continue  # author route remains separate
        paired=policy.require(config,snapshot['identity'],health=health)
        ledger=verified_depth(snapshot,trusted=config['trusted_account'])['ledger']
        eligible=ledger.next_attempt_admission(item['source'],max_attempts=paired['current_limit'])
        if not eligible['eligible']:continue
        proof=ledger.source_proof(item['source'])
        if proof is None:raise PipelineError('pipeline candidate has no canonical technical source')
        task=pp._pipeline_task_id(required=True);started=pp._pipeline_task_started_at(required=True)
        if pp._provider_ownership_kind(required=True)!='headless':
            raise PipelineError('pipeline admission requires local headless transport')
        reservation=db.reserve_pipeline_target(config['repository'],'fix-pr',item['number'],item['head'],task,
            config['target_reservation_ttl_seconds'],task_started_at=started,ownership_kind='headless',strict_lifetime=True)
        if reservation is None:
            if own is not None:raise PipelineError('pipeline admission exact reservation lost; no replacement')
            continue
        seed=dict(protocol='review-routing-v1',kind='attempt',pr=snapshot['pr_key'],owner=str(uuid4()),
            context_sha256=context(proof.publication.payload),source=item['source'],
            series=ledger.attempts[0].payload['series'] if ledger.attempts else proof.completion.id,
            previous=ledger.attempts[-1].id if ledger.attempts else None,attempt=ledger.budget_used+1,
            executor=config['technical_fix_execution']['executor'])
        value=requests.build(config,snapshot,paired,seed,reservation);token=requests.encode(value)
        AdmissionGuard(gh,config_path,token)(seed)
        require_task(value);requests.renew(value,config);requests.verify_local(value);requests.require_current(value,config)
        return dict(action='pipeline-fix',stage='fix',protocol='review-routing-v1',lease=token,target=item,
                    provider_started=False,mutation_authorized=False)
    return None


def issue(gh,config,token,*,config_path):
    from .review_routing_adapter import _journal,_recover_visible_pending
    from .review_routing_transport import Transport
    from . import review_routing_start_request as starts
    guard=AdmissionGuard(gh,config_path,token)
    state=guard.state();journal=_journal(config)
    history,_deleted=records(state['snapshot'],trusted=config['trusted_account'],expected_pr=guard.seed['pr'])
    if _recover_visible_pending(gh,config,state['snapshot'],history,journal,journal.pending_for(guard.seed['pr'])):
        raise PipelineError('pipeline admission has an unresolved publication; no launch')
    transport=Transport(gh,config,journal,guard)
    def publish(payload):
        return transport.publish(payload,owner=guard.seed['owner'],phase='pipeline-admission:'+sha(payload))
    root=state['root'] or publish(guard.seed)
    for phase in ['assigned','handoff-done']:
        state=guard.state()
        if phase in state['ledger'].attempt_phases(root):continue
        publish(dict(protocol='review-routing-v1',kind='phase',pr=guard.seed['pr'],owner=guard.seed['owner'],
            context_sha256=context(root.payload),attempt_id=root.id,lease_id=state['view'].active.id,
            phase=phase,details={}))
    state=guard.state();phases=state['ledger'].attempt_phases(root)
    started=phases.get('started')
    store=OneUseStart(db.DB_PATH,create=started is None,allow_new=False)
    payload=dict(protocol='review-routing-v1',kind='phase',pr=guard.seed['pr'],owner=guard.seed['owner'],
        context_sha256=context(root.payload),attempt_id=root.id,lease_id=state['view'].active.id,
        phase='started',details=guard.occurrence)
    entry=journal.get(owner=guard.seed['owner'],phase='pipeline-admission:'+sha(payload))
    can_prepare=started is None and (entry is None or entry['state']=='not-sent')
    if started is None:started=publish(payload)
    state=guard.state()
    value=starts.build(state['config'],state['snapshot'],state['paired'],root,started,
                       guard.request['target_reservation'])
    local=store.status(value['scope'])
    if local is None:
        if not can_prepare:
            raise PipelineError('existing server START cannot reconstruct missing local issuance')
        store.prepare(value['scope'])
    elif local['state']!='not-started':
        raise PipelineError('pipeline START was already consumed or remains unknown; no second launch')
    return dict(action='pipeline-start',stage='fix',protocol='review-routing-v1',lease=starts.encode(value),
        target=dict(stage='fix-pr',number=guard.request['number'],head=guard.request['head'],
                    attempt_id=root.id,started_id=started.id,**guard.occurrence),
        provider_started=False,mutation_authorized=False)
