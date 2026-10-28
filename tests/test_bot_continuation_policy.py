"""Real conversation callbacks retain parent policy and current authorization."""
import asyncio
from types import SimpleNamespace

import pytest

from promptpilot import bot, config
from promptpilot.models import TaskCreate


class Message:
    def __init__(self, text="continue safely"):
        self.text, self.replies = text, []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kwargs):
        self.replies.append(text)
        return self


class Query:
    def __init__(self, task_id, user):
        self.data = f"reply_task:{task_id}"
        self.from_user, self.message = user, Message()

    async def answer(self, *args, **kwargs):
        if hasattr(self, "on_answer"):
            self.on_answer()

    async def edit_message_reply_markup(self, **kwargs):
        pass

    async def edit_message_text(self, text, **kwargs):
        return await self.message.reply_text(text, **kwargs)


def _conversation(isolated_db, tmp_path, monkeypatch, rights="read"):
    allowed = {7}
    monkeypatch.setattr(bot, "is_authorized", lambda uid: uid in allowed)
    monkeypatch.setattr(bot, "TASK_PASSWORD", "")
    monkeypatch.setattr(bot, "_main_menu", lambda: None)
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    parent = isolated_db.create_task(TaskCreate(
        prompt="original", provider="claude", working_dir=str(tmp_path),
        rights=rights, skip_permissions=True))
    isolated_db.mark_completed(parent.id, "done", session_id="original-session")
    parent = isolated_db.get_task(parent.id)
    user, chat = SimpleNamespace(id=7), SimpleNamespace(id=7, type="private")
    context = SimpleNamespace(user_data={})
    entry = SimpleNamespace(callback_query=Query(parent.id, user),
                            effective_user=user, effective_chat=chat, message=None)
    assert asyncio.run(bot.cb_reply_task_start(entry, context)) == bot.ASK_REPLY
    final = SimpleNamespace(callback_query=None, message=Message(),
                            effective_user=user, effective_chat=chat)
    return parent, context, final, allowed


@pytest.mark.parametrize("rights", ["none", "read", "write"])
def test_reply_preserves_parent_rights_and_never_inherits_bypass_default(
        isolated_db, tmp_path, monkeypatch, rights):
    parent, context, update, _ = _conversation(isolated_db, tmp_path, monkeypatch, rights)
    asyncio.run(bot.reply_got_text(update, context))
    children = [task for task in isolated_db.list_tasks() if task.parent_task_id == parent.id]
    assert len(children) == 1
    child = children[0]
    assert child.rights == parent.rights and child.session_id == "original-session"
    command = config.build_cmd(child.provider, child.prompt,
        session_id=child.session_id, skip_permissions=child.skip_permissions, rights=child.rights)
    assert "--restricted" in command
    assert "--dangerously-skip-permissions" not in command


def test_open_dialog_reloads_current_parent_permissions(isolated_db, tmp_path, monkeypatch):
    parent, context, update, _ = _conversation(isolated_db, tmp_path, monkeypatch)
    with isolated_db._connect() as conn:
        conn.execute("UPDATE tasks SET rights='none', skip_permissions=0 WHERE id=?", (parent.id,))
    asyncio.run(bot.reply_got_text(update, context))
    children = [task for task in isolated_db.list_tasks() if task.parent_task_id == parent.id]
    assert len(children) == 1 and children[0].rights == "none"
    assert children[0].skip_permissions is False


@pytest.mark.parametrize("denial", ["revoked", "group"])
def test_reply_rechecks_authorization_at_final_message(isolated_db, tmp_path, monkeypatch, denial):
    _, context, update, allowed = _conversation(isolated_db, tmp_path, monkeypatch)
    if denial == "revoked":
        allowed.clear()
    else:
        update.effective_chat.type = "group"
    asyncio.run(bot.reply_got_text(update, context))
    assert len(isolated_db.list_tasks()) == 1
    assert update.message.replies == ["Нет доступа."]


def test_reply_to_deleted_parent_is_refused(isolated_db, tmp_path, monkeypatch):
    parent, context, update, _ = _conversation(isolated_db, tmp_path, monkeypatch)
    assert isolated_db.delete_task(parent.id)
    asyncio.run(bot.reply_got_text(update, context))
    assert isolated_db.list_tasks() == []
    assert "Продолжение не создано" in update.message.replies[-1]


@pytest.mark.parametrize("handler", ["cb_wiz_run", "epf_base_cb"])
def test_other_task_creation_callbacks_recheck_revocation(isolated_db, tmp_path, monkeypatch, handler):
    parent, context, final, allowed = _conversation(isolated_db, tmp_path, monkeypatch)
    query = Query(parent.id, final.effective_user)
    query.data = "wiz_run" if handler == "cb_wiz_run" else "epfb:stub"
    update = SimpleNamespace(callback_query=query, message=None,
        effective_user=final.effective_user, effective_chat=final.effective_chat)
    allowed.clear()
    assert asyncio.run(getattr(bot, handler)(update, context)) == bot.ConversationHandler.END
    assert len(isolated_db.list_tasks()) == 1


def test_wizard_rechecks_after_awaited_callback_answer(isolated_db, tmp_path, monkeypatch):
    parent, context, final, allowed = _conversation(isolated_db, tmp_path, monkeypatch)
    query = Query(parent.id, final.effective_user)
    query.data = "wiz_run"
    query.on_answer = allowed.clear
    context.user_data.update(new_prompt="wizard task", new_provider="claude", new_dir=str(tmp_path))
    update = SimpleNamespace(callback_query=query, message=None,
        effective_user=final.effective_user, effective_chat=final.effective_chat)
    assert asyncio.run(bot.cb_wiz_run(update, context)) == bot.ConversationHandler.END
    assert len(isolated_db.list_tasks()) == 1
    assert "Задача не создана" in query.message.replies[-1]


def test_epf_rechecks_after_awaited_preparation(isolated_db, tmp_path, monkeypatch):
    parent, context, final, allowed = _conversation(isolated_db, tmp_path, monkeypatch)
    source = tmp_path / "dummy.epf"
    source.write_bytes(b"dummy; never decompiled")
    query = Query(parent.id, final.effective_user)
    query.data = "epfb:stub"
    context.user_data.update(epf_file=str(source), epf_name="dummy.epf", epf_task="prepare")
    monkeypatch.setattr(bot, "PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setattr(bot.epf_tools, "resolve_base", lambda key: (None, "stub"))
    prepared = []
    def prepare(*args, **kwargs):
        prepared.append(True)
        allowed.clear()
        return tmp_path / "prepared", []
    monkeypatch.setattr(bot.epf_tools, "prepare_project", prepare)
    update = SimpleNamespace(callback_query=query, message=None,
        effective_user=final.effective_user, effective_chat=final.effective_chat)
    assert asyncio.run(bot.epf_base_cb(update, context)) == bot.ConversationHandler.END
    assert prepared == [True] and len(isolated_db.list_tasks()) == 1
    assert "Задача не создана" in query.message.replies[-1]
