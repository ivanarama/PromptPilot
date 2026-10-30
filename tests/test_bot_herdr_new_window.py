import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from promptpilot import bot, config


def update(data, text=None):
    message = SimpleNamespace(chat_id=123, text=text, reply_text=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock(),
                            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                            message=message)
    return SimpleNamespace(callback_query=query if text is None else None,
                           message=message if text is not None else None,
                           effective_user=SimpleNamespace(id=123),
                           effective_chat=SimpleNamespace(id=123, type="private"))


def buttons(query):
    return [button for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row]


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "existing").mkdir(parents=True)
    monkeypatch.setattr(bot, "PROJECTS_ROOT", str(projects))
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "provider_available", lambda info: True)
    monkeypatch.setattr(bot, "load_machines", lambda: {})
    monkeypatch.setattr(bot, "is_authorized", lambda uid: uid == 123)
    monkeypatch.setattr(bot, "get_provider_models", lambda provider: [])
    monkeypatch.setattr(bot, "_herdr_json", AsyncMock(return_value={"result": {"agents": []}}))
    return SimpleNamespace(user_data={"new_prompt": "Проверь проект"})


def test_empty_windows_still_offer_creation_and_keep_prompt(wizard):
    event = update("pickprov:herdr-session")

    state = asyncio.run(bot.add_task_got_provider(event, wizard))

    assert state == bot.ASK_HERDR_TARGET
    assert [(b.text, b.callback_data) for b in buttons(event.callback_query)] == [
        ("➕ Новое окно", "hst_new"), (bot.BACK_LABEL, "hst_back")]
    assert wizard.user_data["new_prompt"] == "Проверь проект"


def test_existing_window_selection_keeps_original_route(wizard, monkeypatch):
    agent = {"pane_id": "w1:p1", "workspace_id": "w1", "name": "existing-agent"}
    monkeypatch.setattr(bot, "_herdr_json", AsyncMock(side_effect=[
        {"result": {"agents": [agent]}}, {"result": {"workspaces": []}}]))
    event = update("pickprov:herdr-session")
    assert asyncio.run(bot.add_task_got_provider(event, wizard)) == bot.ASK_HERDR_TARGET
    assert "hst_i:0" in [b.callback_data for b in buttons(event.callback_query)]

    assert asyncio.run(bot.add_task_got_herdr_target(update("hst_i:0"), wizard)) == bot.ASK_CONFIRM
    assert wizard.user_data["new_provider"] == "herdr-session"
    assert wizard.user_data["new_herdr_target"] == "existing-agent"


def test_new_window_with_new_folder_preserves_agent_folder_and_keep_pane(wizard, isolated_db, tmp_path):
    project = tmp_path / "project"
    wizard.user_data.update(new_provider="herdr-session", new_herdr_target="stale-pane")
    assert asyncio.run(bot.cb_herdr_new(update("hst_new"), wizard)) == bot.ASK_HERDR_AGENT
    assert "new_herdr_target" not in wizard.user_data
    choices = wizard.user_data["herdr_new_providers"]
    event = update(f"hnew_i:{choices.index('codex')}")
    assert asyncio.run(bot.cb_herdr_new_agent(event, wizard)) == bot.ASK_DIR
    route = wizard.user_data["new_provider"]
    provider = config.load_providers()[route]
    assert provider["executor"] == "herdr"
    assert provider["kind"] == "codex"
    assert provider["hidden"] is True
    assert "cmd" not in provider
    assert route not in config.pickable_providers()

    assert asyncio.run(bot.cb_dir_create_start(update("dir_new"), wizard)) == bot.ASK_DIR_CREATE
    entered = update("", text=str(project))
    assert asyncio.run(bot.add_task_got_new_dir(entered, wizard)) == bot.ASK_CONFIRM
    assert project.is_dir()
    summary = entered.message.reply_text.call_args.args[0]
    assert "Новое окно Herdr" in summary
    assert "__herdr_window_" not in summary
    assert str(project) in summary
    event = update("wiz_run")
    assert asyncio.run(bot.cb_wiz_run(event, wizard)) == bot.ConversationHandler.END
    tasks = isolated_db.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.provider == route
    assert task.working_dir == str(project)
    assert task.herdr_target is None
    assert task.keep_pane is True
    assert task.tg_chat_id == 123
    saved = json.loads(isolated_db.get_setting("tg_last_wizard:123"))
    assert saved["new_provider"] == route
    assert "новое окно Herdr" in bot._last_settings_line(saved)


def test_choices_exclude_missing_hidden_and_unsupported_agents(wizard, monkeypatch):
    providers = config.load_providers()
    monkeypatch.setattr(bot, "pickable_providers", lambda: {
        name: providers[name] for name in ("herdr-session", "codex", "qwen")})
    assert list(bot._herdr_new_choices()) == ["codex"]


def test_remote_choices_use_remote_probe_instead_of_local_tools(wizard, monkeypatch):
    machines = {"mac": {"host": "user@mac", "providers": ["herdr-session", "claude"]},
                "no-herdr": {"host": "other", "providers": ["codex"]}}
    monkeypatch.setattr(bot, "load_machines", lambda: machines)
    monkeypatch.setattr(bot, "pickable_providers", lambda: {})
    assert list(bot._herdr_new_choices("mac")) == ["claude"]
    assert bot._herdr_new_choices("no-herdr") == {}
    assert bot._herdr_new_choices("deleted") == {}


def test_model_selection_uses_interactive_provider_settings(wizard, monkeypatch):
    config._write_custom_providers({"codex": {"models": ["model-one", "model-two"]}})
    monkeypatch.setattr(bot, "get_provider_models", config.get_provider_models)
    asyncio.run(bot.cb_herdr_new(update("hst_new"), wizard))
    wizard.user_data.update(new_model="stale-model", new_effort="high", new_herdr_target="old")
    index = wizard.user_data["herdr_new_providers"].index("codex")
    assert asyncio.run(bot.cb_herdr_new_agent(update(f"hnew_i:{index}"), wizard)) == bot.ASK_MODEL
    assert wizard.user_data["model_list"] == ["model-one", "model-two"]
    assert "new_model" not in wizard.user_data
    assert "new_effort" not in wizard.user_data
    assert "new_herdr_target" not in wizard.user_data


def test_interactive_route_preserves_environment_but_drops_headless_arguments(wizard):
    config._write_custom_providers({"codex": {"env": {"CODEX_HOME": "custom-home"},
        "args": ["exec", "--json"], "effort": "high", "models": ["model-test"]}})
    route = config.ensure_herdr_launch_provider("codex")
    info = config.load_providers()[route]
    assert info["env"] == {"CODEX_HOME": "custom-home"}
    assert info["effort"] == "high"
    assert info["models"] == ["model-test"]
    assert info["supports_effort"] is True
    assert "args" not in info
    assert "prompt_stdin" not in info
    assert config.ensure_herdr_launch_provider("codex") == route


def test_existing_custom_herdr_provider_is_reused(wizard):
    config._write_custom_providers({"my-agent": {"executor": "herdr", "kind": "gemini",
                                               "args": ["--model", "custom"]}})
    assert "my-agent" in bot._herdr_new_choices()
    assert config.ensure_herdr_launch_provider("my-agent") == "my-agent"
    assert config.load_providers()["my-agent"]["args"] == ["--model", "custom"]
    with pytest.raises(ValueError):
        config.ensure_herdr_launch_provider("herdr-session")


def test_missing_agent_during_selection_keeps_task_and_does_not_register(wizard, monkeypatch):
    asyncio.run(bot.cb_herdr_new(update("hst_new"), wizard))
    monkeypatch.setattr(bot, "_herdr_new_choices", lambda machine: {})
    event = update("hnew_i:0")
    assert asyncio.run(bot.cb_herdr_new_agent(event, wizard)) == bot.ASK_HERDR_AGENT
    assert event.callback_query.answer.call_args.kwargs["show_alert"] is True
    assert wizard.user_data["new_prompt"] == "Проверь проект"
    assert not config._providers_file().exists()


def test_no_agents_explains_and_keeps_window_picker(wizard, monkeypatch):
    monkeypatch.setattr(bot, "_herdr_new_choices", lambda machine: {})
    event = update("hst_new")
    assert asyncio.run(bot.cb_herdr_new(event, wizard)) == bot.ASK_HERDR_TARGET
    assert event.callback_query.answer.call_args.kwargs["show_alert"] is True


def test_back_and_cancel_do_not_lose_prompt_or_leave_agent_cache(wizard):
    asyncio.run(bot.cb_herdr_new(update("hst_new"), wizard))
    assert asyncio.run(bot.cb_herdr_back(update("hnew_back"), wizard)) == bot.ASK_HERDR_TARGET
    assert "herdr_new_providers" not in wizard.user_data
    assert wizard.user_data["new_prompt"] == "Проверь проект"
    assert asyncio.run(bot.cb_herdr_back(update("hst_back"), wizard)) == bot.ASK_PROVIDER
    asyncio.run(bot.cb_herdr_new(update("hst_new"), wizard))
    assert asyncio.run(bot.cb_wiz_cancel(update("wiz_cancel"), wizard)) == bot.ConversationHandler.END
    assert wizard.user_data == {}


def test_route_write_failure_keeps_agent_picker_and_prompt(wizard, monkeypatch):
    asyncio.run(bot.cb_herdr_new(update("hst_new"), wizard))
    def fail_write(_source):
        raise OSError("read-only providers")
    monkeypatch.setattr(bot, "ensure_herdr_launch_provider", fail_write)
    event = update("hnew_i:0")
    assert asyncio.run(bot.cb_herdr_new_agent(event, wizard)) == bot.ASK_HERDR_AGENT
    assert wizard.user_data["new_prompt"] == "Проверь проект"
    assert "Не удалось" in event.callback_query.message.reply_text.call_args.args[0]


def test_picker_has_windows_label_without_exposing_internal_routes(wizard):
    route = config.ensure_herdr_launch_provider("codex")
    choices = [b for row in bot._provider_buttons() for b in row]
    assert any(b.text == "🖥 Окна Herdr" and b.callback_data == "pickprov:herdr-session"
               for b in choices)
    assert all(route not in b.callback_data for b in choices)


def test_switching_to_local_preserves_available_new_window_route(wizard, tmp_path):
    route = config.ensure_herdr_launch_provider("codex")
    wizard.user_data.update(new_provider=route, new_machine="remote", new_dir=str(tmp_path))
    state = asyncio.run(bot.cb_wiz_drop_machine(update("wiz_drop_machine"), wizard))
    assert state == bot.ASK_CONFIRM
    assert wizard.user_data["new_provider"] == route
    assert "new_machine" not in wizard.user_data
    assert "Новое окно Herdr" in bot._wizard_summary(wizard)


def test_access_revoked_while_answering_agent_choice_cannot_register_route(wizard, monkeypatch):
    allowed = {123}
    monkeypatch.setattr(bot, "is_authorized", lambda uid: uid in allowed)
    asyncio.run(bot.cb_herdr_new(update("hst_new"), wizard))
    index = wizard.user_data["herdr_new_providers"].index("codex")
    clicked = update(f"hnew_i:{index}")
    clicked.callback_query.answer.side_effect = lambda: allowed.clear()
    assert asyncio.run(bot.cb_herdr_new_agent(clicked, wizard)) == bot.ConversationHandler.END
    assert not config._providers_file().exists()
