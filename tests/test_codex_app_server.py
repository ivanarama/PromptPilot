import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import bot, codex_app_server as app, codex_bridge as bridge, codex_telegram as tg
from promptpilot import db, herdr_exec


# A real subprocess and WebSocket handshake, with no daemon or model access.
SERVER = r'''
import json,sys
from websockets.server import ServerProtocol
from websockets.http11 import Request
from websockets.frames import Frame,Opcode
ws=ServerProtocol()
def flush():
    for data in ws.data_to_send():
        sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
def send(value):
    ws.send_text(json.dumps(value).encode());flush()
while True:
    block=sys.stdin.buffer.read1(65536)
    if not block:break
    ws.receive_data(block)
    for event in ws.events_received():
        if isinstance(event,Request):
            ws.send_response(ws.accept(event));flush()
        elif isinstance(event,Frame) and event.opcode is Opcode.TEXT:
            value=json.loads(event.data)
            if 'method' not in value:continue
            method=value['method']
            if method=='initialized':continue
            if method=='timeout':continue
            if method=='reject':send({'id':value['id'],'error':{'code':-1,'message':'rejected'}});continue
            if method=='question':
                send({'id':99,'method':'item/tool/requestUserInput','params':{'threadId':'t','questions':[]}})
            if method=='resolve':send({'method':'serverRequest/resolved','params':{'requestId':99}})
            result={'id':value['id'],'result':{'echo':value.get('params')}}
            if method=='fragment':
                data=json.dumps(result).encode();ws.send_text(data[:7],fin=False)
                ws.send_continuation(data[7:],fin=True);flush()
            else:send(result)
    flush()
'''


@pytest.fixture
def client():
    value = app.Client([sys.executable, "-u", "-c", SERVER], timeout=3)
    yield value
    value.close()


def test_pipe_websocket_roundtrip_and_fragmented_utf8(client):
    assert client.request("fragment", {"text": "Вопрос из Telegram"}) == {"echo": {"text": "Вопрос из Telegram"}}


def test_rpc_rejection_is_distinct_from_uncertain_submission(client):
    with pytest.raises(app.RPCError, match="rejected"):
        client.request("reject", {}, mutation=True)
    client.timeout = .05
    with pytest.raises(app.Unconfirmed):
        client.request("timeout", {}, mutation=True)


def test_replayed_question_and_resolved_notification(client):
    client.request("question", {})
    pending = client.questions("t")
    assert pending[0]["id"] == 99
    with pytest.raises(app.Unavailable):
        client.answer(99, {}, {"changed": True})
    client.request("resolve", {})
    assert client.questions("t") == []


def test_answer_cannot_be_sent_twice(client):
    client.request("question", {})
    question = client.questions("t")[0]
    client.answer(99, {}, question)
    with pytest.raises(app.Unavailable):
        client.answer(99, {}, question)


def test_completed_event_with_empty_items_retains_final_message(client):
    client._message({"method": "item/completed", "params": {
        "threadId": "t", "turnId": "one", "item": {
            "id": "final", "type": "agentMessage", "phase": "final_answer", "text": "Done"}}})
    client._message({"method": "turn/completed", "params": {
        "threadId": "t", "turn": {"id": "one", "status": "completed", "items": []}}})
    turn = client.live_turns("t")[0]
    assert turn["status"] == "completed" and bridge.final_text(turn) == "Done"


def test_request_replay_timestamp_does_not_change_question_identity(client):
    request = {"id": 42, "method": "item/tool/requestUserInput", "params": {"threadId": "t", "questions": []}}
    client._message({**request, "emittedAtMs": 100})
    first = client.questions("t")
    client._message({**request, "emittedAtMs": 200})
    assert client.questions("t") == first


def test_disabled_adapter_does_not_spawn_or_probe(monkeypatch):
    monkeypatch.setenv("PP_CODEX_APP_SERVER", "0")
    monkeypatch.setattr(app, "proxy_command", lambda *_: pytest.fail("disabled adapter spawned"))
    assert app.get_client() is None


class RPC:
    def __init__(self, threads=None, turns=None):
        self.threads = threads or [dict(id="t", cwd="D:/Projects/test", name="Task",
                                       status={"type": "idle"}, canAcceptDirectInput=True)]
        self.turns = turns or []
        self.calls = []
        self.lock = threading.RLock()
        self.subscriptions = set()

    def request(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        if method == "thread/list":return {"data": self.threads}
        if method == "thread/loaded/list":return {"data": [t["id"] for t in self.threads]}
        if method == "thread/read":return {"thread": next(t for t in self.threads if t["id"] == params["threadId"])}
        if method == "thread/turns/list":return {"data": self.turns}
        if method == "turn/start":return {"turn": {"id": "new-turn"}}
        return {}

    def subscribe(self, thread_id):
        self.calls.append(("subscribe", thread_id, {}))

    def questions(self, _):return []

    def live_turns(self, _):return []


def agent(**changes):
    return dict(agent="codex", cwd="D:\\Projects\\test", terminal_title_stripped="Task | test", **changes)


def test_unique_loaded_dialog_resolves_with_windows_path_normalization(monkeypatch):
    rpc = RPC()
    monkeypatch.setattr(app, "get_client", lambda *_: rpc)
    assert bridge.resolve(agent()).thread_id == "t"
    assert all(call[0] != "thread/list" for call in rpc.calls)


def test_same_folder_alone_cannot_route_a_new_window_to_an_old_dialog(monkeypatch):
    rpc = RPC()
    rpc.threads[0]["name"] = "A different task"
    monkeypatch.setattr(app, "get_client", lambda *_: rpc)
    assert bridge.resolve(agent()) is None
    rpc.threads[0]["name"] = "Task | a different dialog"
    assert bridge.resolve(agent()) is None


@pytest.mark.parametrize("change", [{"id": "other"}, {"status": {"type": "notLoaded"}}])
def test_ambiguous_or_unloaded_dialog_keeps_legacy_backend(monkeypatch, change):
    rpc = RPC()
    if "id" in change:rpc.threads.append({**rpc.threads[0], **change})
    else:rpc.threads[0].update(change)
    monkeypatch.setattr(app, "get_client", lambda *_: rpc)
    assert bridge.resolve(agent()) is None


def test_active_turn_is_steered_with_precondition_without_policy_overrides():
    rpc = RPC(turns=[{"id": "active-turn", "status": "inProgress", "items": []}])
    turn_id, steered = bridge.Dialog(rpc, "t").send("Continue")
    assert (turn_id, steered) == ("active-turn", True)
    method, params, options = rpc.calls[-1]
    assert method == "turn/steer" and params["expectedTurnId"] == turn_id
    assert options["mutation"] is True
    assert not set(params) & {"model", "approvalPolicy", "permissions", "sandboxPolicy"}


def test_closed_dialog_is_not_resumed():
    rpc = RPC()
    rpc.threads[0]["status"] = {"type": "notLoaded"}
    with pytest.raises(app.Unavailable):bridge.Dialog(rpc, "t").subscribe()
    assert all(call[0] != "subscribe" for call in rpc.calls)


def test_completed_turn_excludes_commentary_and_async_questions():
    turn = {"items": [
        {"type": "agentMessage", "phase": "commentary", "text": "Проверю…"},
        {"type": "agentMessage", "phase": "final_answer", "delivery": "async", "text": "Question"},
        {"type": "agentMessage", "phase": "final_answer", "questions": [{"title": "Another question"}], "text": "Another question"},
        {"type": "agentMessage", "phase": "final_answer", "text": "Готово\nPP-REPLY-END:" + "a" * 32}]}
    assert bridge.final_text(turn) == "Готово"


def test_async_question_identity_and_answered_envelope():
    identity = '["request_user_input_async","call_one",0]'
    question = {"type": "agentMessage", "id": "call_one", "questions": [{"title": "Куда?", "options": ["ТГ"]}]}
    rpc = RPC(turns=[{"id": "one", "items": [question]}])
    rpc.subscriptions.add("t")
    dialog = bridge.Dialog(rpc, "t")
    assert dialog.questions()[0]["id"] == identity
    payload = json.dumps([{"questionItemId": identity, "question": "Куда?", "answer": "ТГ"}])
    rpc.turns.insert(0, {"id": "two", "items": [{"type": "userMessage", "content": [
        {"type": "text", "text": "<send_user_message_question_reply>" + payload + "</send_user_message_question_reply>"}]}]})
    assert dialog.questions() == []


def test_turn_completion_and_outbox_are_atomic_and_replay_safe(isolated_db):
    record = bridge.tracked_turn(SimpleNamespace(thread_id="t"), "turn", "w1:p1", "", None, 123, None)
    key = tg.TURN_PREFIX + "one"
    db.set_setting(key, json.dumps(record))
    notes = [{"kind": "codex.done", "message": "result"}]
    assert db.finish_codex_tracking(key, "wrong", "wrong", "completed", notes) is False
    assert db.finish_codex_tracking(key, "turn", "result", "completed", notes)
    assert not db.finish_codex_tracking(key, "turn", "duplicate", "completed", notes)
    assert len(db.get_unsent_notifications()) == 1
    assert json.loads(db.get_setting(key))["result"] == "result"


def test_question_outbox_and_answer_claim_survive_restart(isolated_db):
    record = dict(chat_id=123, machine="", pane="w1:p1")
    assert db.queue_codex_question(tg.QUESTION_PREFIX + "same", record)
    assert not db.queue_codex_question(tg.QUESTION_PREFIX + "same", record)
    assert db.claim_codex_answer("same")
    assert not db.claim_codex_answer("same")
    assert len(db.get_unsent_notifications()) == 1


def test_still_pending_question_is_redelivered_after_long_bot_downtime(isolated_db):
    record = dict(chat_id=123, machine="", pane="w1:p1", question={"id": "same"}, expires_at=0)
    key = tg.QUESTION_PREFIX + "old-question"
    assert db.queue_codex_question(key, record)
    first = db.get_unsent_notifications()[0]
    db.mark_notification_sent(first["id"])
    record["expires_at"] = 9999999999
    assert db.queue_codex_question(key, record)
    assert len(db.get_unsent_notifications()) == 1
    assert not db.queue_codex_question(key, record)
    assert len(db.get_unsent_notifications()) == 1


def test_exact_turn_result_does_not_depend_on_terminal_done(monkeypatch):
    turns = iter([
        {"id": "current", "status": "inProgress", "items": []},
        {"id": "current", "status": "completed", "items": [
            {"type": "agentMessage", "phase": "final_answer", "text": "Actual final answer"}]}])
    rpc = RPC()
    observer = SimpleNamespace(poll=lambda: next(turns), dialog=bridge.Dialog(rpc, "t"))
    monkeypatch.setattr(herdr_exec, "_run", lambda *_args, **_kwargs: pytest.fail("read terminal instead of exact turn"))
    monkeypatch.setattr(herdr_exec.time, "sleep", lambda _: None)
    status, result = herdr_exec._wait_codex_reply("pane", "prompt", "marker", None, None, observer=observer)
    assert status == "done" and isinstance(result, bridge.VerifiedReply)
    assert result == "Actual final answer"


def test_transport_loss_never_falls_back_to_duplicate_prompt(isolated_db, monkeypatch):
    user = SimpleNamespace(id=123)
    message = SimpleNamespace(text="Do the task", reply_text=AsyncMock())
    event = SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=123, type="private"), message=message)
    context = SimpleNamespace(user_data={"herdr_reply_pane": "w1:p1"})
    monkeypatch.setattr(bot, "_check_herdr_reply_access", AsyncMock(return_value=True))
    monkeypatch.setattr(bot, "_herdr_json", AsyncMock(return_value={"result": {"agent": agent()}}))
    monkeypatch.setattr(bot.result_files, "native_context", lambda *_: None)
    class Lost:
        def send(self, text):raise app.Unconfirmed("Lost after write")
    monkeypatch.setattr(bridge, "resolve", lambda *_: Lost())
    legacy = AsyncMock()
    monkeypatch.setattr(bot, "_submit_native_reply", legacy)
    asyncio.run(bot.herdr_reply_got_text(event, context))
    legacy.assert_not_called()
    assert "мог получить" in message.reply_text.call_args.args[0]


def test_background_completion_uses_exact_turn_and_survives_restart(isolated_db, monkeypatch):
    rpc = RPC(turns=[{"id": "current", "status": "inProgress", "items": [
        {"type": "agentMessage", "phase": "commentary", "text": "Проверю…"}]}])
    monkeypatch.setattr(app, "get_client", lambda *_: rpc)
    monkeypatch.setattr(bot, "list_authorized", lambda: {})
    dialog = bridge.Dialog(rpc, "t")
    key = tg.track(dialog, "current", "w1:p1", "", None, 123)
    asyncio.run(tg.watch_tick())
    assert db.get_unsent_notifications() == []
    rpc.turns[0].update(status="completed", items=[
        {"type": "agentMessage", "phase": "final_answer", "text": "Проверка завершена"}])
    asyncio.run(tg.watch_tick())
    asyncio.run(tg.watch_tick())
    assert json.loads(db.get_setting(key))["result"] == "Проверка завершена"
    notes = db.get_unsent_notifications()
    assert len(notes) == 1 and notes[0]["kind"] == "codex.done"


def test_question_auth_rechecked_after_asynchronous_lookup(isolated_db, monkeypatch):
    query = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    event = SimpleNamespace(message=None, callback_query=query, effective_chat=SimpleNamespace(id=123))
    record = {"question": {"kind": "async", "options": ["Yes"]}}
    dialog = SimpleNamespace(answer=lambda *_: pytest.fail("revoked user answered"))
    monkeypatch.setattr(tg, "current_question", AsyncMock(return_value=(record, dialog, "", "w1:p1", None)))
    monkeypatch.setattr(bot, "_check_herdr_reply_access", AsyncMock(return_value=False))
    asyncio.run(tg.answer(event, SimpleNamespace(user_data={}), "token", option=0))


def test_explicit_answer_is_claimed_across_duplicate_cards(isolated_db, monkeypatch):
    replies = []
    class Dialog:
        thread_id = "t"
        def answer(self, *args):
            replies.append(args)
            return ("next-turn", False)
    dialog = Dialog()
    question = {"kind": "async", "id": "question", "text": "Куда?", "options": ["Telegram"]}
    record = {"thread_id": "t", "endpoint": None, "question": question, "chat_id": 123}
    query = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    event = SimpleNamespace(message=None, callback_query=query, effective_chat=SimpleNamespace(id=123))
    monkeypatch.setattr(tg, "current_question", AsyncMock(return_value=(record, dialog, "", "w1:p1", None)))
    monkeypatch.setattr(bot, "_check_herdr_reply_access", AsyncMock(return_value=True))
    asyncio.run(tg.answer(event, SimpleNamespace(user_data={}), "first-card", option=0))
    asyncio.run(tg.answer(event, SimpleNamespace(user_data={}), "second-card", option=0))
    assert len(replies) == 1
