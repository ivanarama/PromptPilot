"""Closed signed REVIEW requests for the future public routing adapter.

Signature/schema validation is not a current stage gate. The public adapter
must freshly prove policy, actor, coherent identity/history, canonical ownership
and expiry after all slow work. No public route or capability is enabled here.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import secrets
import time
from uuid import uuid4

from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing import VERSION
from .review_routing_lease import decimal_id,owner_id
from .review_routing_policy import ROLES
from .review_routing_wire import _fields,_integer,_pr,_text,audit_binding,canonical,hash64,pr_key


AUDIT='review-routing-v1-audit'
RECOVERY='review-routing-v1-publication-recovery'
FIELDS=('version protocol purpose stage repository number pr_key identity identity_sha256 author '
        'head epoch anchor audit config_sha256 reader_sha256 depth budget_used current_limit '
        'resume_id owner_uuid issued_at expires_at nonce pipeline_replicas target_reservation recovery')
RESERVATION_FIELDS=('repository stage number head task_id task_started_at ownership_kind '
                    'herdr_session_state herdr_pane_id herdr_tab_id herdr_workspace_id token reserved_at expires_at')


def _aware(value):
    if not isinstance(value,str):raise PipelineError('routing task reservation timestamp is invalid')
    try:parsed=datetime.fromisoformat(value)
    except ValueError as exc:raise PipelineError('routing task reservation timestamp is invalid') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PipelineError('routing task reservation timestamp must be timezone aware')


def validate(request):
    """Validate closed serialization only; do not mint current authorization."""
    from . import project_pipeline as pp

    _fields(request,FIELDS)
    if (type(request['version']) is not int or request['version']!=1
            or request['protocol']!=VERSION or not isinstance(request['purpose'],str)
            or request['purpose'] not in {AUDIT,RECOVERY}
            or request['stage']!='review'):
        raise PipelineError('unsupported typed REVIEW request')
    try:identity=PRIdentityV1(**request['identity'])
    except (TypeError,KeyError) as exc:raise PipelineError('invalid typed REVIEW request identity') from exc
    _pr(request['pr_key'])
    if (request['repository']!=identity.base_repository or request['number']!=identity.pr_number
            or type(request['number']) is not int or request['pr_key']!=pr_key(identity)
            or request['head']!=identity.head_sha or request['identity_sha256']!=identity.sha256):
        raise PipelineError('typed REVIEW request contradicts its full identity')
    _fields(request['author'],'id login');_text(request['author']['id'])
    if not isinstance(request['author']['login'],str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})',request['author']['login']):
        raise PipelineError('invalid typed REVIEW request author')
    hash64(request['epoch']);_text(request['anchor']);audit_binding(request['audit'])
    hash64(request['config_sha256']);_fields(request['reader_sha256'],' '.join(ROLES))
    for value in request['reader_sha256'].values():hash64(value)
    _integer(request['depth'],0,2147483647);_integer(request['budget_used'],0,16)
    _integer(request['current_limit'],1,16);_integer(request['pipeline_replicas'],1,16)
    if request['resume_id'] is not None:decimal_id(request['resume_id'])
    owner_id(request['owner_uuid'])
    issued,expires=request['issued_at'],request['expires_at']
    if type(issued) is not int or type(expires) is not int or issued<0 or not 0<expires-issued<=28800:
        raise PipelineError('invalid typed REVIEW request lifetime')
    if not isinstance(request['nonce'],str) or not re.fullmatch('[0-9a-f]{32}',request['nonce']):
        raise PipelineError('invalid typed REVIEW request nonce')
    reservation=request['target_reservation']
    if reservation is None:
        if request['pipeline_replicas']!=1:raise PipelineError('replicated typed REVIEW requires a task reservation')
    else:
        _fields(reservation,RESERVATION_FIELDS)
        repo,stage,number,head,_task,_token=pp._reservation_identity(reservation)
        if (repo,stage,number,head)!=(request['repository'].lower(),'review',request['number'],request['head']):
            raise PipelineError('typed REVIEW request reservation contradicts target')
        if any(reservation[key]!=expected for key,expected in [('repository',repo),('stage',stage),('number',number),('head',head)]):
            raise PipelineError('typed REVIEW task reservation is not canonical')
        if not isinstance(reservation['ownership_kind'],str) or reservation['ownership_kind'] not in {'headless','herdr'}:
            raise PipelineError('typed REVIEW request has no owned task process')
        for name in ['task_started_at','reserved_at','expires_at']:_aware(reservation[name])
        for name in ['herdr_session_state','herdr_pane_id','herdr_tab_id','herdr_workspace_id']:
            value=reservation[name]
            if value is not None and (not isinstance(value,str) or any(ord(char)<32 for char in value)):
                raise PipelineError('invalid typed REVIEW task descriptor')
    recovery=request['recovery']
    if request['purpose']==AUDIT:
        if recovery is not None:raise PipelineError('audit request cannot borrow recovery scope')
    else:
        _fields(recovery,'publication_id context_sha256')
        decimal_id(recovery['publication_id']);hash64(recovery['context_sha256'])
    if len(canonical(request))>32768:raise PipelineError('typed REVIEW request exceeds bounded size')
    return request


def require_current(request,config,*,now=None,write=True):
    """Cheap local binding/expiry check for use AFTER slow external/DB work.

    This does not read GitHub, renew SQLite or establish server lease ownership.
    The public final-before-POST gate must also repeat its active returned ID.
    """
    from . import project_pipeline as pp

    validate(request)
    ttl=config.get('review_lease_seconds',7200)
    if type(ttl) is not int or not 300<=ttl<=28800:
        raise PipelineError('invalid configured typed REVIEW lifetime')
    if (pp.digest(config)!=request['config_sha256'] or config.get('repository')!=request['repository']
            or request['expires_at']-request['issued_at']>ttl):
        raise PipelineError('typed REVIEW effective configuration changed')
    if write:
        if pp._configured_replica_count()!=request['pipeline_replicas']:
            raise PipelineError('typed REVIEW replica count changed')
        if request['target_reservation'] is None and pp._pipeline_task_id(required=False) is not None:
            raise PipelineError('pipeline task cannot borrow an unreserved manual REVIEW request')
        pp._validate_lease_reservation(request,config)
    current=int(time.time()) if now is None else now
    if type(current) is not int:raise PipelineError('typed REVIEW clock must be integral UTC seconds')
    if request['issued_at']>current+60 or (write and request['expires_at']<=current):
        raise PipelineError('typed REVIEW request expired or is not yet valid')


def encode(request):
    from .project_pipeline import encode_signed_lease
    validate(request)
    return encode_signed_lease(request)


def decode(token):
    from .project_pipeline import decode_signed_lease,encode_lease
    if (not isinstance(token,str) or len(token)>65536
            or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',token)):
        raise PipelineError('typed REVIEW token is absent or oversized')
    value=validate(decode_signed_lease(token))
    if token.split('.')[0]!=encode_lease(value):
        raise PipelineError('noncanonical typed REVIEW request payload')
    return value


def build(config,snapshot,policy,*,depth,budget_used,resume_id=None,reservation=None,recovery=None,now=None):
    """Freeze already-proved public-stage inputs; not an election or gate.

    The service generates UUID/nonce. Neither report nor public wire owner can
    choose them. A new recovery scope preserves the canonical root's audit.
    """
    from . import project_pipeline as pp
    from .review_routing_baseline import capture

    _fields(policy,'enabled protocol current_limit consumer_sha256 mutation_authorized')
    if policy['enabled'] is not True or policy['protocol']!=VERSION or policy['mutation_authorized'] is not False:
        raise PipelineError('typed REVIEW requires complete read-only paired eligibility')
    if reservation is None and pp._pipeline_task_id(required=False) is not None:
        raise PipelineError('typed pipeline REVIEW requires its exact task reservation')
    identity=PRIdentityV1(**snapshot['identity'])
    issued=int(time.time()) if now is None else now
    ttl=config.get('review_lease_seconds',7200)
    if type(issued) is not int or type(ttl) is not int or not 300<=ttl<=28800:
        raise PipelineError('invalid typed REVIEW request issuance time/lifetime')
    if recovery is not None:
        _fields(recovery,'publication_id context_sha256 audit')
    value={'version':1,'protocol':VERSION,'purpose':RECOVERY if recovery is not None else AUDIT,
        'stage':'review','repository':config['repository'],'number':identity.pr_number,
        'pr_key':pr_key(identity),'identity':identity.to_dict(),'identity_sha256':identity.sha256,
        'author':snapshot['author'],'head':identity.head_sha,'epoch':pp.epoch(snapshot,config['trusted_account'])['hash'],
        'anchor':pp.epoch(snapshot,config['trusted_account'])['anchor_id'],
        'audit':recovery['audit'] if recovery is not None else capture(snapshot),
        'config_sha256':pp.digest(config),'reader_sha256':policy['consumer_sha256'],
        'depth':depth,'budget_used':budget_used,'current_limit':policy['current_limit'],'resume_id':resume_id,
        'owner_uuid':str(uuid4()),'issued_at':issued,'expires_at':issued+ttl,'nonce':secrets.token_hex(16),
        'pipeline_replicas':pp._configured_replica_count(),'target_reservation':reservation,
        'recovery':{key:recovery[key] for key in ['publication_id','context_sha256']} if recovery is not None else None}
    # Freeze caller-owned dictionaries before validating/signing later.
    return validate(json.loads(canonical(value)))
