"""Closed author-progress scope; evidence and administrative publication only.

The extra new_head binding deliberately cannot be decoded by v1 admission or
the reserved v1 progress shape. The public progress coordinator separately proves the canonical
attempt/executor/lease, current source evidence and exact live task.
"""
import copy
import json
import re
from uuid import uuid4

from . import project_pipeline as pp
from . import review_routing_fix_request as admission
from .pipeline_errors import PipelineError
from .review_routing_wire import _fields, canonical, head40


FIELDS = admission.FIELDS + ' progress_version new_head'


def _v1(value):
    result = copy.deepcopy(value)
    result.pop('new_head')
    result.pop('progress_version')
    return result


def validate(value):
    _fields(value, FIELDS)
    if (type(value['version']) is not int or value['version'] != 1
            or type(value['progress_version']) is not int or value['progress_version'] != 1
            or value['purpose'] != admission.PROGRESS or value['attempt_id'] is None):
        raise PipelineError('author progress requires its exact versioned purpose and canonical attempt ID')
    admission.validate(_v1(value))
    head40(value['new_head'])
    if value['new_head'] == value['head']:
        raise PipelineError('author progress must bind a different exact new HEAD')
    if len(canonical(value)) > 32768:
        raise PipelineError('author progress scope exceeds bounded size')
    return value


def encode(value):
    return pp.encode_signed_lease(validate(value))


def decode(token):
    if (not isinstance(token, str) or len(token) > 65536
            or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token)):
        raise PipelineError('invalid signed author progress token')
    value = validate(pp.decode_signed_lease(token))
    if token.split('.')[0] != pp.encode_lease(value):
        raise PipelineError('noncanonical author progress token')
    return value


def require_current(value, config, *, write=True):
    validate(value)
    admission.require_current(_v1(value), config, write=write)


def build(config, snapshot, policy, root, reservation):
    attempt = dict(root.payload, owner=str(uuid4()))
    original = dict(snapshot, identity=dict(snapshot['identity'], head_sha=attempt['source']['head']))
    value = admission.build(config, original, policy, attempt, reservation)
    value.update(progress_version=1, purpose=admission.PROGRESS, attempt_id=root.id,
                 new_head=snapshot['identity']['head_sha'])
    return validate(json.loads(canonical(value)))
