"""Completion cursors and their durable notification outbox survive bot restarts."""
import asyncio

import pytest

from promptpilot import bot, db


def install_watch(monkeypatch, agents):
    async def listing(*args, **kwargs): return {"result": {"agents": agents}}
    async def unexpected_notify(*args, **kwargs):
        raise AssertionError("Sequenced completions must use the durable outbox")
    monkeypatch.setattr(bot, "_herdr_watch_targets", lambda: [("", None)])
    monkeypatch.setattr(bot, "_herdr_json", listing)
    monkeypatch.setattr(bot, "_herdr_notify", unexpected_notify)
    monkeypatch.setattr(bot, "list_authorized", lambda: {1: {}})


def poll(notified=None, initialized=None):
    return asyncio.run(bot._herdr_watch_tick(None, notified if notified is not None else {},
                                           initialized if initialized is not None else set()))


def test_watch_skips_existing_done_agents_but_reports_new_completion(isolated_db, monkeypatch):
    agents = [{"pane_id": "w1:pA", "name": "t15", "agent_status": "done", "completion_seq": 49}]
    install_watch(monkeypatch, agents)
    notified, initialized = {}, set()
    poll(notified, initialized)
    assert db.get_unsent_notifications() == []
    agents[0]["agent_status"] = "working"
    poll(notified, initialized)
    agents[0].update(agent_status="done", completion_seq=60)
    poll(notified, initialized)
    assert len(db.get_unsent_notifications()) == 1
    poll(notified, initialized)
    poll()  # New bot process state; durable cursor still suppresses the replay.
    assert len(db.get_unsent_notifications()) == 1


def test_completion_while_bot_was_down_is_queued_after_restart(isolated_db, monkeypatch):
    agents = [{"pane_id": "w1:pA", "name": "t15", "agent_status": "done", "completion_seq": 1}]
    install_watch(monkeypatch, agents)
    poll()
    agents[0]["completion_seq"] = 2
    poll()  # Both in-memory maps were lost in the restart.
    pending = db.get_unsent_notifications()
    assert len(pending) == 1 and "w1:pA" in pending[0]["message"]
    assert pending[0]["kind"] == "herdr.done" and pending[0]["pane_id"] == "w1:pA"
    poll()
    assert len(db.get_unsent_notifications()) == 1


def test_zero_sequence_does_not_fall_back_to_state_change(isolated_db, monkeypatch):
    agent = {"pane_id": "w1:pA", "name": "t15", "agent_status": "done", "completion_seq": 0, "state_change_seq": 5}
    install_watch(monkeypatch, [agent])
    poll()
    assert bot._herdr_completion_sequence(agent) == 0
    agent["state_change_seq"] = 999
    poll()
    assert db.get_unsent_notifications() == []
    agent["completion_seq"] = None
    poll()
    assert len(db.get_unsent_notifications()) == 1


def test_cursor_and_all_recipient_messages_roll_back_together(isolated_db, monkeypatch):
    def completion(sequence):
        return [{"pane": "w1:p1", "name": "agent", "sequence": sequence, "message": "Completed"}]
    db.queue_herdr_completions("machine", completion(0), [1, 2])
    real_now, calls = db._now, []
    def failing_now():
        calls.append(True)
        if len(calls) == 2: raise OSError("Simulated crash during enqueue")
        return real_now()
    monkeypatch.setattr(db, "_now", failing_now)
    with pytest.raises(OSError):
        db.queue_herdr_completions("machine", completion(1), [1, 2])
    assert db.get_unsent_notifications() == []
    monkeypatch.setattr(db, "_now", real_now)
    assert db.queue_herdr_completions("machine", completion(1), [1, 2]) == 2
    assert db.queue_herdr_completions("machine", completion(1), [1, 2]) == 0
    assert [row["tg_chat_id"] for row in db.get_unsent_notifications()] == [1, 2]


def test_failed_poll_cannot_consume_first_successful_baseline(isolated_db, monkeypatch):
    async def unavailable(*args, **kwargs): return None
    monkeypatch.setattr(bot, "_herdr_watch_targets", lambda: [("", None)])
    monkeypatch.setattr(bot, "_herdr_json", unavailable)
    poll()
    agents = [{"pane_id": "w1:pA", "name": "t15", "agent_status": "done", "completion_seq": 1}]
    install_watch(monkeypatch, agents)
    poll()
    assert db.get_unsent_notifications() == []
    agents.append({"pane_id": "w2:pA", "name": "new", "agent_status": "done", "completion_seq": 2})
    poll()
    assert len(db.get_unsent_notifications()) == 1


def test_cursor_namespace_includes_machine_endpoint(isolated_db):
    assert bot._herdr_watch_source("build", "first-host") != bot._herdr_watch_source("build", "second-host")
    assert bot._herdr_watch_source("build", "first-host") != bot._herdr_watch_source("other", "first-host")


def test_durable_completion_keeps_screen_and_reply_buttons():
    buttons = bot._herdr_done_keyboard("build:w1:p2")
    callbacks = [button.callback_data for row in buttons for button in row]
    assert callbacks == ["hd_screen:build:w1:p2", "hd_reply:build:w1:p2"]


def test_reused_pane_new_terminal_is_new_completion_but_rename_is_not(isolated_db, monkeypatch):
    agents = [{"pane_id": "w1:pA", "terminal_id": "terminal-old", "name": "worker",
               "agent_status": "done", "completion_seq": 0}]
    install_watch(monkeypatch, agents)
    poll()
    agents.clear()
    poll()
    agents.append({"pane_id": "w1:pA", "terminal_id": "terminal-new", "name": "worker",
                   "agent_status": "done", "completion_seq": 0})
    poll()
    assert len(db.get_unsent_notifications()) == 1
    agents[0]["name"] = "renamed"
    poll()
    assert len(db.get_unsent_notifications()) == 1
    agents[0]["pane_id"] = "w2:pB"
    poll()
    assert len(db.get_unsent_notifications()) == 1  # Layout move is not new work.


def test_session_fallback_and_observed_legacy_absence(isolated_db, monkeypatch):
    agent = {"pane_id": "w1:pA", "name": "worker", "agent_status": "done", "completion_seq": 0,
             "agent_session": {"source": "native", "agent": "claude", "kind": "id", "value": "first"}}
    agents = [agent]
    install_watch(monkeypatch, agents)
    poll()
    agent["agent_session"]["value"] = "second"
    poll()
    assert len(db.get_unsent_notifications()) == 1
    agents.clear()
    poll()
    legacy = {"pane_id": "w2:pA", "name": "old", "agent_status": "done", "completion_seq": 0}
    agents.append(legacy)
    poll()
    assert len(db.get_unsent_notifications()) == 2
    legacy["name"] = "renamed"
    poll()
    assert len(db.get_unsent_notifications()) == 2
    agents.clear()
    poll()
    agents.append(dict(legacy))
    poll()
    assert len(db.get_unsent_notifications()) == 3
