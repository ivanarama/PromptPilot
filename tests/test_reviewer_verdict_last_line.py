# -*- coding: utf-8 -*-
"""P24: парсер берёт ПОСЛЕДНЮЮ строку AUDIT_VERDICT.

Отчёты ревью цитируют прошлые аудиты и вывод гейта («гейт PASS на …»,
{{previous_review}} с их AUDIT_VERDICT) — первый матч может быть ЦИТАТОЙ.
Инциденты 2026-10-04: каскад видел ложный «PASS + открытый blocker» и
открывал человеческий блокер, хотя финальный вердикт был REVISION_REQUIRED."""
from promptpilot import workflows as W
import pytest


REPORT_QUOTED_PASS = (
    "Сводка: предыдущий раунд\n"
    "AUDIT_FINDINGS_JSON: []\n"
    "AUDIT_VERDICT: PASS\n"          # цитата прошлого аудита (embedded context)
    "\n... мой новый аудит ...\n"
    'AUDIT_FINDINGS_JSON: [{"fingerprint":"F-1","severity":"medium",'
    '"category":"contract","title":"расхождение","status":"open",'
    '"payload":{"path":"API_V1.md","line":10,"reproduction":"x","expected":"y"}}]\n'
    "AUDIT_VERDICT: REVISION_REQUIRED"
)


def test_last_verdict_line_wins_over_quoted_earlier_one():
    d = W.parse_reviewer_report(REPORT_QUOTED_PASS)
    assert d is not None
    assert d.verdict.value == "REVISION_REQUIRED"
    assert len(d.findings) == 1


def test_single_verdict_still_parses():
    d = W.parse_reviewer_report(
        "Аудит.\nAUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS")
    assert d is not None and d.verdict.value == "PASS"


def test_last_findings_block_wins_too():
    # цитата с пустыми находками раньше — финальные находки не теряются
    d = W.parse_reviewer_report(REPORT_QUOTED_PASS)
    assert d.findings[0].fingerprint == "F-1"


@pytest.mark.parametrize("tail", [
    "AUDIT_FINDINGS_JSON: not-json\nAUDIT_VERDICT: PASS",
    "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: INVALID",
    "AUDIT_FINDINGS_JSON: {}\nAUDIT_VERDICT: PASS",
    "AUDIT_VERDICT: PASS\nAUDIT_FINDINGS_JSON: []",
    "AUDIT_VERDICT: PASS",  # do not pair this with a quoted older findings block
])
def test_invalid_final_contract_never_revives_quoted_pass(tail):
    report = "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\n\nNew audit:\n" + tail
    assert W.parse_reviewer_report(report) is None


def test_invalid_final_contract_stops_real_workflow_transition(isolated_db, tmp_path):
    from promptpilot.models import WorkflowCreate, WorkflowStartRequest
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="invalid-final-contract", objective="x", repository_path=str(tmp_path),
        candidate_branch="main", config={"automation": {"enabled": True}, "gate": {"enabled": False}}))
    W.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    W.advance_workflow(workflow.id)
    def finish(result):
        task = isolated_db.get_next_runnable()
        assert task is not None
        W.sync_task(task.id)
        isolated_db.mark_completed(task.id, result, exit_code=0)
        isolated_db.set_verdict(task.id, "ГОТОВО")
        W.sync_task(task.id)
        W.advance_linked_task(task.id)
    finish("executor complete")
    finish("AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\n\nNew audit:\nAUDIT_FINDINGS_JSON: not-json\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО")
    assert isolated_db.get_workflow(workflow.id).status.value == "awaiting_human"
    assert not any(e.event_type == "review.passed" for e in isolated_db.list_workflow_events(workflow.id))
