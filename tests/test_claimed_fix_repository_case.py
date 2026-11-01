"""Public signed FIX admission preserves GitHub's canonical name casing."""
from datetime import timezone
import copy
import json

import pytest

from promptpilot import project_pipeline as pp
from promptpilot import review_routing_fix_request as requests
from promptpilot import review_routing_pipeline_policy as policy
from promptpilot.models import TaskCreate
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401


@pytest.mark.parametrize('executor', ['author', 'pipeline'])
def test_public_fix_accepts_canonical_mixed_case_but_rejects_another_repository(
        adapter, isolated_db, monkeypatch, executor):
    peer, health, path, report, run = adapter
    repository = 'owner/MixedCase'
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['repository'] = repository
    if executor == 'pipeline':
        raw['technical_fix_execution'] = dict(
            enabled=True, protocol='review-routing-v1',
            executor=dict(kind='pipeline', login='owner'))
    path.write_text(json.dumps(raw), encoding='utf-8')
    state = peer.current['data']['repository']
    state['nameWithOwner'] = repository
    pr = state['pullRequest']
    for key in ('baseRepository', 'headRepository'):
        pr[key]['nameWithOwner'] = repository
    original = peer.json

    def canonical_peer(*args, input_value=None):
        if args == ('api', 'repos/' + repository):
            return dict(full_name=repository, node_id='R_BASE')
        if args == ('api', 'repos/' + repository + '/pulls/42'):
            return dict(node_id=pr['id'], number=42,
                        base=dict(ref=pr['baseRefName'], repo=dict(
                            node_id=pr['baseRepository']['id'], full_name=repository)),
                        head=dict(ref=pr['headRefName'], sha=pr['headRefOid'], repo=dict(
                            node_id=pr['headRepository']['id'], full_name=repository)),
                        assignees=[])
        # Transport aliases only: actual GraphQL metadata and every generated
        # signed/publication identity retain the canonical mixed-case name.
        mapped = tuple(arg.replace('repos/' + repository, 'repos/owner/repo')
                       if isinstance(arg, str) else arg for arg in args)
        return original(*mapped, input_value=input_value)

    peer.json = canonical_peer
    code, audit = run('next', 'review')
    assert code == 0 and audit['action'] == 'audit', audit
    code, completed = run('complete', 'review', '--lease', audit['lease'],
                          '--report', str(report))
    assert code == 0 and completed['outcome'] == 'technical-handoff', completed
    health['fix_bridge_contract'] = copy.deepcopy(requests.BRIDGE)
    health['fix_execution_contract'] = copy.deepcopy(policy.CONTRACT)
    health['technical_fix_candidates'] = [dict(
        stage='fix-pr', number=42, head=completed['source']['head'],
        source=completed['source'])]
    task = isolated_db.create_task(TaskCreate(
        prompt='Canonical-name FIX', working_dir=str(path.parent)))
    task = isolated_db.get_next_runnable()
    monkeypatch.setenv('PP_TASK_ID', str(task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv('PP_PROVIDER_OWNERSHIP_KIND', 'headless')
    monkeypatch.setenv('PP_PIPELINE_REPLICAS', '1')
    monkeypatch.setenv('PP_PIPELINE_LEASE_KEY_FILE', str(path.parent / 'case-lease.key'))
    before = list(peer.posts)
    code, selected = run('next', 'fix')
    assert code == 0 and selected['action'] == (
        'pipeline-fix' if executor == 'pipeline' else 'author-handoff'), selected
    value = pp.decode_signed_lease(selected['lease'])
    assert value['repository'] == value['identity']['base_repository'] == repository
    assert value['identity']['head_repository'] == repository
    assert value['target_reservation']['repository'] == repository.lower()
    assert value['target_reservation']['ownership_kind'] == 'headless'
    assert peer.posts == before  # Admission is not a published/spent attempt.
    value['repository'] = 'other/MixedCase'
    token = pp.encode_signed_lease(value)
    command = ('prepare-fix-start', 'fix') if executor == 'pipeline' else ('complete', 'fix')
    code, rejected = run(*command, '--lease', token)
    assert code == 2 and rejected['action'] == 'error', rejected
    assert peer.posts == before
