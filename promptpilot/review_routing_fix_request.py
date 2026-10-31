"""Closed, source-bound purposes for the claimed FIX bridge.

The current public bridge implements author assignment only. A start purpose is
reserved and rejected: an authenticated token must never fall into a skill.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import project_pipeline as pp
from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing import VERSION
from .review_routing_policy import ROLES, preflight
from .review_routing_request import RESERVATION_FIELDS, _aware
from .review_routing_wire import _fields, _integer, _text, canonical, context, hash64, pr_key, validate as wire_validate


ADMISSION = 'review-routing-v1-fix-admission'
PROGRESS = 'review-routing-v1-fix-progress'
START = 'review-routing-v1-fix-start'
PURPOSES = {ADMISSION, PROGRESS, START}
FIELDS = ('version protocol purpose stage target_stage repository number head identity author '
          'attempt attempt_id config_sha256 reader_sha256 current_limit pipeline_replicas '
          'target_reservation issued_at expires_at nonce')
BRIDGE = {'protocol': 'claimed-fix-v1', 'snapshot': 'complete', 'purposes': ['author-handoff']}


def capability(config, health):
    policy = preflight(config, health)
    if policy.get('enabled') is not True:
        raise PipelineError('claimed FIX bridge is disabled')
    if health.get('fix_bridge_contract') != BRIDGE:
        raise PipelineError('checker lacks the complete claimed FIX author-handoff capability')
    if config.get('technical_fix_execution') is not None:
        raise PipelineError('pipeline FIX execution is not supported by this bridge slice')
    return policy


def validate(value):
    _fields(value, FIELDS)
    if (type(value['version']) is not int or value['version'] != 1
            or value['protocol'] != VERSION or not isinstance(value['purpose'], str)
            or value['purpose'] not in {ADMISSION, PROGRESS}
            or value['stage'] != 'fix' or value['target_stage'] != 'fix-pr'):
        raise PipelineError('unsupported claimed FIX purpose or transport')
    try:
        identity = PRIdentityV1(**value['identity'])
    except (TypeError, KeyError) as exc:
        raise PipelineError('invalid claimed FIX identity') from exc
    attempt = wire_validate(value['attempt'])
    if (attempt['kind'] != 'attempt' or attempt['pr'] != pr_key(identity)
            or attempt['source']['identity_sha256'] != identity.sha256
            or attempt['source']['head'] != identity.head_sha
            or value['repository'] != identity.base_repository
            or value['number'] != identity.pr_number or type(value['number']) is not int
            or value['head'] != identity.head_sha):
        raise PipelineError('claimed FIX request changes its exact source/identity')
    _fields(value['author'], 'id login')
    _text(value['author']['id'])
    if (not isinstance(value['author']['id'], str) or not value['author']['id']
            or attempt['executor'] != {'kind': 'author', 'login': value['author']['login']}):
        raise PipelineError('claimed FIX request must address its bound author')
    from .review_routing_lease import decimal_id
    if value['attempt_id'] is not None:
        decimal_id(value['attempt_id'])
    if value['purpose'] == PROGRESS and value['attempt_id'] is None:
        raise PipelineError('FIX progress requires its canonical attempt ID')
    hash64(value['config_sha256'])
    _fields(value['reader_sha256'], ' '.join(ROLES))
    for digest in value['reader_sha256'].values():
        hash64(digest)
    _integer(value['current_limit'], 1, 16)
    _integer(value['pipeline_replicas'], 1, 16)
    reservation = value['target_reservation']
    _fields(reservation, RESERVATION_FIELDS)
    repo, stage, number, head, _task, _token = pp._reservation_identity(reservation)
    if ((repo, stage, number, head) != (value['repository'], 'fix-pr', value['number'], value['head'])
            or reservation['ownership_kind'] != 'headless'
            or any(reservation[name] not in ('', None) for name in
                   ['herdr_session_state', 'herdr_pane_id', 'herdr_tab_id', 'herdr_workspace_id'])):
        raise PipelineError('claimed FIX requires its exact local headless reservation')
    for name in ['task_started_at', 'reserved_at', 'expires_at']:
        _aware(reservation[name])
    issued, expires = value['issued_at'], value['expires_at']
    if type(issued) is not int or type(expires) is not int or issued < 0 or not 0 < expires-issued <= 7200:
        raise PipelineError('invalid claimed FIX lifetime')
    if not isinstance(value['nonce'], str) or not re.fullmatch('[0-9a-f]{32}', value['nonce']):
        raise PipelineError('invalid claimed FIX nonce')
    if len(canonical(value)) > 32768:
        raise PipelineError('claimed FIX request exceeds bounded size')
    return value


def encode(value):
    return pp.encode_signed_lease(validate(value))


def decode(token):
    if (not isinstance(token, str) or len(token) > 65536
            or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token)):
        raise PipelineError('invalid claimed FIX signed token')
    value = validate(pp.decode_signed_lease(token))
    if token.split('.')[0] != pp.encode_lease(value):
        raise PipelineError('noncanonical claimed FIX token')
    return value


def require_current(value, config, *, write=True):
    validate(value)
    if (pp.digest(config) != value['config_sha256'] or config.get('repository') != value['repository']
            or pp._configured_replica_count() != value['pipeline_replicas']):
        raise PipelineError('claimed FIX effective configuration changed')
    if write:
        pp._validate_lease_reservation(value, config)
    now = int(time.time())
    if value['issued_at'] > now+60 or write and now >= value['expires_at']:
        raise PipelineError('claimed FIX request expired or is not yet valid')


def renew(value, config):
    """Renew only a live exact reservation, reading time after SQLite waits.

    Unlike the legacy administrative renewal, an expired row is not revived
    just because a task with that integer ID is still marked running.
    """
    from . import db
    require_current(value, config)
    expected = value['target_reservation']
    with db._connect(immediate=True) as connection:
        task = connection.execute('SELECT * FROM tasks WHERE id=?', (expected['task_id'],)).fetchone()
        cancelled = connection.execute('SELECT value FROM settings WHERE key=?', ('cancel_task:'+str(expected['task_id']),)).fetchone()
        row = connection.execute('SELECT * FROM pipeline_target_reservations WHERE repository=? AND number=?',
                                 (expected['repository'], expected['number'])).fetchone()
        if (task is None or task['status'] != 'running' or task['started_at'] != expected['task_started_at']
                or any(task[name] for name in ['machine', 'detached', 'herdr_target', 'worktree'])
                or cancelled is not None and cancelled[0] == '1'
                or row is None or any(row[name] != expected[name] for name in RESERVATION_FIELDS.split() if name != 'expires_at')):
            raise PipelineError('claimed FIX task or exact reservation changed inside its renewal transaction')
        now = datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(row['expires_at'])
        if expiry.tzinfo is None or expiry.utcoffset() is None or expiry <= now:
            raise PipelineError('claimed FIX exact reservation expired; no resurrection')
        if now.timestamp() >= value['expires_at']:
            raise PipelineError('claimed FIX request expired during its database wait')
        expiry = max(expiry, now+timedelta(seconds=config['target_reservation_ttl_seconds']))
        connection.execute('UPDATE pipeline_target_reservations SET expires_at=?,strict_lifetime=1 WHERE repository=? AND number=?',
                           (expiry.isoformat(), expected['repository'], expected['number']))
        return dict(db._reservation_dict(row), expires_at=expiry.isoformat())


def verify_local(value):
    """One read snapshot after remote work; no renewal or expiry resurrection."""
    from . import db
    expected = value['target_reservation']
    with db._connect() as connection:
        row = connection.execute('''SELECT r.* FROM pipeline_target_reservations r
            JOIN tasks t ON t.id=r.task_id
            WHERE r.repository=? AND r.number=? AND t.status='running' AND t.started_at=r.task_started_at
              AND COALESCE(t.machine,'')='' AND COALESCE(t.detached,0)=0
              AND COALESCE(t.worktree,0)=0 AND COALESCE(t.herdr_target,'')=''
              AND NOT EXISTS (SELECT 1 FROM settings WHERE key=? AND value='1')''',
            (expected['repository'], expected['number'], 'cancel_task:'+str(expected['task_id']))).fetchone()
        if (row is None or row['strict_lifetime']!=1
                or any(row[name] != expected[name] for name in RESERVATION_FIELDS.split() if name != 'expires_at')):
            raise PipelineError('claimed FIX final task/cancel/reservation snapshot changed')
        expiry = datetime.fromisoformat(row['expires_at'])
        if expiry.tzinfo is None or expiry.utcoffset() is None or datetime.now(timezone.utc) >= expiry:
            raise PipelineError('claimed FIX final reservation expired')


def build(config, snapshot, policy, attempt, reservation):
    issued = int(time.time())
    value = {'version': 1, 'protocol': VERSION, 'purpose': ADMISSION, 'stage': 'fix',
             'target_stage': 'fix-pr', 'repository': config['repository'], 'number': attempt['pr']['pr_number'],
             'head': attempt['source']['head'], 'identity': snapshot['identity'], 'author': snapshot['author'],
             'attempt': attempt, 'attempt_id': None, 'config_sha256': pp.digest(config),
             'reader_sha256': policy['consumer_sha256'], 'current_limit': policy['current_limit'],
             'pipeline_replicas': pp._configured_replica_count(), 'target_reservation': reservation,
             'issued_at': issued, 'expires_at': issued+min(7200, config['review_lease_seconds']),
             'nonce': secrets.token_hex(16)}
    # context() also closes the immutable source/executor root before issuance.
    context(attempt)
    return validate(json.loads(canonical(value)))
