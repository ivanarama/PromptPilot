import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from promptpilot import bot, config, herdr_exec


def test_model_change_in_extras_preserves_draft_and_revalidates_effort(tmp_path, monkeypatch):
    models = ["codex-large", "codex-small"]
    levels = config.CODEX_EFFORT_LEVELS
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": model, "supported_reasoning_levels": [{"effort": level} for level in supported]}
        for model, supported in zip(models, (levels, levels[:-1]))
    ]}), encoding="utf-8")
    provider = "__herdr_window_codex"
    settings = {"kind": "codex", "executor": "herdr", "supports_effort": True,
                "models": models, "env": {"CODEX_HOME": str(tmp_path)}}
    monkeypatch.setattr(bot, "load_providers", lambda: {provider: settings})
    monkeypatch.setattr(bot, "get_provider_models", lambda name: settings["models"])
    query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
    event = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={"new_provider": provider, "new_prompt": "Check sample-project",
                                        "new_model": models[0], "new_dir": str(tmp_path)})

    assert asyncio.run(bot.cb_extras_effort(event, context)) == bot.ASK_EFFORT
    buttons = {b.callback_data: b.text for row in query.edit_message_text.call_args.kwargs[
        "reply_markup"].inline_keyboard for b in row}
    assert buttons["eff:xhigh"] == "Extra High (xhigh)"
    assert buttons["eff:ultra"] == "Ultra"
    query.data = "eff:ultra"
    assert asyncio.run(bot.cb_effort_pick(event, context)) == bot.ASK_EXTRAS
    assert context.user_data["new_effort"] == "ultra"

    assert asyncio.run(bot.cb_extras_model(event, context)) == bot.ASK_MODEL
    query.data = "model_i:1"
    assert asyncio.run(bot.add_task_got_model_idx(event, context)) == bot.ASK_EXTRAS
    assert context.user_data["new_model"] == models[1]
    assert context.user_data["new_dir"] == str(tmp_path)
    assert context.user_data["new_prompt"] == "Check sample-project"
    assert "new_effort" not in context.user_data
    assert "Модель · codex-small" in bot._wizard_summary(context)
    assert config.provider_effort_levels(settings, models[1]) == levels[:-1]


def test_herdr_starts_codex_with_chosen_reasoning_effort(monkeypatch):
    starts = []

    def run(args, **kwargs):
        if args[:2] == ["tab", "create"]:
            return 0, {"result": {"root_pane": {"pane_id": "pane"}, "tab": {"tab_id": "tab"}}}, ""
        if args[:2] == ["agent", "start"]:
            starts.append(args)
        return 0, {"result": {"agent": {"agent_status": "idle", "pane_id": "pane",
                                        "terminal_id": "term", "state_change_seq": 1}}}, ""

    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda *args: None)
    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", lambda *args: None)
    monkeypatch.setattr(herdr_exec, "_run", run)
    task = SimpleNamespace(id=42, herdr_target=None, model="codex-large", effort="ultra",
                           session_id=None, skip_permissions=False, detached=True,
                           working_dir=".", worktree=False)
    outcome = herdr_exec.run_in_herdr(task, {"kind": "codex"}, prompt_override="Status")

    assert outcome["ok"]
    assert starts[0][-5:] == ["--", "--model", "codex-large", "-c", 'model_reasoning_effort="ultra"']
    assert config.resolve_effort({"kind": "claude"}, "ultra") == ""



def test_local_codex_catalog_needs_no_manual_provider_model_list(tmp_path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "first", "visibility": "list"},
        {"slug": "internal", "visibility": "hide"},
        {"slug": "second", "visibility": "list"},
    ]}), encoding="utf-8")
    settings = {"kind": "codex", "env": {"CODEX_HOME": str(tmp_path)}}
    monkeypatch.setattr(config, "load_providers", lambda: {"codex": settings})
    assert config.get_provider_models("codex") == ["first", "second"]
    assert config.get_provider_models("codex", local=False) == []
    settings["models"] = ["explicit"]
    assert config.get_provider_models("codex") == ["explicit"]
