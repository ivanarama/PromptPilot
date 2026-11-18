import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from promptpilot import api, db, workflows, workflow_progress, task_feed
from promptpilot import workflow_review_recovery as recovery
from promptpilot.models import (WorkflowCreate, WorkflowStartRequest,
    WorkflowTaskDispatch, WorkflowExternalResult, WorkflowGateDecision, WorkflowUpdate,
    WorkflowRunCreate, TaskCreate, WorkflowEventCreate)

HUMAN = ('Network unavailable.\nAUDIT_FINDINGS_JSON: [{"fingerprint":"network",'
         '"severity":"medium","category":"verification","title":"GitHub is unavailable"}]\n'
         'AUDIT_VERDICT: HUMAN_REQUIRED\nИТОГ: НУЖЕН ЧЕЛОВЕК — проверить доступ')
PASS = 'AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО — проверено'


def setup_review(database, *, retries=0, report=None, verdict='НУЖЕН ЧЕЛОВЕК'):
    workflow = database.create_workflow(WorkflowCreate(slug='review-recovery',
        objective='Review the delivered documentation', repository_path=str(database.DB_DIR),
        candidate_branch='test', config={'automation':{'enabled':False},
            'gate':{'enabled':False}, 'roles':{'reviewer':{'technical_review_retries':retries}}}))
    workflow = workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    dispatched = workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(
        expected_version=workflow.state_version, role='executor',execution_mode='external',prompt='Prepare patch'))
    workflow = workflows.submit_external_result(workflow.id, WorkflowExternalResult(
        expected_version=dispatched.workflow.state_version, run_id=dispatched.run.id,
        input_sha256=dispatched.run.input_sha256,result='immutable patch receipt',performer='external'))
    workflow = workflows.record_gate(workflow.id, WorkflowGateDecision(
        expected_version=workflow.state_version,verdict='PASS',gate_id='test',summary='checked'))
    reviewer = workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(
        expected_version=workflow.state_version,role='reviewer',prompt='Review')).task
    database.get_next_runnable()
    workflows.sync_task(reviewer.id)
    if report is None:
        database.mark_failed(reviewer.id,'Execution timed out after 300s',exit_code=-1)
    else:
        database.mark_completed(reviewer.id,report,exit_code=0)
        database.set_verdict(reviewer.id,verdict)
    workflows.sync_task(reviewer.id)
    return database.get_workflow(workflow.id), reviewer


def enable(database, workflow, *, retries=1):
    config = dict(workflow.config)
    config['automation']={'enabled':True}
    config['roles']['reviewer']['technical_review_retries']=retries
    return database.update_workflow(workflow.id,WorkflowUpdate(expected_version=workflow.state_version,config=config))


def test_timeout_retry_preserves_executor_and_round_and_rechecks_gate(isolated_db):
    workflow, old_task = setup_review(isolated_db)
    before = db.list_workflow_runs(db.list_workflow_rounds(workflow.id)[0].id)[0]
    workflow = recovery.retry(workflow.id, workflow.state_version,'Restore review')
    assert workflow.status.value == 'gating' and workflow.current_round == 1
    workflow = enable(db,workflow,retries=0)
    workflow = workflows.advance_workflow(workflow.id)
    assert workflow.status.value == 'reviewing'
    runs = db.list_workflow_runs(db.list_workflow_rounds(workflow.id)[0].id)
    assert len([r for r in runs if r.role.value=='executor']) == 1
    assert len([r for r in runs if r.role.value=='reviewer']) == 2
    assert runs[0].output_sha256 == before.output_sha256
    assert db.get_task(old_task.id).status.value == 'failed'
    assert len([e for e in db.list_workflow_events(workflow.id) if e.event_type=='gate.passed']) == 2
    task = db.get_next_runnable()
    db.mark_completed(task.id,PASS,exit_code=0)
    db.set_verdict(task.id,'ГОТОВО')
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    assert db.get_workflow(workflow.id).status.value == 'completed'


def test_opt_in_timeout_retries_once_and_does_not_loop_on_restart(isolated_db):
    workflow, _ = setup_review(isolated_db,retries=1)
    workflow = enable(db,workflow)
    workflow = workflows.advance_workflow(workflow.id)
    assert workflow.status.value=='reviewing'
    workflows.sync_all_tasks(workflow.id)
    task=db.get_next_runnable()
    db.mark_failed(task.id,'Execution timed out after 300s',exit_code=-1)
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    assert db.get_workflow(workflow.id).status.value=='awaiting_human'
    workflows.sync_all_tasks(workflow.id)
    runs=db.list_workflow_runs(db.list_workflow_rounds(workflow.id)[0].id)
    assert len([r for r in runs if r.role.value=='reviewer'])==2


def test_no_automatic_retries_without_owner_policy(isolated_db):
    workflow,_=setup_review(isolated_db)
    workflow=enable(db,workflow,retries=0)
    assert workflows.advance_workflow(workflow.id).status.value=='awaiting_human'


def test_valid_human_review_is_recorded_not_misreported_as_invalid(isolated_db):
    workflow,_=setup_review(isolated_db,report=HUMAN)
    workflow=enable(db,workflow)
    workflows.advance_workflow(workflow.id)
    events=db.list_workflow_events(workflow.id)
    assert 'reviewer.invalid_output' not in [e.event_type for e in events]
    assert any(e.event_type=='review.human_required' for e in events)
    assert len(db.list_workflow_findings(workflow.id))==1
    assert not any(e.event_type=='reviewer.retry_requested' for e in events)
    version=db.get_workflow(workflow.id).state_version
    workflows.advance_workflow(workflow.id)
    workflows.sync_all_tasks(workflow.id)
    assert db.get_workflow(workflow.id).state_version==version
    assert len(db.list_workflow_events(workflow.id))==len(events)


def test_unknown_or_mismatched_report_is_never_accepted(isolated_db):
    workflow,_=setup_review(isolated_db,report=PASS,verdict='НУЖЕН ЧЕЛОВЕК')
    assert any(e.event_type=='reviewer.invalid_output' for e in db.list_workflow_events(workflow.id))
    assert workflow.status.value=='awaiting_human'


def test_new_output_on_same_run_is_not_suppressed_by_old_human_decision(isolated_db):
    workflow,task=setup_review(isolated_db,report=HUMAN)
    run=db.list_workflow_runs(db.list_workflow_rounds(workflow.id)[0].id)[-1]
    with db._connect() as conn:
        db._append_workflow_event(conn,WorkflowEventCreate(workflow_id=workflow.id,
            round_id=run.round_id,run_id=run.id,event_type='review.human_required',
            idempotency_key='historical-other-output',payload={'reviewer_output_sha256':'other-output','summary':'Old observation'}))
    workflow=enable(db,workflow)
    workflows.advance_workflow(workflow.id)
    events=[e for e in db.list_workflow_events(workflow.id) if e.event_type=='review.human_required']
    assert len(events)==2
    assert events[0].payload['reviewer_output_sha256']!=events[1].payload['reviewer_output_sha256']


def test_concurrent_retry_only_one_wins(isolated_db):
    workflow,_=setup_review(isolated_db)
    def attempt():
        try:
            return recovery.retry(workflow.id,workflow.state_version,'retry').status.value
        except db.WorkflowConflictError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _:attempt(),range(2)))==['conflict','gating']


def test_tampered_executor_receipt_cannot_be_retried(isolated_db):
    workflow,_=setup_review(isolated_db)
    with db._connect() as conn:
        conn.execute("UPDATE workflow_runs SET output_json='{}' WHERE workflow_id=? AND role='executor'",(workflow.id,))
    with pytest.raises(db.WorkflowConflictError,match='binding'):
        recovery.retry(workflow.id,workflow.state_version,'retry')


def test_timeout_is_shown_as_review_problem_and_progress_is_readonly(isolated_db):
    workflow,_=setup_review(isolated_db)
    with db._connect() as conn:
        before=list(conn.iterdump())
    progress=workflow_progress.get_progress(workflow.id)
    assert progress['review_stop']['kind']=='review_timeout'
    assert progress['current']['owner']=='PromptPilot'
    with db._connect() as conn:
        item=task_feed._business_items(conn,kind='workflow',object_id=workflow.id)[0]
        assert item['status']=='blocked'
        assert list(conn.iterdump())==before


def test_failed_new_review_never_reuses_previous_completed_report(isolated_db):
    workflow,_=setup_review(isolated_db,report=HUMAN)
    workflow=recovery.retry(workflow.id,workflow.state_version,'repair access')
    workflow=enable(db,workflow,retries=0)
    workflow=workflows.advance_workflow(workflow.id)
    task=db.get_next_runnable()
    db.mark_failed(task.id,'Execution timed out after 300s',exit_code=-1)
    workflows.sync_task(task.id)
    workflow=db.get_workflow(workflow.id)
    assert workflows._latest_completed_reviewer(workflow) is None


def test_api_retry_checks_version_and_does_not_duplicate_executor(isolated_db):
    workflow,_=setup_review(isolated_db)
    async def send(version):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://localhost') as client:
            return await client.post('/api/workflows/'+workflow.id+'/review-retry',json={'expected_version':version,'reason':'Restore network'})
    assert asyncio.run(send(workflow.state_version+1)).status_code==409
    response=asyncio.run(send(workflow.state_version))
    assert response.status_code==200 and response.json()['status']=='gating'


def test_cancelled_workflow_cannot_be_recovered(isolated_db):
    workflow,_=setup_review(isolated_db)
    workflow=workflows.cancel_workflow(workflow.id,workflow.state_version)
    with pytest.raises(db.WorkflowConflictError):
        recovery.retry(workflow.id,workflow.state_version,'retry')


def test_older_active_reviewer_blocks_retry_even_if_newest_is_terminal(isolated_db):
    workflow,_=setup_review(isolated_db)
    round_id=db.list_workflow_rounds(workflow.id)[0].id
    with db._connect() as conn:
        conn.execute("UPDATE workflow_runs SET attempt_no=2 WHERE workflow_id=? AND role='reviewer'",(workflow.id,))
    task=db.create_task(TaskCreate(prompt='older review still pending'))
    db.create_workflow_run(WorkflowRunCreate(workflow_id=workflow.id,round_id=round_id,
        role='reviewer',attempt_no=1,task_id=task.id,input_sha256='older'))
    with pytest.raises(db.WorkflowConflictError,match='another reviewer'):
        recovery.retry(workflow.id,workflow.state_version,'retry')
