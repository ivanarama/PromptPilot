"""Closed pipeline START binding; serialization does not authorize a launch.

The coordinator must independently verify canonical attempt/started/lease and
source history, actor, current checker/readers and task after all waits. This
schema alone does not connect the worker factory or enable production execution.
"""
import json
import re
import secrets
import time

from . import project_pipeline as pp
from . import review_routing_pipeline_policy as policy
from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing_execution import freeze_scope
from .review_routing_fix_request import START
from .review_routing_lease import decimal_id
from .review_routing_wire import _fields,canonical,context,pr_key,validate as wire_validate

FIELDS='version protocol purpose scope identity author attempt lease_id pipeline_replicas issued_at expires_at nonce'


def validate(value):
    _fields(value,FIELDS)
    if (type(value['version']) is not int or value['version']!=1
            or value['protocol']!='review-routing-v1' or value['purpose']!=START):
        raise PipelineError('unsupported exact pipeline START purpose/version')
    scope=freeze_scope(value['scope'])
    attempt=wire_validate(value['attempt'])
    try:identity=PRIdentityV1(**value['identity'])
    except (TypeError,KeyError) as exc:raise PipelineError('pipeline START lost full identity') from exc
    if (attempt['kind']!='attempt' or identity.cross_repository
            or scope['pr']!=pr_key(identity) or scope['repository']!=identity.base_repository
            or attempt['pr']!=scope['pr'] or attempt['source']!=scope['source']
            or attempt['owner']!=scope['owner'] or attempt['executor']!=scope['executor']
            or scope['context_sha256']!=context(attempt)
            or identity.head_sha!=scope['source']['head']
            or identity.sha256!=scope['source']['identity_sha256']):
        raise PipelineError('pipeline START changes its canonical attempt/source/executor/identity')
    _fields(value['author'],'id login')
    if any(not isinstance(part,str) or not part or any(ord(c)<32 or ord(c)==127 for c in part)
           for part in value['author'].values()):
        raise PipelineError('pipeline START author node identity is invalid')
    decimal_id(value['lease_id'])
    if scope['attempt_id']==scope['started_id']:
        raise PipelineError('pipeline START must bind a distinct canonical started receipt')
    if type(value['pipeline_replicas']) is not int or not 1<=value['pipeline_replicas']<=16:
        raise PipelineError('pipeline START replica count is invalid')
    issued,expires=value['issued_at'],value['expires_at']
    if type(issued) is not int or type(expires) is not int or issued<0 or not 0<expires-issued<=1800:
        raise PipelineError('pipeline START lifetime is invalid')
    if not isinstance(value['nonce'],str) or not re.fullmatch('[0-9a-f]{32}',value['nonce']):
        raise PipelineError('pipeline START nonce is invalid')
    if len(canonical(value))>32768:raise PipelineError('pipeline START scope exceeds bounded size')
    return value


def encode(value):return pp.encode_signed_lease(validate(value))


def decode(token):
    if not isinstance(token,str) or len(token)>65536 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',token):
        raise PipelineError('invalid signed pipeline START token')
    value=validate(pp.decode_signed_lease(token))
    if token.split('.')[0]!=pp.encode_lease(value):raise PipelineError('noncanonical pipeline START token')
    return value


def require_current(value,config,*,write=True):
    validate(value)
    policy.require(config,value['identity'])
    scope=value['scope']
    if (pp.digest(config)!=scope['config_sha256']
            or value['pipeline_replicas']!=pp._configured_replica_count()
            or scope['executor']['login']!=config.get('trusted_account')):
        raise PipelineError('pipeline START effective policy/replicas changed')
    if write:
        pp._validate_lease_reservation(dict(target_reservation=scope['reservation'],
            pipeline_replicas=value['pipeline_replicas'],target_stage='fix-pr',
            number=scope['pr']['pr_number'],head=scope['source']['head']),config)
    now=int(time.time())
    if value['issued_at']>now+60 or write and now>=value['expires_at']:
        raise PipelineError('pipeline START scope expired or is not yet valid')


def build(config,snapshot,paired,root,started,reservation):
    identity=policy.require(config,snapshot['identity'])
    seed=root.payload
    record=wire_validate(started.payload)
    if (record['kind']!='phase' or record['phase']!='started'
            or record['attempt_id']!=root.id or record['pr']!=seed['pr']
            or record['owner']!=seed['owner'] or record['context_sha256']!=context(seed)):
        raise PipelineError('pipeline START requires its exact canonical started receipt')
    scope=dict(version=1,repository=config['repository'],pr=seed['pr'],source=seed['source'],
        attempt_id=root.id,started_id=started.id,context_sha256=context(seed),owner=seed['owner'],
        executor=seed['executor'],attempt_uuid=record['details']['attempt_uuid'],
        task_id=record['details']['task_id'],task_started_at=reservation['task_started_at'],
        reservation=reservation,config_sha256=pp.digest(config),reader_sha256=paired['consumer_sha256'])
    now=int(time.time())
    result=dict(version=1,protocol='review-routing-v1',purpose=START,scope=scope,
        identity=identity.to_dict(),author=snapshot['author'],attempt=seed,lease_id=record['lease_id'],
        pipeline_replicas=pp._configured_replica_count(),issued_at=now,
        expires_at=now+min(1800,config['target_reservation_ttl_seconds']),nonce=secrets.token_hex(16))
    return validate(json.loads(canonical(result)))
