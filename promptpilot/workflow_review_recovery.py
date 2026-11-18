"""Retry only review of an existing result, preserving the executor and history."""
import hashlib
import re

from . import db
from .models import WorkflowEventCreate, WorkflowStatus


def timed_out(output):
    return (output.get('task_status') == 'failed'
            and re.fullmatch(r'Execution timed out after [1-9][0-9]*s',
                             output.get('error') or '') is not None)


def retry(workflow_id, expected_version, reason, *, automatic=False):
    from . import workflows, workflow_elicitation
    with db._connect(immediate=True) as conn:
        workflow = workflows._workflow_row(conn, workflow_id)
        workflows._require_version(conn, workflow, expected_version)
        if workflow['status'] != 'awaiting_human':
            raise db.WorkflowConflictError('review retry requires a stopped review')
        if db._json_load(workflow['config_json']).get('workorder_guard') is not None:
            raise db.WorkflowConflictError('workorders require their own recovery authorization')
        db.require_gate_idle(conn, workflow_id)
        if (workflows._pending_stage_checkpoint(conn, workflow_id)
                or workflow_elicitation.active(conn, workflow)):
            raise db.WorkflowConflictError('resolve the pending operator action before review retry')
        round_row = workflows._current_round_row(conn, workflow)
        reviewer = conn.execute("SELECT * FROM workflow_runs WHERE round_id=? AND role='reviewer' ORDER BY attempt_no DESC LIMIT 1", (round_row['id'],)).fetchone()
        executor = conn.execute("SELECT * FROM workflow_runs WHERE round_id=? AND role='executor' ORDER BY attempt_no DESC LIMIT 1", (round_row['id'],)).fetchone()
        if (reviewer is None or reviewer['status'] not in ('completed', 'failed')
                or executor is None or executor['status'] != 'completed'):
            raise db.WorkflowConflictError('no terminal reviewer with a completed executor result')
        active = conn.execute("SELECT 1 FROM workflow_runs r LEFT JOIN tasks t ON t.id=r.task_id WHERE r.workflow_id=? AND r.role='reviewer' AND (r.status NOT IN ('completed','failed','cancelled') OR t.status NOT IN ('completed','failed','cancelled'))", (workflow_id,)).fetchone()
        if active:
            raise db.WorkflowConflictError('another reviewer is still active')
        output = db._json_load(reviewer['output_json'])
        task = conn.execute('SELECT status FROM tasks WHERE id=?', (reviewer['task_id'],)).fetchone()
        if task is None or task['status'] not in ('completed', 'failed'):
            raise db.WorkflowConflictError('reviewer task is not terminal')
        for run in (reviewer, executor):
            if (not run['output_json'] or hashlib.sha256(run['output_json'].encode()).hexdigest() != run['output_sha256']):
                raise db.WorkflowConflictError('review retry output binding changed')
        gate = conn.execute("SELECT MAX(seq) FROM workflow_events WHERE workflow_id=? AND round_id=? AND event_type='gate.passed'", (workflow_id, round_row['id'])).fetchone()[0]
        if not gate:
            raise db.WorkflowConflictError('review retry requires an earlier passed gate')
        accepted = conn.execute("SELECT 1 FROM workflow_events WHERE workflow_id=? AND round_id=? AND event_type IN ('review.passed','review.revision_required','stage.completed')", (workflow_id, round_row['id'])).fetchone()
        if accepted:
            raise db.WorkflowConflictError('a decided review cannot be retried as a technical failure')
        if automatic:
            config = workflows._config_for(db._row_to_workflow(workflow))
            count = conn.execute("SELECT COUNT(*) FROM workflow_events WHERE workflow_id=? AND round_id=? AND event_type='reviewer.retry_requested' AND json_extract(payload_json,'$.automatic')=1", (workflow_id, round_row['id'])).fetchone()[0]
            if (not config.automation.enabled or not config.automation.auto_dispatch_reviewer
                    or not config.automation.auto_gate or not config.automation.auto_apply_review
                    or not timed_out(output)
                    or count >= config.roles.reviewer.technical_review_retries):
                raise db.WorkflowConflictError('technical review retry is not admitted')
        db._append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=workflow_id, round_id=round_row['id'], run_id=reviewer['id'],
            event_type='human.input' if not automatic else 'reviewer.recovery_note',
            idempotency_key=f"reviewer.retry.note:{reviewer['id']}:{expected_version}",
            payload={'text':reason, 'review_only':True, 'automatic':automatic}))
        conn.execute("UPDATE workflow_rounds SET status='gating',completed_at=NULL WHERE id=?", (round_row['id'],))
        result = workflows._transition(conn, workflow, WorkflowStatus.GATING,
            'reviewer.retry_requested', {'reason':reason,'automatic':automatic,
                'previous_reviewer_run_id':reviewer['id'],
                'executor_run_id':executor['id'], 'executor_output_sha256':executor['output_sha256'],
                'recheck_gate':True}, round_id=round_row['id'])
        return db._row_to_workflow(result)


def automatic_retry(workflow):
    """Only an opt-in machine timeout, never a model-authored HUMAN_REQUIRED."""
    try:
        return retry(workflow.id, workflow.state_version,
                     'Технический таймаут ревью: повтор проверки того же результата.',
                     automatic=True)
    except db.WorkflowConflictError:
        return workflow


def stop_reason(conn, workflow):
    """Read-only diagnostic. A report is evidence, not retry permission."""
    from .workflows import parse_reviewer_report
    if workflow['status'] != 'awaiting_human':
        return None
    from . import workflows, workflow_elicitation
    retry_available = (db._json_load(workflow['config_json']).get('workorder_guard') is None
                       and not workflow_elicitation.active(conn, workflow)
                       and not workflows._pending_stage_checkpoint(conn, workflow['id']))
    run = conn.execute("SELECT r.* FROM workflow_runs r JOIN workflow_rounds q ON q.id=r.round_id WHERE r.workflow_id=? AND q.round_no=? AND r.role='reviewer' ORDER BY r.attempt_no DESC LIMIT 1", (workflow['id'],workflow['current_round'])).fetchone()
    if run is None:
        return None
    output = db._json_load(run['output_json'])
    if timed_out(output):
        return {'kind':'review_timeout','summary':'Проверяющий превысил лимит времени.',
                'detail':output['error'],'action':'Повторить только ревью сданного результата; исполнителю заново выполнять задачу не требуется.',
                'owner':'PromptPilot','basis':'recorded','retry_available':retry_available}
    report = output.get('report_text', output.get('result') or '')
    decision = parse_reviewer_report(report)
    if run['status'] == 'completed' and decision and decision.verdict.value == 'HUMAN_REQUIRED':
        return {'kind':'review_human_required','summary':'Проверяющий остановил приёмку.',
                'detail':(decision.findings[0].title if decision.findings else decision.summary)[:600],
                'action':'Открыть заключение проверяющего и устранить указанную причину перед повтором ревью.',
                'owner':'Проверяющий','basis':'reviewer_report','retry_available':retry_available}
    return None
