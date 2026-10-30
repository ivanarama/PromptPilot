"""Claimed-task, source-bound author FIX handoff. No provider is authorized.

The pipeline start/post-push/settlement bridge is deliberately unsupported in
this slice. Its signed purposes must be refused before any generic fallback.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from uuid import uuid4

from . import db, project_pipeline as pp
from . import review_routing_fix_request as requests
from .pipeline_errors import PipelineError
from .review_routing_assignment import Assignment, require_assigned
from .review_routing_history import verified_depth
from .review_routing_lease import resolve
from .review_routing_proof import _lease_nodes
from .review_routing_snapshot import stable_timeline
from .review_routing_transport import Transport
from .review_routing_wire import _fields, _source, context, parse, records, render, sha


def candidates(health):
    values = health.get('technical_fix_candidates')
    if not isinstance(values, list) or len(values) > 4096:
        raise PipelineError('checker lacks the complete source-bound technical FIX allowlist')
    seen = set()
    for item in values:
        _fields(item, 'stage number head source')
        if pp._fix_candidate_key(item) != ('fix-pr', item['number'], item['head']):
            raise PipelineError('technical FIX allowlist contains an unsupported target')
        _source(item['source'])
        if item['head'] != item['source']['head'] or item['number'] in seen:
            raise PipelineError('technical FIX allowlist duplicates or changes a source')
        seen.add(item['number'])
    return values


def require_task(request):
    task = db.get_task(request['target_reservation']['task_id'])
    expected = request['target_reservation']
    if (task is None or task.status.value != 'running' or task.started_at is None
            or task.started_at.astimezone(timezone.utc).isoformat() != expected['task_started_at']
            or task.machine or task.detached or task.herdr_target or task.worktree
            or db.is_cancel_requested(task.id)):
        raise PipelineError('claimed FIX task changed, was cancelled or uses an unsupported transport')


class FixGuard:
    """Fresh gates for one immutable author attempt, never a REVIEW guard."""
    def __init__(self, gh, config_path, token):
        if not isinstance(config_path, str) or not config_path:
            raise PipelineError('claimed FIX requires its effective configuration path')
        self.gh, self.config_path, self.token = gh, config_path, token
        self.request = requests.decode(token)
        if self.request['purpose'] != requests.ADMISSION:
            raise PipelineError('FIX progress cannot substitute for admission or delivery')
        self.seed = self.request['attempt']
        self._ticket = None

    def __call__(self, payload, *, write=False):
        return self._check(payload, write=write)

    def assignment_gate(self):
        return self._check(self.seed, write=True, assignment=True)

    def _check(self, payload, *, write=False, assignment=False, after_waits=False):
        self._ticket = None
        value = parse(render(payload))
        request = requests.decode(self.token)
        if request != self.request:
            raise PipelineError('claimed FIX signed scope changed')
        if (value['kind'] not in {'attempt', 'phase'}
                or value['pr'] != self.seed['pr'] or value['owner'] != self.seed['owner']
                or value['kind'] == 'attempt' and value != self.seed
                or value['kind'] == 'phase' and value['phase'] not in {'assigned', 'handoff-done'}):
            raise PipelineError('author FIX admission cannot publish another operation')
        config = pp.load_config(self.config_path)
        health = pp.run_health(config, config_path=self.config_path)
        policy = requests.capability(config, health)
        requests.require_current(request, config, write=False)
        if (policy['consumer_sha256'] != request['reader_sha256']
                or policy['current_limit'] != request['current_limit']):
            raise PipelineError('claimed FIX paired readers or current budget changed')
        expected = {'stage': 'fix-pr', 'number': request['number'], 'head': request['head'],
                    'source': self.seed['source']}
        if expected not in candidates(health):
            raise PipelineError('claimed FIX exact source left the fresh checker allowlist')
        snapshot = stable_timeline(self.gh, config, request['number'])
        verified = verified_depth(snapshot, trusted=config['trusted_account'])
        ledger = verified['ledger']
        proof = ledger.source_proof(self.seed['source'])
        if (proof is None or proof is not ledger.proofs[-1] or proof.outcome != 'technical-handoff'
                or context(proof.publication.payload) != self.seed['context_sha256']):
            raise PipelineError('claimed FIX lacks its latest canonical technical REVIEW source')
        if (snapshot['identity'] != request['identity'] or snapshot['author'] != request['author']
                or proof.publication.payload['identity'] != request['identity']
                or proof.publication.payload['author'] != request['author']):
            raise PipelineError('claimed FIX full PR identity, fork or author changed')
        root_info = proof.publication.payload
        pp.review_gate(snapshot, config, {'head': request['head'], 'epoch': root_info['epoch'], 'anchor': root_info['anchor']})
        if (ledger.parking_id is not None or self.seed['attempt'] > policy['current_limit']):
            raise PipelineError('claimed FIX is parked or exceeds the current lifetime budget')
        roots = [item for item in ledger.attempts if item.payload['source'] == self.seed['source']]
        root = roots[0] if roots else None
        view = None
        if root is not None:
            if context(root.payload) != context(self.seed):
                raise PipelineError('claimed FIX canonical root has another immutable executor/context')
            history, _deleted = records(snapshot, trusted=config['trusted_account'], expected_pr=self.seed['pr'])
            view = resolve(_lease_nodes(history, root, purpose='technical-attempt'),
                           context_sha256=context(root.payload), purpose='technical-attempt', now=datetime.now(timezone.utc))
            phases = ledger.attempt_phases(root)
            if write and ('barrier' in phases or 'fix-done' in phases or 'started' in phases):
                raise PipelineError('claimed FIX attempt already ended or has an execution barrier')
        if write:
            if assignment:
                if root is None:
                    raise PipelineError('author assignment requires a canonical attempt receipt')
                if {'assigned', 'handoff-done'} & set(ledger.attempt_phases(root)):
                    raise PipelineError('author assignment already has server receipts; respect later manual changes')
            elif value['kind'] == 'attempt':
                admission = ledger.next_attempt_admission(self.seed['source'], max_attempts=policy['current_limit'])
                if root is not None or not admission['eligible']:
                    raise PipelineError('canonical FIX root already exists or admission changed; recover it')
                if (self.seed['attempt'] != ledger.budget_used+1
                        or self.seed['previous'] != (ledger.attempts[-1].id if ledger.attempts else None)
                        or self.seed['series'] != (ledger.attempts[0].payload['series'] if ledger.attempts else proof.completion.id)):
                    raise PipelineError('claimed FIX lifetime series changed before reservation')
            else:
                if (root is None or value['attempt_id'] != root.id or value['context_sha256'] != context(root.payload)
                        or value['phase'] in ledger.attempt_phases(root)):
                    raise PipelineError('claimed FIX phase has no exact canonical attempt or is already present')
                if value['phase'] == 'handoff-done' and 'assigned' not in ledger.attempt_phases(root):
                    raise PipelineError('claimed FIX handoff requires verified assignment first')
                if not after_waits:
                    Assignment(db.DB_PATH).receipt(request, root.id)
                require_assigned(self.gh, request)
            if view is not None:
                view.require_owner(returned_id=view.active.id if assignment else value.get('lease_id', root.id),
                                   owner=self.seed['owner'], now=datetime.now(timezone.utc))
        pp.ensure_identity(self.gh, config)
        renewed = None
        if write and not after_waits:
            require_task(request)
            renewed = requests.renew(request, config)
        requests.require_current(request, config, write=write)
        if write:
            self._ticket = {'snapshot': snapshot, 'snapshot_hash': sha(snapshot), 'body': render(value),
                            'health': copy.deepcopy(health), 'view': view, 'reservation': renewed, 'assignment': assignment}
        return snapshot

    def final_before_post(self, payload, snapshot):
        ticket, self._ticket = self._ticket, None
        if (ticket is None or snapshot is not ticket['snapshot'] or sha(snapshot) != ticket['snapshot_hash']
                or render(payload) != ticket['body']):
            raise PipelineError('claimed FIX has no one-use final write ticket')
        require_task(self.request)
        renewed = requests.renew(self.request, pp.load_config(self.config_path))
        # A DB wait can outlast the earlier server snapshot. Repeat actor,
        # checker/source/identity/canonical lease AFTER that wait, without
        # another write transaction that would move the same race later.
        self._check(payload, write=True, assignment=ticket['assignment'], after_waits=True)
        ticket, self._ticket = self._ticket, None
        requests.verify_local(self.request)
        # All database waits precede the last actual config/reader and clock checks.
        config = pp.load_config(self.config_path)
        policy = requests.capability(config, ticket['health'])
        if policy['consumer_sha256'] != self.request['reader_sha256'] or policy['current_limit'] != self.request['current_limit']:
            raise PipelineError('claimed FIX final reader/config policy changed')
        if pp.load_config(self.config_path) != config:
            raise PipelineError('claimed FIX effective configuration changed during final reader checks')
        requests.require_current(self.request, config)
        now = datetime.now(timezone.utc)
        if datetime.fromisoformat(renewed['expires_at']) <= now:
            raise PipelineError('claimed FIX reservation expired before mutation')
        if ticket['view'] is not None:
            ticket['view'].require_owner(returned_id=ticket['view'].active.id, owner=self.seed['owner'], now=now)


def next_fix(gh, config, *, config_path, health):
    policy = requests.capability(config, health)
    ordered = [item for item in candidates(health) if item['number'] not in pp.admission_exclusions()]
    if not ordered:
        return {'action': 'empty', 'stage': 'fix', 'verdict': 'ПУСТО', 'reason': 'no source-bound technical FIX target'}
    # Unlike legacy parallel FIX, this explicitly versioned path admits one
    # claimed local lane. It never enqueues or changes that task's timeout.
    task_id = pp._pipeline_task_id(required=True)
    started = pp._pipeline_task_started_at(required=True)
    if pp._provider_ownership_kind(required=True) != 'headless':
        raise PipelineError('claimed FIX supports only local headless transport')
    own = next((row for row in db.list_pipeline_target_reservations(repository=config['repository'])
                if row['task_id'] == task_id), None)
    if own is not None:
        ordered = [item for item in ordered if (own['stage'], own['number'], own['head']) == pp._fix_candidate_key(item)]
        if not ordered:
            raise PipelineError('claimed FIX reservation target changed; no retargeting')
    for item in ordered:
        snapshot = stable_timeline(gh, config, item['number'])
        ledger = verified_depth(snapshot, trusted=config['trusted_account'])['ledger']
        admission = ledger.next_attempt_admission(item['source'], max_attempts=policy['current_limit'])
        if not admission['eligible']:
            return {'action': 'wait', 'stage': 'fix', 'verdict': 'ПУСТО', 'reason': admission['reason']}
        proof = ledger.source_proof(item['source'])
        if snapshot['identity'] != proof.publication.payload['identity'] or snapshot['author'] != proof.publication.payload['author']:
            raise PipelineError('claimed FIX source identity or author changed before election')
        pp.review_gate(snapshot, config, dict(head=item['head'], epoch=proof.source['epoch'], anchor=proof.publication.payload['anchor']))
        pp.ensure_identity(gh, config)
        reservation = db.reserve_pipeline_target(config['repository'], 'fix-pr', item['number'], item['head'],
            task_id, config['target_reservation_ttl_seconds'], task_started_at=started, ownership_kind='headless')
        if reservation is None:
            if own is not None:
                raise PipelineError('claimed FIX exact reservation was lost; no replacement target')
            continue
        seed = {'protocol': 'review-routing-v1', 'kind': 'attempt', 'pr': snapshot['pr_key'], 'owner': str(uuid4()),
                'context_sha256': context(proof.publication.payload), 'source': proof.source,
                'series': ledger.attempts[0].payload['series'] if ledger.attempts else proof.completion.id,
                'previous': ledger.attempts[-1].id if ledger.attempts else None, 'attempt': ledger.budget_used+1,
                'executor': {'kind': 'author', 'login': snapshot['author']['login']}}
        request = requests.build(config, snapshot, policy, seed, reservation)
        token = requests.encode(request)
        FixGuard(gh, config_path, token)(seed)
        require_task(request)
        requests.renew(request, config)
        requests.require_current(request, config)
        return {'action': 'author-handoff', 'stage': 'fix', 'protocol': 'review-routing-v1', 'lease': token,
                'target': item, 'mutation_authorized': False}
    return {'action': 'wait', 'stage': 'fix', 'verdict': 'ПУСТО', 'reason': 'all technical FIX targets are reserved'}


def complete_fix(gh, config, token, *, config_path):
    from .review_routing_adapter import _journal
    guard = FixGuard(gh, config_path, token)
    request, seed = guard.request, guard.seed
    guard(seed)
    journal = _journal(config)
    transport = Transport(gh, config, journal, guard)
    def publish(value):
        return transport.publish(value, owner=seed['owner'], phase=value['kind']+':'+sha(value))
    for pending in journal.pending_for(seed['pr']):
        value = parse(pending['body'])
        transport.publish(value, owner=pending['owner'], phase=pending['phase'])
    snapshot = guard(seed)
    ledger = verified_depth(snapshot, trusted=config['trusted_account'])['ledger']
    root = next((item for item in ledger.attempts if item.payload['source'] == seed['source']), None)
    created_here = root is None
    if root is None:
        root = publish(seed)
    if root.payload != seed:
        raise PipelineError('canonical FIX attempt belongs to another owner; no delivery or provider fallback')
    existing_phases = ledger.attempt_phases(root)
    if {'assigned', 'handoff-done'} & set(existing_phases):
        Assignment(db.DB_PATH).receipt(request, root.id)
        require_assigned(gh, request)
    else:
        Assignment(db.DB_PATH).deliver(gh, request, root.id, guard, allow_new=created_here)
    for phase in ['assigned', 'handoff-done']:
        snapshot = guard(seed)
        ledger = verified_depth(snapshot, trusted=config['trusted_account'])['ledger']
        existing = ledger.attempt_phases(root).get(phase)
        if existing is not None:
            continue
        history, _deleted = records(snapshot, trusted=config['trusted_account'], expected_pr=seed['pr'])
        view = resolve(_lease_nodes(history, root, purpose='technical-attempt'), context_sha256=context(seed),
                       purpose='technical-attempt', now=datetime.now(timezone.utc))
        publish({'protocol': seed['protocol'], 'kind': 'phase', 'pr': seed['pr'], 'owner': seed['owner'],
                 'context_sha256': context(seed), 'attempt_id': root.id, 'lease_id': view.active.id,
                 'phase': phase, 'details': {}})
    return {'action': 'handed-off', 'stage': 'fix', 'protocol': seed['protocol'], 'attempt_id': root.id,
            'source': seed['source'], 'executor': seed['executor'], 'provider_started': False,
            'mutation_authorized': False, 'reason': 'author assignment verified; FIX completion still awaits post-push proof'}
