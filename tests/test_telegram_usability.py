import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import bot, herdr_exec
from promptpilot.models import TaskCreate


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def event(data):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(reply_document=AsyncMock(), reply_text=AsyncMock()))
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=123),
                           effective_chat=SimpleNamespace(id=123, type="private"))


def test_question_action_survives_long_terminal_footer(monkeypatch):
    raw = "Working\nQueued follow-up inputs\n1 question · 9s\nshift+↑ to answer\n" + "footer\n" * 30
    monkeypatch.setattr(bot, "_herdr_cli", AsyncMock(return_value=raw))
    tail = asyncio.run(bot._herdr_screen_tail("w1:p1"))
    assert bot._codex_question_pending(tail)
    assert "1 question" in tail


def test_question_summary_does_not_depend_on_arrow_glyph():
    assert bot._codex_question_pending("Queued follow-up inputs\n1 question · 9s\nshift+↟ to answer")
    assert bot._codex_question_pending("• Queued follow-up inputs\n? 1 question · 9s\n  shift+↟ to answer")
    assert not bot._codex_question_pending("There is 1 question about this implementation")
    assert not bot._codex_question_pending("code = 'Queued follow-up inputs\\n1 question\\nshift+↑ to answer'")
    dialog = "Куда отправить результат?\n› 1. Telegram\n  2. Email\nEnter to select"
    preview = bot._question_text("old transcript\n" * 500 + dialog)
    assert "Куда отправить результат?" in preview and "2. Email" in preview
    assert len(preview) <= 3500


def test_window_offers_question_and_output_file(monkeypatch):
    monkeypatch.setattr(bot, "_herdr_json", AsyncMock(return_value={"result": {"agent": {
        "agent": "codex", "agent_status": "blocked"}}}))
    monkeypatch.setattr(bot, "_herdr_screen_tail", AsyncMock(return_value=
                        "Queued follow-up inputs\n1 question\nshift+↑ to answer"))
    context = SimpleNamespace(user_data={"win_cache": [{
        "pane_id": "w1:p1", "machine": "", "status": "blocked", "title": "", "cwd": "",
        "session": None, "ws": "", "name": ""}]})
    query = event("win_i:0").callback_query
    asyncio.run(bot._render_window_card(query, context, 0))
    args = query.edit_message_text.call_args
    keys = callbacks(args.kwargs["reply_markup"])
    assert "hd_question::w1:p1" in keys
    assert "hd_output::w1:p1" in keys
    assert not any(k.startswith("hd_enter:") for k in keys)


def test_native_window_output_is_delivered_as_utf8_file(monkeypatch):
    monkeypatch.setattr(bot, "is_authorized", lambda uid: True)
    monkeypatch.setattr(bot, "load_machines", lambda: {})
    screen = AsyncMock(return_value="Полный доступный вывод\n" + "line\n" * 100)
    monkeypatch.setattr(bot, "_herdr_cli", screen)
    update = event("hd_output::w1:p1")
    asyncio.run(bot.cb_herdr_output(update, SimpleNamespace(user_data={})))
    screen.assert_awaited_once_with("agent", "read", "w1:p1", "--source", "recent-unwrapped",
                                   "--lines", "1000", "--format", "text", host=None, timeout=30)
    document = update.callback_query.message.reply_document.call_args.kwargs["document"]
    assert document.read().decode("utf-8").startswith("Полный доступный вывод\n")
    assert document.name == "herdr_w1_p1.txt"


def test_short_result_can_be_downloaded_from_task_and_notification(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="Check", tg_chat_id=123))
    isolated_db.mark_completed(task.id, "Готово")
    assert f"full_result:{task.id}" in callbacks(bot._task_detail_keyboard(isolated_db.get_task(task.id)))
    ticks = []
    async def sleep(_):
        if ticks:
            raise asyncio.CancelledError()
        ticks.append(True)
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    monkeypatch.setattr(bot, "_epf_maybe_deliver", AsyncMock())
    client = SimpleNamespace(send_message=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._notify_loop(client))
    assert f"full_result:{task.id}" in callbacks(client.send_message.call_args.kwargs["reply_markup"])


@pytest.mark.parametrize("target", [None, "w1:p1"])
def test_codex_commentary_and_transient_done_do_not_complete_task(monkeypatch, target):
    submitted = []
    reads = []
    phases = iter([("idle", "Проверю тестовый бот и Mini App"),
                   ("working", "Проверка ещё выполняется"),
                   ("done", "Готово")])
    current = ["idle", ""]

    def fake_run(args, host=None, timeout=None):
        if args[:2] == ["tab", "create"]:
            return 0, {"result": {"root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}}}, ""
        if args[:2] == ["agent", "start"]:
            return 0, {}, ""
        if args[:2] == ["agent", "get"]:
            if submitted:
                current[:] = next(phases)
            return 0, {"result": {"agent": {"agent": "codex", "agent_status": current[0], "pane_id": "w1:p1"}}}, ""
        if args[:2] == ["agent", "prompt"]:
            submitted.append(args[3])
            # Reproduce #96: herdr claims the turn is done at its opening commentary.
            return 0, {"result": {"agent": {"agent_status": "done"}}}, ""
        if args[:2] == ["agent", "read"]:
            prompt = submitted[0]
            text = current[1]
            if text == "Готово":
                text += "\n" + re.search(r"PP-REPLY-END:[0-9a-f]{32}", prompt)[0]
            reads.append(text)
            return 0, None, "> " + prompt.replace("\n", "\n  ") + "\n" + text
        raise AssertionError(args)

    monkeypatch.setattr(herdr_exec, "_run", fake_run)
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda host: None)
    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", lambda *args: None)
    monkeypatch.setattr(herdr_exec, "_close_owned_session", lambda *args, **kwargs: "")
    monkeypatch.setattr(herdr_exec.time, "sleep", lambda seconds: None)
    task = SimpleNamespace(id=96, herdr_target=target, detached=False, model=None,
                           effort=None, session_id=None, skip_permissions=False,
                           working_dir=".", worktree=False)
    result = herdr_exec.run_in_herdr(task, {"kind": "codex"} if target is None else {},
                                    prompt_override="Проверь тестовый бот", timeout=60)
    assert result["ok"] is True
    assert result["output"].startswith("Готово\n")
    assert "Проверю тестовый бот" not in result["output"]
    assert "PP-REPLY-END:" not in result["output"]
    assert len(submitted) == 1 and len(reads) == 4


def test_missing_final_marker_waits_until_timeout(monkeypatch):
    prompt = herdr_exec._with_turn_marker("Task")
    monkeypatch.setattr(herdr_exec, "_run", lambda *args, **kwargs:
                        (0, {"result": {"agent": {"agent_status": "done"}}}, ""))
    monkeypatch.setattr(herdr_exec, "_read_turn_screen", lambda *args:
                        (0, None, "> " + prompt + "\nI will check that"))
    clock = iter([0, 1, 11])
    monkeypatch.setattr(herdr_exec.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(herdr_exec.time, "sleep", lambda seconds: None)
    state, _ = herdr_exec._wait_codex_reply("pane", prompt, "PP-REPLY-END:current", 10, None)
    assert state == "__timeout__"


def test_cancel_during_final_reply_wait_does_not_touch_terminal(monkeypatch):
    monkeypatch.setattr(herdr_exec, "_run", lambda *args, **kwargs: pytest.fail("cancelled wait issued CLI call"))
    assert herdr_exec._wait_codex_reply("pane", "prompt", "marker", None, lambda: True)[0] == "__cancel__"


def test_unconfirmed_idle_without_task_timeout_does_not_wait_forever(monkeypatch):
    prompt = herdr_exec._with_turn_marker("Task")
    monkeypatch.setattr(herdr_exec, "_run", lambda *args, **kwargs:
                        (0, {"result": {"agent": {"agent_status": "idle"}}}, ""))
    monkeypatch.setattr(herdr_exec, "_read_turn_screen", lambda *args:
                        (0, None, "> " + prompt + "\nПроверю тестовый бот"))
    clock = iter([0, 31])
    monkeypatch.setattr(herdr_exec.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(herdr_exec.time, "sleep", lambda seconds: None)
    state, reason = herdr_exec._wait_codex_reply("pane", prompt, "marker", None, None)
    assert state == "__error__" and "unconfirmed" in reason


def test_final_reply_wait_excludes_human_time_and_notifies_once(monkeypatch):
    prompt = herdr_exec._with_turn_marker("Task")
    states = iter(["blocked", "blocked", "working", "done"])
    monkeypatch.setattr(herdr_exec, "_run", lambda *args, **kwargs:
                        (0, {"result": {"agent": {"agent_status": next(states), "pane_id": "w1:p1"}}}, ""))
    monkeypatch.setattr(herdr_exec, "_read_turn_screen", lambda *args:
                        (0, None, "> " + prompt + "\nResult\nmarker"))
    clock = iter([0, 100, 200, 201])
    monkeypatch.setattr(herdr_exec.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(herdr_exec.time, "sleep", lambda seconds: None)
    notifications = []
    state, _ = herdr_exec._wait_codex_reply("pane", prompt, "marker", 10, None,
                                           on_blocked=notifications.append)
    assert state == "done" and notifications == ["w1:p1"]


def test_output_export_rejects_group_chat_before_reading_terminal(monkeypatch):
    monkeypatch.setattr(bot, "is_authorized", lambda uid: True)
    screen = AsyncMock()
    monkeypatch.setattr(bot, "_herdr_cli", screen)
    update = event("hd_output::w1:p1")
    update.effective_chat.type = "group"
    asyncio.run(bot.cb_herdr_output(update, SimpleNamespace(user_data={})))
    screen.assert_not_called()
    update.callback_query.message.reply_document.assert_not_called()
