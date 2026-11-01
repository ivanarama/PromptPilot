"""Closed signed START serialization; no public source authority is simulated."""
import copy
from datetime import datetime,timedelta,timezone

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_start_request as start
from promptpilot import review_routing_fix_request as admission
from promptpilot import review_routing_progress_request as progress
from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing_wire import Record,context
from test_review_routing_proof import history


@pytest.fixture
def scope(tmp_path,monkeypatch):
    monkeypatch.setenv('PP_DATA_DIR',str(tmp_path/'private'))
    monkeypatch.setenv('PP_PIPELINE_REPLICAS','1')
    monkeypatch.delenv('PP_PIPELINE_LEASE_KEY_FILE',raising=False)
    records=history()
    seed=copy.deepcopy(records[3].payload)
    seed['executor']={'kind':'pipeline','login':'owner'}
    root=Record(records[3].id,records[3].created_at,seed)
    started=Record('9000',root.created_at,dict(protocol='review-routing-v1',kind='phase',
        pr=seed['pr'],owner=seed['owner'],context_sha256=context(seed),attempt_id=root.id,
        lease_id=root.id,phase='started',details=dict(task_id=1,
        attempt_uuid='00000000-0000-4000-8000-000000000002')))
    config=dict(repository='owner/repo',trusted_account='owner',target_reservation_ttl_seconds=300,
        technical_fix_execution=dict(enabled=True,protocol='review-routing-v1',executor=seed['executor']))
    paired=dict(consumer_sha256={role:'d'*64 for role in ['review','fix','merge','tail']})
    now=datetime.now(timezone.utc)
    reservation=dict(repository='owner/repo',stage='fix-pr',number=42,head='a'*40,task_id=1,
        task_started_at=now.isoformat(),ownership_kind='headless',herdr_session_state=None,
        herdr_pane_id=None,herdr_tab_id=None,herdr_workspace_id=None,token='1'*32,
        reserved_at=now.isoformat(),expires_at=(now+timedelta(minutes=5)).isoformat())
    snapshot=dict(identity=records[0].payload['identity'],author=records[0].payload['author'])
    return config,start.build(config,snapshot,paired,root,started,reservation)


def test_exact_start_scope_roundtrip_cannot_downgrade_to_author_or_progress(scope):
    config,value=scope
    token=start.encode(value)
    assert start.decode(token)==value
    start.require_current(value,config,write=False)
    for decoder in [admission.decode,progress.decode]:
        with pytest.raises(PipelineError):decoder(token)


@pytest.mark.parametrize('fault',['purpose','replicas','extra','uuid','task','owner','executor','source','started','identity'])
def test_signed_changes_cannot_rebind_a_pipeline_start(scope,fault):
    _config,value=scope
    if fault=='purpose':value['purpose']=admission.ADMISSION
    elif fault=='replicas':value['pipeline_replicas']=True
    elif fault=='extra':value['launch_anything']=True
    elif fault=='uuid':value['scope']['attempt_uuid']='not-a-uuid'
    elif fault=='task':value['scope']['task_id']=2
    elif fault=='owner':value['scope']['owner']='00000000-0000-4000-8000-000000000003'
    elif fault=='executor':value['scope']['executor']['kind']='author'
    elif fault=='source':value['scope']['source']['head']='c'*40
    elif fault=='started':value['scope']['started_id']=value['scope']['attempt_id']
    else:value['identity']['head_ref']='replacement'
    with pytest.raises(PipelineError):start.decode(pp.encode_signed_lease(value))


@pytest.mark.parametrize('fault',['off','removed','executor','fork','replicas'])
def test_effective_execution_policy_or_replica_drift_refuses_start(scope,monkeypatch,fault):
    config,value=scope
    if fault=='off':config['technical_fix_execution']['enabled']=False
    elif fault=='removed':config.pop('technical_fix_execution')
    elif fault=='executor':config['technical_fix_execution']['executor']['login']='other'
    elif fault=='fork':
        value['identity'].update(head_repository='fork/repo',head_repository_id='R_FORK',cross_repository=True)
    else:monkeypatch.setenv('PP_PIPELINE_REPLICAS','2')
    with pytest.raises(PipelineError):start.require_current(value,config,write=False)
