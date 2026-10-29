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
import sqlite3
import time

from .pipeline_errors import PipelineError
from .review_routing_lease import decimal_id, owner_id
from .review_routing_wire import parse


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
            with connection:
                version=connection.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0,1):
                    raise PipelineError('unsupported routing journal version')
                connection.execute('''CREATE TABLE IF NOT EXISTS requests (
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
                    PRIMARY KEY(owner,phase))''')
                connection.execute('PRAGMA user_version=1')

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

    def prepare(self,*,owner,phase,repository,number,body):
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
                    raise PipelineError('routing POST result is ambiguous; exact read-back required before recovery')
                # A fresh owner UUID/phase/repository spelling cannot erase
                # uncertainty about an earlier request for the same lifetime
                # PR. Only authenticated confirmation resolves that barrier.
                unresolved=connection.execute("SELECT body FROM requests WHERE state IN ('pending','ambiguous')").fetchall()
                if any(parse(row['body'])['pr']==payload['pr'] for row in unresolved):
                    raise PipelineError('routing target has an unresolved POST; exact read-back required before a new owner/phase')
                now=time.time()
                connection.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,NULL,?,?)',
                                   (owner,phase,repository,number,body,body_sha,'pending',now,now))
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def get(self,*,owner,phase):
        self._key(owner,phase)
        with closing(self._connect()) as connection:
            row=connection.execute('SELECT * FROM requests WHERE owner=? AND phase=?',(owner,phase)).fetchone()
            return dict(row) if row else None

    def pending_for(self,pr):
        """Recovery inventory only; no pending operation is permission to POST."""
        with closing(self._connect()) as connection:
            rows=connection.execute("SELECT * FROM requests WHERE state IN ('pending','ambiguous') ORDER BY created_at,owner,phase").fetchall()
            return [dict(row) for row in rows if parse(row['body'])['pr']==pr]

    def ambiguous(self,*,owner,phase):
        self._key(owner,phase)
        with closing(self._connect()) as connection:
            with connection:
                cursor=connection.execute("UPDATE requests SET state='ambiguous',updated_at=? WHERE owner=? AND phase=? AND state IN ('pending','ambiguous')",
                                          (time.time(),owner,phase))
                if cursor.rowcount!=1:
                    raise PipelineError('routing journal pending request is missing or already completed')

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
