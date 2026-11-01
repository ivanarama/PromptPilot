"""A signed progress scope cannot downgrade into the old administrative route."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_fix_request as admission
from promptpilot import review_routing_progress_request as progress
from promptpilot.pipeline_errors import PipelineError
from test_review_routing_proof import history


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setenv('PP_DATA_DIR', str(tmp_path/'private'))
    monkeypatch.setenv('PP_PIPELINE_REPLICAS', '1')
    monkeypatch.delenv('PP_PIPELINE_LEASE_KEY_FILE', raising=False)
    root=history()[3]
    identity=dict(history()[0].payload['identity'], head_sha='b'*40)
    snapshot=dict(identity=identity, author=history()[0].payload['author'])
    config=dict(repository='owner/repo', trusted_account='owner', review_lease_seconds=7200)
    policy=dict(current_limit=2,consumer_sha256={role:'d'*64 for role in ['review','fix','merge','tail']})
    now=datetime.now(timezone.utc)
    reservation=dict(repository='owner/repo',stage='fix-pr',number=42,head='a'*40,task_id=1,
        task_started_at=now.isoformat(),ownership_kind='headless',
        herdr_session_state=None,herdr_pane_id=None,herdr_tab_id=None,herdr_workspace_id=None,
        token='1'*32,reserved_at=now.isoformat(),expires_at=(now+timedelta(hours=2)).isoformat())
    return config, progress.build(config,snapshot,policy,root,reservation)


def test_progress_freezes_new_head_and_roundtrips_without_v1_admission(scope):
    _config,value=scope
    frozen=copy.deepcopy(value)
    token=progress.encode(value)
    assert progress.decode(token)==frozen and value['new_head']=='b'*40
    with pytest.raises(PipelineError):admission.decode(token)
    with pytest.raises(PipelineError):
        from promptpilot.review_routing_fix import FixGuard
        FixGuard(object(),'private-config.json',token)


@pytest.mark.parametrize('field,value',[
    ('version',2),('version',True),('progress_version',True),('progress_version',2),
    ('purpose',admission.ADMISSION),('purpose',admission.START),
    ('new_head','a'*40),('new_head','bad'),('attempt_id',None),('head','c'*40),
])
def test_wrong_purpose_or_source_cannot_acquire_progress_scope(scope,field,value):
    _config,request=scope
    request[field]=value
    with pytest.raises(PipelineError):progress.decode(pp.encode_signed_lease(request))


def test_extra_fields_and_config_drift_are_not_progress_authority(scope):
    config,value=scope
    value['launch_provider']=True
    with pytest.raises(PipelineError):progress.validate(value)
    value.pop('launch_provider')
    config['trusted_account']='changed'
    with pytest.raises(PipelineError):progress.require_current(value,config,write=False)
