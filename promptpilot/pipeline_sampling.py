"""Schedule costed background observations before an opted-in Source expires."""

import math
import logging
import time

from . import pipeline_insights as insights


def refresh_ahead_seconds(profile):
    budget = profile.get("github_budget") or {}
    if not isinstance(budget, dict):
        raise ValueError("github_budget must be an object")
    lead = budget.get("refresh_ahead_seconds", 0)
    if (isinstance(lead, bool) or not isinstance(lead, int) or lead < 0
            or (lead and lead >= insights._CACHE_TTL_SECONDS)):
        raise ValueError("github_budget.refresh_ahead_seconds must be an integer from 0 to 299")
    if lead and budget.get("essential_snapshot_headroom") is not True:
        raise ValueError("refresh_ahead_seconds requires essential_snapshot_headroom")
    if lead and insights._github_budget_policy(profile) is None:
        raise ValueError("refresh_ahead_seconds requires enabled budgeting")
    return lead


def _delay(profile_id, profile, series, ordinary_deadline, now, *, reuse=True):
    lead = refresh_ahead_seconds(profile)
    if not lead:
        return ordinary_deadline - now
    cache = (insights.read_cached(profile_id, series).get("cache") or {})
    deferred = insights._parse_time(cache.get("refresh_deferred_until"))
    if deferred is not None and deferred.timestamp() > now:
        return deferred.timestamp() - now
    if cache.get("refresh_blocked"):
        return ordinary_deadline - now
    if not reuse:
        return ordinary_deadline - now
    age, ttl = cache.get("age_seconds"), cache.get("ttl_seconds")
    if (cache.get("available") is True and cache.get("complete") is True
            and not cache.get("invalidated") and cache.get("stale") is False
            and isinstance(age, (int, float)) and not isinstance(age, bool)
            and math.isfinite(age) and age >= 0
            and isinstance(ttl, (int, float)) and not isinstance(ttl, bool)
            and math.isfinite(ttl) and ttl > lead):
        return ttl - lead - age
    return ordinary_deadline - now


def sample_scheduled_profiles(series, interval, state):
    """Return the next delay; use existing scan/Native/budget fences for work.

    Deadlines are local scheduling hints, never Source timestamps or admission.
    Legacy profiles retain their interval after a completed sample. An opted-in
    profile can refresh early, without resampling unrelated profiles early.
    """
    if insights.db.is_paused():
        return interval
    now = time.time()
    initial = state.setdefault("started_at", now)
    deadlines = state.setdefault("deadlines", {})
    retries = state.setdefault("retry_deadlines", {})
    active = {key: value for key, value in insights._profiles().items()
              if insights._profile_active(value, series)}
    due, errors = [], {}
    for key, profile in active.items():
        try:
            lead = refresh_ahead_seconds(profile)
            ordinary = deadlines.get(key, now if lead else initial + interval)
            if _delay(key, profile, series, ordinary, now,
                      reuse=now >= retries.get(key, 0)) <= 0:
                due.append(key)
        except (TypeError, ValueError) as exc:
            errors[key] = True
            logging.getLogger(__name__).warning("pipeline profile %s: %s", key, exc)
    failed = set()
    if due:
        outcomes = insights.sample_active_profiles(series, profile_ids=set(due))
        failed = {key for key in due if not outcomes.get(key, "").startswith("ok")}
        # Even a refused or failed sample gets the normal retry interval unless
        # its existing scan status supplies a different explicit defer deadline.
        completed = time.time()
        for key in due:
            deadlines[key] = completed + interval
            if key in failed:
                retries[key] = deadlines[key]
                cache = insights.read_cached(key, series).get("cache") or {}
                deferred = insights._parse_time(cache.get("refresh_deferred_until"))
                if deferred is not None and deferred.timestamp() > completed:
                    retries[key] = deadlines[key] = deferred.timestamp()
            else:
                retries.pop(key, None)
    now = time.time()
    waits = [interval]
    for key, profile in active.items():
        if key in errors:
            continue
        ordinary = deadlines.get(key, now if refresh_ahead_seconds(profile)
                                 else initial + interval)
        waits.append(_delay(key, profile, series, ordinary, now,
                            reuse=now >= retries.get(key, 0)))
    return max(1, min(waits))
