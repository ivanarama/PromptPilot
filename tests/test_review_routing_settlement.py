"""Explicit settlement is engineering parking, never success or a refund.

The public reader test supplies trusted server evidence from a fake issuer.
There is no public settlement writer or automatic inference from process exit.
"""
import copy
from datetime import timezone
import json
from pathlib import Path

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot import review_routing_wire as wire
from promptpilot.review_routing_adapter import _admit_audit
from promptpilot.review_routing_proof import reduce
from test_review_routing_proof import history, next_review, resume, changed
from test_review_routing_fix import author_bridge as author_bridge, ledger  # noqa: F401
from test_review_routing_adapter import adapter as adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard as setup_guard  # noqa: F401


def settlement(records, **changes):
    root = records[3]
    phases = {entry.payload['phase']: entry for entry in records if entry.payload['kind'] == 'phase'}
    data = {'protocol': 'review-routing-v1', 'kind': 'settlement', 'pr': root.payload['pr'],
            'owner': root.payload['owner'], 'context_sha256': wire.context(root.payload),
            'attempt_id': root.id, 'lease_id': root.id, 'source': root.payload['source'],
            'barrier_id': phases['barrier'].id if 'barrier' in phases else None,
            'started_id': phases['started'].id if 'started' in phases else None,
            'observed_head': 'a'*40, 'reason': 'verified old executor stopped; engineering recovery required',
            'executor_outcome': 'author-unassigned', 'evidence_sha256': 'e'*64}
    data.update(changes)
    return wire.Record('1080', '2026-10-29T00:00:50Z', data)


def test_duplicate_settlement_still_requires_its_actual_lease_owner():
    records=history()[:6]
    receipt=settlement(records)
    expired=wire.Record('1090','2026-10-29T01:00:00Z',receipt.payload)
    with pytest.raises(PipelineError):
        reduce(records+[receipt,expired])


def test_explicit_settlement_parks_the_spent_slot_and_requires_exact_new_head_resume():
    records = history()[:6]
    root = records[3]
    barrier = wire.Record('1070', '2026-10-29T00:00:40Z',
        {'protocol': 'review-routing-v1', 'kind': 'phase', 'pr': root.payload['pr'],
         'owner': root.payload['owner'], 'context_sha256': wire.context(root.payload),
         'attempt_id': root.id, 'lease_id': root.id, 'phase': 'barrier', 'details': {'reason': 'assignment uncertain'}})
    records.append(barrier)
    settled = settlement(records)
    records.append(settled)
    state = reduce(records)
    assert state.budget_used == 1 and state.parking_id == settled.id
    assert state.attempt_settlement(root) == settled
    assert 'fix-done' not in state.attempt_phases(root)
    assert wire.parse(wire.render(settled.payload)) == settled.payload
    with pytest.raises(PipelineError, match='exact current HEAD resume'):
        _admit_audit(state, 'b'*40)
    with pytest.raises(PipelineError):
        reduce(next_review(records, head='b'*40, budget=1))
    grant = resume(settled.id, root.id, head='b'*40)
    admitted = reduce(records+[grant])
    assert _admit_audit(admitted, 'b'*40) == grant.id
    pending = next_review(records+[grant], head='b'*40, budget=1, resume_id=grant.id)[:-1]
    assert reduce(pending).resuming_publication_id == '1100'
    completed = next_review(records+[grant], head='b'*40, budget=1, resume_id=grant.id)
    final = reduce(completed)
    assert final.parking_id is None and final.budget_used == 1
    assert final.next_attempt_admission(final.proofs[-1].source, max_attempts=1)['eligible'] is False
    assert final.next_attempt_admission(final.proofs[-1].source, max_attempts=2)['eligible'] is True
    with pytest.raises(PipelineError, match='current parking/lifetime'):
        reduce(completed+[wire.Record('1199', '2026-10-29T00:03:00Z', grant.payload)])


@pytest.mark.parametrize('change', [
    {'attempt_id': '999'}, {'context_sha256': '0'*64}, {'barrier_id': '1004'}, {'started_id': '1006'},
    {'source': dict(history()[3].payload['source'], report_sha256='0'*64)},
    {'executor_outcome': 'not-started'}, {'lease_id': '999'},
])
def test_settlement_cannot_borrow_another_attempt_source_owner_or_outcome(change):
    records = history()[:6]
    with pytest.raises(PipelineError):
        reduce(records+[settlement(records, **change)])


def test_success_and_settlement_are_mutually_exclusive_and_same_head_resume_is_refused():
    successful = history()[:8]
    with pytest.raises(PipelineError, match='cannot replace success'):
        reduce(successful+[settlement(successful)])
    records = history()[:6]
    settled = settlement(records)
    with pytest.raises(PipelineError, match='new HEAD'):
        reduce(records+[settled, resume(settled.id, records[3].id, head='a'*40)])
    with pytest.raises(PipelineError, match='current parking/lifetime'):
        reduce(records+[settled, resume('1002', records[3].id, head='b'*40)])


def test_pipeline_started_settlement_requires_confirmed_stopped_outcome_and_exact_start():
    records = history()[:7]
    records[3] = changed(records[3], executor={'kind': 'pipeline', 'login': 'owner'})
    for index in range(4, len(records)):
        records[index] = changed(records[index], context_sha256=wire.context(records[3].payload))
    with pytest.raises(PipelineError, match='exact old executor'):
        reduce(records+[settlement(records)])
    state = reduce(records+[settlement(records, executor_outcome='pipeline-stopped')])
    assert state.budget_used == 1 and state.parking_id == '1080'
    with pytest.raises(PipelineError, match='barrier/started'):
        reduce(records+[settlement(records, executor_outcome='pipeline-stopped', started_id=None)])


def test_unstarted_pipeline_and_later_phase_are_not_inferred_from_lease_expiry():
    records = history()[:4]
    records[3] = changed(records[3], executor={'kind': 'pipeline', 'login': 'owner'})
    assert reduce(records).parking_id is None
    state = reduce(records+[settlement(records, executor_outcome='not-started')])
    assert state.parking_id == '1080'
    phase = wire.Record('1081', '2026-10-29T00:00:51Z',
        {'protocol': 'review-routing-v1', 'kind': 'phase', 'pr': records[3].payload['pr'],
         'owner': records[3].payload['owner'], 'context_sha256': wire.context(records[3].payload),
         'attempt_id': records[3].id, 'lease_id': records[3].id, 'phase': 'assigned', 'details': {}})
    with pytest.raises(PipelineError, match='later attempt phase'):
        reduce(records+[settlement(records, executor_outcome='not-started'), phase])


def test_public_reader_resumes_one_review_after_trusted_failure_settlement(author_bridge, tmp_path, monkeypatch):
    from promptpilot.models import TaskCreate
    peer, health, path, task, run, database = author_bridge
    peer.assignment_fault = 'ignored'
    code, selected = run('next', 'fix')
    assert code == 0
    code, failed = run('complete', 'fix', '--lease', selected['lease'])
    assert code == 2, failed
    state = ledger(peer, path)
    root = state.attempts[0]
    settled = {'protocol': 'review-routing-v1', 'kind': 'settlement', 'pr': root.payload['pr'],
               'owner': root.payload['owner'], 'context_sha256': wire.context(root.payload), 'attempt_id': root.id,
               'lease_id': root.id, 'source': root.payload['source'], 'barrier_id': None, 'started_id': None,
               'observed_head': 'a'*40, 'reason': 'trusted issuer verified author remains unassigned',
               'executor_outcome': 'author-unassigned', 'evidence_sha256': 'e'*64}
    response = peer.json('api', 'repos/owner/repo/issues/42/comments', '--method', 'POST', '--input', '-',
                         input_value={'body': wire.render(settled)})
    parking_id = response['id']
    pr = peer.current['data']['repository']['pullRequest']
    pr['headRefOid'] = 'b'*40
    peer.comments.append({'cursor': 'NEW_HEAD', 'node': {'__typename': 'PullRequestCommit', 'id': 'NEW_HEAD',
                                                       'commit': {'oid': 'b'*40}}})
    candidate = {'stage': 'review', 'number': 42, 'head': 'b'*40, 'review_depth': 3}
    health.update(review_candidates=[candidate], content_review_candidates=[candidate])
    database.mark_failed(task.id, 'author assignment failed')
    database.create_task(TaskCreate(prompt='Project - REVIEW', working_dir=str(tmp_path)))
    review_task = database.get_next_runnable()
    assert review_task is not None
    monkeypatch.setenv('PP_TASK_ID', str(review_task.id))
    monkeypatch.setenv('PP_TASK_STARTED_AT', review_task.started_at.astimezone(timezone.utc).isoformat())
    code, refused = run('next', 'review')
    assert code == 2 and 'resume' in refused['error'], refused
    grant = {'protocol': 'review-routing-v1', 'kind': 'resume', 'pr': root.payload['pr'], 'head': 'b'*40,
             'parking_id': parking_id, 'previous': root.id, 'reason': 'engineer authorized one current HEAD audit'}
    peer.json('api', 'repos/owner/repo/issues/42/comments', '--method', 'POST', '--input', '-',
              input_value={'body': wire.render(grant)})
    code, resumed = run('next', 'review')
    assert code == 0 and resumed['action'] == 'audit', resumed
    report = copy.deepcopy(state.proofs[-1].publication.payload['report'])
    report['head'] = 'b'*40
    report_path = tmp_path/'resumed-report.json'
    report_path.write_text(json.dumps(report), encoding='utf-8')
    code, completed = run('complete', 'review', '--lease', resumed['lease'], '--report', str(report_path))
    assert code == 0 and completed['action'] == 'completed', completed
    final = ledger(peer, path)
    assert final.budget_used == 1 and final.parking_id is None and len(final.proofs) == 2
    code, repeated = run('next', 'review')
    assert code == 0 and repeated['action'] == 'wait', repeated


def test_settlement_record_has_closed_schema_and_requires_explicit_evidence():
    payload = copy.deepcopy(settlement(history()[:6]).payload)
    for field in ['evidence_sha256', 'source', 'executor_outcome', 'observed_head']:
        changed_payload = dict(payload)
        changed_payload.pop(field)
        with pytest.raises(PipelineError):
            wire.parse(wire.render(changed_payload))


def test_settlement_shared_vectors_preserve_exact_canonical_bytes_and_lifetime_state():
    vectors = json.loads((Path(__file__).parent/'fixtures/review-routing-settlement-v1.json').read_text(encoding='utf-8'))
    records = history()[:vectors['base_record_count']]
    for value in vectors['records']:
        assert wire.parse(value['body']) == value['payload']
        assert wire.render(value['payload']) == value['body']
        records.append(wire.Record(value['id'], value['created_at'], value['payload']))
    assert reduce(records).to_dict() == vectors['expected_ledger']
