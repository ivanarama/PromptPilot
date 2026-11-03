import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import bot


PENDING = 'Queued follow-up inputs\n1 question\nshift+↑ to answer'
QUESTION = 'Choose a destination\n› 1. Telegram\n  2. Email\nEnter to select'


@pytest.fixture
def dialog(monkeypatch):
    monkeypatch.setattr(bot, 'is_authorized', lambda uid: uid == 123)
    monkeypatch.setattr(bot, 'load_machines', lambda: {})
    query = SimpleNamespace(data='hd_question::w1:p1', answer=AsyncMock(),
                            message=SimpleNamespace(reply_text=AsyncMock()))
    event = SimpleNamespace(effective_user=SimpleNamespace(id=123),
                            effective_chat=SimpleNamespace(id=123, type='private'), callback_query=query)
    context = SimpleNamespace(user_data={})
    rpc = AsyncMock(return_value={'result': True})
    screen = AsyncMock(side_effect=[PENDING, QUESTION])
    monkeypatch.setattr(bot, '_herdr_json', rpc)
    monkeypatch.setattr(bot, '_herdr_cli', screen)
    return event, context, rpc, screen


def test_question_summary_is_not_a_permission_dialog():
    assert bot._codex_question_pending(PENDING)
    assert not bot._codex_question_pending('Allow this command?\n1. Yes\n2. No')
    assert not bot._codex_question_pending('0 questions\nshift+↑ to answer')


def test_open_question_reveals_text_without_choosing_an_answer(dialog):
    event, context, rpc, _ = dialog
    asyncio.run(bot.cb_herdr_question(event, context))
    rpc.assert_awaited_once_with('agent', 'send-keys', 'w1:p1', 'shift+up', host=None)
    args = event.callback_query.message.reply_text.call_args
    assert 'Choose a destination' in args.args[0]
    labels = [b.text for row in args.kwargs['reply_markup'].inline_keyboard for b in row]
    assert '✓ Выбрать' in labels and '↓ Следующий' in labels
    assert ':w1:p1' in context.user_data['herdr_question_views']


def test_question_that_disappeared_is_not_opened(dialog):
    event, context, rpc, screen = dialog
    screen.side_effect = None
    screen.return_value = 'Working'
    asyncio.run(bot.cb_herdr_question(event, context))
    rpc.assert_not_called()


def test_old_question_buttons_do_not_send_keys_to_changed_screen(dialog):
    event, context, rpc, screen = dialog
    asyncio.run(bot.cb_herdr_question(event, context))
    rpc.reset_mock()
    screen.side_effect = None
    screen.return_value = 'A different question'
    event.callback_query.data = 'hd_qkey:enter::w1:p1'
    asyncio.run(bot.cb_herdr_question_key(event, context))
    rpc.assert_not_called()


def test_question_choice_is_explicit_and_cannot_be_replayed(dialog):
    event, context, rpc, screen = dialog
    asyncio.run(bot.cb_herdr_question(event, context))
    rpc.reset_mock()
    screen.side_effect = None
    screen.return_value = QUESTION
    event.callback_query.data = 'hd_qkey:enter::w1:p1'
    asyncio.run(bot.cb_herdr_question_key(event, context))
    rpc.assert_awaited_once_with('agent', 'send-keys', 'w1:p1', 'enter', host=None)
    asyncio.run(bot.cb_herdr_question_key(event, context))
    assert rpc.await_count == 1


def test_unauthorized_question_button_has_no_terminal_effect(dialog):
    event, context, rpc, screen = dialog
    event.effective_chat.type = 'group'
    asyncio.run(bot.cb_herdr_question(event, context))
    rpc.assert_not_called()
    screen.assert_not_called()


def test_question_card_replaces_opaque_enter_buttons(dialog):
    event, _, _, _ = dialog
    client = SimpleNamespace(send_message=AsyncMock())
    agent = {'agent': 'codex'}
    original = bot.list_authorized
    try:
        bot.list_authorized = lambda: {123: {}}
        asyncio.run(bot._herdr_notify(client, 'w1:p1', 'blocked', agent, tail=PENDING))
    finally:
        bot.list_authorized = original
    rows = client.send_message.call_args.kwargs['reply_markup'].inline_keyboard
    assert rows[0][0].text == '❓ Открыть вопрос'
    assert all('hd_enter:' not in button.callback_data for row in rows for button in row)
