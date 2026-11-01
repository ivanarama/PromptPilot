"""One controlled author revocation with exact node-ID readback.

Absence of an assignee alone is not delivery/stop proof. The observer requires
the original verified delivery receipt and terminal old coordinator, seals any
future delivery, attempts DELETE once and confirms the bound author is absent.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing_assignment import Assignment,TABLE as ASSIGNMENTS,read_assignment
from .review_routing_journal import _schema_signature
from .review_routing_wire import canonical,context,sha

TABLE='technical_fix_revocations_v1'
MARKER=TABLE+':established'
SCHEMA='''CREATE TABLE IF NOT EXISTS technical_fix_revocations_v1 (
    attempt_key TEXT PRIMARY KEY,
    binding TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('attempting','confirmed')))'''


class Revocation:
    def __init__(self,database,root,author):
        self.database=Path(database);self.root=root;self.author=author
        if (not self.database.is_file() or self.database.is_symlink()
                or root.payload['executor']!={'kind':'author','login':author['login']}):
            raise PipelineError('author revocation requires its original delivery executor')
        self.key=sha(dict(pr=root.payload['pr'],attempt_id=root.id))

    def _connect(self):
        connection=sqlite3.connect(self.database,timeout=10);connection.row_factory=sqlite3.Row
        try:connection.execute('PRAGMA synchronous=EXTRA')
        except BaseException:connection.close();raise
        return connection

    def _delivery(self,connection):
        Assignment._schema(connection,create=False)
        row=connection.execute('SELECT scope_json,state FROM '+ASSIGNMENTS+' WHERE attempt_key=?',(self.key,)).fetchone()
        if row is None or row['state']!='delivered' or len(row['scope_json'])>65536:
            raise PipelineError('author revocation has no durable verified original delivery')
        try:
            original=json.loads(row['scope_json'])
            identity=PRIdentityV1(**original['identity'])
            reservation=original['target_reservation']
        except (KeyError,TypeError,ValueError) as exc:raise PipelineError('author original delivery is malformed') from exc
        if (set(original)!={'repository','identity','author','attempt','target_reservation','attempt_id'}
                or original['attempt']!=self.root.payload or original['attempt_id']!=self.root.id
                or original['author']!=self.author or identity.sha256!=self.root.payload['source']['identity_sha256']
                or identity.base_repository!=original['repository']
                or canonical(original).decode('utf-8')!=row['scope_json']):
            raise PipelineError('author revocation differs from its original source/node-ID delivery')
        task=connection.execute('SELECT status,started_at FROM tasks WHERE id=?',(reservation['task_id'],)).fetchone()
        if (task is None or task['started_at']!=reservation['task_started_at']
                or task['status'] not in {'completed','failed','cancelled'}):
            raise PipelineError('author original delivery coordinator is not terminal')
        return dict(protocol='technical-fix-author-revocation-v1',pr=self.root.payload['pr'],
            attempt_id=self.root.id,context_sha256=context(self.root.payload),author=self.author,
            assignment_sha256=sha(original),executor_outcome='author-unassigned')

    def _schema(self,connection,*,create=False):
        marker=connection.execute('SELECT value FROM settings WHERE key=?',(MARKER,)).fetchone()
        table=connection.execute('SELECT type,name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE',(TABLE,)).fetchone()
        if table is None:
            if marker is not None:raise PipelineError('author revocation history disappeared')
            if not create:return False
            connection.execute(SCHEMA);connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(MARKER,'1'))
        elif (marker is None or marker['value']!='1' or table['type']!='table' or table['name']!=TABLE
                or _schema_signature(table['sql'] or '')!=_schema_signature(SCHEMA)
                or connection.execute("SELECT 1 FROM sqlite_master WHERE tbl_name=? AND type IN ('trigger','index') AND sql IS NOT NULL",(TABLE,)).fetchone()):
            raise PipelineError('author revocation metadata is unknown')
        return True

    def _row(self,connection,evidence):
        row=connection.execute('SELECT binding,state FROM '+TABLE+' WHERE attempt_key=?',(self.key,)).fetchone()
        issued=connection.execute('SELECT value FROM settings WHERE key=?',(ASSIGNMENTS+':revoking:'+self.key,)).fetchone()
        if row is None:
            if issued is not None:raise PipelineError('author revocation intent disappeared; no repeated DELETE')
        elif (issued is None or issued['value']!=sha(evidence) or row['binding']!=canonical(evidence).decode('utf-8')
                or row['state'] not in {'attempting','confirmed'}):
            raise PipelineError('author revocation binding/issuance changed')
        return row

    def observe(self):
        with closing(self._connect()) as connection:
            connection.execute('BEGIN');evidence=self._delivery(connection)
            exists=self._schema(connection)
            row=self._row(connection,evidence) if exists else None
            sealed=connection.execute('SELECT value FROM settings WHERE key=?',(ASSIGNMENTS+':revoked:'+self.key,)).fetchone()
            if sealed is not None and (row is None or row['state']!='confirmed' or sealed['value']!=sha(evidence)):
                raise PipelineError('author revocation confirmation is missing or changed')
            if row is not None and row['state']=='confirmed' and sealed is None:
                raise PipelineError('author revocation confirmation seal disappeared')
            return dict(evidence=evidence,confirmed=row is not None and row['state']=='confirmed')

    def revoke(self,gh,identity,*,final_guard):
        if PRIdentityV1(**dict(identity,head_sha=self.root.payload['source']['head'])).sha256!=self.root.payload['source']['identity_sha256']:
            raise PipelineError('author revocation substituted its lifetime PR identity')
        before=self.observe();evidence=before['evidence'];fresh=False
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if self._delivery(connection)!=evidence:raise PipelineError('author delivery changed before revocation')
                self._schema(connection,create=True)
                row=self._row(connection,evidence)
                if row is None:
                    connection.execute('INSERT INTO '+TABLE+' VALUES (?,?,?)',(self.key,canonical(evidence).decode('utf-8'),'attempting'))
                    connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(ASSIGNMENTS+':revoking:'+self.key,sha(evidence)))
                    fresh=True
                connection.commit()
            except BaseException:connection.rollback();raise
        request=dict(repository=identity['base_repository'],number=identity['pr_number'],identity=identity,author=self.author)
        assignees=read_assignment(gh,request)
        if fresh:
            if final_guard() is not True:raise PipelineError('author revocation final guard refused')
            try:
                gh.json('api',f'repos/{request["repository"]}/issues/{request["number"]}/assignees',
                    '--method','DELETE','--input','-',input_value={'assignees':[self.author['login']]})
            except Exception:pass
        elif not before['confirmed']:
            # Unknown DELETE is read-only recovery. Never retry from absence.
            if (self.author['id'],self.author['login']) in assignees:
                raise PipelineError('author revocation DELETE remains ambiguous; no repeat')
        after=read_assignment(gh,request)
        expected=(self.author['id'],self.author['login'])
        if expected in after or fresh and not (set(assignees)-{expected}).issubset(after):
            raise PipelineError('author revocation lacks exact node-ID readback or changed other assignees')
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if self._delivery(connection)!=evidence:raise PipelineError('author delivery changed before revocation confirmation')
                self._schema(connection);self._row(connection,evidence)
                connection.execute('UPDATE '+TABLE+" SET state='confirmed' WHERE attempt_key=?",(self.key,))
                previous=connection.execute('SELECT value FROM settings WHERE key=?',(ASSIGNMENTS+':revoked:'+self.key,)).fetchone()
                if previous is not None and previous['value']!=sha(evidence):raise PipelineError('author revocation seal changed')
                if previous is None:connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(ASSIGNMENTS+':revoked:'+self.key,sha(evidence)))
                connection.commit()
            except BaseException:connection.rollback();raise
        return sha(evidence)
