"""Closed administrative settlement purpose; never content execution authority."""
from datetime import datetime,timedelta,timezone
import re
import time

from . import db,project_pipeline as pp
from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing_request import RESERVATION_FIELDS,_aware,_local_reservation
from .review_routing_wire import _fields,context,hash64,pr_key,validate as wire_validate
from .review_routing_lease import decimal_id,owner_id
from .review_routing_policy import ROLES

PURPOSE='review-routing-v1-fix-settlement'
FIELDS=('version protocol purpose repository identity author attempt attempt_id owner config_sha256 '
        'reader_sha256 pipeline_replicas target_reservation evidence_sha256 issued_at expires_at nonce')


def validate(value):
    _fields(value,FIELDS)
    if type(value['version']) is not int or value['version']!=1 or value['protocol']!='review-routing-v1' or value['purpose']!=PURPOSE:
        raise PipelineError('unsupported technical settlement purpose/version')
    try:identity=PRIdentityV1(**value['identity'])
    except (KeyError,TypeError) as exc:raise PipelineError('settlement lost full current identity') from exc
    seed=wire_validate(value['attempt'])
    if (seed['kind']!='attempt' or seed['pr']!=pr_key(identity) or value['repository']!=identity.base_repository
            or identity.head_sha==seed['source']['head']
            or PRIdentityV1(**dict(value['identity'],head_sha=seed['source']['head'])).sha256!=seed['source']['identity_sha256']):
        raise PipelineError('settlement changes lifetime source identity or lacks a new current HEAD')
    context(seed);decimal_id(value['attempt_id']);owner_id(value['owner'])
    _fields(value['author'],'id login')
    if any(not isinstance(part,str) or not part or any(ord(c)<32 or ord(c)==127 for c in part) for part in value['author'].values()):
        raise PipelineError('settlement lacks canonical author node identity')
    for name in ['config_sha256','evidence_sha256']:hash64(value[name])
    _fields(value['reader_sha256'],' '.join(ROLES))
    for digest in value['reader_sha256'].values():hash64(digest)
    replicas=value['pipeline_replicas']
    if type(replicas) is not int or not 1<=replicas<=16:raise PipelineError('settlement replicas are invalid')
    reservation=value['target_reservation'];_fields(reservation,RESERVATION_FIELDS)
    repository,stage,number,head,_task,_token=pp._reservation_identity(reservation)
    if ((repository,stage,number,head)!=(identity.base_repository,'fix-pr',identity.pr_number,identity.head_sha)
            or reservation['ownership_kind']!='headless'
            or any(reservation[name] not in ('',None) for name in ['herdr_session_state','herdr_pane_id','herdr_tab_id','herdr_workspace_id'])):
        raise PipelineError('settlement changes its exact new-head headless target')
    for name in ['reserved_at','expires_at','task_started_at']:_aware(reservation[name])
    issued,expires=value['issued_at'],value['expires_at']
    if type(issued) is not int or type(expires) is not int or issued<0 or not 0<expires-issued<=1800:
        raise PipelineError('settlement lifetime is invalid')
    if not isinstance(value['nonce'],str) or not re.fullmatch('[0-9a-f]{32}',value['nonce']):
        raise PipelineError('settlement nonce is invalid')
    return value


def encode(value):return pp.encode_signed_lease(validate(value))


def decode(token):
    if not isinstance(token,str) or len(token)>65536 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',token):
        raise PipelineError('invalid signed technical settlement')
    value=validate(pp.decode_signed_lease(token))
    if token.split('.')[0]!=pp.encode_lease(value):raise PipelineError('noncanonical technical settlement')
    return value


def lease_view(value):
    return dict(target_reservation=value['target_reservation'],pipeline_replicas=value['pipeline_replicas'],
        target_stage='fix-pr',number=value['identity']['pr_number'],head=value['identity']['head_sha'])


def require_current(value,config):
    validate(value)
    from .review_routing_policy import settings
    if settings(config) is None or settings(config)['enabled'] is not True:
        raise PipelineError('technical settlement is disabled')
    if pp.digest(config)!=value['config_sha256'] or pp._configured_replica_count()!=value['pipeline_replicas']:
        raise PipelineError('technical settlement current configuration/replicas changed')
    pp._validate_lease_reservation(lease_view(value),config)
    now=int(time.time())
    if value['issued_at']>now+60 or now>=value['expires_at']:raise PipelineError('technical settlement expired')


def verify_local(value):
    validate(value)
    with db._connect() as connection:_local_reservation(connection,value['target_reservation'])


def renew(value,config):
    require_current(value,config)
    ttl=config['target_reservation_ttl_seconds']
    with db._connect(immediate=True) as connection:
        row,expiry=_local_reservation(connection,value['target_reservation'])
        now=datetime.now(timezone.utc)
        if now.timestamp()>=value['expires_at']:raise PipelineError('settlement expired during reservation writer wait')
        expiry=max(expiry,now+timedelta(seconds=ttl))
        connection.execute('UPDATE pipeline_target_reservations SET expires_at=? WHERE repository=? AND number=?',
                           (expiry.isoformat(),row['repository'],row['number']))
        return expiry
