"""Trusted local evidence of an old, terminal pipeline executor.

This never infers a stop from TTL, PID absence, task exit text or current HEAD.
Public settlement must separately recheck the actor, delivery and canonical
source/current HEAD. Unknown launches and unknown pushes stay barriers.
"""
from contextlib import closing
import json

from .pipeline_errors import PipelineError
from .review_routing_execution import OneUseStart,TABLE,freeze_scope
from .review_routing_push import Push,TABLE as PUSH_TABLE,MARKER,SCHEMA,binding
from .review_routing_journal import _schema_signature
from .review_routing_wire import canonical,context,hash64,sha


class Retirement:
    def __init__(self,database,root,phases):
        self.store=OneUseStart(database,create=False,allow_new=False)
        self.root=root;self.phases=phases
        if (root.payload['executor']['kind']!='pipeline' or 'fix-done' in phases
                or phases.get('started') is None):
            raise PipelineError('pipeline settlement requires its exact unfinished canonical START')
        self.key=sha(dict(pr=root.payload['pr'],attempt_id=root.id))

    def _publication(self,connection,scope):
        marker=connection.execute('SELECT value FROM settings WHERE key=?',(MARKER,)).fetchone()
        issued=connection.execute('SELECT value FROM settings WHERE key=?',(PUSH_TABLE+':issued:'+self.key,)).fetchone()
        table=connection.execute('SELECT type,name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE',(PUSH_TABLE,)).fetchone()
        if table is None:
            if marker is not None or issued is not None:
                raise PipelineError('old executor lost its publication history; preserve ambiguity')
            return dict(outcome='not-issued',binding_sha256=None),None
        if (marker is None or marker['value']!='1' or table['type']!='table' or table['name']!=PUSH_TABLE
                or _schema_signature(table['sql'] or '')!=_schema_signature(SCHEMA)
                or connection.execute("SELECT 1 FROM sqlite_master WHERE tbl_name=? AND type IN ('trigger','index') AND sql IS NOT NULL",(PUSH_TABLE,)).fetchone()):
            raise PipelineError('old executor has unknown publication metadata')
        rows=connection.execute('SELECT * FROM '+PUSH_TABLE+' WHERE attempt_key=? OR source_key=?',
                                (self.key,self.store._keys(scope)[1])).fetchall()
        if not rows:
            if issued is not None:raise PipelineError('old executor publication receipt disappeared')
            return dict(outcome='not-issued',binding_sha256=None),None
        if len(rows)!=1:raise PipelineError('old executor publication scope is ambiguous')
        row=rows[0]
        try:
            value=json.loads(row['binding'])
            verified=binding(value['scope'],value['identity'],dict(new_head=value['new_head'],tree=value['tree']))
        except (KeyError,ValueError,TypeError) as exc:raise PipelineError('old executor publication binding is malformed') from exc
        if (verified!=value or value['scope']!=scope or row['attempt_key']!=self.key
                or row['source_key']!=self.store._keys(scope)[1] or row['binding_sha256']!=sha(value)
                or canonical(value).decode('utf-8')!=row['binding'] or issued is None or issued['value']!=sha(value)):
            raise PipelineError('old executor publication binding changed or lost its issuance')
        if row['state'] not in {'not-sent','sending','confirmed'}:
            raise PipelineError('old executor publication state is invalid')
        return dict(outcome=row['state'],binding_sha256=sha(value)),value

    def _observe(self,connection):
        OneUseStart._schema(connection)
        row=connection.execute('SELECT * FROM '+TABLE+' WHERE attempt_key=?',(self.key,)).fetchone()
        if row is None:raise PipelineError('old executor has no trusted local start/stop history')
        hash64(row['generation'])
        try:scope=freeze_scope(json.loads(row['scope_json']))
        except (ValueError,TypeError) as exc:raise PipelineError('old executor start binding is malformed') from exc
        started=self.phases['started']
        seed=self.root.payload
        if (scope['attempt_id']!=self.root.id or scope['started_id']!=started.id
                or scope['source']!=seed['source'] or scope['pr']!=seed['pr'] or scope['owner']!=seed['owner']
                or scope['executor']!=seed['executor'] or scope['context_sha256']!=context(seed)
                or started.payload['details']!={'task_id':scope['task_id'],'attempt_uuid':scope['attempt_uuid']}
                or row['scope_sha256']!=sha(scope) or row['scope_json']!=canonical(scope).decode('utf-8')
                or row['source_key']!=self.store._keys(scope)[1]):
            raise PipelineError('old executor evidence differs from its canonical START/source/task')
        if row['state'] not in {'not-started','stopped'}:
            raise PipelineError('old executor stop is unknown; no settlement from PID, expiry or task status')
        if row['state']=='stopped' and (type(row['pid']) is not int or row['pid']<1):
            raise PipelineError('old executor stopped receipt lost its owned process binding')
        if row['state']=='not-started' and row['pid'] is not None:
            raise PipelineError('old executor pre-factory receipt has an unexpected process')
        task=connection.execute('SELECT status,started_at FROM tasks WHERE id=?',(scope['task_id'],)).fetchone()
        if (task is None or task['started_at']!=scope['task_started_at']
                or task['status'] not in {'completed','failed','cancelled'}):
            raise PipelineError('old executor coordinator has not reached its exact terminal task state')
        publication,value=self._publication(connection,scope)
        evidence=dict(protocol='technical-fix-retirement-v1',scope_sha256=sha(scope),
            generation=row['generation'],pid=row['pid'],state=row['state'],publication=publication)
        marker=connection.execute('SELECT value FROM settings WHERE key=?',(TABLE+':retired:'+self.key,)).fetchone()
        if marker is not None and marker['value']!=canonical(evidence).decode('utf-8'):
            raise PipelineError('old executor retirement evidence changed')
        return dict(evidence=evidence,scope=scope,publication=value,retired=marker is not None)

    def observe(self):
        with closing(self.store._connect()) as connection:
            connection.execute('BEGIN')
            return self._observe(connection)

    def confirm_publication(self,readback):
        """Trusted public caller independently checks the exact graph/trailers."""
        observed=self.observe()
        if observed['evidence']['publication']['outcome']!='sending':return observed
        if not callable(readback) or readback(observed['publication']) is not True:
            raise PipelineError('old executor push remains unknown; no settlement or repeated push')
        Push(self.store.database,observed['publication'])._transition({'sending'},'confirmed')
        return self.observe()

    def retire(self,expected):
        with closing(self.store._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                observed=self._observe(connection)
                if observed['evidence']!=expected or expected['publication']['outcome']=='sending':
                    raise PipelineError('old executor evidence changed or delivery remains unknown')
                body=canonical(expected).decode('utf-8')
                if not observed['retired']:
                    connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(TABLE+':retired:'+self.key,body))
                connection.commit()
                return sha(expected)
            except BaseException:connection.rollback();raise
