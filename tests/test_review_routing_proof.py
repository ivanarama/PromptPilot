"""Lifetime budget, concurrency losers and post-push proof boundaries."""
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from promptpilot.pipeline_errors import PipelineError
from promptpilot.review_routing import inspect_report
from promptpilot import review_routing_wire as wire
from promptpilot.review_routing_proof import reduce


GOLDEN = json.loads((Path(__file__).parent / "fixtures/review-routing-wire-v1.json").read_text(encoding="utf-8"))
B = "00000000-0000-4000-8000-000000000002"


def history():
    return [wire.Record(item["id"], item["created_at"], copy.deepcopy(item["payload"]))
            for item in GOLDEN["records"]]


def changed(record, **values):
    data = copy.deepcopy(record.payload)
    data.update(values)
    return wire.Record(record.id, record.created_at, data)


def test_author_cannot_start_after_its_terminal_fix_done():
    records=history()[:8]
    late=records[6]
    records[6]=wire.Record(late.id,'2026-10-29T00:00:08Z',late.payload)
    with pytest.raises(PipelineError,match='terminal fix-done'):
        reduce(records)


def test_shared_vectors_bind_every_record_and_finite_lifetime_ledger():
    records = history()
    assert reduce(records).to_dict() == GOLDEN["expected_ledger"]
    for record, golden in zip(records, GOLDEN["records"]):
        assert wire.render(record.payload) == golden["body"]
        assert wire.parse(golden["body"]) == record.payload
        if golden["context_sha256"]:
            assert wire.context(record.payload) == golden["context_sha256"]
    state = reduce(list(reversed(records)))
    assert state.budget_used == 2
    assert [proof.source["head"] for proof in state.proofs] == [letter * 40 for letter in "abc"]
    assert state.proofs[-1].outcome == "engineering-handoff"
    assert state.to_dict()["mutation_authorized"] is False


def test_concurrent_equivalent_roots_reserve_one_slot_and_loser_never_revives():
    records = history()[:4]
    root = records[-1]
    loser = wire.Record("9007199254740993", root.created_at, dict(root.payload, owner=B))
    result = reduce(records + [loser])
    assert result.budget_used == 1
    assert result.attempts[0].id == root.id
    assert loser.id in result.ignored_ids
    with pytest.raises(PipelineError, match="budget unknown"):
        reduce(records[:-1] + [loser], deleted_at=[root.created_at])


def test_changed_executor_is_conflict_not_a_second_equivalent_root():
    records = history()[:4]
    root = records[-1]
    data = dict(root.payload, owner=B, executor={"kind": "pipeline", "login": "owner"})
    loser = wire.Record("9007199254740993", root.created_at, data)
    with pytest.raises(PipelineError, match="conflicting technical"):
        reduce(records + [loser])


def test_deleting_local_cache_or_all_server_reservations_does_not_refill_budget():
    records = history()
    for survivors in ([], records[:3], records[:3] + records[8:]):
        with pytest.raises(PipelineError, match="budget unknown"):
            reduce(survivors, deleted_at=["2026-10-29T00:00:04Z"])
    # Server root absence without a deletion fence is not a syntactically valid
    # contiguous later attempt either.
    with pytest.raises(PipelineError):
        reduce(records[:3] + records[8:])


def test_cancelled_or_unknown_started_attempt_still_costs_its_slot():
    records = history()[:4]
    attempt = records[-1]
    common = {"protocol": "review-routing-v1", "kind": "phase", "pr": attempt.payload["pr"],
              "owner": attempt.payload["owner"], "context_sha256": wire.context(attempt.payload),
              "attempt_id": attempt.id, "lease_id": attempt.id}
    barrier = wire.Record("1004", "2026-10-29T00:00:04Z", dict(common,phase="barrier",details={"reason":"unknown spawn result"}))
    state = reduce(records + [barrier])
    assert state.budget_used == 1
    assigned = wire.Record("1005", "2026-10-29T00:00:05Z", dict(common,phase="assigned",details={}))
    with pytest.raises(PipelineError, match="ambiguity barrier"):
        reduce(records + [barrier, assigned])


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4, 5, 7, 8, 9, 10])
def test_missing_server_history_is_never_permission_to_skip_a_phase(index):
    records = history()
    with pytest.raises(PipelineError):
        reduce(records[:index] + records[index+1:])


def test_author_completion_needs_handoff_but_no_fictitious_pipeline_start():
    records=history()[:8]
    assert records[3].payload['executor']['kind']=='author'
    without_started=records[:6]+records[7:]
    ledger=reduce(without_started)
    assert ledger.budget_used==1
    assert set(ledger.attempt_phases(ledger.attempts[0]))=={'assigned','handoff-done','fix-done'}
    with pytest.raises(PipelineError,match='prerequisite'):
        reduce(records[:5]+records[7:])


def test_pipeline_completion_still_requires_its_canonical_started_phase():
    records=history()[:8]
    records[3]=changed(records[3],executor={'kind':'pipeline','login':'owner'})
    bound=wire.context(records[3].payload)
    for index in range(4,len(records)):
        records[index]=changed(records[index],context_sha256=bound)
    assert reduce(records).budget_used==1
    with pytest.raises(PipelineError,match='prerequisite'):
        reduce(records[:6]+records[7:])


def test_head_or_blocker_rename_cannot_reset_previous_or_counter():
    records = history()
    for values in ({"previous": None}, {"attempt": 1}, {"series": "1010"}):
        bad = list(records)
        bad[11] = changed(bad[11], **values)
        with pytest.raises(PipelineError):
            reduce(bad)


def test_uncompleted_push_cannot_start_a_second_review_attempt():
    records = history()
    with pytest.raises(PipelineError, match="post-push provenance"):
        reduce(records[:7] + records[8:])


def test_edited_review_payload_or_outcome_cannot_reuse_old_claim_completion():
    records = history()
    data = copy.deepcopy(records[0].payload)
    data["report"]["blocking"][0]["evidence"]["actual"] = "changed reproduction"
    data["report_sha256"] = inspect_report(data["report"], data["identity"]["head_sha"])["report_sha256"]
    records[0] = wire.Record(records[0].id, records[0].created_at, data)
    with pytest.raises(PipelineError, match="publication/order"):
        reduce(records)


def test_wrong_returned_id_and_uuid_do_not_gain_publication_ownership():
    records = history()[:3]
    records[1] = changed(records[1], owner=B)
    with pytest.raises(PipelineError, match="returned ID/UUID"):
        reduce(records)


def test_expired_owner_cannot_finish_or_dispatch_after_thirty_minutes():
    records = history()[:3]
    records[2] = wire.Record(records[2].id, "2026-10-29T00:30:00Z", records[2].payload)
    with pytest.raises(PipelineError, match="not currently active"):
        reduce(records)


def next_review(records, *, head='c'*40, resume_id=None, blocking=None, budget=2,
                start_id=1100,at='00:02:00'):
    """Add an independent new-epoch review to supplied server history."""
    data=copy.deepcopy(history()[0].payload)
    data['identity']['head_sha']=head
    data['epoch']='a'*64
    data['anchor']='NEXT_ANCHOR'
    data['report']['head']=head
    if blocking is not None:
        data['report']['blocking']=blocking
    data['report_sha256']=inspect_report(data['report'],head)['report_sha256']
    data['budget_used']=budget
    data['depth']=5
    data['resume_id']=resume_id
    data['outcome']=wire.outcome(data['report'],depth=5,budget_used=budget)
    common={'protocol':'review-routing-v1','pr':data['pr'],'owner':data['owner'],
            'context_sha256':wire.context(data),'publication_id':str(start_id),'lease_id':str(start_id)}
    records=list(records)
    for offset,payload in enumerate([data,dict(common,kind='claim'),dict(common,kind='completion',claim_id=str(start_id+1))]):
        instant=datetime.fromisoformat('2026-10-29T'+at+'+00:00')+timedelta(seconds=offset)
        records.append(wire.Record(str(start_id+offset),instant.isoformat().replace('+00:00','Z'),payload))
    return records


def resume(parking_id,previous,*,head='c'*40):
    return wire.Record('1099','2026-10-29T00:01:00Z',
                       {'protocol':'review-routing-v1','kind':'resume','pr':history()[0].payload['pr'],
                        'head':head,'parking_id':parking_id,'previous':previous,
                        'reason':'engineering checked current HEAD and permits one independent review'})


def test_completed_review_itself_requires_fix_done_even_without_next_attempt():
    # The original regression accepted proof B as long as no second attempt
    # followed it. It must already refuse this publication/completion boundary.
    records=history()
    with pytest.raises(PipelineError,match='post-push provenance'):
        reduce(records[:7]+records[8:11])


def test_engineering_parking_requires_exact_explicit_single_use_resume():
    records=history()
    with pytest.raises(PipelineError,match='engineering parking'):
        reduce(next_review(records,blocking=[]))
    grant=resume('1018','1011')
    result=reduce(next_review(records+[grant],blocking=[],resume_id=grant.id))
    assert result.budget_used==2 and result.proofs[-1].outcome=='reviewed'
    assert result.resumes==(grant,)
    with pytest.raises(PipelineError,match='engineering parking'):
        reduce(next_review(records+[resume('1018','1011',head='d'*40)],blocking=[],resume_id=grant.id))
    with pytest.raises(PipelineError,match='current parking/lifetime'):
        reduce(records+[resume('1018','1003')])


def test_resume_can_allow_new_head_but_does_not_refill_exhausted_budget():
    grant=resume('1018','1011',head='d'*40)
    result=reduce(next_review(history()+[grant],head='d'*40,resume_id=grant.id))
    assert result.budget_used==2 and result.proofs[-1].outcome=='engineering-handoff'


def test_insufficient_data_parking_with_zero_attempts_can_be_resumed():
    records=history()[:3]
    data=copy.deepcopy(records[0].payload)
    data['report']['blocking'][0].update(kind='insufficient_data',evidence={'missing':'test service unavailable','verification':'reproduce through HTTP once service is available'})
    data['report_sha256']=inspect_report(data['report'],data['identity']['head_sha'])['report_sha256']
    data['outcome']='engineering-handoff'
    records[0]=wire.Record(records[0].id,records[0].created_at,data)
    context_hash=wire.context(data)
    records[1]=changed(records[1],context_sha256=context_hash)
    records[2]=changed(records[2],context_sha256=context_hash)
    grant=resume('1002',None,head='a'*40)
    result=reduce(next_review(records+[grant],head='a'*40,blocking=[],budget=0,resume_id=grant.id))
    assert result.budget_used==0 and result.proofs[-1].outcome=='reviewed'


def test_consumed_engineering_parking_cannot_issue_another_resume():
    grant=resume('1018','1011',head='d'*40)
    completed=next_review(history()+[grant],head='d'*40,blocking=[],resume_id=grant.id)
    state=reduce(completed)
    assert state.parking_id is None and state.resume_id is None
    another=wire.Record('1199','2026-10-29T00:03:00Z',dict(grant.payload,head='e'*40))
    with pytest.raises(PipelineError,match='current parking/lifetime'):
        reduce(completed+[another])


def test_incomplete_resumed_publication_keeps_park_and_consumed_grant_visible():
    grant=resume('1018','1011')
    pending=next_review(history()+[grant],blocking=[],resume_id=grant.id)[:-1]
    state=reduce(pending)
    assert state.parking_id=='1018' and state.resume_id==grant.id
    assert state.resuming_publication_id=='1100'
    with pytest.raises(PipelineError,match='current parking/lifetime'):
        reduce(pending+[wire.Record('1199','2026-10-29T00:03:00Z',grant.payload)])


def test_resumed_technical_review_and_fix_do_not_require_another_historical_resume():
    records=history()[:3]
    data=copy.deepcopy(records[0].payload)
    data['report']['blocking'][0].update(kind='insufficient_data',evidence={'missing':'service unavailable','verification':'run HTTP reproduction'})
    data['report_sha256']=inspect_report(data['report'],data['identity']['head_sha'])['report_sha256']
    data['outcome']='engineering-handoff'
    records[0]=wire.Record(records[0].id,records[0].created_at,data)
    records[1]=changed(records[1],context_sha256=wire.context(data))
    records[2]=changed(records[2],context_sha256=wire.context(data))
    grant=resume('1002',None,head='a'*40)
    records=next_review(records+[grant],head='a'*40,budget=0,resume_id=grant.id)
    state=reduce(records)
    assert state.parking_id is None
    proof=state.proofs[-1]
    common={'protocol':'review-routing-v1','pr':data['pr'],'owner':data['owner']}
    attempt=wire.Record('1200','2026-10-29T00:03:00Z',dict(common,kind='attempt',
                        context_sha256=wire.context(proof.publication.payload),source=proof.source,
                        series=proof.completion.id,previous=None,attempt=1,
                        executor={'kind':'author','login':'contributor'}))
    records.append(attempt)
    for index,(phase,details) in enumerate([('assigned',{}),('handoff-done',{}),
          ('started',{'task_id':123,'attempt_uuid':data['owner']}),
          ('fix-done',{'new_head':'b'*40,'transition':'routing-v1:1200','source_trailer':wire.sha(proof.source)})]):
        records.append(wire.Record(str(1201+index),f'2026-10-29T00:03:0{index+1}Z',
                       dict(common,kind='phase',context_sha256=wire.context(attempt.payload),attempt_id=attempt.id,
                            lease_id=attempt.id,phase=phase,details=details)))
    final=reduce(next_review(records,head='b'*40,budget=1,blocking=[],start_id=1300,at='00:04:00'))
    assert final.budget_used==1 and final.parking_id is None
    assert final.proofs[-1].outcome=='reviewed'


def test_resume_of_unparked_technical_attempt_never_synthesizes_permission():
    with pytest.raises(PipelineError,match='current parking/lifetime'):
        reduce(history()[:4]+[resume('1002','1003',head='a'*40)])


def test_current_budget_cannot_be_inferred_from_a_historical_technical_outcome():
    records=history()
    data=copy.deepcopy(records[16].payload)
    data.update(max_attempts=3,outcome='technical-handoff')
    records[16]=wire.Record(records[16].id,records[16].created_at,data)
    context_hash=wire.context(data)
    records[17]=changed(records[17],context_sha256=context_hash)
    records[18]=changed(records[18],context_sha256=context_hash)
    state=reduce(records,max_attempts=2)
    proof=state.proofs[-1]
    assert proof.outcome=='technical-handoff'  # Historical policy remains immutable.
    current=state.next_attempt_admission(proof.source,max_attempts=2)
    assert current['eligible'] is False and current['budget_used']==2
    assert current['reason']=='current lifetime budget is exhausted'
    assert current['mutation_authorized'] is False
    assert state.next_attempt_admission(proof.source,max_attempts=3)['eligible'] is True
    assert state.next_attempt_admission(state.proofs[0].source,max_attempts=3)['eligible'] is False


def test_reserved_source_cannot_be_readmitted_while_its_slot_is_in_flight():
    state=reduce(history()[:4])
    result=state.next_attempt_admission(state.proofs[-1].source,max_attempts=2)
    assert result['eligible'] is False and result['budget_used']==1
    assert result['reason']=='same source already reserved its lifetime slot'


def test_lower_limit_engineering_completion_keeps_the_full_lifetime_ledger():
    records=history()
    data=copy.deepcopy(records[16].payload)
    data['max_attempts']=1
    records[16]=wire.Record(records[16].id,records[16].created_at,data)
    context_hash=wire.context(data)
    records[17]=changed(records[17],context_sha256=context_hash)
    records[18]=changed(records[18],context_sha256=context_hash)
    # Historical validation is separate from the current admission limit.
    state=reduce(records,max_attempts=16)
    assert state.budget_used==2 and state.proofs[-1].outcome=='engineering-handoff'
    assert state.parking_id==records[18].id
    assert state.next_attempt_admission(state.proofs[-1].source,max_attempts=1)['eligible'] is False
