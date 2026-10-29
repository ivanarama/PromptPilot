"""Crash/timeout receipts do not permit a second non-idempotent POST."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
from pathlib import Path
import threading

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_journal import Journal
from promptpilot.review_routing_wire import render


PAYLOAD=json.loads((Path(__file__).parent/'fixtures/review-routing-wire-v1.json').read_text(encoding='utf-8'))['records'][0]['payload']
OWNER=PAYLOAD['owner']
BODY=render(PAYLOAD)
LEGACY_SCHEMA=(Path(__file__).parent/'fixtures/review-routing-journal-v1.sql').read_text(encoding='utf-8')
REQUEST={'owner':OWNER,'phase':'publication','repository':'owner/repo','number':42,'body':BODY}


def test_crash_after_prepare_or_invisible_timeout_is_not_permission_to_repeat_post(tmp_path):
    first=Journal(tmp_path)
    assert first.prepare(**REQUEST) is True
    recovered=Journal(tmp_path)
    assert recovered.get(owner=OWNER,phase='publication')['state']=='pending'
    with pytest.raises(PipelineError,match='ambiguous'):
        recovered.prepare(**REQUEST)
    recovered.ambiguous(owner=OWNER,phase='publication')
    with pytest.raises(PipelineError,match='ambiguous'):
        Journal(tmp_path).prepare(**REQUEST)
    # Later authenticated read-back of the exact request can recover the
    # returned ID. Mere lack of a visible result never clears the request.
    recovered.confirm(owner=OWNER,phase='publication',returned_id='9007199254740993',body=BODY)
    assert Journal(tmp_path).prepare(**REQUEST) is False
    assert recovered.get(owner=OWNER,phase='publication')['returned_id']=='9007199254740993'


def test_two_local_process_equivalents_can_attempt_exact_post_only_once(tmp_path):
    readers=[Journal(tmp_path),Journal(tmp_path)]
    barrier=threading.Barrier(2)
    def attempt(journal):
        barrier.wait()
        try:return journal.prepare(**REQUEST)
        except PipelineError as exc:
            assert 'ambiguous' in str(exc)
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,readers))
    assert sum(results)==1


def test_response_from_another_id_or_modified_body_cannot_rewrite_known_receipt(tmp_path):
    journal=Journal(tmp_path)
    journal.prepare(**REQUEST)
    with pytest.raises(PipelineError,match='prepared exact body'):
        journal.confirm(owner=OWNER,phase='publication',returned_id='10',body=BODY+' ')
    journal.confirm(owner=OWNER,phase='publication',returned_id='10',body=BODY)
    journal.confirm(owner=OWNER,phase='publication',returned_id='10',body=BODY)
    with pytest.raises(PipelineError,match='known returned ID'):
        journal.confirm(owner=OWNER,phase='publication',returned_id='11',body=BODY)
    with pytest.raises(PipelineError,match='already completed'):
        journal.ambiguous(owner=OWNER,phase='publication')


def test_request_key_is_bound_to_exact_body_target_and_owner(tmp_path):
    journal=Journal(tmp_path)
    journal.prepare(**REQUEST)
    changed=copy.deepcopy(PAYLOAD)
    changed['anchor']='OTHER_ANCHOR'
    for request in [dict(REQUEST,body=render(changed)),dict(REQUEST,number=43),dict(REQUEST,repository='owner/other')]:
        with pytest.raises(PipelineError,match='another body/target|wire target'):
            journal.prepare(**request)
    with pytest.raises(PipelineError,match='owner differs'):
        journal.prepare(**dict(REQUEST,owner='00000000-0000-4000-8000-000000000002'))


def test_unsupported_local_journal_version_fails_before_any_request(tmp_path):
    journal=Journal(tmp_path)
    import sqlite3
    with closing(sqlite3.connect(journal.path)) as connection:
        connection.execute('PRAGMA user_version=3')
    with pytest.raises(PipelineError,match='unsupported routing journal'):
        Journal(tmp_path)


def test_first_request_cannot_bind_pr42_wire_to_pr43_endpoint(tmp_path):
    with pytest.raises(PipelineError,match='wire target'):
        Journal(tmp_path).prepare(**dict(REQUEST,number=43))


def test_new_owner_or_phase_cannot_bypass_an_unresolved_post_for_same_lifetime_pr(tmp_path):
    journal=Journal(tmp_path)
    journal.prepare(**REQUEST)
    changed=copy.deepcopy(PAYLOAD)
    changed['owner']='00000000-0000-4000-8000-000000000002'
    for request in [dict(REQUEST,phase='another-phase'),dict(REQUEST,owner=changed['owner'],body=render(changed))]:
        with pytest.raises(PipelineError,match='unresolved POST'):
            Journal(tmp_path).prepare(**request)
    # Resolving the exact earlier response permits the next guarded phase;
    # it does not itself grant server lease ownership or budget authority.
    journal.confirm(owner=OWNER,phase='publication',returned_id='10',body=BODY)
    assert journal.prepare(**dict(REQUEST,phase='another-phase')) is True


def test_pre_network_refusal_is_generation_cas_and_never_releases_a_spent_request(tmp_path):
    journal=Journal(tmp_path)
    first=journal.prepare(**REQUEST,with_generation=True)
    assert 'generation' not in journal.get(owner=OWNER,phase='publication')
    assert all('generation' not in item for item in journal.pending_for(PAYLOAD['pr']))
    journal.arm(owner=OWNER,phase='publication',generation=first,body=BODY)
    for wrong in ['f'*32,None]:
        with pytest.raises(PipelineError,match='CAS failed|fresh prepare'):
            journal.refuse_before_post(owner=OWNER,phase='publication',generation=wrong,body=BODY)
    journal.refuse_before_post(owner=OWNER,phase='publication',generation=first,body=BODY)
    assert journal.pending_for(PAYLOAD['pr'])==[]
    second=journal.prepare(**REQUEST,with_generation=True)
    assert second!=first
    journal.arm(owner=OWNER,phase='publication',generation=second,body=BODY)
    with pytest.raises(PipelineError,match='CAS failed'):
        journal.refuse_before_post(owner=OWNER,phase='publication',generation=first,body=BODY)
    journal.observe_returned_id(owner=OWNER,phase='publication',returned_id='10')
    with pytest.raises(PipelineError,match='CAS failed'):
        journal.refuse_before_post(owner=OWNER,phase='publication',generation=second,body=BODY)
    journal.ambiguous(owner=OWNER,phase='publication')
    with pytest.raises(PipelineError,match='CAS failed'):
        journal.refuse_before_post(owner=OWNER,phase='publication',generation=second,body=BODY)
    with pytest.raises(PipelineError,match='ambiguous'):
        journal.prepare(**REQUEST,with_generation=True)


def test_legacy_pending_and_known_receipts_migrate_without_pre_network_rights(tmp_path):
    import hashlib
    import sqlite3
    tmp_path.mkdir(exist_ok=True)
    database=tmp_path/'review-routing-journal.sqlite3'
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(LEGACY_SCHEMA)
        connection.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',
            (OWNER,'publication','owner/repo',42,BODY,hashlib.sha256(BODY.encode()).hexdigest(),
             'pending',None,1.0,1.0))
        connection.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',
            (OWNER,'done','owner/repo',42,BODY,hashlib.sha256(BODY.encode()).hexdigest(),
             'done','123',1.0,2.0))
        connection.execute('PRAGMA user_version=1')
        connection.commit()
    journal=Journal(tmp_path)
    assert journal.get(owner=OWNER,phase='done')['returned_id']=='123'
    pending=journal.get(owner=OWNER,phase='publication')
    assert 'generation' not in pending and pending['state']=='pending'
    with pytest.raises(PipelineError,match='fresh prepare'):
        journal.refuse_before_post(owner=OWNER,phase='publication',generation=None,body=BODY)
    with pytest.raises(PipelineError,match='ambiguous'):
        journal.prepare(**REQUEST,with_generation=True)
    journal.confirm(owner=OWNER,phase='publication',returned_id='124',body=BODY)
    assert Journal(tmp_path).get(owner=OWNER,phase='publication')['returned_id']=='124'


def test_interrupted_legacy_constructor_recovers_its_unversioned_empty_schema(tmp_path):
    import sqlite3
    class Interrupted(sqlite3.Connection):
        def execute(self,statement,*args,**kwargs):
            if statement=='PRAGMA user_version=1':
                raise SystemExit('crash after old autocommitted CREATE TABLE')
            return super().execute(statement,*args,**kwargs)
    database=tmp_path/'review-routing-journal.sqlite3'
    with pytest.raises(SystemExit):
        with closing(sqlite3.connect(database,factory=Interrupted)) as connection:
            with connection:
                connection.execute(LEGACY_SCHEMA)
                connection.execute('PRAGMA user_version=1')
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==0
        assert len(connection.execute('PRAGMA table_info(requests)').fetchall())==10
    journal=Journal(tmp_path)
    generation=journal.prepare(**REQUEST,with_generation=True)
    assert isinstance(generation,str)
    journal.arm(owner=OWNER,phase='publication',generation=generation,body=BODY)
    journal.refuse_before_post(owner=OWNER,phase='publication',generation=generation,body=BODY)


@pytest.mark.parametrize('version',[0,1,2])
def test_unknown_schema_is_refused_without_false_version_upgrade(tmp_path,version):
    import sqlite3
    database=tmp_path/'review-routing-journal.sqlite3'
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE TABLE requests(owner TEXT,unexpected_data BLOB)')
        connection.execute(f'PRAGMA user_version={version}')
    with pytest.raises(PipelineError,match='schema'):
        Journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==version
        assert [row[1] for row in connection.execute('PRAGMA table_info(requests)')]==['owner','unexpected_data']


@pytest.mark.parametrize('variant',['history','literal','index'])
def test_unversioned_database_is_not_inferred_empty_or_known_from_column_names(tmp_path,variant):
    import hashlib
    import sqlite3
    database=tmp_path/'review-routing-journal.sqlite3'
    with closing(sqlite3.connect(database)) as connection:
        schema=LEGACY_SCHEMA.replace("'done'","'d one'") if variant=='literal' else LEGACY_SCHEMA
        connection.execute(schema)
        if variant=='history':
            connection.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',
                (OWNER,'publication','owner/repo',42,BODY,hashlib.sha256(BODY.encode()).hexdigest(),
                 'pending',None,1.0,1.0))
        if variant=='index':
            connection.execute('CREATE INDEX unknown_custom_index ON requests(updated_at)')
        connection.commit()
    with pytest.raises(PipelineError,match='schema'):
        Journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==0
        assert len(connection.execute('PRAGMA table_info(requests)').fetchall())==10
        assert connection.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==(1 if variant=='history' else 0)
