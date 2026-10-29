"""One future flag must not enable a half-upgraded four-stage protocol."""
import copy

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot import review_routing_policy as policy


def config():
    return {'technical_handoff':{'enabled':True,'protocol':'review-routing-v1','max_extra_attempts':2},
            'review_completion_gate':'target-v1','fallback_handoff':'target-v1'}


def peers(tmp_path):
    root=tmp_path/'.claude/skills'
    for role,stage in policy.ROLES.items():
        path=root/stage/'SKILL.md';path.parent.mkdir(parents=True)
        path.write_text(f'# {stage}\n<!-- pp:review-routing-v1-consumer stage={role} -->\n',encoding='utf-8')
    c=config();c['health_command']=['checker','-contract',str(root/'review-queue/SKILL.md'),'-json']
    h={'state':'green','findings':[],
       'review_routing_contract':{'protocol':'review-routing-v1','snapshot':'complete',
                                 'reader_sha256':{role:policy.consumer_tree(root,role) for role in policy.ROLES}}}
    return root,c,h


def test_absent_or_off_flag_leaves_legacy_config_exact_and_never_reads_consumers(monkeypatch):
    def forbidden(*args):pytest.fail('disabled protocol must not open consumer files')
    monkeypatch.setattr(policy,'consumer_tree',forbidden)
    for value in [{},dict(config(),technical_handoff={'enabled':False,'protocol':'review-routing-v1','max_extra_attempts':2})]:
        before=copy.deepcopy(value)
        assert policy.preflight(value,None)=={'enabled':False,'mutation_authorized':False}
        assert value==before


@pytest.mark.parametrize('key,value',[('enabled',1),('protocol','review-routing-v2'),('max_extra_attempts',True),('max_extra_attempts',0),('max_extra_attempts',17)])
def test_invalid_configuration_is_not_a_default_or_partial_enable(key,value):
    c=config();c['technical_handoff'][key]=value
    with pytest.raises(PipelineError):policy.settings(c)


def test_complete_paired_reader_set_is_read_only_eligibility(tmp_path):
    _,c,h=peers(tmp_path)
    result=policy.preflight(c,h)
    assert result['enabled'] and result['current_limit']==2
    assert result['mutation_authorized'] is False


def test_edit_of_referenced_legacy_reader_invalidates_whole_consumer_tree(tmp_path):
    root,c,h=peers(tmp_path)
    path=root/'review-queue/references/legacy-protocol.md'
    path.parent.mkdir();path.write_text('new incompatible reader',encoding='utf-8')
    with pytest.raises(PipelineError,match='tree hashes differ'):
        policy.preflight(c,h)


@pytest.mark.parametrize('role',['review','fix','merge','tail'])
def test_missing_role_or_only_config_version_cannot_enable_protocol(tmp_path,role):
    root,c,h=peers(tmp_path)
    entry=root/policy.ROLES[role]/'SKILL.md'
    entry.write_text('old consumer without version marker',encoding='utf-8')
    with pytest.raises(PipelineError,match='version marker'):
        policy.preflight(c,h)


def test_partial_or_red_peer_and_changed_legacy_gate_refuse(tmp_path):
    _,c,h=peers(tmp_path)
    bad=copy.deepcopy(h);bad['review_routing_contract']['snapshot']='partial'
    red=copy.deepcopy(h);red['state']='red'
    for snapshot in [bad,red]:
        with pytest.raises(PipelineError):policy.preflight(c,snapshot)
    c['fallback_handoff']='legacy'
    with pytest.raises(PipelineError,match='both target-v1'):
        policy.preflight(c,h)
