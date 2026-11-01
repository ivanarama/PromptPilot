"""Explicit same-repository pipeline executor policy; not launch authority.

The future public coordinator must repeat this preflight at every write and
factory boundary. No current route advertises this incomplete capability.
"""
import re

from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing_policy import preflight
from .review_routing_wire import _fields

CONTRACT={'protocol':'claimed-pipeline-fix-v1','snapshot':'complete',
          'purposes':['admission','start','heartbeat','progress','settlement']}


def settings(config):
    value=config.get('technical_fix_execution')
    if value is None:return None
    _fields(value,'enabled protocol executor')
    if (not isinstance(config.get('trusted_account'),str)
            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})',config['trusted_account'])
            or type(value['enabled']) is not bool or value['protocol']!='review-routing-v1'
            or value['executor']!={'kind':'pipeline','login':config.get('trusted_account')}):
        raise PipelineError('pipeline FIX requires an explicit versioned trusted executor policy')
    return value


def require(config,identity,*,health=None):
    value=settings(config)
    if value is None or value['enabled'] is not True:
        raise PipelineError('pipeline FIX execution is disabled')
    try:current=PRIdentityV1(**identity)
    except (TypeError,KeyError) as exc:raise PipelineError('pipeline FIX requires full PR identity') from exc
    if (current.base_repository!=config.get('repository') or current.cross_repository
            or current.head_repository!=current.base_repository
            or current.head_repository_id!=current.base_repository_id):
        raise PipelineError('pipeline FIX cannot execute a fork or a different repository')
    if health is not None:
        policy=preflight(config,health)
        if policy.get('enabled') is not True or health.get('fix_execution_contract')!=CONTRACT:
            raise PipelineError('pipeline FIX requires all paired execution capabilities')
        return policy
    return current
