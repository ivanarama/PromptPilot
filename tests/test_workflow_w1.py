import pytest

from promptpilot import workflows
from promptpilot.models import (
    FindingSeverity,
    FindingStatus,
    GateVerdict,
    HistoricalFactImport,
    HistoricalFactStatus,
    HistoricalRoundImport,
    ReviewFindingInput,
    ReviewVerdict,
    WorkflowCreate,
    WorkflowGateDecision,
    WorkflowHistoryImport,
    WorkflowHumanInput,
    WorkflowReviewDecision,
    WorkflowRole,
    WorkflowStartRequest,
    WorkflowTaskDispatch,
)


def create_workflow(db, slug="w1-pilot", max_rounds=2):
    return db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="Автономно закрыть замечания аудита",
        repository_path=r"D:\Projects\exchange",
        candidate_branch="workflow/pilot",
        config={"limits": {"max_rounds": max_rounds}},
    ))


def settle_next_task(db, result="ok"):
    task = db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    db.mark_completed(task.id, result, exit_code=0)
    db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    return task


def dispatch(db, workflow_id, role, prompt):
    current = db.get_workflow(workflow_id)
    return workflows.dispatch_task(workflow_id, WorkflowTaskDispatch(
        expected_version=current.state_version,
        role=role,
        prompt=prompt,
        provider=None,
    ))


def gate_pass(db, workflow_id):
    current = db.get_workflow(workflow_id)
    return workflows.record_gate(workflow_id, WorkflowGateDecision(
        expected_version=current.state_version,
        verdict=GateVerdict.PASS,
        gate_id="pytest",
        summary="all deterministic checks passed",
    ))


def test_full_manual_w1_cycle_revision_then_pass(isolated_db):
    workflow = create_workflow(isolated_db)
    history = WorkflowHistoryImport(
        expected_version=workflow.state_version,
        idempotency_key="legacy-ut10-rounds-v1",
        source="git+docs/audit",
        notes="До включения PromptPilot workflow",
        rounds=[HistoricalRoundImport(
            round_no=1,
            status="revision_required",
            candidate_sha="b13cfa7",
            audit_sha="8744181",
            summary="U3-FIX-1 не принят",
            facts=[HistoricalFactImport(
                claim="Полный дамп технически обработан",
                status=HistoricalFactStatus.VERIFIED,
                source="docs/audit/UT10-BP3_U3_FIX1_REVIEW.md",
                evidence=["ut10_u3_fix1_runtime_audit_20260825.json"],
            )],
            metrics={"pytest_passed": 1048},
        )],
    )
    imported = workflows.import_history(workflow.id, history)
    replayed = workflows.import_history(workflow.id, history)
    assert replayed.state_version == imported.state_version == 1
    assert imported.current_round == 1

    started = workflows.start_workflow(workflow.id, WorkflowStartRequest(
        expected_version=imported.state_version,
        base_sha="8744181",
    ))
    assert started.status.value == "queued"
    assert started.current_round == 2

    executor = dispatch(
        isolated_db, workflow.id, WorkflowRole.EXECUTOR,
        "Исправь блокеры U3-FIX-2",
    )
    assert executor.workflow.status.value == "executing"
    assert executor.run.task_id == executor.task.id
    settle_next_task(isolated_db, "executor round 2 complete")
    assert isolated_db.get_workflow(workflow.id).status.value == "gating"

    assert gate_pass(isolated_db, workflow.id).status.value == "reviewing"
    reviewer = dispatch(
        isolated_db, workflow.id, WorkflowRole.REVIEWER,
        "Проверь точный commit и evidence",
    )
    assert reviewer.workflow.status.value == "reviewing"
    settle_next_task(isolated_db, "REVISION_REQUIRED: semantic map incomplete")
    waiting = isolated_db.get_workflow(workflow.id)
    assert waiting.status.value == "awaiting_human"

    blocker = ReviewFindingInput(
        fingerprint="bank-cash-operation-map-incomplete",
        severity=FindingSeverity.BLOCKER,
        category="semantic_mapping",
        title="СБДС создаётся как ПБДС",
        status=FindingStatus.OPEN,
        payload={"document": "ТК000000016"},
    )
    revision = workflows.record_review(workflow.id, WorkflowReviewDecision(
        expected_version=waiting.state_version,
        verdict=ReviewVerdict.REVISION_REQUIRED,
        summary="Нужен ещё один раунд",
        findings=[blocker],
    ))
    assert revision.status.value == "revision_required"
    assert isolated_db.list_workflow_findings(workflow.id)[0].status.value == "open"

    resumed = workflows.human_input(workflow.id, WorkflowHumanInput(
        expected_version=revision.state_version,
        text="Исправить замечание без расширения scope",
        resume=True,
    ))
    assert resumed.status.value == "queued"
    assert resumed.current_round == 3

    dispatch(isolated_db, workflow.id, WorkflowRole.EXECUTOR, "Исправь mapping")
    settle_next_task(isolated_db, "executor round 3 complete")
    gate_pass(isolated_db, workflow.id)
    dispatch(isolated_db, workflow.id, WorkflowRole.REVIEWER, "Повторный аудит")
    settle_next_task(isolated_db, "PASS")
    waiting = isolated_db.get_workflow(workflow.id)

    resolved = blocker.model_copy(update={
        "status": FindingStatus.RESOLVED,
        "payload": {"verified_by": "exact dump probe"},
    })
    completed = workflows.record_review(workflow.id, WorkflowReviewDecision(
        expected_version=waiting.state_version,
        verdict=ReviewVerdict.PASS,
        summary="Все блокеры закрыты",
        findings=[resolved],
    ))
    assert completed.status.value == "completed"
    assert completed.completed_at is not None
    finding = isolated_db.list_workflow_findings(workflow.id)[0]
    assert finding.status.value == "resolved"
    assert finding.first_seen_round == 2
    assert finding.last_seen_round == 3

    event_types = [
        event.event_type
        for event in isolated_db.list_workflow_events(workflow.id, limit=1000)
    ]
    assert "history.fact_imported" in event_types
    assert event_types.count("workflow.started") == 1
    assert event_types.count("executor.completed") == 2
    assert event_types.count("gate.passed") == 2
    assert event_types.count("review.awaiting_decision") == 2
    assert event_types[-1] == "review.passed"


def test_review_cannot_pass_with_open_blocker(isolated_db):
    workflow = create_workflow(isolated_db, slug="open-blocker")
    started = workflows.start_workflow(workflow.id, WorkflowStartRequest(
        expected_version=0,
    ))
    dispatch(isolated_db, workflow.id, WorkflowRole.EXECUTOR, "execute")
    settle_next_task(isolated_db)
    gate_pass(isolated_db, workflow.id)
    dispatch(isolated_db, workflow.id, WorkflowRole.REVIEWER, "review")
    settle_next_task(isolated_db)
    waiting = isolated_db.get_workflow(workflow.id)

    with pytest.raises(isolated_db.WorkflowConflictError, match="cannot pass"):
        workflows.record_review(workflow.id, WorkflowReviewDecision(
            expected_version=waiting.state_version,
            verdict=ReviewVerdict.PASS,
            findings=[ReviewFindingInput(
                fingerprint="still-broken",
                severity=FindingSeverity.BLOCKER,
                category="runtime",
                title="Still broken",
            )],
        ))

    # The rejected PASS is one transaction: neither the finding nor a partial
    # event leaks into durable history.
    assert isolated_db.list_workflow_findings(workflow.id) == []
    assert isolated_db.get_workflow(workflow.id).state_version == waiting.state_version


def test_invalid_transitions_stale_version_and_reviewer_permissions(isolated_db):
    workflow = create_workflow(isolated_db, slug="invalid-transitions")
    started = workflows.start_workflow(workflow.id, WorkflowStartRequest(
        expected_version=0,
    ))

    with pytest.raises(isolated_db.WorkflowConflictError, match="version"):
        workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(
            expected_version=0,
            role=WorkflowRole.EXECUTOR,
            prompt="stale",
        ))
    with pytest.raises(isolated_db.WorkflowConflictError, match="cannot dispatch reviewer"):
        workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(
            expected_version=started.state_version,
            role=WorkflowRole.REVIEWER,
            prompt="too early",
        ))
    with pytest.raises(isolated_db.WorkflowConflictError, match="skip_permissions"):
        workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(
            expected_version=started.state_version,
            role=WorkflowRole.REVIEWER,
            prompt="unsafe",
            skip_permissions=True,
        ))


def test_round_budget_moves_control_to_human(isolated_db):
    workflow = create_workflow(isolated_db, slug="round-budget", max_rounds=1)
    started = workflows.start_workflow(workflow.id, WorkflowStartRequest(
        expected_version=0,
    ))
    dispatch(isolated_db, workflow.id, WorkflowRole.EXECUTOR, "execute")
    settle_next_task(isolated_db)
    current = isolated_db.get_workflow(workflow.id)
    failed_gate = workflows.record_gate(workflow.id, WorkflowGateDecision(
        expected_version=current.state_version,
        verdict=GateVerdict.FAIL,
        summary="needs another round",
    ))
    assert failed_gate.status.value == "revision_required"

    stopped = workflows.human_input(workflow.id, WorkflowHumanInput(
        expected_version=failed_gate.state_version,
        text="try again",
        resume=True,
    ))
    assert stopped.status.value == "awaiting_human"
    assert stopped.current_round == 1
    assert isolated_db.list_workflow_events(workflow.id)[-1].event_type == "limit.max_rounds"


def test_sync_repairs_crash_gap_and_is_idempotent(isolated_db):
    workflow = create_workflow(isolated_db, slug="crash-recovery")
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    result = dispatch(
        isolated_db, workflow.id, WorkflowRole.EXECUTOR, "execute",
    )

    task = isolated_db.get_next_runnable()
    # Simulate a process that updated the durable task row and died before the
    # workflow callback ran.
    isolated_db.mark_completed(task.id, "finished before crash", exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    assert isolated_db.get_workflow(workflow.id).status.value == "executing"

    assert workflows.sync_all_tasks(workflow.id) == 1
    first_events = isolated_db.list_workflow_events(workflow.id, limit=1000)
    assert isolated_db.get_workflow(workflow.id).status.value == "gating"
    assert isolated_db.get_workflow_run(result.run.id).status.value == "completed"

    assert workflows.sync_all_tasks(workflow.id) == 1
    second_events = isolated_db.list_workflow_events(workflow.id, limit=1000)
    assert len(second_events) == len(first_events)


def test_cancel_workflow_cancels_queued_linked_task(isolated_db):
    workflow = create_workflow(isolated_db, slug="cancel-linked")
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    dispatched = dispatch(
        isolated_db, workflow.id, WorkflowRole.EXECUTOR, "execute later",
    )
    current = isolated_db.get_workflow(workflow.id)

    cancelled = workflows.cancel_workflow(workflow.id, current.state_version)
    assert cancelled.status.value == "cancelled"
    assert isolated_db.get_task(dispatched.task.id).status.value == "cancelled"
    assert isolated_db.get_workflow_run(dispatched.run.id).status.value == "cancelled"


def test_history_import_rejects_unknown_rewrite_and_nonterminal_round(isolated_db):
    workflow = create_workflow(isolated_db, slug="history-safety")
    valid = WorkflowHistoryImport(
        expected_version=0,
        idempotency_key="history-v1",
        source="manual reconstruction",
        rounds=[HistoricalRoundImport(round_no=1, status="completed")],
    )
    workflows.import_history(workflow.id, valid)

    with pytest.raises(isolated_db.WorkflowConflictError, match="different content"):
        workflows.import_history(workflow.id, valid.model_copy(update={
            "source": "forged source",
        }))

    second = create_workflow(isolated_db, slug="history-nonterminal")
    with pytest.raises(isolated_db.WorkflowConflictError, match="terminal"):
        workflows.import_history(second.id, WorkflowHistoryImport(
            expected_version=0,
            idempotency_key="bad-history",
            source="manual",
            rounds=[HistoricalRoundImport(round_no=1, status="reviewing")],
        ))


def test_worker_wrapper_reconciles_linked_task(monkeypatch, isolated_db):
    from promptpilot import worker

    workflow = create_workflow(isolated_db, slug="worker-hook")
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    dispatch(isolated_db, workflow.id, WorkflowRole.EXECUTOR, "execute")
    task = isolated_db.get_next_runnable()

    def fake_execute(current_task):
        isolated_db.mark_completed(current_task.id, "wrapped completion")
        isolated_db.set_verdict(current_task.id, "ГОТОВО")

    monkeypatch.setattr(worker, "_execute_task_inner", fake_execute)
    worker.execute_task(task)

    assert isolated_db.get_workflow(workflow.id).status.value == "gating"


def test_missing_workflow_verdict_never_reaches_gating(isolated_db):
    workflow = create_workflow(isolated_db, slug="missing-verdict")
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    result = dispatch(isolated_db, workflow.id, WorkflowRole.EXECUTOR, "execute")
    assert "promptpilot-workflow-contract" in result.task.prompt
    assert "ИТОГ: ГОТОВО" in result.task.prompt

    task = isolated_db.get_next_runnable()
    isolated_db.mark_completed(
        task.id, "Какую роль мне принять и что нужно сделать?", exit_code=0
    )
    workflows.sync_task(task.id)

    current = isolated_db.get_workflow(workflow.id)
    assert current.status.value == "awaiting_human"
    assert isolated_db.list_workflow_events(workflow.id)[-1].event_type == (
        "executor.invalid_output"
    )
