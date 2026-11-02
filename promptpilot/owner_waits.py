"""Negative-only, finite OWNER observations; never decision/ship authority.

A receipt only excludes one exact, unchanged candidate. Other work stays
eligible. Missing observations and changed inputs always run normal admission.
"""
from datetime import datetime, timezone
import hashlib
import json


def fingerprint(candidate, context):
    if (not isinstance(candidate, dict) or type(candidate.get('number')) is not int
            or candidate['number'] <= 0 or not candidate.get('updated_at')
            or not isinstance(candidate.get('labels'), list)
            or not isinstance(context, dict) or not context.get('environment')
            or not isinstance(context.get('heads'), dict)
            or not isinstance(context['heads'].get(str(candidate['number'])), str)):
        raise ValueError('incomplete OWNER observation')
    # Include every observed field; do not discard a new proof or priority.
    raw = json.dumps({'candidate': candidate, 'context': context}, sort_keys=True,
                     separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def receipt(candidate, context, *, head, reason, evidence, now, seconds=600):
    if (len(head) != 40 or any(c not in '0123456789abcdef' for c in head)
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 500
            or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000
            or head != context.get('heads', {}).get(str(candidate.get('number')))
            or type(seconds) is not int or not 1 <= seconds <= 600):
        raise ValueError('invalid finite negative observation')
    return dict(version=1, number=candidate['number'], head=head,
                fingerprint=fingerprint(candidate, context), reason=reason,
                evidence=evidence, observed_at=now, expires_at=now + seconds)


def select(data, records, context, *, now):
    """Only fresh complete membership may conclude that all work is waiting."""
    fallback = dict(action='fallback', excluded=[], reason='Normal exact-target OWNER admission required')
    cache = data.get('cache') or {}
    queues = data.get('queues') or []
    if (cache.get('complete') is not True or cache.get('available') is not True
            or cache.get('stale') or cache.get('invalidated') or cache.get('refresh_blocked')
            or type(cache.get('age_seconds')) not in (int, float) or not 0 <= cache['age_seconds'] <= 300):
        return dict(action='error', excluded=[], reason='OWNER snapshot is stale or incomplete; defer before provider launch')
    queue = next((q for q in queues if q.get('id') == 'owner'), None)
    if (not queue or queue.get('membership_complete') is not True
            or not isinstance(queue.get('admission_items'), list)
            or type(queue.get('backlog')) is not int
            or queue['backlog'] != len(queue['admission_items'])):
        return fallback
    candidates = queue['admission_items']
    numbers = [c.get('number') for c in candidates if isinstance(c, dict)]
    if len(numbers) != len(candidates) or len(set(numbers)) != len(numbers):
        return fallback
    excluded = []
    for candidate in candidates:
        try:
            digest = fingerprint(candidate, context)
        except (TypeError, ValueError):
            return fallback
        matches = [r for r in records if isinstance(r, dict) and r.get('number') == candidate['number']]
        if len(matches) != 1:
            continue
        r = matches[0]
        start, end = r.get('observed_at'), r.get('expires_at')
        if (r.get('version') == 1 and r.get('fingerprint') == digest
                and type(start) in (float, int) and type(end) in (float, int)
                and 0 < end - start <= 600 and start <= now < end
                and isinstance(r.get('reason'), str) and r['reason']
                and isinstance(r.get('evidence'), str) and r['evidence']):
            excluded.append(dict(number=r['number'], head=r.get('head'), reason=r['reason'], expires_at=end))
    remaining = [c['number'] for c in candidates if c['number'] not in {r['number'] for r in excluded}]
    if not remaining:
        return dict(action='wait', excluded=excluded, reason='OWNER: unchanged observed waits; no provider needed')
    return dict(fallback, excluded=excluded, eligible=remaining)


def now_timestamp():
    return datetime.now(timezone.utc).timestamp()
