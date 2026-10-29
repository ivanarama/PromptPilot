import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import bot


def event(data="", text=None):
    message = SimpleNamespace(chat_id=123, text=text, reply_text=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=message,
                            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
    return SimpleNamespace(callback_query=query if text is None else None,
                           message=message if text is not None else None,
                           effective_user=SimpleNamespace(id=123),
                           effective_chat=SimpleNamespace(id=123, type="private"))


def callbacks(mock):
    markup = mock.call_args.kwargs.get("reply_markup")
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setattr(bot.db, "recent_working_dirs", lambda **kwargs: [])
    monkeypatch.setattr(bot, "is_authorized", lambda uid: uid == 123)
    return SimpleNamespace(user_data={"new_prompt": "Создай новый проект", "new_provider": "codex"})


def test_create_new_folder_with_spaces_and_unicode_and_select_it(wizard, tmp_path):
    start = event("dir_new")
    assert asyncio.run(bot.cb_dir_create_start(start, wizard)) == bot.ASK_DIR_CREATE
    assert not list(tmp_path.iterdir())
    name = "Новый проект 2026"
    entered = event(text=name)
    assert asyncio.run(bot.add_task_got_new_dir(entered, wizard)) == bot.ASK_CONFIRM
    path = tmp_path / name
    assert path.is_dir()
    assert wizard.user_data["new_dir"] == str(path)
    assert wizard.user_data["new_prompt"] == "Создай новый проект"
    assert not any(k.startswith("dir_create_") for k in wizard.user_data)
    assert str(path) in entered.message.reply_text.call_args.args[0]


def test_new_folder_uses_current_navigation_location(wizard, tmp_path):
    parent = tmp_path / "existing-project"
    parent.mkdir()
    wizard.user_data["dir_base"] = str(parent)
    _, keyboard = bot._dir_screen(wizard)
    assert "dir_new" in [b.callback_data for row in keyboard.inline_keyboard for b in row]
    asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard))
    assert asyncio.run(bot.add_task_got_new_dir(event(text="child"), wizard)) == bot.ASK_CONFIRM
    assert (parent / "child").is_dir()
    assert not (tmp_path / "child").exists()


def test_full_path_can_create_missing_parent_directories(wizard, tmp_path):
    path = tmp_path / "new-parent" / "new-child"
    asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard))
    assert asyncio.run(bot.add_task_got_new_dir(event(text=str(path)), wizard)) == bot.ASK_CONFIRM
    assert path.is_dir()
    assert wizard.user_data["new_dir"] == str(path)


def test_manual_missing_path_requires_create_button_and_then_selects(wizard, tmp_path):
    entered = event(text="manual-project")
    assert asyncio.run(bot.add_task_got_dir(entered, wizard)) == bot.ASK_DIR_MANUAL
    path = tmp_path / "manual-project"
    assert not path.exists()
    create = next(c for c in callbacks(entered.message.reply_text) if c.startswith("dir_make:"))
    assert len(create.encode("utf-8")) <= 64
    assert asyncio.run(bot.cb_dir_create_confirm(event(create), wizard)) == bot.ASK_CONFIRM
    assert path.is_dir()
    assert wizard.user_data["new_dir"] == str(path)


def test_manual_path_to_file_is_rejected_without_create_button(wizard, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("keep me", encoding="utf-8")
    entered = event(text=str(path))
    assert asyncio.run(bot.add_task_got_dir(entered, wizard)) == bot.ASK_DIR_MANUAL
    assert "Это файл" in entered.message.reply_text.call_args.args[0]
    assert not callbacks(entered.message.reply_text)
    assert path.read_text(encoding="utf-8") == "keep me"


def test_existing_folder_and_its_contents_are_preserved(wizard, tmp_path):
    folder = tmp_path / "existing"
    folder.mkdir()
    marker = folder / "content.txt"
    marker.write_text("keep", encoding="utf-8")
    asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard))
    assert asyncio.run(bot.add_task_got_new_dir(event(text=str(folder)), wizard)) == bot.ASK_CONFIRM
    assert marker.read_text(encoding="utf-8") == "keep"


def test_mkdir_error_keeps_creation_step_and_task(wizard, monkeypatch):
    asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard))
    def denied(*args, **kwargs):
        raise PermissionError("permission denied")
    monkeypatch.setattr(bot.os, "makedirs", denied)
    entered = event(text="denied")
    assert asyncio.run(bot.add_task_got_new_dir(entered, wizard)) == bot.ASK_DIR_CREATE
    assert "Не удалось создать папку" in entered.message.reply_text.call_args.args[0]
    assert wizard.user_data["new_prompt"] == "Создай новый проект"
    assert "new_dir" not in wizard.user_data


def test_empty_name_does_not_choose_or_create_folder(wizard, tmp_path):
    asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard))
    assert asyncio.run(bot.add_task_got_new_dir(event(text="   "), wizard)) == bot.ASK_DIR_CREATE
    assert not list(tmp_path.iterdir())
    assert "new_dir" not in wizard.user_data


def test_stale_create_button_cannot_create_another_path(wizard, tmp_path):
    first = event(text="first")
    asyncio.run(bot.add_task_got_dir(first, wizard))
    stale = next(c for c in callbacks(first.message.reply_text) if c.startswith("dir_make:"))
    asyncio.run(bot.add_task_got_dir(event(text="second"), wizard))
    clicked = event(stale)
    assert asyncio.run(bot.cb_dir_create_confirm(clicked, wizard)) == bot.ASK_DIR_MANUAL
    assert clicked.callback_query.answer.call_args.kwargs["show_alert"] is True
    assert not (tmp_path / "first").exists()
    assert not (tmp_path / "second").exists()


def test_empty_picker_and_manual_entry_both_offer_creation(wizard):
    first = event()
    assert asyncio.run(bot._ask_dir(first.callback_query, wizard)) == bot.ASK_DIR_MANUAL
    assert "dir_new" in callbacks(first.callback_query.edit_message_text)
    second = event("dir:__manual__")
    assert asyncio.run(bot.add_task_got_dir_btn(second, wizard)) == bot.ASK_DIR_MANUAL
    assert "dir_new" in callbacks(second.callback_query.edit_message_text)


def test_remote_context_never_creates_directory_on_local_machine(wizard, tmp_path):
    wizard.user_data["new_machine"] = "remote"
    assert bot._dir_create_buttons(wizard) == []
    assert asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard)) is None
    assert asyncio.run(bot.add_task_got_new_dir(event(text="remote-folder"), wizard)) == bot.ASK_DIR_CREATE
    assert not (tmp_path / "remote-folder").exists()


def test_back_and_cancel_clear_creation_state_without_creating(wizard, tmp_path):
    wizard.user_data["dir_base"] = str(tmp_path)
    asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard))
    assert asyncio.run(bot.cb_dir_create_back(event("dir_create_back"), wizard)) == bot.ASK_DIR
    assert wizard.user_data["new_prompt"] == "Создай новый проект"
    assert "dir_create_base" not in wizard.user_data
    asyncio.run(bot.add_task_got_dir(event(text="cancelled"), wizard))
    assert asyncio.run(bot.cb_wiz_cancel(event("wiz_cancel"), wizard)) == bot.ConversationHandler.END
    assert wizard.user_data == {}
    assert not (tmp_path / "cancelled").exists()


def test_creating_on_top_of_file_does_not_overwrite_it(wizard, tmp_path):
    path = tmp_path / "existing.txt"
    path.write_text("original", encoding="utf-8")
    asyncio.run(bot.cb_dir_create_start(event("dir_new"), wizard))
    assert asyncio.run(bot.add_task_got_new_dir(event(text=str(path)), wizard)) == bot.ASK_DIR_CREATE
    assert path.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("reason", ["revoked", "group"])
def test_creation_requires_current_private_chat_access(wizard, tmp_path, monkeypatch, reason):
    path = tmp_path / "not-created"
    entered = event(text=str(path))
    if reason == "revoked":
        monkeypatch.setattr(bot, "is_authorized", lambda uid: False)
    else:
        entered.effective_chat.type = "group"
    assert asyncio.run(bot.add_task_got_new_dir(entered, wizard)) == bot.ConversationHandler.END
    assert not path.exists()


def test_access_revoked_while_answering_create_button_cannot_create(wizard, tmp_path, monkeypatch):
    allowed = {123}
    monkeypatch.setattr(bot, "is_authorized", lambda uid: uid in allowed)
    entered = event(text="not-created")
    asyncio.run(bot.add_task_got_dir(entered, wizard))
    create = next(c for c in callbacks(entered.message.reply_text) if c.startswith("dir_make:"))
    clicked = event(create)
    clicked.callback_query.answer.side_effect = lambda: allowed.clear()
    assert asyncio.run(bot.cb_dir_create_confirm(clicked, wizard)) == bot.ConversationHandler.END
    assert not (tmp_path / "not-created").exists()
