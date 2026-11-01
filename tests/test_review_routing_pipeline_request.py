"""Pipeline admission cannot acquire an author's or a START scope."""
import copy

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_pipeline_request as pipeline
from promptpilot import review_routing_fix_request as author
from promptpilot import review_routing_progress_request as progress
from promptpilot import review_routing_start_request as start
from promptpilot.pipeline_errors import PipelineError
from test_review_routing_start_request import scope as start_scope  # noqa: F401


@pytest.fixture
def scope(request):
    config,prepared=request.getfixturevalue('start_scope')
    snapshot=dict(identity=prepared['identity'],author=prepared['author'])
    paired=dict(consumer_sha256=prepared['scope']['reader_sha256'],current_limit=2)
    value=pipeline.build(config,snapshot,paired,prepared['attempt'],prepared['scope']['reservation'])
    return config,value


def test_closed_admission_roundtrip_rejects_all_other_decoders(scope):
    config,value=scope
    token=pipeline.encode(value)
    assert pipeline.decode(token)==value
    pipeline.require_current(value,config,write=False)
    for decoder in [author.decode,progress.decode,start.decode]:
        with pytest.raises(PipelineError):decoder(token)


@pytest.mark.parametrize('fault',['purpose','author-executor','source','identity','attempt-id','extra','version'])
def test_signed_changes_cannot_substitute_a_pipeline_admission(scope,fault):
    _config,value=scope
    if fault=='purpose':value['purpose']=author.ADMISSION
    elif fault=='author-executor':value['attempt']['executor']=dict(kind='author',login=value['author']['login'])
    elif fault=='source':value['attempt']['source']['head']='d'*40
    elif fault=='identity':value['identity']['head_ref']='changed'
    elif fault=='attempt-id':value['attempt_id']='2000'
    elif fault=='extra':value['started_id']='2000'
    else:value['version']=True
    with pytest.raises(PipelineError):pipeline.decode(pp.encode_signed_lease(value))


def test_explicit_executor_drift_does_not_grant_pipeline_admission(scope):
    config,value=scope
    changed=copy.deepcopy(config)
    changed['technical_fix_execution']['executor']['login']='another'
    with pytest.raises(PipelineError):pipeline.require_current(value,changed,write=False)
