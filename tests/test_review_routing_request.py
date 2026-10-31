"""Closed signed request scope; schema/HMAC never substitute public gates."""
import copy
import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_policy as policy
from promptpilot import review_routing_request as requests
from promptpilot.pipeline_errors import PipelineError
from test_review_routing_policy import peers
from test_review_routing_baseline import snapshot


@pytest.fixture
def context(tmp_path,monkeypatch):
    monkeypatch.setenv('PP_PIPELINE_REPLICAS','1')
    monkeypatch.setenv('PP_DATA_DIR',str(tmp_path/'private-data'))
    monkeypatch.delenv('PP_PIPELINE_LEASE_KEY_FILE',raising=False)
    for name in ['PP_TASK_ID','PP_TASK_STARTED_AT','PP_PROVIDER_OWNERSHIP_KIND']:
        monkeypatch.delenv(name,raising=False)
    _,config,health=peers(tmp_path)
    config.update(repository='owner/repo',base_branch='main',trusted_account='owner',review_lease_seconds=7200)
    return config,snapshot(),policy.preflight(config,health)


def make(context,**overrides):
    config,state,eligibility=context
    return requests.build(config,state,eligibility,depth=2,budget_used=0,now=100000,**overrides)


def test_request_freezes_full_scope_and_roundtrips_only_with_valid_signature(context):
    config,state,eligibility=context
    request=make(context)
    before=copy.deepcopy(request)
    token=requests.encode(request)
    assert requests.decode(token)==request
    changed=copy.deepcopy(request);changed['owner_uuid']='00000000-0000-4000-8000-000000000001'
    forged=pp.encode_lease(changed)+'.'+token.split('.')[1]
    with pytest.raises(PipelineError,match='signature'):requests.decode(forged)
    state['author']['login']='changed';eligibility['consumer_sha256']['fix']='f'*64
    assert request==before
    requests.require_current(request,config,now=100001)


@pytest.mark.parametrize('field,value',[
    ('version',True),('purpose',[]),('purpose','legacy-review'),('stage','fix'),
    ('number',True),('head','b'*40),('identity_sha256','b'*64),
    ('pr_key',{'base_repository_id':'R_OTHER','pr_id':'PR_NODE_42','pr_number':42}),
    ('owner_uuid','00000000-0000-4000-8000-000000000001-extra'),
    ('nonce','bad'),('issued_at',True),('expires_at',100000),
    ('current_limit',0),('budget_used',17),('pipeline_replicas',2),('recovery',{}),
])
def test_malformed_or_contradictory_signed_scope_refuses_before_use(context,field,value):
    request=make(context);request[field]=value
    with pytest.raises(PipelineError):requests.validate(request)


def test_unknown_fields_and_partial_reader_contract_are_not_new_privileges(context):
    for edit in [lambda r:r.update(skip_permissions=True),
                 lambda r:r['reader_sha256'].pop('tail'),
                 lambda r:r['audit'].update(allow_future=True),
                 lambda r:r['author'].update(is_admin=True)]:
        request=make(context);edit(request)
        with pytest.raises(PipelineError):requests.validate(request)


@pytest.mark.parametrize('token',[None,'','a.'+'x'*65536,'é.'+'x'*43,'a=.'+'x'*43],
                         ids=['missing','empty','oversized','nonascii','padded'])
def test_malformed_tokens_refuse_before_even_reading_a_private_key(token,monkeypatch):
    def forbidden(**kwargs):pytest.fail('malformed token must not read the signing key')
    monkeypatch.setattr(pp,'pipeline_lease_key',forbidden)
    with pytest.raises(PipelineError):requests.decode(token)


@pytest.mark.parametrize('variant',['whitespace','duplicate-key'])
def test_even_correct_hmac_does_not_admit_noncanonical_request_json(context,variant):
    request=make(context)
    raw=json.dumps(request,ensure_ascii=False,sort_keys=True,indent=2)
    if variant=='duplicate-key':raw='{"version":1,'+raw[1:]
    payload=base64.urlsafe_b64encode(raw.encode('utf-8')).decode('ascii').rstrip('=')
    signature=hmac.new(pp.pipeline_lease_key(create=True),payload.encode('ascii'),hashlib.sha256).digest()
    token=payload+'.'+base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')
    with pytest.raises(PipelineError,match='noncanonical'):requests.decode(token)


def test_expiry_after_slow_work_prevents_write_but_known_receipt_can_be_read(context):
    request=make(context);config=context[0]
    requests.require_current(request,config,now=request['expires_at']-1)
    with pytest.raises(PipelineError,match='expired'):
        requests.require_current(request,config,now=request['expires_at'])
    requests.require_current(request,config,now=request['expires_at']+1,write=False)
    with pytest.raises(PipelineError,match='not yet valid'):
        requests.require_current(request,config,now=request['issued_at']-61,write=False)


def test_final_clock_is_read_after_configuration_digest_work(context,monkeypatch):
    request=make(context);config=context[0]
    clock={'now':100001};original=pp.digest
    def delayed_digest(value):
        result=original(value);clock['now']=request['expires_at']
        return result
    monkeypatch.setattr(pp,'digest',delayed_digest)
    monkeypatch.setattr(requests.time,'time',lambda:clock['now'])
    with pytest.raises(PipelineError,match='expired'):
        requests.require_current(request,config)


def test_configuration_or_replica_change_cannot_borrow_old_scope(context,monkeypatch):
    request=make(context);config=copy.deepcopy(context[0])
    config['technical_handoff']['max_extra_attempts']=1
    with pytest.raises(PipelineError,match='configuration changed'):
        requests.require_current(request,config,now=100001)
    monkeypatch.setenv('PP_PIPELINE_REPLICAS','2')
    with pytest.raises(PipelineError,match='replica count'):
        requests.require_current(request,context[0],now=100001)


def test_scoped_task_request_cannot_be_borrowed_by_another_task_or_retry(context,monkeypatch):
    reservation={'repository':'owner/repo','stage':'review','number':42,'head':'a'*40,
        'task_id':7,'task_started_at':'1970-01-02T03:46:39+00:00','ownership_kind':'headless',
        'herdr_session_state':'','herdr_pane_id':None,'herdr_tab_id':None,'herdr_workspace_id':None,
        'token':'c'*32,'reserved_at':'1970-01-02T03:46:39+00:00','expires_at':'1970-01-02T05:46:40+00:00'}
    monkeypatch.setenv('PP_TASK_ID','7')
    monkeypatch.setenv('PP_TASK_STARTED_AT',reservation['task_started_at'])
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND','headless')
    request=make(context,reservation=reservation)
    requests.require_current(request,context[0],now=100001)
    monkeypatch.setenv('PP_TASK_ID','8')
    with pytest.raises(PipelineError,match='another task'):
        requests.require_current(request,context[0],now=100001)
    monkeypatch.setenv('PP_TASK_ID','7')
    monkeypatch.setenv('PP_TASK_STARTED_AT','1970-01-02T03:46:40+00:00')
    with pytest.raises(PipelineError,match='another task attempt'):
        requests.require_current(request,context[0],now=100001)


def test_pipeline_task_cannot_downgrade_itself_to_manual_unreserved_scope(context,monkeypatch):
    manual=make(context)
    monkeypatch.setenv('PP_TASK_ID','7')
    with pytest.raises(PipelineError,match='exact task reservation'):make(context)
    with pytest.raises(PipelineError,match='unreserved manual'):
        requests.require_current(manual,context[0],now=100001)


def test_recovery_gets_new_service_uuid_and_preserves_canonical_audit_binding(context):
    original=make(context)
    recovery={'publication_id':'9007199254740993','context_sha256':'d'*64,'audit':original['audit']}
    resumed=make(context,recovery=recovery)
    assert resumed['purpose']==requests.RECOVERY
    assert resumed['owner_uuid']!=original['owner_uuid'] and resumed['nonce']!=original['nonce']
    assert resumed['audit']==original['audit'] and resumed['recovery']['publication_id']=='9007199254740993'
    recovery['owner_uuid']=original['owner_uuid']
    with pytest.raises(PipelineError):make(context,recovery=recovery)


def test_readonly_schema_work_does_not_create_key_or_enable_disabled_policy(context,monkeypatch,tmp_path):
    data=tmp_path/'schema-only';monkeypatch.setenv('PP_DATA_DIR',str(data))
    request=make(context)
    requests.validate(request)
    assert not (data/'pipeline-lease.key').exists()
    disabled=copy.deepcopy(context[2]);disabled['enabled']=False
    with pytest.raises(PipelineError,match='paired eligibility'):
        requests.build(context[0],context[1],disabled,depth=2,budget_used=0,now=100000)
    assert not list(Path(data).glob('*'))
