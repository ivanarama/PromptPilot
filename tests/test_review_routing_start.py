"""Private canonical START guard with actual scheduler and owned process.

The trusted peer records are fixture inputs, not public issuer or paired checker
acceptance. This seam is deliberately not advertised by the installed worker.
"""
import json
import subprocess
import sys
from uuid import uuid4

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import db
from promptpilot import review_routing_pipeline_policy as policy
from promptpilot import review_routing_start_request as requests
from promptpilot import review_routing_start as start
from promptpilot.pipeline_errors import PipelineError
from promptpilot.process_tree import OwnedProcess
from promptpilot.review_routing_execution import OneUseStart
from promptpilot.review_routing_policy import preflight
from promptpilot.review_routing_snapshot import stable_timeline
from promptpilot.review_routing_wire import context,render
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as author_bridge,ledger  # noqa: F401


def metadata_launch(gh,config_path,token,*,factory):
    """Test-only composition probe; the real worker must use the workspace gate."""
    guard=start.StartGuard(gh,config_path,token)
    guard.state()
    store=OneUseStart(db.DB_PATH,create=False,allow_new=False)
    return store.invoke(guard.scope,final_guard=lambda scope:guard.final(store,scope),factory=factory)


@pytest.fixture
def pipeline_start(author_bridge):
    peer,health,path,task,_run,database=author_bridge
    config=pp.load_config(str(path))
    snapshot=stable_timeline(peer,config,42)
    state=ledger(peer,path);proof=state.proofs[-1]
    seed=dict(protocol='review-routing-v1',kind='attempt',pr=snapshot['pr_key'],owner=str(uuid4()),
        context_sha256=context(proof.publication.payload),source=proof.source,
        series=proof.completion.id,previous=None,attempt=1,executor=dict(kind='pipeline',login='owner'))
    def post(payload):
        return str(peer.json('api','repos/owner/repo/issues/42/comments','--method','POST','--input','-',
                            input_value={'body':render(payload)})['id'])
    store=OneUseStart(database.DB_PATH)
    root_id=post(seed)
    occurrence=dict(task_id=task.id,attempt_uuid=str(uuid4()))
    for phase in ['assigned','handoff-done','started']:
        post(dict(protocol='review-routing-v1',kind='phase',pr=seed['pr'],owner=seed['owner'],
            context_sha256=context(seed),attempt_id=root_id,lease_id=root_id,phase=phase,
            details=occurrence if phase=='started' else {}))
    raw=json.loads(path.read_text(encoding='utf-8'))
    raw['technical_fix_execution']=dict(enabled=True,protocol='review-routing-v1',executor=seed['executor'])
    path.write_text(json.dumps(raw),encoding='utf-8')
    config=pp.load_config(str(path))
    reservation=database.reserve_pipeline_target('owner/repo','fix-pr',42,'a'*40,task.id,300,
        task_started_at=task.started_at.isoformat(),ownership_kind='headless',strict_lifetime=True)
    state=ledger(peer,path);root=state.attempts[0];started=state.attempt_phases(root)['started']
    health['fix_execution_contract']=policy.CONTRACT.copy()
    health['technical_fix_start_candidates']=[dict(stage='fix-pr',number=42,head='a'*40,source=proof.source,
        attempt_id=root_id,started_id=started.id,**occurrence)]
    value=requests.build(config,snapshot,preflight(config,health),root,started,reservation)
    store.prepare(value['scope'])
    return peer,health,path,task,database,value,requests.encode(value)


def test_canonical_start_seam_launches_one_actual_owned_child_and_never_relaunches(pipeline_start):
    peer,_health,path,_task,database,value,token=pipeline_start
    calls=[]
    def factory():
        calls.append(True)
        return OwnedProcess.start([sys.executable,'-c','print("owned-start")'],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    tree=metadata_launch(peer,str(path),token,factory=factory)
    try:
        store=OneUseStart(database.DB_PATH)
        store.observe_started(value['scope'],tree.process.pid)
        output,_error=tree.process.communicate(timeout=10)
        assert b'owned-start' in output
        assert store.status(value['scope'])['state']=='launched'
        with pytest.raises(PipelineError):metadata_launch(peer,str(path),token,factory=factory)
        assert len(calls)==1
    finally:tree.close()


@pytest.mark.parametrize('fault',['cancel','actor','reader','strict-lifetime','checker-started','checker-task'])
def test_late_change_after_consumption_stops_before_any_factory(pipeline_start,monkeypatch,fault):
    peer,health,path,task,database,value,token=pipeline_start
    consume=OneUseStart._consume
    def changed(store,scope):
        generation=consume(store,scope)
        if fault=='cancel':database.request_cancel(task.id)
        elif fault=='actor':peer.actor='wrong'
        elif fault=='checker-started':health['technical_fix_start_candidates'][0]['started_id']='9999'
        elif fault=='checker-task':health['technical_fix_start_candidates'][0]['task_id']=task.id+1
        elif fault=='strict-lifetime':
            from contextlib import closing
            import sqlite3
            with closing(sqlite3.connect(database.DB_PATH)) as connection:
                with connection:connection.execute('UPDATE pipeline_target_reservations SET strict_lifetime=0')
        else:(path.parent/'.claude/skills/fix-approved/SKILL.md').write_text('changed',encoding='utf-8')
        return generation
    monkeypatch.setattr(OneUseStart,'_consume',changed)
    calls=[]
    with pytest.raises(PipelineError):metadata_launch(peer,str(path),token,factory=lambda:calls.append(True))
    assert not calls
    assert OneUseStart(database.DB_PATH).status(value['scope'])['state']=='not-started'


def test_ambiguous_factory_failure_remains_unknown_without_spawn_retry(pipeline_start):
    peer,_health,path,_task,database,value,token=pipeline_start
    calls=[]
    def failed():calls.append(True);raise OSError('unknown factory outcome')
    with pytest.raises(OSError):metadata_launch(peer,str(path),token,factory=failed)
    assert OneUseStart(database.DB_PATH).status(value['scope'])['state']=='unknown'
    with pytest.raises(PipelineError):metadata_launch(peer,str(path),token,factory=failed)
    assert len(calls)==1


def test_existing_server_started_cannot_reconstruct_lost_local_issuance(pipeline_start):
    from contextlib import closing
    import sqlite3
    from promptpilot.review_routing_execution import TABLE
    peer,_health,path,_task,database,_value,token=pipeline_start
    with closing(sqlite3.connect(database.DB_PATH)) as connection:
        with connection:connection.execute('DELETE FROM '+TABLE)
    calls=[]
    with pytest.raises(PipelineError,match='no prepared local issuance'):
        metadata_launch(peer,str(path),token,factory=lambda:calls.append(True))
    assert not calls
