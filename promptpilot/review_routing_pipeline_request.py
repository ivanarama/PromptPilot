"""Closed pipeline admission schema; serialization is not factory permission."""
import json
import re
import secrets
import time

from . import project_pipeline as pp
from . import review_routing_fix_request as common
from . import review_routing_pipeline_policy as policy
from .pipeline_errors import PipelineError
from .review_routing_wire import canonical,context

ADMISSION=common.PIPELINE_ADMISSION


def validate(value):return common._validate_structure(value,pipeline=True)


def encode(value):return pp.encode_signed_lease(validate(value))


def decode(token):
    if not isinstance(token,str) or len(token)>65536 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',token):
        raise PipelineError('invalid signed pipeline admission token')
    value=validate(pp.decode_signed_lease(token))
    if token.split('.')[0]!=pp.encode_lease(value):raise PipelineError('noncanonical pipeline admission token')
    return value


def require_current(value,config,*,write=True):
    validate(value)
    policy.require(config,value['identity'])
    if value['attempt']['executor']!=config['technical_fix_execution']['executor']:
        raise PipelineError('pipeline admission executor changed')
    return common._require_current(value,config,write=write)


def renew(value,config):return common._renew(value,config,require_current)


def verify_local(value):
    validate(value)
    return common.verify_local(value)


def build(config,snapshot,paired,attempt,reservation):
    policy.require(config,snapshot['identity'])
    issued=int(time.time())
    value=dict(version=1,protocol='review-routing-v1',purpose=ADMISSION,stage='fix',target_stage='fix-pr',
        repository=config['repository'],number=attempt['pr']['pr_number'],head=attempt['source']['head'],
        identity=snapshot['identity'],author=snapshot['author'],attempt=attempt,attempt_id=None,
        config_sha256=pp.digest(config),reader_sha256=paired['consumer_sha256'],current_limit=paired['current_limit'],
        pipeline_replicas=pp._configured_replica_count(),target_reservation=reservation,issued_at=issued,
        expires_at=issued+min(1800,config['target_reservation_ttl_seconds']),nonce=secrets.token_hex(16))
    context(attempt)
    return validate(json.loads(canonical(value)))
