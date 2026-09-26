import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from promptpilot import bot


def _callback_update(data: str, edit_message_text=None):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=edit_message_text or AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(type="private"),
        callback_query=query,
        message=None,
    )


def test_report_command_offers_configured_profiles(monkeypatch):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(type="private"),
        message=message,
    )
    monkeypatch.setattr(bot, "is_authorized", lambda _user_id: True)
    monkeypatch.setattr(bot.pipeline_insights, "list_profiles", lambda: [
        {"id": "onebase", "title": "OneBase", "repository": "owner/onebase"},
        {"id": "tools", "title": "Tools", "repository": "owner/tools"},
    ])

    asyncio.run(bot.show_pipeline_report(update, None))

    call = message.reply_text.await_args
    assert "Выберите проект" in call.args[0]
    buttons = call.kwargs["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in buttons] == [
        "report:profile:onebase", "report:profile:tools",
    ]


def test_report_profile_offers_all_supported_periods(monkeypatch):
    monkeypatch.setattr(bot, "is_authorized", lambda _user_id: True)
    monkeypatch.setattr(bot.pipeline_insights, "list_profiles", lambda: [
        {"id": "onebase", "title": "OneBase", "repository": "owner/onebase"},
    ])
    update = _callback_update("report:profile:onebase")

    asyncio.run(bot.cb_report_profile(update, None))

    update.callback_query.answer.assert_awaited_once()
    call = update.callback_query.edit_message_text.await_args
    assert "OneBase" in call.args[0]
    buttons = call.kwargs["reply_markup"].inline_keyboard[0]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("24 часа", "report:period:24:onebase"),
        ("7 дней", "report:period:168:onebase"),
        ("30 дней", "report:period:720:onebase"),
    ]


def test_report_period_uses_local_history_without_spending_github_budget(monkeypatch):
    calls = []
    series = [{"id": 7}]
    report = {
        "profile_id": "onebase",
        "delivery": {"status": "budget_denied"},
    }
    monkeypatch.setattr(bot, "is_authorized", lambda _user_id: True)
    monkeypatch.setattr(bot.pipeline_insights, "list_profiles", lambda: [
        {"id": "onebase", "title": "OneBase", "repository": "owner/onebase"},
    ])
    monkeypatch.setattr(bot.db, "list_series", lambda: series)

    def build(profile_id, actual_series, *, hours, refresh_delivery):
        calls.append((profile_id, actual_series, hours, refresh_delivery))
        return report

    monkeypatch.setattr(
        bot.pipeline_insights, "build_period_report", build, raising=False)
    monkeypatch.setattr(
        bot.pipeline_insights,
        "render_period_report_markdown",
        lambda actual: (
            "*OneBase — 24 часа*\n"
            "Доставка: обновление отложено из-за резерва GitHub API\n"
            "• [#42](https://github.com/owner/onebase/pull/42) продвинулся"
        ) if actual is report else "",
        raising=False,
    )
    update = _callback_update("report:period:24:onebase")

    asyncio.run(bot.cb_report_period(update, None))

    assert calls == [("onebase", series, 24, False)]
    assert update.callback_query.edit_message_text.await_count == 2
    rendered = update.callback_query.edit_message_text.await_args
    assert "обновление отложено" in rendered.args[0]
    assert "https://github.com/owner/onebase/pull/42" in rendered.args[0]
    assert rendered.kwargs["parse_mode"] == "Markdown"
    buttons = rendered.kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data == "report:refresh:24:onebase"


def test_report_refresh_explicitly_requests_delivery_and_keeps_budget_denial(
        monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "is_authorized", lambda _user_id: True)
    monkeypatch.setattr(bot.pipeline_insights, "list_profiles", lambda: [
        {"id": "onebase", "title": "OneBase", "repository": "owner/onebase"},
    ])
    monkeypatch.setattr(bot.db, "list_series", lambda: [])

    def build(profile_id, series, *, hours, refresh_delivery):
        calls.append((profile_id, series, hours, refresh_delivery))
        return {"delivery": {"status": "budget_denied"}}

    monkeypatch.setattr(
        bot.pipeline_insights, "build_period_report", build, raising=False)
    monkeypatch.setattr(
        bot.pipeline_insights,
        "render_period_report_markdown",
        lambda _report: "Доставка: GitHub API пока не опрашивался — бюджет сохранён.",
        raising=False,
    )
    update = _callback_update("report:refresh:168:onebase")

    asyncio.run(bot.cb_report_refresh(update, None))

    assert calls == [("onebase", [], 168, True)]
    assert update.callback_query.edit_message_text.await_count == 2
    rendered = update.callback_query.edit_message_text.await_args.args[0]
    assert "бюджет сохранён" in rendered


def test_report_period_falls_back_to_plain_text_on_telegram_markdown_error(
        monkeypatch):
    edit = AsyncMock(side_effect=[
        None,
        BadRequest("Can't parse entities"),
        None,
    ])
    update = _callback_update("report:period:168:onebase", edit)
    monkeypatch.setattr(bot, "is_authorized", lambda _user_id: True)
    monkeypatch.setattr(bot.pipeline_insights, "list_profiles", lambda: [
        {"id": "onebase", "title": "OneBase", "repository": "owner/onebase"},
    ])
    monkeypatch.setattr(bot.db, "list_series", lambda: [])
    monkeypatch.setattr(
        bot.pipeline_insights,
        "build_period_report",
        lambda *_args, **_kwargs: {"delivery": {"status": "fresh"}},
        raising=False,
    )
    monkeypatch.setattr(
        bot.pipeline_insights,
        "render_period_report_markdown",
        lambda _report: "**Итоги**\n[#7](https://github.com/o/r/pull/7)",
        raising=False,
    )

    asyncio.run(bot.cb_report_period(update, None))

    assert edit.await_count == 3
    fallback = edit.await_args_list[2]
    assert fallback.args[0] == "Итоги\n#7 — https://github.com/o/r/pull/7"
    assert "parse_mode" not in fallback.kwargs


def test_report_markdown_clips_only_between_complete_lines():
    line = "[#1](https://github.com/o/r/pull/1) delivered"
    rendered = "Header\n" + "\n".join(line for _ in range(200))

    clipped = bot._clip_report_markdown(rendered, limit=180)

    assert len(clipped) <= 180
    assert clipped.endswith("\n…")
    assert clipped.count("[") == clipped.count("](")
