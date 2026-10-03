"""What the operator writes when resuming a workflow must reach the agents.

It used to be journaled in workflow_events and nowhere else: the next
executor and reviewer never saw it.
"""

from promptpilot import workflows
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowHumanInput,
    WorkflowStartRequest,
    WorkflowUpdate,
)

NOTE = "Не трогай модуль оплаты, чини только парсер дат"
REVISION = (
    'AUDIT_FINDINGS_JSON: [{"fingerprint":"f1","severity":"high",'
    '"category":"runtime","title":"Broken","status":"open","payload":{}}]\n'
    "AUDIT_VERDICT: REVISION_REQUIRED\n"
    "ИТОГ: ГОТОВО — аудит завершён"
)


def create(isolated_db, *, slug="notes", auto_resume=False, max_rounds=5,
           executor_template=""):
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="Исправить парсер",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/notes",
        config={
            "schema_version": 1,
            "automation": {"enabled": True, "auto_resume_revision": auto_resume},
            "roles": {
                "executor": {"provider": None, "prompt_template": executor_template},
                "reviewer": {"provider": None},
            },
            "gate": {"commands": []},
            "limits": {"max_rounds": max_rounds},
        },
    ))


def complete_next(isolated_db, result):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def pending_prompt(isolated_db):
    pending = isolated_db.list_tasks(status=TaskStatus.PENDING)
    assert len(pending) == 1
    return pending[0].prompt


def run_to_revision(isolated_db, workflow):
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    complete_next(isolated_db, "executor report round 1")
    complete_next(isolated_db, REVISION)
    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "revision_required"
    return stopped


def resume(workflow, *, note="", text="Продолжить"):
    resumed = workflows.human_input(workflow.id, WorkflowHumanInput(
        expected_version=workflow.state_version, text=text, note=note, resume=True,
    ))
    return workflows.advance_workflow(resumed.id)


def test_note_reaches_next_executor_and_reviewer_once(isolated_db):
    workflow = run_to_revision(isolated_db, create(isolated_db))

    resume(workflow, note=NOTE)

    executor_prompt = pending_prompt(isolated_db)
    assert NOTE in executor_prompt
    assert "<указания-оператора>" in executor_prompt
    # The note outranks the round and precedes the output contract.
    assert executor_prompt.index(NOTE) < executor_prompt.index(
        "<promptpilot-workflow-contract")

    complete_next(isolated_db, "executor report round 2")
    assert NOTE in pending_prompt(isolated_db)  # first reviewer run after the note

    complete_next(isolated_db, REVISION)
    resume(isolated_db.get_workflow(workflow.id))  # resumed without a new note

    assert NOTE not in pending_prompt(isolated_db)


def test_decision_text_and_automation_boilerplate_are_not_delivered(isolated_db):
    workflow = run_to_revision(isolated_db, create(isolated_db, slug="plain"))

    resume(workflow, text="Продолжить работу с учётом сохранённого состояния")

    prompt = pending_prompt(isolated_db)
    assert "<указания-оператора>" not in prompt
    assert "Продолжить работу с учётом" not in prompt


def test_resumed_executor_receives_previous_run_report(isolated_db):
    workflow = create(isolated_db, slug="handoff")
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    assert "Коммит abc123 и ожидающий CI" not in pending_prompt(isolated_db)

    task = isolated_db.get_next_runnable()
    workflows.sync_task(task.id)
    isolated_db.mark_completed(
        task.id, "Коммит abc123 и ожидающий CI\nИТОГ: НУЖЕН ЧЕЛОВЕК", exit_code=0,
    )
    isolated_db.set_verdict(task.id, "НУЖЕН ЧЕЛОВЕК")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"

    resume(stopped)
    prompt = pending_prompt(isolated_db)
    assert "Коммит abc123 и ожидающий CI" in prompt
    assert "ИТОГ: НУЖЕН ЧЕЛОВЕК" in prompt
    assert "Продолжить работу с учётом" not in prompt


def test_automatic_resume_carries_no_note(isolated_db):
    workflow = create(isolated_db, slug="auto", auto_resume=True)
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    complete_next(isolated_db, "executor report round 1")

    complete_next(isolated_db, REVISION)  # automation resumes on its own

    assert isolated_db.get_workflow(workflow.id).current_round == 2
    assert "<указания-оператора>" not in pending_prompt(isolated_db)


def test_template_can_place_notes_itself(isolated_db):
    template = "Цель: {{objective}}\nОт оператора:\n{{human_input}}\nКонец."
    workflow = run_to_revision(
        isolated_db, create(isolated_db, slug="placed", executor_template=template))

    resume(workflow, note=NOTE)

    prompt = pending_prompt(isolated_db)
    assert f"От оператора:\n{NOTE}\nКонец." in prompt
    assert "<указания-оператора>" not in prompt


def test_note_given_while_limit_refuses_is_kept_for_later(isolated_db):
    workflow = run_to_revision(
        isolated_db, create(isolated_db, slug="limited", max_rounds=1))
    stopped = workflows.human_input(workflow.id, WorkflowHumanInput(
        expected_version=workflow.state_version, text="Продолжить", resume=True,
    ))
    assert stopped.status.value == "awaiting_human"  # max_rounds reached

    refused = workflows.human_input(stopped.id, WorkflowHumanInput(
        expected_version=stopped.state_version, text="Продолжить", note=NOTE,
        resume=True,
    ))
    assert refused.status.value == "awaiting_human"

    config = dict(refused.config)
    config["limits"] = {"max_rounds": 3}
    raised = isolated_db.update_workflow(refused.id, WorkflowUpdate(
        config=config, expected_version=refused.state_version,
    ))
    resume(raised)

    assert NOTE in pending_prompt(isolated_db)


def test_long_journal_does_not_launch_a_second_reviewer(isolated_db):
    """advance_workflow read the OLDEST 1000 events, so in a long workflow
    the current round's reviewer dispatch was invisible."""
    workflow = create(isolated_db, slug="long")
    with isolated_db._connect() as conn:
        conn.executemany(
            """INSERT INTO workflow_events
               (workflow_id, event_type, payload_json, idempotency_key, created_at)
               VALUES (?, 'test.padding', '{}', ?, '2026-01-01T00:00:00+00:00')""",
            [(workflow.id, f"pad:{index}") for index in range(1100)],
        )
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    complete_next(isolated_db, "executor report")
    assert isolated_db.get_workflow(workflow.id).status.value == "reviewing"

    workflows.advance_workflow(workflow.id)  # e.g. worker restart during review

    reviewers = [task for task in isolated_db.list_tasks(status=TaskStatus.PENDING)
                 if "AUDIT_VERDICT" in task.prompt]
    assert len(reviewers) == 1
