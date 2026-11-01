"""Source-bound author progress guard under an explicit complete peer contract.

Public selection and the worker use this guard. A signed scope alone never
substitutes for these fresh identity/history/source/task/lease/clock checks.
"""
import copy
from datetime import datetime, timezone

from . import project_pipeline as pp
from . import review_routing_fix_request as admission
from . import review_routing_progress_request as requests
from .pipeline_errors import PipelineError
from .review_routing_assignment import require_assigned
from .review_routing_fix import require_task
from .review_routing_history import verified_depth
from .review_routing_lease import RENEW_WINDOW, resolve
from .review_routing_policy import preflight
from .review_routing_post_push import verify
from .review_routing_proof import _lease_nodes
from .review_routing_snapshot import stable_timeline
from .review_routing_wire import _fields, context, parse, records, render, sha, _source, head40


BRIDGE = {'protocol':'claimed-fix-v1','snapshot':'complete',
          'purposes':['author-handoff','author-progress']}


def _now():
    return datetime.now(timezone.utc)


def capability(config, health):
    result = preflight(config, health)
    if result.get('enabled') is not True or health.get('fix_bridge_contract') != BRIDGE:
        raise PipelineError('author progress requires its complete paired bridge capability')
    if config.get('technical_fix_execution') is not None:
        raise PipelineError('author progress cannot enable pipeline execution')
    return result


def candidates(health):
    values = health.get('technical_fix_progress_candidates')
    if not isinstance(values, list) or len(values)>4096:
        raise PipelineError('author progress lacks its complete bounded checker allowlist')
    seen = set()
    for item in values:
        _fields(item, 'stage number head source attempt_id new_head')
        if pp._fix_candidate_key(item) != ('fix-pr', item['number'], item['head']):
            raise PipelineError('unsupported author progress target')
        _source(item['source']); head40(item['new_head'])
        from .review_routing_lease import decimal_id
        decimal_id(item['attempt_id'])
        if (item['number'] in seen or item['head'] != item['source']['head']
                or item['new_head'] == item['head']):
            raise PipelineError('author progress duplicates or changes its exact source')
        seen.add(item['number'])
    return values


def _source_gate(snapshot, config, proof, request):
    info = pp.epoch(snapshot, config['trusted_account'])
    pp.review_gate(snapshot, config, dict(head=request['new_head'],epoch=info['hash'],anchor=info['anchor_id']))
    if set(snapshot['labels']) & {'hold','ship','needs-decision','manual'}:
        raise PipelineError('author progress is stopped by a current routing label')
    anchor = proof.publication.payload['anchor']
    positions = [i for i,edge in enumerate(snapshot['edges']) if (edge.get('node') or {}).get('id') == anchor]
    if len(positions) != 1:
        raise PipelineError('author progress source anchor is missing or ambiguous')
    dangerous = {'HeadRefForcePushedEvent','HeadRefDeletedEvent','HeadRefRestoredEvent',
                 'BaseRefChangedEvent','BaseRefForcePushedEvent','BaseRefDeletedEvent',
                 'CommentDeletedEvent','MergedEvent'}
    for edge in snapshot['edges'][positions[0]+1:]:
        node=edge.get('node') or {}
        if node.get('__typename') in dangerous:
            raise PipelineError('author progress source crossed an unsafe lifecycle/ref event')
        if (node.get('__typename') == 'IssueComment'
                and (node.get('author') or {}).get('login') == config['trusted_account']
                and (node.get('lastEditedAt') is not None or pp.OVERRIDE.search(node.get('body') or ''))):
            raise PipelineError('author progress source crossed a trusted edit or override')
    before, after = proof.publication.payload['audit']['base_ref_oid'], snapshot['baseRefOid']
    return before, after


class ProgressGuard:
    def __init__(self, gh, config_path, token, *, clock=None):
        if not isinstance(config_path, str) or not config_path:
            raise PipelineError('author progress needs its actual configuration path')
        self.gh, self.config_path, self.token = gh, config_path, token
        self.request = requests.decode(token)
        self.seed = self.request['attempt']
        self.clock = clock or _now
        self._ticket = None

    def now(self):
        value=self.clock()
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PipelineError('author progress clock must be timezone aware')
        return value.astimezone(timezone.utc)

    def _phase(self, payload):
        value=parse(render(payload))
        if (value['pr'] != self.seed['pr'] or value['owner'] != self.seed['owner']
                or value.get('context_sha256') != context(self.seed)):
            raise PipelineError('author progress changes its signed source/owner/context')
        if value['kind'] == 'lease':
            if value['purpose'] != 'technical-attempt' or value['root'] != self.request['attempt_id']:
                raise PipelineError('author progress cannot renew another purpose or root')
        elif (value['kind'] != 'phase' or value['phase'] != 'fix-done'
                or value['attempt_id'] != self.request['attempt_id']):
            raise PipelineError('author progress cannot publish admission, delivery or a process start')
        return value

    def state(self, *, write=False):
        request=requests.decode(self.token)
        if request != self.request:
            raise PipelineError('author progress signed scope changed')
        config=pp.load_config(self.config_path)
        health=pp.run_health(config, config_path=self.config_path)
        policy=capability(config, health)
        requests.require_current(request, config, write=False)
        if (policy['consumer_sha256'] != request['reader_sha256']
                or policy['current_limit'] != request['current_limit']):
            raise PipelineError('author progress readers or current limit changed')
        expected=dict(stage='fix-pr',number=request['number'],head=request['head'],source=self.seed['source'],
                      attempt_id=request['attempt_id'],new_head=request['new_head'])
        if write and expected not in candidates(health):
            raise PipelineError('author progress left the fresh exact checker allowlist')
        snapshot=stable_timeline(self.gh, config, request['number'])
        ledger=verified_depth(snapshot, trusted=config['trusted_account'])['ledger']
        root=next((item for item in ledger.attempts if item.id==request['attempt_id']),None)
        proof=ledger.source_proof(self.seed['source'])
        if (root is None or proof is None or proof is not ledger.proofs[-1]
                or context(root.payload) != context(self.seed) or root.payload['executor']['kind'] != 'author'
                or proof.publication.payload['author'] != request['author']
                or snapshot['author'] != request['author'] or ledger.parking_id is not None
                or ledger.attempt_settlement(root) is not None):
            raise PipelineError('author progress lacks its current canonical author attempt/source')
        phases=ledger.attempt_phases(root)
        if ('handoff-done' not in phases or {'started','barrier'} & set(phases)
                or write and 'fix-done' in phases):
            raise PipelineError('author progress attempt is incomplete, started, ended or ambiguous')
        before, after=_source_gate(snapshot, config, proof, request)
        if before != after:
            graph=self.gh.json('api',f'repos/{request["repository"]}/compare/{before}...{after}')
            base=graph.get('base_commit') if isinstance(graph,dict) else None
            merge_base=graph.get('merge_base_commit') if isinstance(graph,dict) else None
            if (not isinstance(graph,dict) or graph.get('status')!='ahead'
                    or not isinstance(base,dict) or base.get('sha')!=before
                    or not isinstance(merge_base,dict) or merge_base.get('sha')!=before):
                raise PipelineError('author progress integration base did not move forward')
        details=verify(self.gh, config, snapshot, root.payload, root.id, request['new_head'])
        require_assigned(self.gh, dict(request, identity=snapshot['identity']))
        history,_deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=self.seed['pr'])
        view=resolve(_lease_nodes(history,root,purpose='technical-attempt'),
                     context_sha256=context(root.payload),purpose='technical-attempt',now=self.now())
        pp.ensure_identity(self.gh, config)
        return dict(config=config,health=health,policy=policy,snapshot=snapshot,
                    ledger=ledger,root=root,view=view,details=details)

    def __call__(self,payload,*,write=False):
        return self._check(payload,write=write)

    def _check(self,payload,*,write=False,after_waits=False):
        self._ticket=None
        value=self._phase(payload)
        state=self.state(write=write)
        view=state['view']; now=self.now()
        if value['kind']=='phase' and value['details'] != state['details']:
            raise PipelineError('author progress phase changes independently verified post-push evidence')
        if write:
            if value['kind']=='lease':
                same=value['owner']==view.active.owner
                if (value['previous']!=view.active.id
                        or not (same and view.active.time<=now<view.expires_at and view.expires_at-now<RENEW_WINDOW
                                or not same and now>=view.expires_at)):
                    raise PipelineError('author progress renewal/takeover is not canonical and eligible')
            else:
                view.require_owner(returned_id=value['lease_id'],owner=value['owner'],now=now)
            if not after_waits:
                require_task(self.request)
                admission.renew(requests._v1(self.request),state['config'])
            requests.require_current(self.request,state['config'])
            self._ticket=dict(snapshot=state['snapshot'],snapshot_sha256=sha(state['snapshot']),
                              body=render(value),health=copy.deepcopy(state['health']),view=view)
        return state['snapshot']

    def final_before_post(self,payload,snapshot):
        ticket,self._ticket=self._ticket,None
        if (ticket is None or snapshot is not ticket['snapshot']
                or sha(snapshot)!=ticket['snapshot_sha256'] or render(payload)!=ticket['body']):
            raise PipelineError('author progress has no one-use final write ticket')
        require_task(self.request)
        renewed=admission.renew(requests._v1(self.request),pp.load_config(self.config_path))
        self._check(payload,write=True,after_waits=True)
        ticket,self._ticket=self._ticket,None
        admission.verify_local(requests._v1(self.request))
        config=pp.load_config(self.config_path)
        policy=capability(config,ticket['health'])
        if (policy['consumer_sha256']!=self.request['reader_sha256']
                or policy['current_limit']!=self.request['current_limit']
                or pp.load_config(self.config_path)!=config):
            raise PipelineError('author progress final reader/config state changed')
        requests.require_current(self.request,config)
        now=self.now(); view=ticket['view']
        if datetime.fromisoformat(renewed['expires_at'])<=now:
            raise PipelineError('author progress reservation expired before publication')
        if payload['kind']=='lease':
            same=payload['owner']==view.active.owner
            if (payload['previous']!=view.active.id or payload['root']!=view.root.id
                    or not (same and view.active.time<=now<view.expires_at and view.expires_at-now<RENEW_WINDOW
                            or not same and now>=view.expires_at)):
                raise PipelineError('author progress lease changed or expired before publication')
        else:
            view.require_owner(returned_id=payload['lease_id'],owner=payload['owner'],now=now)


def select(gh, config, *, config_path, health):
    """Reserve an administrative source readback, never another content FIX."""
    from . import db
    policy=capability(config,health)
    values=[item for item in candidates(health) if item['number'] not in pp.admission_exclusions()]
    if not values:
        return None
    task=pp._pipeline_task_id(required=True)
    started=pp._pipeline_task_started_at(required=True)
    if pp._provider_ownership_kind(required=True)!='headless':
        raise PipelineError('author progress requires a local headless scheduler attempt')
    waiting=None
    for item in values:
        snapshot=stable_timeline(gh,config,item['number'])
        if snapshot['headRefOid']!=item['new_head']:
            raise PipelineError('author progress checker new HEAD is stale')
        ledger=verified_depth(snapshot,trusted=config['trusted_account'])['ledger']
        root=next((row for row in ledger.attempts if row.id==item['attempt_id']),None)
        if root is None or root.payload['source']!=item['source']:
            raise PipelineError('author progress checker source lacks its canonical server attempt')
        if ledger.attempt_phases(root).get('fix-done') is not None:
            waiting=dict(action='wait',stage='fix',protocol='review-routing-v1',verdict='ПУСТО',
                         number=item['number'],reason='author progress already has its completed server receipt')
            continue
        history,_deleted=records(snapshot,trusted=config['trusted_account'],expected_pr=root.payload['pr'])
        now=_now()
        view=resolve(_lease_nodes(history,root,purpose='technical-attempt'),
                     context_sha256=context(root.payload),purpose='technical-attempt',now=now)
        if now<view.expires_at:
            waiting=dict(action='wait',stage='fix',protocol='review-routing-v1',verdict='ПУСТО',
                         number=item['number'],reason='author progress waits for the current canonical owner lease',
                         retry_at=view.expires_at.isoformat())
            continue
        reservation=db.reserve_pipeline_target(config['repository'],'fix-pr',item['number'],item['head'],
            task,config['target_reservation_ttl_seconds'],task_started_at=started,
            ownership_kind='headless',strict_lifetime=True)
        if reservation is None:
            waiting=dict(action='wait',stage='fix',protocol='review-routing-v1',verdict='\u041f\u0423\u0421\u0422\u041e',
                         number=item['number'],reason='author progress exact target is reserved by another live attempt')
            continue
        value=requests.build(config,snapshot,policy,root,reservation)
        token=requests.encode(value)
        guard=ProgressGuard(gh,config_path,token)
        guard.state()
        require_task(value)
        admission.renew(requests._v1(value),config)
        requests.require_current(value,config)
        return dict(action='author-progress',stage='fix',protocol='review-routing-v1',lease=token,
                    target=item,mutation_authorized=False)
    return waiting


def complete(gh, config, token, *, config_path):
    from .review_routing_adapter import _journal, _recover_visible_pending
    from .review_routing_transport import Transport
    guard=ProgressGuard(gh,config_path,token)
    request=guard.request
    state=guard.state()
    journal=_journal(config)
    history,_deleted=records(state['snapshot'],trusted=config['trusted_account'],expected_pr=guard.seed['pr'])
    pending=journal.pending_for(guard.seed['pr'])
    if _recover_visible_pending(gh,config,state['snapshot'],history,journal,pending):
        raise PipelineError('author progress has an unresolved publication; preserve the engineering barrier')
    transport=Transport(gh,config,journal,guard)

    def publish(payload):
        return transport.publish(payload,owner=guard.seed['owner'],phase='author-progress:'+sha(payload))

    state=guard.state()
    root,view=state['root'],state['view']
    existing=state['ledger'].attempt_phases(root).get('fix-done')
    if existing is None:
        now=guard.now()
        if view.active.owner!=guard.seed['owner'] or view.expires_at-now<RENEW_WINDOW:
            same=view.active.owner==guard.seed['owner']
            if not same and now<view.expires_at:
                return dict(action='wait',stage='fix',protocol='review-routing-v1',provider_started=False,
                            source=guard.seed['source'],executor=guard.seed['executor'],
                            reason='author progress canonical owner lease remains active')
            publish(dict(protocol='review-routing-v1',kind='lease',pr=guard.seed['pr'],
                owner=guard.seed['owner'],context_sha256=context(root.payload),purpose='technical-attempt',
                root=root.id,previous=view.active.id))
        state=guard.state()
        root,view=state['root'],state['view']
        existing=publish(dict(protocol='review-routing-v1',kind='phase',pr=guard.seed['pr'],
            owner=guard.seed['owner'],context_sha256=context(root.payload),attempt_id=root.id,
            lease_id=view.active.id,phase='fix-done',details=state['details']))
    if existing.payload['details']!=state['details']:
        raise PipelineError('author progress completed source receipt disagrees with independent readback')
    return dict(action='fixed',stage='fix',protocol='review-routing-v1',attempt_id=root.id,
                source=guard.seed['source'],executor=guard.seed['executor'],new_head=request['new_head'],
                provider_started=False,mutation_authorized=False,
                reason='author post-push source verified; independent REVIEW may inspect the new HEAD')
