"""One attempted author assignment, followed by authenticated node-ID readback.

This is an assignee API operation, not a notification/read/consent claim. An
unknown or manually removed assignment cannot be retried as a new delivery.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .pipeline_errors import PipelineError
from .pr_identity import identity_from_rest
from .review_routing_journal import _schema_signature
from .review_routing_wire import canonical, sha


TABLE = 'technical_fix_assignments_v1'
SCHEMA = '''CREATE TABLE technical_fix_assignments_v1 (
    attempt_key TEXT PRIMARY KEY,
    scope_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('attempting','delivered'))
)'''


def read_assignment(gh, request):
    repository, number = request['repository'], request['number']
    # Pull metadata carries the PR's global ID. The issue endpoint's node ID
    # must not be assumed to be the PR identity merely because numbers match.
    issue = gh.json('api', f'repos/{repository}/pulls/{number}')
    if (not isinstance(issue, dict)
            or identity_from_rest(issue, repository=repository,
                repository_id=request['identity']['base_repository_id']).to_dict() != request['identity']
            or not isinstance(issue.get('assignees'), list) or len(issue['assignees']) > 100):
        raise PipelineError('author assignment readback has another or incomplete PR identity')
    assignees = []
    for user in issue['assignees']:
        if (not isinstance(user, dict) or not isinstance(user.get('node_id'), str) or not user['node_id']
                or not isinstance(user.get('login'), str) or not user['login']):
            raise PipelineError('author assignment readback has incomplete assignee identities')
        assignees.append((user['node_id'], user['login']))
    if len(set(item[0] for item in assignees)) != len(assignees):
        raise PipelineError('author assignment readback has duplicate node IDs')
    return tuple(sorted(assignees))


def require_assigned(gh, request):
    expected = (request['author']['id'], request['author']['login'])
    if expected not in read_assignment(gh, request):
        raise PipelineError('bound author node ID is not an assignee; handoff remains unverified')


class Assignment:
    def __init__(self, database):
        self.database = Path(database)
        if not self.database.is_file() or self.database.is_symlink():
            raise PipelineError('author assignment requires the actual scheduler database')
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection, create=True)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _connect(self):
        connection = sqlite3.connect(str(self.database), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA synchronous=EXTRA')
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _schema(connection, *, create=False):
        row = connection.execute('SELECT type,name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE', (TABLE,)).fetchone()
        if row is None:
            if not create:
                raise PipelineError('author assignment receipt table disappeared; preserve uncertainty')
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                raise PipelineError('author assignment requires the scheduler schema')
            connection.execute(SCHEMA)
        elif row['type'] != 'table' or row['name'] != TABLE or _schema_signature(row['sql']) != _schema_signature(SCHEMA):
            raise PipelineError('unknown author assignment schema; preserve uncertainty')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE tbl_name=? COLLATE NOCASE AND type IN ('trigger','index') AND sql IS NOT NULL", (TABLE,)).fetchone():
            raise PipelineError('unknown author assignment schema objects')

    def deliver(self, gh, request, attempt_id, guard, *, allow_new=False):
        scope = {name: request[name] for name in ['repository', 'identity', 'author', 'attempt', 'target_reservation']}
        scope['attempt_id'] = attempt_id
        body = canonical(scope).decode('utf-8')
        key = sha({'pr': request['attempt']['pr'], 'attempt_id': attempt_id})
        # The row is a one-use side-effect fence. Expiry or task retry cannot
        # clear it. Do all database waits before the last external/local gates.
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection)
                if connection.execute('SELECT 1 FROM settings WHERE key IN (?,?)',
                    (TABLE+':revoking:'+key,TABLE+':revoked:'+key)).fetchone():
                    raise PipelineError('author delivery was permanently revoked; no new delivery or handoff')
                row = connection.execute('SELECT * FROM '+TABLE+' WHERE attempt_key=?', (key,)).fetchone()
                if row is not None and row['scope_json'] != body:
                    raise PipelineError('author assignment immutable scope changed')
                existing = row is not None
                if not existing:
                    connection.execute('INSERT INTO '+TABLE+' VALUES (?,?,?)', (key, body, 'attempting'))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        before = read_assignment(gh, request)
        expected = (request['author']['id'], request['author']['login'])
        if expected not in before:
            if existing or not allow_new:
                raise PipelineError('author assignment outcome is unknown or was manually changed; no retry')
            snapshot = guard.assignment_gate()
            if read_assignment(gh, request) != before:
                raise PipelineError('author assignees changed before delivery; no overwrite or retry')
            guard.final_before_post(request['attempt'], snapshot)
            try:
                gh.json('api', f"repos/{request['repository']}/issues/{request['number']}/assignees",
                        '--method', 'POST', '--input', '-', input_value={'assignees': [request['author']['login']]})
            except Exception:
                # An accepted request with a lost response can be recovered by
                # GET. It never authorizes a second POST or pipeline fallback.
                pass
        guard(request['attempt'], write=False)
        after = read_assignment(gh, request)
        if expected not in after or not set(before).issubset(after):
            raise PipelineError('author assignment was ignored, changed or remains unknown; no handoff-done')
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection)
                changed = connection.execute('UPDATE '+TABLE+" SET state='delivered' WHERE attempt_key=? AND scope_json=?", (key, body))
                if changed.rowcount != 1:
                    raise PipelineError('author assignment durable receipt disappeared')
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def receipt(self, request, attempt_id):
        key = sha({'pr': request['attempt']['pr'], 'attempt_id': attempt_id})
        with closing(self._connect()) as connection:
            self._schema(connection)
            row = connection.execute('SELECT scope_json,state FROM '+TABLE+' WHERE attempt_key=?', (key,)).fetchone()
            if row is None or row['state'] != 'delivered':
                raise PipelineError('author assignment has no durable verified receipt')
            scope = json.loads(row['scope_json'])
            expected = {name: request[name] for name in ['repository', 'identity', 'author', 'attempt', 'target_reservation']}
            expected['attempt_id'] = attempt_id
            if scope != expected:
                raise PipelineError('author assignment receipt differs from its immutable scope')
            return scope
