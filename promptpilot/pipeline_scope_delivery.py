"""Explicit operator transport to future and existing neverstarted consumer.

Text is opaque data, not a permission or execution choice. No filesystem scope
lookup, provider, election, wake/resume or automatic occurrence creation.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from . import db

PREFIX = 'pipeline_scope_delivery:v1:'
NONCE = re.compile(r'[0-9a-f]{32}')
HASH = re.compile(r'[0-9a-f]{64}')
FIELDS = {'version', 'series_id', 'pending_id', 'request_nonce', 'request_sha256',
          'addition_sha256', 'before_series_revision', 'before_pending_revision',
          'after_series_revision', 'after_pending_revision', 'prompt_sha256',
          'delivered_at', 'paused', 'delivery_state'}


class ScopeDeliveryError(ValueError):
    pass


@contextmanager
def _connection(*, readonly):
    try:
        c = sqlite3.connect(db.DB_PATH.resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=10)
    except sqlite3.OperationalError:
        raise ScopeDeliveryError('Scheduler database is unavailable') from None
    try:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        if readonly:
            c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN' if readonly else 'BEGIN IMMEDIATE')
        yield c
        if not readonly:
            c.commit()
    except BaseException:
        if c.in_transaction:
            c.rollback()
        raise
    finally:
        c.close()


@contextmanager
def _direct_writes_only(c):
    denied = []
    def authorize(action, table, column, database, source):
        if source is not None and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            denied.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    # _connection exclusively owns and closes this connection. Keep the guard
    # until close: Python 3.10 cannot disable an authorizer using None.
    c.set_authorizer(authorize)
    try:
        yield
    except sqlite3.DatabaseError:
        if denied:
            raise ScopeDeliveryError('Trigger attempted an unrelated write; transaction rolled back') from None
        raise


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _key(series_id, nonce):
    if type(series_id) is not int or not 1 <= series_id < 2**63 or not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise ScopeDeliveryError('Invalid scope delivery identity')
    return f'{PREFIX}{series_id}:{nonce}'


def _receipt(c, series_id, nonce):
    row = c.execute('SELECT value FROM settings WHERE key=?', (_key(series_id, nonce),)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row['value'])
        valid = (isinstance(value, dict) and set(value) == FIELDS and type(value['version']) is int and value['version'] == 1
                 and type(value['series_id']) is int and value['series_id'] == series_id and value['request_nonce'] == nonce
                 and value['delivery_state'] == 'delivered' and type(value['pending_id']) is int
                 and type(value['paused']) is bool and isinstance(value['delivered_at'], str)
                 and all(isinstance(value[k], str) and HASH.fullmatch(value[k]) for k in FIELDS if k.endswith(('sha256', 'revision'))))
    except (ValueError, TypeError, RecursionError):
        valid = False
    if not valid:
        raise ScopeDeliveryError('Scope delivery receipt is malformed; reconcile without replay')
    return value


def receipt(series_id, nonce):
    _key(series_id, nonce)
    with _connection(readonly=True) as c:
        return _receipt(c, series_id, nonce)


def _state(c, series_id):
    if type(series_id) is not int or not 1 <= series_id < 2**63:
        raise ScopeDeliveryError('Invalid series identity')
    row = c.execute('SELECT * FROM task_series WHERE id=?', (series_id,)).fetchone()
    if row is None or row['ended_at']:
        raise ScopeDeliveryError('Active series not found')
    series = dict(row)
    consumers = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE series_id=? AND status IN('pending','running','rate_limited') ORDER BY id", (series_id,))]
    reason = None
    if series_id in db.workorder_origins(c, series_id=series_id)[1]:
        reason = 'guarded_series'
    elif len(consumers) != 1:
        reason = 'missing_consumer' if not consumers else 'ambiguous_consumers'
    else:
        task = consumers[0]
        if task['status'] != 'pending' or task['started_at'] is not None or task['session_id'] is not None:
            reason = 'consumer_already_started'
        elif task['prompt'] != series['prompt'] or any(task[k] != series[k] for k in ('working_dir', 'provider', 'model', 'effort', 'task_timeout')):
            reason = 'consumer_differs_from_future'
    state = {'series_id': series_id, 'series_revision': digest(series), 'paused': bool(series['paused']),
             'ready': reason is None, 'blocked_reason': reason}
    if reason is None:
        state.update(pending_id=consumers[0]['id'], pending_revision=digest(consumers[0]))
    return state, series, consumers


def state(series_id):
    with _connection(readonly=True) as c:
        return _state(c, series_id)[0]


def deliver(series_id, request):
    key = _key(series_id, request['request_nonce'])
    addition = request['addition']
    try:
        raw = addition.encode('utf-8')
    except (AttributeError, UnicodeError):
        raise ScopeDeliveryError('Invalid scope text') from None
    if not addition.strip() or len(raw) > 65536:
        raise ScopeDeliveryError('Scope text must contain at most 65536 UTF-8 bytes')
    fingerprint = digest({'series_id': series_id, **request})
    with _connection(readonly=False) as c:
        existing = _receipt(c, series_id, request['request_nonce'])
        if existing is not None:
            if existing['request_sha256'] != fingerprint:
                raise ScopeDeliveryError('Nonce belongs to another immutable scope request')
            return existing  # Readback of old commit; never touch a new consumer.
        snapshot, series, consumers = _state(c, series_id)
        if (not snapshot['ready'] or snapshot['series_revision'] != request['series_revision']
                or snapshot['pending_id'] != request['pending_id'] or snapshot['pending_revision'] != request['pending_revision']):
            raise ScopeDeliveryError('Future or neverstarted consumer changed; delivery remains pending')
        task = consumers[0]
        settings_before = c.execute('SELECT key,value FROM settings WHERE key<>? ORDER BY key', (key,)).fetchall()
        text, now = series['prompt'] + addition, db._now()
        with _direct_writes_only(c):
            c.execute('UPDATE task_series SET prompt=?,updated_at=? WHERE id=?', (text, now, series_id))
            c.execute('UPDATE tasks SET prompt=? WHERE id=?', (text, task['id']))
            after_series = dict(c.execute('SELECT * FROM task_series WHERE id=?', (series_id,)).fetchone())
            after_task = dict(c.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone())
            if after_series != dict(series, prompt=text, updated_at=now) or after_task != dict(task, prompt=text):
                raise ScopeDeliveryError('Unexpected delivery afterimage; transaction rolled back')
            value = {'version': 1, 'series_id': series_id, 'pending_id': task['id'],
                     'request_nonce': request['request_nonce'], 'request_sha256': fingerprint,
                     'addition_sha256': hashlib.sha256(raw).hexdigest(),
                     'before_series_revision': snapshot['series_revision'], 'before_pending_revision': snapshot['pending_revision'],
                     'after_series_revision': digest(after_series), 'after_pending_revision': digest(after_task),
                     'prompt_sha256': hashlib.sha256(text.encode()).hexdigest(), 'delivered_at': now,
                     'paused': snapshot['paused'], 'delivery_state': 'delivered'}
            c.execute('INSERT INTO settings(key,value) VALUES(?,?)', (key, json.dumps(value, sort_keys=True)))
        if c.execute('SELECT key,value FROM settings WHERE key<>? ORDER BY key', (key,)).fetchall() != settings_before:
            raise ScopeDeliveryError('Unrelated control state changed; transaction rolled back')
        return value


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_nonce: str = Field(pattern=r'^[0-9a-f]{32}$')
    series_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    pending_id: int = Field(gt=0, le=2**63 - 1)
    pending_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    addition: str = Field(min_length=1, max_length=65536)


class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def protected(request):
            try:
                return await original(request)
            except (RequestValidationError, RecursionError):
                raise HTTPException(422, 'Invalid scope delivery request') from None
        return protected


router = APIRouter(route_class=PrivateValidationRoute)


def _call(function, *args):
    try:
        return function(*args)
    except ScopeDeliveryError as error:
        raise HTTPException(409, str(error)) from None


@router.get('/api/schedule/{series_id}/scope-delivery')
def read_state(series_id: int):
    return _call(state, series_id)


@router.post('/api/schedule/{series_id}/scope-delivery')
def deliver_scope(series_id: int, request: DeliveryRequest):
    return _call(deliver, series_id, request.model_dump())


@router.get('/api/schedule/{series_id}/scope-delivery/{request_nonce}')
def read_receipt(series_id: int, request_nonce: str):
    result = _call(receipt, series_id, request_nonce)
    if result is None:
        raise HTTPException(404, 'Scope delivery receipt not found')
    return result
