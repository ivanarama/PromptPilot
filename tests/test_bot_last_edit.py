import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from promptpilot import bot


@pytest.mark.parametrize("error", [
    None,
    BadRequest("Query is too old and response timeout expired or query id is invalid"),
    NetworkError("Bad Gateway"),
    asyncio.TimeoutError(),
])
def test_reconfigure_opens_picker_after_callback_ack_failure(monkeypatch, error):
    monkeypatch.setattr(bot, "load_machines", lambda: {})
    monkeypatch.setattr(bot, "_provider_buttons", lambda: [])
    query = SimpleNamespace(answer=AsyncMock(side_effect=error), edit_message_text=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={"new_prompt": "Check Onebase", "last_settings": {}})

    assert asyncio.run(bot.cb_last_edit(update, context)) == bot.ASK_PROVIDER
    query.edit_message_text.assert_awaited_once()
    assert query.edit_message_text.call_args.args == ("Выберите провайдера:",)
    assert context.user_data == {"new_prompt": "Check Onebase"}
