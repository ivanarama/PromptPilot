from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from conftest import isolated_db as isolated_db
from test_review_routing_stage_guard import setup_guard as setup_guard
from test_review_routing_adapter import adapter as adapter
from test_review_routing_fix import author_bridge as author_bridge
import pytest
from promptpilot.models import TaskCreate


def expire(database):
    with database._connect(immediate=True) as connection:
        connection.execute("UPDATE pipeline_target_reservations SET expires_at='2000-01-01T00:00:00+00:00'")


def test_review_re_election_cannot_reanimate_expired_claim(adapter, isolated_db, monkeypatch, tmp_path):
    peer, _health, _path, report, run = adapter
    isolated_db.create_task(TaskCreate(prompt='Private REVIEW', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    monkeypatch.setenv('PP_TASK_ID', str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND', 'headless')
    code, old = run('next', 'review')
    assert code == 0 and old['action'] == 'audit', old
    expire(isolated_db)
    code, refused = run('complete', 'review', '--lease', old['lease'], '--report', str(report))
    assert code == 2 and not peer.posts, refused
    code, elected = run('next', 'review')
    if code == 0 and elected['action'] == 'audit':
        code, completion = run('complete', 'review', '--lease', old['lease'], '--report', str(report))
        assert code == 2 and not peer.posts, {'completion': completion, 'posts': peer.posts}


def test_fix_re_election_cannot_reanimate_expired_claim(author_bridge):
    peer, _health, _path, _task, run, database = author_bridge
    code, old = run('next', 'fix')
    assert code == 0 and old['action'] == 'author-handoff', old
    expire(database)
    code, refused = run('complete', 'fix', '--lease', old['lease'])
    assert code == 2 and not peer.assignment_posts, refused
    code, elected = run('next', 'fix')
    if code == 0 and elected['action'] == 'author-handoff':
        code, completion = run('complete', 'fix', '--lease', old['lease'])
        assert code == 2 and not peer.assignment_posts, {'completion': completion, 'posts': peer.posts,
                                                        'assignments': peer.assignment_posts}


def test_review_worker_heartbeat_cannot_reanimate_expired_claim(adapter, isolated_db, monkeypatch, tmp_path):
    from promptpilot import review_routing_request as requests, worker
    peer, _health, _path, report, run = adapter
    isolated_db.create_task(TaskCreate(prompt='Private REVIEW', working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    started = task.started_at.astimezone(timezone.utc).isoformat()
    monkeypatch.setenv('PP_TASK_ID', str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', started)
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND', 'headless')
    code, selected = run('next', 'review')
    assert code == 0 and selected['action'] == 'audit', selected
    request = requests.decode(selected['lease'])
    heartbeat = worker._pipeline_target_heartbeater(request['target_reservation'], task.id,
                                                   task_started_at=started, ownership_kind='headless')
    expire(isolated_db)
    code, refused = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 2 and not peer.posts, refused
    # The actual worker callback, invoked synchronously without starting a
    # thread or a provider. It must not restore expired typed write authority.
    try:
        heartbeat(force=True)
    except worker.PipelineTargetLeaseLost:
        return
    code, completion = run('complete', 'review', '--lease', selected['lease'], '--report', str(report))
    assert code == 2 and not peer.posts, {'completion': completion, 'posts': peer.posts}


@pytest.mark.parametrize('stage', ['review', 'fix'])
@pytest.mark.parametrize('action', ['wait', 'empty', 'error'])
def test_tokenless_typed_worker_envelopes_never_dispatch_provider(isolated_db, monkeypatch, tmp_path, stage, action):
    from promptpilot import pipeline_insights as insights
    isolated_db.create_task(TaskCreate(prompt='Private '+stage, working_dir=str(tmp_path)))
    task = isolated_db.get_next_runnable()
    queue = {'id': stage, 'execution': {'mode': 'auto', 'command': ['pipelinectl', 'next', stage],
                                      'review_routing_protocol': 'review-routing-v1'}}
    monkeypatch.setattr(insights, '_matching_queue', lambda _: ('private', {'repository': 'owner/repo'}, queue))
    monkeypatch.setattr(insights, '_tool_available', lambda *a: (True, ''))
    monkeypatch.setattr(insights, 'load_providers', lambda: {})
    monkeypatch.setattr(insights, '_tool_preflight', lambda *a, **k: {'action': action, 'protocol': 'review-routing-v1',
                                                                   'reason': 'private passive response'})
    route = insights.execution_route(task, 'must not dispatch', str(tmp_path))
    assert route['action'] in {'complete_empty', 'block', 'defer'}, route
    assert 'prompt' not in route, route


def claimed(database,*,strict=True):
    database.create_task(TaskCreate(prompt='Private exact reservation'))
    task=database.get_next_runnable()
    row=database.reserve_pipeline_target('owner/repo','review',42,'a'*40,task.id,300,
        task_started_at=task.started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind='headless',strict_lifetime=strict)
    assert row is not None
    return task,row


@pytest.mark.parametrize('operation',['reserve','renew','bulk'])
@pytest.mark.parametrize('change',['expired','cancelled'])
def test_persistent_typed_policy_fences_even_legacy_callers(isolated_db,operation,change):
    from promptpilot.review_routing_request import RESERVATION_FIELDS
    task,row=claimed(isolated_db)
    assert set(row)==set(RESERVATION_FIELDS.split())
    if change=='expired':expire(isolated_db)
    else:assert isolated_db.request_cancel(task.id)
    with isolated_db._connect() as connection:
        before=dict(connection.execute('SELECT * FROM pipeline_target_reservations').fetchone())
    if operation=='reserve':
        result=isolated_db.reserve_pipeline_target('owner/repo','review',42,'a'*40,task.id,300,
            task_started_at=row['task_started_at'],ownership_kind='headless')
        assert result is None
    elif operation=='renew':
        assert isolated_db.renew_pipeline_target_reservation(row,300) is None
    else:
        assert isolated_db.renew_task_pipeline_target_reservations(task.id,300)==0
    with isolated_db._connect() as connection:
        after=dict(connection.execute('SELECT * FROM pipeline_target_reservations').fetchone())
    assert after==before and after['strict_lifetime']==1
    # An expired running owner remains a barrier, not an available new slot.
    assert isolated_db.list_pipeline_target_reservations()[0]['token']==row['token']


def test_live_legacy_upgrade_is_sticky_and_wire_remains_unchanged(isolated_db):
    task,row=claimed(isolated_db,strict=False)
    upgraded=isolated_db.reserve_pipeline_target('owner/repo','review',42,'a'*40,task.id,300,
        task_started_at=row['task_started_at'],ownership_kind='headless',strict_lifetime=True)
    assert upgraded['token']==row['token'] and set(upgraded)==set(row)
    expire(isolated_db)
    assert isolated_db.renew_pipeline_target_reservation(row,300) is None
    assert isolated_db.renew_task_pipeline_target_reservations(task.id,300)==0


def test_cancelled_task_cannot_obtain_first_typed_reservation(isolated_db):
    isolated_db.create_task(TaskCreate(prompt='Cancelled private claimant'))
    task=isolated_db.get_next_runnable();assert isolated_db.request_cancel(task.id)
    assert isolated_db.reserve_pipeline_target('owner/repo','review',42,'a'*40,task.id,300,
        task_started_at=task.started_at.isoformat(),ownership_kind='headless',strict_lifetime=True) is None
    with isolated_db._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM pipeline_target_reservations').fetchone()[0]==0


@pytest.mark.parametrize('operation',['reserve','renew','bulk'])
def test_legacy_running_owner_keeps_deliberate_recovery_rule(isolated_db,operation):
    task,row=claimed(isolated_db,strict=False);expire(isolated_db)
    if operation=='reserve':
        result=isolated_db.reserve_pipeline_target('owner/repo','review',42,'a'*40,task.id,300,
            task_started_at=row['task_started_at'],ownership_kind='headless')
        assert result is not None and result['token']==row['token']
    elif operation=='renew':
        assert isolated_db.renew_pipeline_target_reservation(row,300) is not None
    else:
        assert isolated_db.renew_task_pipeline_target_reservations(task.id,300)==1
    with isolated_db._connect() as connection:
        assert connection.execute('SELECT strict_lifetime FROM pipeline_target_reservations').fetchone()[0]==0


@pytest.mark.parametrize('operation',['reserve','renew','bulk'])
def test_strict_clock_is_read_after_entering_database_writer(isolated_db,monkeypatch,operation):
    task,row=claimed(isolated_db)
    deadline=datetime.now(timezone.utc)+timedelta(seconds=60)
    with isolated_db._connect(immediate=True) as connection:
        connection.execute('UPDATE pipeline_target_reservations SET expires_at=?',(deadline.isoformat(),))
    clock={'now':deadline-timedelta(seconds=1)}
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return clock['now'].astimezone(tz or timezone.utc)
    original=isolated_db._connect
    @contextmanager
    def waited(*args,**kwargs):
        with original(*args,**kwargs) as connection:
            clock['now']=deadline+timedelta(seconds=1)
            yield connection
    monkeypatch.setattr(isolated_db,'datetime',Clock)
    monkeypatch.setattr(isolated_db,'_connect',waited)
    if operation=='reserve':
        assert isolated_db.reserve_pipeline_target('owner/repo','review',42,'a'*40,task.id,300,
            task_started_at=row['task_started_at'],ownership_kind='headless') is None
    elif operation=='renew':
        assert isolated_db.renew_pipeline_target_reservation(row,300) is None
    else:
        assert isolated_db.renew_task_pipeline_target_reservations(task.id,300)==0


def test_old_reservation_schema_migrates_without_reinterpreting_legacy_rows(isolated_db):
    _task,row=claimed(isolated_db,strict=False)
    fields=list(row)
    with isolated_db._connect(immediate=True) as connection:
        connection.execute('DROP TABLE pipeline_target_reservations')
        connection.execute('''CREATE TABLE pipeline_target_reservations (
            repository TEXT NOT NULL,stage TEXT NOT NULL,number INTEGER NOT NULL,head TEXT NOT NULL,
            task_id INTEGER NOT NULL,task_started_at TEXT NOT NULL DEFAULT '',ownership_kind TEXT NOT NULL DEFAULT '',
            herdr_session_state TEXT NOT NULL DEFAULT '',herdr_pane_id TEXT NOT NULL DEFAULT '',
            herdr_tab_id TEXT NOT NULL DEFAULT '',herdr_workspace_id TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL,reserved_at TEXT NOT NULL,expires_at TEXT NOT NULL,
            PRIMARY KEY(repository,stage,number,head))''')
        connection.execute('INSERT INTO pipeline_target_reservations ('+','.join(fields)+') VALUES ('+
                           ','.join('?' for _ in fields)+')',tuple(row[name] for name in fields))
    for _ in range(2):
        isolated_db.init_db()
        assert isolated_db.list_pipeline_target_reservations()==[row]
        with isolated_db._connect() as connection:
            assert connection.execute('SELECT strict_lifetime FROM pipeline_target_reservations').fetchone()[0]==0
