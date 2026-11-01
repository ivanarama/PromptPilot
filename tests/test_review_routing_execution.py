"""One-use local start barrier with actual scheduler task/reservation rows.

The supplied test gates do not prove a GitHub source. Public FIX admission and
the real worker launch seam still require their separate implementation/tests.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
from datetime import datetime,timedelta,timezone
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from promptpilot.models import TaskCreate
from promptpilot.pipeline_errors import PipelineError
from promptpilot.pr_identity import PRIdentityV1
from promptpilot.review_routing_execution import OneUseStart,TABLE,SCHEMA,ESTABLISHED
from promptpilot.review_routing_wire import context
from test_review_routing_wire import publication


@pytest.fixture
def start_scope(isolated_db,tmp_path):
    task=isolated_db.create_task(TaskCreate(prompt='Project - FIX',working_dir=str(tmp_path)))
    task=isolated_db.get_next_runnable();assert task is not None
    started=task.started_at.astimezone(timezone.utc).isoformat()
    reservation=isolated_db.reserve_pipeline_target('owner/repo','fix-pr',42,'a'*40,task.id,300,
                          task_started_at=started,ownership_kind='headless')
    assert reservation is not None
    seed=publication()
    source={'publication_id':'1000','claim_id':'1001','completion_id':'1002','head':'a'*40,
            'identity_sha256':PRIdentityV1(**seed['identity']).sha256,
            'epoch':seed['epoch'],'report_sha256':seed['report_sha256']}
    scope={'version':1,'repository':'owner/repo','pr':seed['pr'],'source':source,
           'attempt_id':'2000','started_id':'2003','context_sha256':context(seed),
           'owner':seed['owner'],'executor':{'kind':'pipeline','login':'owner'},
           'attempt_uuid':'00000000-0000-4000-8000-000000000002',
           'task_id':task.id,'task_started_at':started,'reservation':reservation,
           'config_sha256':'c'*64,'reader_sha256':{role:'d'*64 for role in ['review','fix','merge','tail']}}
    return isolated_db,scope


def update(database,sql,parameters=()):
    with closing(sqlite3.connect(str(database))) as connection:
        with connection:connection.execute(sql,parameters)


def test_consumption_is_durable_before_final_gate_and_only_one_factory(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);events=[]
    def gate(frozen):
        assert frozen==scope and store.status(scope)['state']=='spawn-attempting'
        events.append('gate');return True
    def factory():events.append('factory');return SimpleNamespace(pid=123)
    child=store.invoke(scope,final_guard=gate,factory=factory)
    assert child.pid==123 and events==['gate','factory']
    store.observe_started(scope,child.pid);store.observe_started(scope,child.pid)
    assert store.status(scope)['state']=='launched'
    with pytest.raises(PipelineError):store.invoke(scope,final_guard=gate,factory=factory)
    assert events==['gate','factory']


def test_two_concurrent_callers_get_one_consumption_and_one_factory(start_scope):
    db,scope=start_scope;stores=[OneUseStart(db.DB_PATH),OneUseStart(db.DB_PATH)]
    barrier=threading.Barrier(2);calls=[]
    def invoke(index):
        barrier.wait()
        try:
            stores[index].invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append(index))
            return True
        except PipelineError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(invoke,[0,1]))
    assert sum(results)==1 and len(calls)==1


@pytest.mark.parametrize('fault',['failed','changed-attempt','cancel','wrong-token','expired',
                                  'author','herdr','remote','detached','worktree'])
def test_invalid_live_task_or_executor_never_reaches_gate_or_factory(start_scope,fault):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH)
    if fault=='failed':update(db.DB_PATH,"UPDATE tasks SET status='failed' WHERE id=?",(scope['task_id'],))
    elif fault=='changed-attempt':
        changed=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        scope['task_started_at']=changed;scope['reservation']['task_started_at']=changed
    elif fault=='cancel':assert db.request_cancel(scope['task_id'])
    elif fault=='wrong-token':scope['reservation']['token']='f'*32
    elif fault=='expired':
        update(db.DB_PATH,'UPDATE pipeline_target_reservations SET expires_at=?',
               ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    elif fault=='author':scope['executor']['kind']='author'
    elif fault=='herdr':scope['reservation']['ownership_kind']='herdr'
    elif fault=='remote':update(db.DB_PATH,"UPDATE tasks SET machine='remote' WHERE id=?",(scope['task_id'],))
    elif fault=='detached':update(db.DB_PATH,'UPDATE tasks SET detached=1 WHERE id=?',(scope['task_id'],))
    else:update(db.DB_PATH,'UPDATE tasks SET worktree=1 WHERE id=?',(scope['task_id'],))
    def forbidden(*args):pytest.fail('invalid start must not reach external gate/factory')
    with pytest.raises(PipelineError):store.invoke(scope,final_guard=forbidden,factory=forbidden)
    with closing(sqlite3.connect(str(db.DB_PATH))) as connection:
        assert connection.execute('SELECT count(*) FROM '+TABLE).fetchone()[0]==0


def test_admission_pause_keeps_already_claimed_task_start_valid(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH)
    db.set_setting('worker_paused','1')
    assert store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:'owned child')=='owned child'
    assert db.is_paused() and db.get_task(scope['task_id']).status.value=='running'


@pytest.mark.parametrize('result',[False,None])
def test_definite_final_gate_refusal_allows_only_same_scope_controlled_retry(start_scope,result):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);calls=[]
    with pytest.raises(PipelineError):
        store.invoke(scope,final_guard=lambda _scope:result,factory=lambda:calls.append('unexpected'))
    assert store.status(scope)['state']=='not-started' and calls==[]
    store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('one'))
    with pytest.raises(PipelineError):
        store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('two'))
    assert calls==['one']


def test_interrupt_during_final_gate_preserves_unknown_start_barrier(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);calls=[]
    def interrupted(_scope):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):store.invoke(scope,final_guard=interrupted,factory=lambda:calls.append('child'))
    assert store.status(scope)['state']=='spawn-attempting'
    with pytest.raises(PipelineError):store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('second'))
    assert calls==[]


def test_factory_file_error_is_unknown_and_never_uses_legacy_spawn_retry(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);calls=[]
    def failed():calls.append('call');raise FileNotFoundError('ambiguous launch boundary')
    with pytest.raises(FileNotFoundError):store.invoke(scope,final_guard=lambda _scope:True,factory=failed)
    assert store.status(scope)['state']=='unknown'
    with pytest.raises(PipelineError):store.invoke(scope,final_guard=lambda _scope:True,factory=failed)
    assert calls==['call']


def test_actual_child_then_lost_factory_return_cannot_launch_again(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);children=[]
    def uncertain():
        children.append(subprocess.Popen([sys.executable,'-c','pass'],stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        raise OSError('child exists, but factory response lost')
    try:
        with pytest.raises(OSError):store.invoke(scope,final_guard=lambda _scope:True,factory=uncertain)
        with pytest.raises(PipelineError):store.invoke(scope,final_guard=lambda _scope:True,factory=uncertain)
        assert len(children)==1 and store.status(scope)['state']=='unknown'
    finally:
        for child in children:child.wait(timeout=15)


def test_reopening_after_receipt_table_loss_cannot_launch_again(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);calls=[]
    store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('one'))
    update(db.DB_PATH,'DROP TABLE '+TABLE)
    with pytest.raises(PipelineError,match='disappeared'):
        reopened=OneUseStart(db.DB_PATH)
        reopened.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('second'))
    assert calls==['one']


@pytest.mark.parametrize('erase', [True,False])
def test_missing_or_corrupt_established_marker_preserves_start_barrier(start_scope,erase):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);calls=[]
    store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('one'))
    if erase:
        update(db.DB_PATH,'DELETE FROM settings WHERE key=?',(ESTABLISHED,))
    else:
        update(db.DB_PATH,"UPDATE settings SET value='changed' WHERE key=?",(ESTABLISHED,))
    with pytest.raises(PipelineError,match='marker'):
        OneUseStart(db.DB_PATH)
    with pytest.raises(PipelineError,match='marker'):
        store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('second'))
    assert calls==['one']


def test_new_attempt_or_new_running_task_never_reuses_consumed_source(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH);calls=[]
    store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:calls.append('one'))
    alias=copy.deepcopy(scope);alias['attempt_id']='2001'
    with pytest.raises(PipelineError):store.invoke(alias,final_guard=lambda _scope:True,factory=lambda:calls.append('alias'))
    db.release_pipeline_target_reservations(scope['task_id'])
    changed=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
    update(db.DB_PATH,'UPDATE tasks SET started_at=? WHERE id=?',(changed,scope['task_id']))
    retry=copy.deepcopy(scope);retry['task_started_at']=changed
    retry['reservation']=db.reserve_pipeline_target('owner/repo','fix-pr',42,'a'*40,scope['task_id'],300,
                              task_started_at=changed,ownership_kind='headless')
    retry['attempt_uuid']='00000000-0000-4000-8000-000000000003'
    with pytest.raises(PipelineError):store.invoke(retry,final_guard=lambda _scope:True,factory=lambda:calls.append('retry'))
    assert calls==['one']


def test_unknown_existing_schema_is_preserved(start_scope):
    db,_scope=start_scope
    update(db.DB_PATH,'CREATE TABLE '+TABLE+' (wrong TEXT)')
    update(db.DB_PATH,'INSERT INTO '+TABLE+' VALUES (?)',('preserved',))
    with pytest.raises(PipelineError,match='unknown technical start schema'):OneUseStart(db.DB_PATH)
    with closing(sqlite3.connect(str(db.DB_PATH))) as connection:
        assert connection.execute('SELECT * FROM '+TABLE).fetchall()==[('preserved',)]


@pytest.mark.parametrize('alias',[TABLE.upper(),TABLE.title()])
def test_case_alias_with_state_reset_triggers_is_rejected_without_any_mutation(start_scope,alias):
    db,_scope=start_scope
    update(db.DB_PATH,SCHEMA.replace(TABLE,alias))
    for event in ['INSERT','UPDATE']:
        update(db.DB_PATH,'CREATE TRIGGER reset_'+event+' AFTER '+event+' ON '+alias+
               " BEGIN UPDATE "+alias+" SET state='not-started' WHERE attempt_key=NEW.attempt_key; END")
    with closing(sqlite3.connect(str(db.DB_PATH))) as connection:
        before=connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE OR tbl_name=? COLLATE NOCASE ORDER BY name",(TABLE,TABLE)).fetchall()
    with pytest.raises(PipelineError,match='unknown technical start schema'):OneUseStart(db.DB_PATH)
    with closing(sqlite3.connect(str(db.DB_PATH))) as connection:
        assert connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE OR tbl_name=? COLLATE NOCASE ORDER BY name",(TABLE,TABLE)).fetchall()==before
        assert connection.execute('SELECT count(*) FROM '+alias).fetchone()[0]==0


@pytest.mark.parametrize('change',['trigger','missing-table'])
def test_schema_is_checked_again_under_the_consume_writer_lock(start_scope,change):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH)
    if change=='trigger':
        update(db.DB_PATH,'CREATE TRIGGER late_reset AFTER INSERT ON '+TABLE+
               " BEGIN UPDATE "+TABLE+" SET state='not-started' WHERE attempt_key=NEW.attempt_key; END")
    else:update(db.DB_PATH,'DROP TABLE '+TABLE)
    def forbidden(*args):pytest.fail('schema drift must stop before gate/factory')
    with pytest.raises(PipelineError):store.invoke(scope,final_guard=forbidden,factory=forbidden)
    with closing(sqlite3.connect(str(db.DB_PATH))) as connection:
        if change=='trigger':assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='late_reset'").fetchone()
        else:assert connection.execute('SELECT 1 FROM sqlite_master WHERE name=?',(TABLE,)).fetchone() is None


def test_child_observation_cannot_reset_or_rebind_a_consumed_start(start_scope):
    db,scope=start_scope;store=OneUseStart(db.DB_PATH)
    store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:None)
    store.observe_started(scope,123)
    with pytest.raises(PipelineError):store.observe_started(scope,124)
    with pytest.raises(PipelineError):store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:None)
    assert store.status(scope)['pid']==123



def test_stopped_receipt_requires_original_kernel_boundary_and_keeps_start_spent(start_scope):
    from promptpilot.process_tree import OwnedProcess
    db,scope=start_scope;store=OneUseStart(db.DB_PATH)
    tree=store.invoke(scope,final_guard=lambda _scope:True,
        factory=lambda:OwnedProcess.start([sys.executable,'-c','import time; time.sleep(60)'],
                                          stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
    try:
        store.retain_owned(scope,tree);store.observe_started(scope,tree.process.pid)
        store.confirm_stopped(scope,tree)
        assert tree.process.poll() is not None and store.status(scope)['state']=='stopped'
        with pytest.raises(PipelineError):store.invoke(scope,final_guard=lambda _scope:True,factory=lambda:pytest.fail('second factory'))
    finally:tree.close()


def test_other_process_handle_never_settles_the_original_started_task(start_scope):
    from promptpilot.process_tree import OwnedProcess
    db,scope=start_scope;store=OneUseStart(db.DB_PATH)
    def child():return OwnedProcess.start([sys.executable,'-c','import time; time.sleep(60)'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    tree=store.invoke(scope,final_guard=lambda _scope:True,factory=child)
    other=child()
    try:
        store.retain_owned(scope,tree);store.observe_started(scope,tree.process.pid)
        impostor=OwnedProcess(tree.process)
        with pytest.raises(PipelineError):store.retain_owned(scope,impostor)
        with pytest.raises(PipelineError):store.confirm_stopped(scope,impostor)
        with pytest.raises(PipelineError,match='original owned lifetime'):store.confirm_stopped(scope,other)
        assert tree.process.poll() is None and store.status(scope)['state']=='launched'
        store.confirm_stopped(scope,tree)
        assert other.process.poll() is None
    finally:
        tree.close();other.close()
        tree.process.wait(timeout=10);other.process.wait(timeout=10)


def test_original_owned_cleanup_failure_retains_unknown_until_same_handle_succeeds(start_scope):
    from promptpilot.process_tree import OwnedProcess
    db,scope=start_scope;store=OneUseStart(db.DB_PATH)
    tree=store.invoke(scope,final_guard=lambda _scope:True,
        factory=lambda:OwnedProcess.start([sys.executable,'-c','import time; time.sleep(60)'],
                                          stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
    original=tree.close;attempts=[]
    def uncertain():
        if not attempts:attempts.append(True);raise OSError('original kernel boundary not confirmed')
        original()
    try:
        store.retain_owned(scope,tree);store.observe_started(scope,tree.process.pid)
        tree.close=uncertain
        with pytest.raises(OSError):store.confirm_stopped(scope,tree)
        assert store.status(scope)['state']=='launched' and tree.process.poll() is None
        store.confirm_stopped(scope,tree)
        assert store.status(scope)['state']=='stopped'
    finally:
        original();tree.process.wait(timeout=10)
