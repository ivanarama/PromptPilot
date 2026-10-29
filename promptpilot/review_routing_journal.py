"""Durable local write-ahead receipts for non-idempotent routing POSTs.

This is not server ownership or a budget ledger. A pending/ambiguous POST is
never retried merely because the result is not visible yet. Recovery requires
authenticated exact-body server read-back before marking a request done.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .pipeline_errors import PipelineError
from .review_routing_lease import decimal_id, owner_id
from .review_routing_wire import parse


LEGACY_SCHEMA='''CREATE TABLE IF NOT EXISTS requests (
    owner TEXT NOT NULL,
    phase TEXT NOT NULL,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','ambiguous','done')),
    returned_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(owner,phase))'''
SCHEMA=LEGACY_SCHEMA.replace("('pending','ambiguous','done')",
    "('pending','attempting','ambiguous','done','not-sent')").replace(
    'PRIMARY KEY(owner,phase)','generation TEXT, PRIMARY KEY(owner,phase)')


def _schema_signature(value):
    # SQLite removes IF NOT EXISTS when storing the original CREATE TABLE.
    value=re.sub(r'\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS(?=\s)','CREATE TABLE',value,count=1)
    # Ignore layout only outside quoted SQL literals; 'd one' is not 'done'.
    tokens=re.findall(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[^\s'\"]+",value)
    return ''.join(tokens)


class Journal:
    def __init__(self, directory):
        directory=Path(directory)
        directory.mkdir(parents=True,exist_ok=True)
        self.path=directory/'review-routing-journal.sqlite3'
        if self.path.is_symlink():
            raise PipelineError('routing journal cannot be a symbolic link')
        try:
            descriptor=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            with connection:
                version=connection.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0,1,2):
                    raise PipelineError('unsupported routing journal version')
                stored=connection.execute("SELECT type,sql FROM sqlite_master WHERE name='requests'").fetchone()
                if stored is not None:
                    expected=SCHEMA if version==2 else LEGACY_SCHEMA
                    if stored['type']!='table' or _schema_signature(stored['sql'] or '')!=_schema_signature(expected):
                        raise PipelineError('unsupported routing journal schema; preserve existing database')
                    extras=connection.execute("SELECT name FROM sqlite_master WHERE tbl_name='requests' AND type IN ('trigger','index') AND sql IS NOT NULL").fetchall()
                    if extras:
                        raise PipelineError('unsupported routing journal schema objects; preserve existing database')
                    if version==0:
                        # v1 CREATE TABLE was autocommitted before its version
                        # write. Only its known empty interrupted-init shape
                        # is safe to recover as v1, never an unknown database.
                        if connection.execute('SELECT 1 FROM requests LIMIT 1').fetchone():
                            raise PipelineError('unversioned routing journal schema contains unknown history')
                        version=1
                elif version!=0:
                    raise PipelineError('versioned routing journal schema is missing')
                if version==1:
                    connection.execute('ALTER TABLE requests RENAME TO requests_v1')
                connection.execute(SCHEMA)
                if version==1:
                    # A legacy pending operation may already have sent a POST.
                    # NULL generation can never acquire pre-network refusal rights.
                    connection.execute('INSERT INTO requests SELECT *,NULL FROM requests_v1')
                    connection.execute('DROP TABLE requests_v1')
                connection.execute('PRAGMA user_version=2')

    def _connect(self):
        connection=sqlite3.connect(self.path,timeout=10)
        connection.row_factory=sqlite3.Row
        try:
            connection.execute('PRAGMA synchronous=EXTRA')
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _key(owner,phase):
        owner_id(owner)
        if not isinstance(phase,str) or not re.fullmatch(r'[a-z][a-z0-9:-]{0,255}',phase):
            raise PipelineError('invalid routing journal phase key')

    def prepare(self,*,owner,phase,repository,number,body,with_generation=False):
        """Commit pending intent BEFORE the one permitted network attempt.

        Returns False for an already completed identical request. Any previous
        pending/ambiguous request requires read-back; it cannot post again.
        """
        self._key(owner,phase)
        if (not isinstance(repository,str) or not re.fullmatch(r'[^/\s]+/[^/\s]+',repository)
                or type(number) is not int or not 1<=number<=2147483647):
            raise PipelineError('invalid routing journal target')
        payload=parse(body)
        if payload['pr']['pr_number']!=number or (payload['kind']=='publication' and payload['identity']['base_repository']!=repository):
            raise PipelineError('routing journal wire target differs from requested target')
        if 'owner' in payload and payload['owner']!=owner:
            raise PipelineError('routing journal owner differs from prepared wire body')
        body_sha=hashlib.sha256(body.encode('utf-8')).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                previous=connection.execute('SELECT * FROM requests WHERE owner=? AND phase=?',(owner,phase)).fetchone()
                if previous:
                    if (previous['repository'],previous['pr_number'],previous['body_sha256'],previous['body'])!=(repository,number,body_sha,body):
                        raise PipelineError('routing journal request key was reused for another body/target')
                    if previous['state']=='done':
                        connection.commit()
                        return False
                    if previous['state']!='not-sent':
                        raise PipelineError('routing POST result is ambiguous; exact read-back required before recovery')
                # A fresh owner UUID/phase/repository spelling cannot erase
                # uncertainty about an earlier request for the same lifetime
                # PR. Only authenticated confirmation resolves that barrier.
                unresolved=connection.execute("SELECT body FROM requests WHERE state IN ('pending','attempting','ambiguous')").fetchall()
                if any(parse(row['body'])['pr']==payload['pr'] for row in unresolved):
                    raise PipelineError('routing target has an unresolved POST; exact read-back required before a new owner/phase')
                now=time.time()
                generation=secrets.token_hex(16)
                if previous:
                    connection.execute("UPDATE requests SET state='pending',returned_id=NULL,updated_at=?,generation=? WHERE owner=? AND phase=?",
                                       (now,generation,owner,phase))
                else:
                    connection.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,NULL,?,?,?)',
                                       (owner,phase,repository,number,body,body_sha,'pending',now,now,generation))
                connection.commit()
                return generation if with_generation else True
            except BaseException:
                connection.rollback()
                raise

    def get(self,*,owner,phase):
        self._key(owner,phase)
        with closing(self._connect()) as connection:
            row=connection.execute('SELECT * FROM requests WHERE owner=? AND phase=?',(owner,phase)).fetchone()
            if row is None:
                return None
            result=dict(row)
            # The cancellation capability belongs only to the call that won
            # prepare. A recovery reader must never acquire it from inventory.
            result.pop('generation')
            return result

    def pending_for(self,pr):
        """Recovery inventory only; no pending operation is permission to POST."""
        with closing(self._connect()) as connection:
            rows=connection.execute("SELECT * FROM requests WHERE state IN ('pending','attempting','ambiguous') ORDER BY created_at,owner,phase").fetchall()
            result=[]
            for row in rows:
                if parse(row['body'])['pr']==pr:
                    item=dict(row);item.pop('generation');result.append(item)
            return result

    def ambiguous(self,*,owner,phase):
        self._key(owner,phase)
        with closing(self._connect()) as connection:
            with connection:
                cursor=connection.execute("UPDATE requests SET state='ambiguous',updated_at=? WHERE owner=? AND phase=? AND state IN ('pending','attempting','ambiguous')",
                                          (time.time(),owner,phase))
                if cursor.rowcount!=1:
                    raise PipelineError('routing journal pending request is missing or already completed')

    def _pre_network_transition(self,*,owner,phase,generation,body,source,target):
        self._key(owner,phase)
        if not isinstance(generation,str) or not re.fullmatch('[0-9a-f]{32}',generation):
            raise PipelineError('routing pre-network transition requires its fresh prepare generation')
        with closing(self._connect()) as connection:
            with connection:
                cursor=connection.execute('''UPDATE requests SET state=?,updated_at=?
                    WHERE owner=? AND phase=? AND generation=? AND state=?
                    AND returned_id IS NULL AND body=? AND body_sha256=?''',
                    (target,time.time(),owner,phase,generation,source,body,
                     hashlib.sha256(body.encode('utf-8')).hexdigest()))
                if cursor.rowcount!=1:
                    raise PipelineError('routing pre-network generation/state CAS failed; keep recovery barrier')

    def arm(self,*,owner,phase,generation,body):
        """The prepare winner records possible sending before its final guard.

        A crash from here leaves uncertainty. Recovery never infers not-sent
        from this state or from absence of a server comment.
        """
        self._pre_network_transition(owner=owner,phase=phase,generation=generation,body=body,
                                     source='pending',target='attempting')

    def refuse_before_post(self,*,owner,phase,generation,body):
        """Only the live prepare winner's controlled PRE-POST failure path.

        This is not a recovery API. The caller must still be before its first
        gh.json POST. Any POST/response/receipt error must retain uncertainty.
        A later generation still needs fresh server dedup and all stage gates.
        """
        self._pre_network_transition(owner=owner,phase=phase,generation=generation,body=body,
                                     source='attempting',target='not-sent')

    def confirm(self,*,owner,phase,returned_id,body):
        """Store a receipt AFTER independent authenticated server verification.

        The journal cannot authenticate this argument or the server: the
        transport must verify immutable author, target and exact body first.
        """
        self._key(owner,phase)
        decimal_id(returned_id)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row=connection.execute('SELECT * FROM requests WHERE owner=? AND phase=?',(owner,phase)).fetchone()
                if row is None or row['body']!=body or hashlib.sha256(body.encode('utf-8')).hexdigest()!=row['body_sha256']:
                    raise PipelineError('routing receipt does not match its prepared exact body')
                if row['returned_id'] is not None and row['returned_id']!=returned_id:
                    raise PipelineError('routing receipt conflicts with its known returned ID')
                connection.execute("UPDATE requests SET state='done',returned_id=?,updated_at=? WHERE owner=? AND phase=?",
                                   (returned_id,time.time(),owner,phase))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def observe_returned_id(self,*,owner,phase,returned_id):
        """Persist the POST's observed ID without claiming verified completion."""
        self._key(owner,phase)
        decimal_id(returned_id)
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row=connection.execute('SELECT * FROM requests WHERE owner=? AND phase=?',(owner,phase)).fetchone()
                if row is None:
                    raise PipelineError('routing returned ID has no pending request')
                if row['returned_id'] is not None and row['returned_id']!=returned_id:
                    raise PipelineError('routing returned ID conflicts with its previous observation')
                connection.execute('UPDATE requests SET returned_id=?,updated_at=? WHERE owner=? AND phase=?',
                                   (returned_id,time.time(),owner,phase))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
