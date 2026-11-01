"""Durable one-use start for a future source-bound pipeline FIX factory.

This private component grants no GitHub/source/executor permission. A future
caller must verify a signed FIX-start purpose and a canonical started receipt,
then provide the fresh final gate here. It is not yet a public FIX route.
"""
from __future__ import annotations

from contextlib import closing
import copy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from . import project_pipeline as pp
from .pipeline_errors import PipelineError
from .process_tree import OwnedProcess
from .review_routing_journal import _schema_signature
from .review_routing_lease import decimal_id,owner_id
from .review_routing_policy import ROLES
from .review_routing_request import RESERVATION_FIELDS,_aware
from .review_routing_wire import _fields,_integer,_pr,_source,canonical,hash64,sha


TABLE='technical_fix_starts_v1'
ESTABLISHED=TABLE+':established'
_OWNED_HANDLES={}
_OWNED_MUTEX=threading.Lock()
SCHEMA='''CREATE TABLE IF NOT EXISTS technical_fix_starts_v1 (
    attempt_key TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    scope_json TEXT NOT NULL,
    scope_sha256 TEXT NOT NULL,
    generation TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('spawn-attempting','not-started','unknown','launched','stopped')),
    pid INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL)'''
FIELDS=('version repository pr source attempt_id started_id context_sha256 owner executor '
        'attempt_uuid task_id task_started_at reservation config_sha256 reader_sha256')


def freeze_scope(scope):
    """Closed local binding only; UUIDs and source data are not authority."""
    _fields(scope,FIELDS)
    if type(scope['version']) is not int or scope['version']!=1:
        raise PipelineError('unsupported technical start binding version')
    _pr(scope['pr']);_source(scope['source'])
    for name in ['attempt_id','started_id']:decimal_id(scope[name])
    for name in ['context_sha256','config_sha256']:hash64(scope[name])
    for name in ['owner','attempt_uuid']:owner_id(scope[name])
    _integer(scope['task_id'],1,9223372036854775807);_aware(scope['task_started_at'])
    _fields(scope['executor'],'kind login')
    if (scope['executor']['kind']!='pipeline' or not isinstance(scope['executor']['login'],str)
            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})',scope['executor']['login'])):
        raise PipelineError('technical start accepts only the already authorized pipeline executor')
    _fields(scope['reader_sha256'],' '.join(ROLES))
    for value in scope['reader_sha256'].values():hash64(value)
    reservation=scope['reservation'];_fields(reservation,RESERVATION_FIELDS)
    repository,stage,number,head,task,_token=pp._reservation_identity(reservation)
    if (scope['repository']!=repository or stage!='fix-pr' or number!=scope['pr']['pr_number']
            or head!=scope['source']['head'] or task!=scope['task_id']
            or reservation['task_started_at']!=scope['task_started_at']
            or reservation['ownership_kind']!='headless'
            or any(reservation[name] not in ('',None) for name in
                   ['herdr_session_state','herdr_pane_id','herdr_tab_id','herdr_workspace_id'])):
        raise PipelineError('technical start differs from its exact owned headless task reservation')
    for name in ['reserved_at','expires_at']:_aware(reservation[name])
    encoded=canonical(scope)
    if len(encoded)>32768:raise PipelineError('technical start binding exceeds bounded size')
    return json.loads(encoded)


class OneUseStart:
    """Consume in the scheduler DB before final read-only gate and ONE factory.

    Every unresolved/attempted row is a barrier. Expiry, retry, new task or PID
    absence never resets it. Only this direct controlled pre-factory refusal
    may record not-started, and only the same immutable scope may retry it.
    """
    def __init__(self,database,*,clock=None,create=True,allow_new=True):
        self.allow_new=allow_new
        self.database=Path(database)
        if not self.database.is_file() or self.database.is_symlink():
            raise PipelineError('technical start requires the existing scheduler database')
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection,create=create)
                connection.commit()
            except BaseException:
                connection.rollback();raise

    @staticmethod
    def _schema(connection,*,create=False):
        for table in ['tasks','settings','pipeline_target_reservations']:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                raise PipelineError('technical start requires the actual scheduler schema')
        marker=connection.execute('SELECT value FROM settings WHERE key=?',(ESTABLISHED,)).fetchone()
        if marker is not None and marker['value']!='1':
            raise PipelineError('technical start established marker is invalid; preserve barriers')
        stored=connection.execute('SELECT type,name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE',(TABLE,)).fetchone()
        if stored is not None:
            if (stored['type']!='table' or stored['name']!=TABLE
                    or _schema_signature(stored['sql'] or '')!=_schema_signature(SCHEMA)):
                raise PipelineError('unknown technical start schema; preserve existing barriers')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE tbl_name=? COLLATE NOCASE AND type IN ('trigger','index') AND sql IS NOT NULL LIMIT 1",(TABLE,)).fetchone():
                raise PipelineError('unknown technical start schema objects')
            if marker is None:
                raise PipelineError('technical start established marker disappeared; preserve existing barriers')
        elif not create or marker is not None:
            raise PipelineError('technical start receipt table disappeared; preserve unknown outcome')
        else:
            connection.execute(SCHEMA)
            connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(ESTABLISHED,'1'))

    def _connect(self):
        connection=sqlite3.connect(str(self.database),timeout=10)
        connection.row_factory=sqlite3.Row
        try:connection.execute('PRAGMA synchronous=EXTRA')
        except BaseException:connection.close();raise
        return connection

    def _now(self):
        value=self.clock()
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise PipelineError('technical start clock must be timezone aware')
        return value.astimezone(timezone.utc)

    @staticmethod
    def _keys(scope):
        return sha({'pr':scope['pr'],'attempt_id':scope['attempt_id']}),sha({'pr':scope['pr'],'source':scope['source']})

    def _current_task(self,connection,scope,now):
        task=connection.execute('SELECT status,started_at,machine,detached,herdr_target,worktree FROM tasks WHERE id=?',
                                (scope['task_id'],)).fetchone()
        if (task is None or task['status']!='running' or task['started_at']!=scope['task_started_at']
                or task['machine'] or task['detached'] or task['herdr_target'] or task['worktree']):
            raise PipelineError('technical start task changed or has an unsupported execution factory')
        cancel=connection.execute('SELECT value FROM settings WHERE key=?',('cancel_task:'+str(scope['task_id']),)).fetchone()
        if cancel is not None and cancel[0]=='1':raise PipelineError('technical start task was cancelled')
        expected=scope['reservation']
        actual=connection.execute('SELECT * FROM pipeline_target_reservations WHERE repository=? AND number=?',
                                  (expected['repository'],expected['number'])).fetchone()
        # Renewal may extend expires_at; identity/attempt/token/descriptors and
        # reserved_at stay exact. Never inherit another task's new reservation.
        names=set(RESERVATION_FIELDS.split())-{'expires_at'}
        if actual is None or any(actual[name]!=expected[name] for name in names):
            raise PipelineError('technical start reservation changed')
        try:expiry=datetime.fromisoformat(actual['expires_at'])
        except (TypeError,ValueError) as exc:raise PipelineError('technical start reservation expiry is invalid') from exc
        if expiry.tzinfo is None or expiry.utcoffset() is None or expiry<=max(now,self._now()):
            raise PipelineError('technical start reservation expired')

    def _consume(self,scope,*,prepare=False):
        attempt_key,source_key=self._keys(scope);body=canonical(scope).decode('utf-8');digest=sha(scope)
        with _OWNED_MUTEX:
            if (str(self.database.resolve()),attempt_key) in _OWNED_HANDLES:
                raise PipelineError('technical start still retains an original owned boundary; no second factory')
        generation=secrets.token_hex(32)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection)
                now=self._now();self._current_task(connection,scope,now)
                previous=connection.execute('SELECT * FROM '+TABLE+' WHERE attempt_key=? OR source_key=?',
                                            (attempt_key,source_key)).fetchall()
                if previous:
                    if len(previous)!=1 or previous[0]['attempt_key']!=attempt_key:
                        raise PipelineError('technical source already has a different start barrier')
                    row=previous[0]
                    if row['scope_sha256']!=digest or row['scope_json']!=body:
                        raise PipelineError('technical start immutable scope changed; preserve prior barrier')
                    if row['state']!='not-started':
                        raise PipelineError('technical start was already attempted or remains unknown; no second factory')
                    if prepare:
                        connection.commit();return row['generation']
                    connection.execute('UPDATE '+TABLE+" SET generation=?,state='spawn-attempting',updated_at=? WHERE attempt_key=?",
                                       (generation,now.isoformat(),attempt_key))
                else:
                    if not prepare and not self.allow_new:
                        raise PipelineError('technical START has no prepared local issuance; preserve unknown outcome')
                    connection.execute('INSERT INTO '+TABLE+' VALUES (?,?,?,?,?,?,?,?,?)',
                        (attempt_key,source_key,body,digest,generation,'not-started' if prepare else 'spawn-attempting',None,now.isoformat(),now.isoformat()))
                connection.commit()
                return generation
            except BaseException:
                connection.rollback();raise

    def prepare(self,scope):
        """Trusted NEW started issuer only; never reconstruct from an old token.

        A public token consumer uses create=False,allow_new=False. Missing
        issuance/table/history cannot become a new launch right on readback.
        """
        return self._consume(freeze_scope(scope),prepare=True)

    def _record(self,scope,generation,state):
        attempt_key,_source=self._keys(scope)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection)
                connection.execute('UPDATE '+TABLE+" SET state=?,updated_at=? WHERE attempt_key=? AND generation=? AND scope_sha256=? AND state='spawn-attempting'",
                                   (state,self._now().isoformat(),attempt_key,generation,sha(scope)))
                connection.commit()
            except BaseException:
                connection.rollback();raise

    def invoke(self,scope,*,final_guard,factory):
        if not callable(final_guard) or not callable(factory):
            raise PipelineError('technical start requires its full final guard and launch factory')
        scope=freeze_scope(scope)
        generation=self._consume(scope)
        try:
            if final_guard(copy.deepcopy(scope)) is not True:
                raise PipelineError('technical start final gate did not explicitly admit this launch')
        except Exception:
            self._record(scope,generation,'not-started')
            raise
        # No journal/SQLite/network call follows the final guard before launch.
        # All factory exceptions, including FileNotFoundError, are conservative
        # unknown outcomes. This path never invokes legacy spawn-retry logic.
        try:child=factory()
        except Exception:
            self._record(scope,generation,'unknown')
            raise
        if isinstance(child,OwnedProcess):
            attempt_key,_source=self._keys(scope)
            key=(str(self.database.resolve()),attempt_key)
            with _OWNED_MUTEX:
                previous=_OWNED_HANDLES.get(key)
                if previous is None:
                    _OWNED_HANDLES[key]=dict(tree=child,generation=generation,scope_sha256=sha(scope),
                                             lock=threading.Lock())
                else:
                    # Preserve both actual handles if trusted local state was
                    # replaced concurrently; never overwrite an unknown owner.
                    previous.setdefault('extra_owned',[]).append(child)
                    previous['ambiguous']=True
        return child

    def status(self,scope):
        scope=freeze_scope(scope);attempt_key,_source=self._keys(scope)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN')
            self._schema(connection)
            row=connection.execute('SELECT state,scope_sha256,pid FROM '+TABLE+' WHERE attempt_key=?',(attempt_key,)).fetchone()
            return dict(row) if row is not None else None

    def verify_consumed(self,scope):
        """Fresh local read for the exact already-consumed final factory gate."""
        scope=freeze_scope(scope);attempt_key,source_key=self._keys(scope)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN')
            self._schema(connection)
            reservation=scope['reservation']
            current=connection.execute('SELECT strict_lifetime FROM pipeline_target_reservations WHERE repository=? AND number=?',
                                       (reservation['repository'],reservation['number'])).fetchone()
            if current is None or current['strict_lifetime']!=1:
                raise PipelineError('technical factory requires a strict-lifetime owned reservation')
            row=connection.execute('SELECT * FROM '+TABLE+' WHERE attempt_key=? AND source_key=?',
                                   (attempt_key,source_key)).fetchone()
            if (row is None or row['scope_sha256']!=sha(scope)
                    or row['scope_json']!=canonical(scope).decode('utf-8')
                    or row['state']!='spawn-attempting'):
                raise PipelineError('technical factory has no exact consumed unresolved start')
            self._current_task(connection,scope,self._now())
        return True

    @staticmethod
    def _live_receipt(scope,generation,pid,nonce):
        if not isinstance(nonce,str) or not re.fullmatch('[0-9a-f]{64}',nonce):
            raise PipelineError('technical live producer requires its private memory nonce')
        body=dict(protocol='technical-fix-live-v1',scope_sha256=sha(scope),generation=generation,pid=pid,
                  nonce_sha256=hashlib.sha256(nonce.encode('ascii')).hexdigest())
        return canonical(body).decode('utf-8')

    @staticmethod
    def _live_row(connection,attempt_key):
        return connection.execute('SELECT CASE WHEN length(value)<4096 THEN value ELSE NULL END AS value FROM settings WHERE key=?',
                                  (TABLE+':live:'+attempt_key,)).fetchone()

    def observe_started(self,scope,pid,*,live_nonce=None):
        """Audit only, after the worker already owns/registers its child handle.

        A caller retains that handle even if this write fails. This receipt
        never creates another start right or proves a later process exit.
        """
        scope=freeze_scope(scope);_integer(pid,1,2147483647)
        attempt_key,_source=self._keys(scope)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection)
                row=connection.execute('SELECT generation,state,scope_sha256,pid FROM '+TABLE+' WHERE attempt_key=?',(attempt_key,)).fetchone()
                if row is None or row['scope_sha256']!=sha(scope):
                    raise PipelineError('technical start observation has no matching consumed scope')
                live=self._live_row(connection,attempt_key)
                expected=None
                if live_nonce is not None:
                    with _OWNED_MUTEX:owner=_OWNED_HANDLES.get((str(self.database.resolve()),attempt_key))
                    if (owner is None or owner.get('ambiguous') or owner['generation']!=row['generation']
                            or owner['scope_sha256']!=sha(scope) or owner['tree'].process.pid!=pid):
                        raise PipelineError('technical live producer has no original factory-owned lifetime')
                    expected=self._live_receipt(scope,row['generation'],pid,live_nonce)
                if row['state']=='launched' and row['pid']==pid:
                    if (expected is None and live is not None or expected is not None and (
                            live is None or not isinstance(live['value'],str)
                            or not secrets.compare_digest(live['value'].encode('utf-8'),expected.encode('utf-8')))):
                        raise PipelineError('technical live producer receipt changed or disappeared')
                    connection.commit();return
                if row['state']!='spawn-attempting':
                    raise PipelineError('technical start observation conflicts with its existing barrier')
                if live is not None:
                    raise PipelineError('technical live producer has unknown existing metadata')
                if expected is not None:
                    connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(TABLE+':live:'+attempt_key,expected))
                connection.execute('UPDATE '+TABLE+" SET state='launched',pid=?,updated_at=? WHERE attempt_key=?",
                                   (pid,self._now().isoformat(),attempt_key))
                connection.commit()
            except BaseException:
                connection.rollback();raise

    def verify_live(self,scope,nonce):
        """Producer-only heartbeat proof; the nonce is never stored in plaintext.

        The controller delegates it only while its original handle is alive.
        A new process/token/PID cannot mint it after worker ownership is lost.
        This grants no launch, stop, push, settlement or budget-refund right.
        """
        scope=freeze_scope(scope);attempt_key,_source=self._keys(scope)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN')
            self._schema(connection)
            row=connection.execute('SELECT generation,state,scope_sha256,pid FROM '+TABLE+' WHERE attempt_key=?',
                                   (attempt_key,)).fetchone()
            if (row is None or row['state']!='launched' or row['scope_sha256']!=sha(scope)
                    or type(row['pid']) is not int or row['pid']<1):
                raise PipelineError('technical heartbeat lacks its exact launched producer')
            expected=self._live_receipt(scope,row['generation'],row['pid'],nonce)
            live=self._live_row(connection,attempt_key)
            if (live is None or not isinstance(live['value'],str)
                    or not secrets.compare_digest(live['value'].encode('utf-8'),expected.encode('utf-8'))):
                raise PipelineError('technical heartbeat lost its original memory producer proof')
            reservation=scope['reservation']
            current=connection.execute('SELECT strict_lifetime FROM pipeline_target_reservations WHERE repository=? AND number=?',
                                       (reservation['repository'],reservation['number'])).fetchone()
            if current is None or current['strict_lifetime']!=1:
                raise PipelineError('technical heartbeat requires strict lifetime ownership')
            self._current_task(connection,scope,self._now())
        return True

    def retain_owned(self,scope,tree):
        """Bind the actual returned kernel boundary, never a PID lookup."""
        scope=freeze_scope(scope)
        if not isinstance(tree,OwnedProcess):
            raise PipelineError('technical stop proof requires the actual owned process boundary')
        attempt_key,_source=self._keys(scope)
        with closing(self._connect()) as connection:
            self._schema(connection)
            row=connection.execute('SELECT generation,scope_sha256,state,pid FROM '+TABLE+' WHERE attempt_key=?',
                                   (attempt_key,)).fetchone()
            if (row is None or row['scope_sha256']!=sha(scope)
                    or row['state'] not in {'spawn-attempting','launched','unknown'}
                    or row['pid'] is not None and row['pid']!=tree.process.pid):
                raise PipelineError('technical owned boundary has no matching consumed start')
        key=(str(self.database.resolve()),attempt_key)
        with _OWNED_MUTEX:
            previous=_OWNED_HANDLES.get(key)
            if (previous is None or previous.get('ambiguous') or previous['tree'] is not tree
                    or previous['generation']!=row['generation'] or previous['scope_sha256']!=sha(scope)):
                raise PipelineError('technical stop requires the original factory-returned boundary')

    def confirm_stopped(self,scope,tree):
        """Trusted observer closes its ORIGINAL boundary before durable audit.

        Exit status, expiry, absent PID and a replacement process cannot produce
        this receipt. Missing ownership remains unknown after a process restart.
        The eventual publisher must separately validate complete provider output.
        """
        scope=freeze_scope(scope);attempt_key,_source=self._keys(scope)
        key=(str(self.database.resolve()),attempt_key)
        with _OWNED_MUTEX:owner=_OWNED_HANDLES.get(key)
        if owner is None or owner.get('ambiguous') or owner['tree'] is not tree or owner['scope_sha256']!=sha(scope):
            raise PipelineError('technical stop requires the retained original owned lifetime')
        if not owner['lock'].acquire(blocking=False):
            raise PipelineError('technical owned stop is already being verified')
        try:
            tree.close()
            tree.process.wait(timeout=10)
            with closing(self._connect()) as connection:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    self._schema(connection)
                    row=connection.execute('SELECT generation,scope_sha256,state,pid FROM '+TABLE+' WHERE attempt_key=?',
                                           (attempt_key,)).fetchone()
                    if (row is None or row['generation']!=owner['generation'] or row['scope_sha256']!=sha(scope)
                            or row['state'] not in {'spawn-attempting','launched','unknown'}
                            or row['pid'] is not None and row['pid']!=tree.process.pid):
                        raise PipelineError('technical stop context changed; preserve its unknown outcome')
                    connection.execute('UPDATE '+TABLE+" SET state='stopped',pid=?,updated_at=? WHERE attempt_key=?",
                                       (tree.process.pid,self._now().isoformat(),attempt_key))
                    connection.commit()
                except BaseException:
                    connection.rollback();raise
            with _OWNED_MUTEX:
                if _OWNED_HANDLES.get(key) is owner:_OWNED_HANDLES.pop(key)
        finally:owner['lock'].release()
