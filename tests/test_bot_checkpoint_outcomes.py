"""A failed resume is reported through the real VK update dispatcher."""
from promptpilot import bot_vk
from test_checkpoint_publication import _pending
from test_bot_vk import _FakeTransport


def test_vk_failed_resume_reports_current_checkpoint_stage_and_latest_reason(isolated_db, tmp_path, monkeypatch):
    from promptpilot import workflows
    from promptpilot.models import WorkflowEventCreate
    _, wf = _pending(isolated_db, tmp_path, monkeypatch)
    isolated_db.append_workflow_event(WorkflowEventCreate(
        workflow_id=wf.id, event_type="workflow.awaiting_human",
        idempotency_key="old-question", payload={"reason": "older blocker"}))
    monkeypatch.setattr(workflows, "_stage_checkpoint_commit", lambda *args, **kwargs: False)
    monkeypatch.setattr(bot_vk, "_allowed_ids", lambda: {7})
    transport = _FakeTransport()
    bot_vk._process_update(transport, {"type": "message_new", "object": {"message": {
        "from_id": 7, "peer_id": 7, "text": "резюм retry checkpoint"}}}, {})
    reply = transport.sent[-1][1]
    assert "Продолжение не выполнено" in reply and "✅" not in reply
    assert "стадия S1" in reply and "стадия S2" not in reply
    assert "checkpoint preparation failed" in reply and "older blocker" not in reply
    assert "Задание исполнителю" not in reply
    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"
    from promptpilot.models import WorkflowRole
    assert any("retry checkpoint" in note for note in
               workflows._operator_notes(wf.id, WorkflowRole.EXECUTOR))


def test_vk_resume_finds_waiting_mission_beyond_latest_fifty(isolated_db, tmp_path):
    from promptpilot.models import WorkflowCreate
    waiting = isolated_db.create_workflow(WorkflowCreate(
        slug="old-waiting", objective="goal", repository_path=str(tmp_path), candidate_branch="b"))
    with isolated_db._connect() as conn:
        conn.execute("UPDATE workflows SET status='awaiting_human' WHERE id=?", (waiting.id,))
    for number in range(55):
        isolated_db.create_workflow(WorkflowCreate(
            slug=f"newer-{number}", objective="goal", repository_path=str(tmp_path), candidate_branch="b"))
    assert [workflow.id for workflow in bot_vk._awaiting_workflows()] == [waiting.id]
