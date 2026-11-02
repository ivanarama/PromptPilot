"""Local observations of a running attempt. Never used for admission or verdicts."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import db


def _time(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _path(task_id, started_at):
    if type(task_id) is not int or task_id <= 0:
        raise ValueError('invalid task id')
    identity = hashlib.sha256(_time(started_at).isoformat().encode()).hexdigest()[:20]
    return Path(db.DB_PATH).parent / 'task-progress' / f'{task_id}-{identity}.json'


def _validate(value, task_id, started_at, now):
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('task_id') != task_id
            or _time(value.get('task_started_at')) != _time(started_at)):
        raise ValueError('progress belongs to another task attempt')
    stage = value.get('stage')
    reason = value.get('wait_reason', '')
    if not isinstance(stage, str) or not stage.strip() or len(stage) > 120:
        raise ValueError('invalid stage')
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValueError('invalid wait reason')
    if value.get('status') not in {'running', 'waiting', 'completed', 'failed'}:
        raise ValueError('invalid progress status')
    if value['status'] == 'waiting' and not reason.strip():
        raise ValueError('waiting requires a reason')
    updated = _time(value.get('updated_at'))
    stage_start = _time(value.get('stage_started_at'))
    if not _time(started_at) <= stage_start <= updated or (updated - now).total_seconds() > 30:
        raise ValueError('invalid progress timestamps')
    tests = value.get('tests')
    if tests is not None:
        if (not isinstance(tests, dict) or set(tests) != {'passed', 'failed', 'skipped', 'total'}
                or any(type(n) is not int or n < 0 for n in tests.values())
                or sum(tests[k] for k in ('passed', 'failed', 'skipped')) > tests['total']):
            raise ValueError('invalid executed test counters')
    return {k: value.get(k) for k in ('stage', 'status', 'tests', 'wait_reason', 'updated_at', 'stage_started_at')}


def observe(task_id, started_at, now):
    """A single bounded exact-path read; malformed/stale attempts grant nothing."""
    try:
        path = _path(task_id, started_at)
        if path.is_symlink() or path.parent.is_symlink():
            return None
        with path.open('rb') as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            return None
        result = _validate(json.loads(raw), task_id, started_at, now)
        result['age_seconds'] = max(0, round((now - _time(result['updated_at'])).total_seconds()))
        result['stale'] = result['age_seconds'] > 180
        result['elapsed_seconds'] = max(0, round((now - _time(result['stage_started_at'])).total_seconds()))
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return None


def publish(task_id, started_at, stage, status='running', *, tests=None, wait_reason=''):
    now = datetime.now(timezone.utc)
    task = db.get_task(task_id)
    if task is None or task.status != 'running' or task.started_at is None or _time(task.started_at) != _time(started_at):
        raise ValueError('task attempt is no longer running')
    previous = observe(task_id, started_at, now)
    value = dict(version=1, task_id=task_id, task_started_at=_time(started_at).isoformat(),
                 stage=stage, status=status, tests=tests, wait_reason=wait_reason,
                 updated_at=now.isoformat(), stage_started_at=(previous['stage_started_at']
                    if previous and previous['stage'] == stage else now.isoformat()))
    _validate(value, task_id, started_at, now)
    path = _path(task_id, started_at)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError('progress directory must not be a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.progress-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return value
