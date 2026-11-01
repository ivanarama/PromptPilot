"""One-attempt expected-HEAD CAS, with durable ambiguity and read-only recovery."""
from contextlib import closing
import json
import secrets

from .pipeline_errors import PipelineError
from .pr_identity import PRIdentityV1
from .review_routing_execution import OneUseStart,freeze_scope
from .review_routing_journal import _schema_signature
from .review_routing_wire import canonical,head40,sha

TABLE='technical_fix_pushes_v1'
MARKER=TABLE+':established'
SCHEMA='''CREATE TABLE IF NOT EXISTS technical_fix_pushes_v1 (
    attempt_key TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    binding TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('not-sent','sending','confirmed')),
    generation TEXT NOT NULL)'''


def binding(scope,identity,output):
    scope=freeze_scope(scope)
    full=PRIdentityV1(**identity)
    if (full.cross_repository or full.base_repository!=scope['repository']
            or full.sha256!=scope['source']['identity_sha256'] or full.head_sha!=scope['source']['head']):
        raise PipelineError('technical CAS changes its exact same-repository source identity')
    if set(output)!={'new_head','tree'}:
        raise PipelineError('technical CAS requires its exact verified output')
    for value in output.values():head40(value)
    if output['new_head']==scope['source']['head']:
        raise PipelineError('technical CAS cannot publish the unchanged source')
    return dict(protocol='technical-fix-cas-v1',scope=scope,identity=full.to_dict(),**output)


class Push:
    def __init__(self,database,value):
        self.value=binding(value['scope'],value['identity'],dict(new_head=value['new_head'],tree=value['tree']))
        if self.value!=value:raise PipelineError('technical CAS binding has unknown fields')
        self.store=OneUseStart(database,create=False,allow_new=False)
        self.scope=self.value['scope']
        self.key,self.source_key=self.store._keys(self.scope)
        self.body=canonical(self.value).decode('utf-8');self.digest=sha(self.value)

    def _schema(self,connection,*,create=False):
        OneUseStart._schema(connection)
        marker=connection.execute('SELECT value FROM settings WHERE key=?',(MARKER,)).fetchone()
        table=connection.execute('SELECT type,name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE',(TABLE,)).fetchone()
        if table is None:
            if marker is not None or not create:
                raise PipelineError('technical CAS history disappeared; preserve publication ambiguity')
            connection.execute(SCHEMA)
            connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(MARKER,'1'))
        elif (marker is None or marker['value']!='1' or table['type']!='table' or table['name']!=TABLE
                or _schema_signature(table['sql'] or '')!=_schema_signature(SCHEMA)
                or connection.execute("SELECT 1 FROM sqlite_master WHERE tbl_name=? AND type IN ('trigger','index') AND sql IS NOT NULL",(TABLE,)).fetchone()):
            raise PipelineError('technical CAS has unknown or lost receipt metadata')

    def _row(self,connection):
        row=connection.execute('SELECT * FROM '+TABLE+' WHERE attempt_key=? OR source_key=?',(self.key,self.source_key)).fetchall()
        marker=connection.execute('SELECT value FROM settings WHERE key=?',(TABLE+':issued:'+self.key,)).fetchone()
        if not row:
            if marker is not None:raise PipelineError('technical CAS issued receipt disappeared; no repeated push')
            return None
        if (len(row)!=1 or row[0]['attempt_key']!=self.key or row[0]['source_key']!=self.source_key
                or row[0]['binding']!=self.body or row[0]['binding_sha256']!=self.digest
                or marker is None or marker['value']!=self.digest):
            raise PipelineError('technical CAS immutable publication binding changed')
        return dict(row[0])

    def prepare(self):
        self.store.verify_stopped(self.scope)
        with closing(self.store._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection,create=True)
                self.store._not_retired(connection,self.key)
                row=self._row(connection)
                if row is None:
                    generation=secrets.token_hex(32)
                    connection.execute('INSERT INTO '+TABLE+' VALUES (?,?,?,?,?,?)',
                        (self.key,self.source_key,self.body,self.digest,'not-sent',generation))
                    connection.execute('INSERT INTO settings(key,value) VALUES (?,?)',(TABLE+':issued:'+self.key,self.digest))
                connection.commit()
            except BaseException:connection.rollback();raise

    def status(self):
        with closing(self.store._connect()) as connection:
            connection.execute('BEGIN')
            self._schema(connection)
            row=self._row(connection)
            if row is None:raise PipelineError('technical CAS has no issued publication receipt')
            return row['state']

    def _transition(self,source,target,*,generation=None):
        with closing(self.store._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection);row=self._row(connection)
                if row is None or row['state'] not in source or generation is not None and row['generation']!=generation:
                    raise PipelineError('technical CAS publication generation/state changed')
                next_generation=secrets.token_hex(32) if target=='sending' else row['generation']
                connection.execute('UPDATE '+TABLE+' SET state=?,generation=? WHERE attempt_key=?',
                                   (target,next_generation,self.key))
                connection.commit()
                return next_generation
            except BaseException:connection.rollback();raise

    def publish(self,*,final_guard,push,readback):
        if not all(callable(value) for value in [final_guard,push,readback]):
            raise PipelineError('technical CAS requires independent final gate, push and exact readback')
        self.prepare()
        if self.status()=='not-sent':
            generation=self._transition({'not-sent'},'sending')
            try:
                self.store.verify_stopped(self.scope)
                if final_guard() is not True:raise PipelineError('technical CAS final gate refused')
            except Exception:
                self._transition({'sending'},'not-sent',generation=generation)
                raise
            # No database/network operations follow the final guard before
            # the ONE push. Any exception after this boundary stays ambiguous.
            try:push()
            except Exception:pass
        if readback() is not True:
            raise PipelineError('technical CAS outcome is unknown; exact authenticated readback required, no repeat')
        self._transition({'sending','confirmed'},'confirmed')
        return json.loads(self.body)

    def require_confirmed(self):
        if self.status()!='confirmed':raise PipelineError('technical FIX has no confirmed expected-HEAD CAS receipt')
        return self.value


def verify_transport(workspace,value):
    """Repeat effective local Git transport policy after publication writer waits."""
    value=binding(value['scope'],value['identity'],dict(new_head=value['new_head'],tree=value['tree']))
    if workspace.scope!=value['scope']:raise PipelineError('technical CAS substituted its source checkout')
    ref='refs/heads/'+value['identity']['head_ref']
    workspace._git(workspace.path,'check-ref-format',ref)
    rewrites=workspace._git(workspace.path,'config','--get-regexp',r'^url\..*\.(insteadof|pushinsteadof)$',allow_failure=True)
    if rewrites.returncode!=1:
        raise PipelineError('technical CAS refuses ambiguous Git URL rewrite configuration')
    destination='https://github.com/'+value['scope']['repository']+'.git'
    # Git resolves a matching configured remote name before interpreting the
    # argument as a URL, including remote.<literal-URL>.url/pushURL aliases.
    # Repeat this effective-config check after the publication writer wait.
    if destination in workspace._git(workspace.path,'remote').stdout.splitlines():
        raise PipelineError('technical CAS refuses a configured remote alias for its literal destination')
    return ref


def prepare_expected(workspace,value):
    """Only an independently proven forward output, one explicit branch ref."""
    value=binding(value['scope'],value['identity'],dict(new_head=value['new_head'],tree=value['tree']))
    ref=verify_transport(workspace,value)
    argv=('push','--porcelain','--atomic','--no-verify','--no-follow-tags',
          '--recurse-submodules=no','--force-with-lease='+ref+':'+value['scope']['source']['head'],
          'https://github.com/'+value['scope']['repository']+'.git',value['new_head']+':'+ref)
    return lambda:workspace._git(workspace.path,*argv)
