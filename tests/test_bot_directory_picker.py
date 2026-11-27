import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from promptpilot import bot


def test_model_picker_lists_only_the_root_until_a_folder_is_opened(tmp_path, monkeypatch):
    (tmp_path / "branch" / "nested").mkdir(parents=True)
    (tmp_path / "leaf").mkdir()
    (tmp_path / ".hidden").mkdir()
    monkeypatch.setattr(bot, "PROJECTS_ROOT", str(tmp_path))
    scan = bot._list_subdirs
    scanned = []

    def record_scan(path):
        scanned.append(Path(path))
        return scan(path)

    monkeypatch.setattr(bot, "_list_subdirs", record_scan)
    monkeypatch.setattr(bot, "_pick_dir", AsyncMock(return_value=bot.ASK_CONFIRM))
    query = SimpleNamespace(
        data="model_i:0",
        answer=AsyncMock(side_effect=BadRequest("Query is too old")),
        edit_message_text=AsyncMock(),
    )
    event = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={"new_prompt": "Check Onebase", "model_list": ["gpt-test"]})

    assert asyncio.run(bot.add_task_got_model_idx(event, context)) == bot.ASK_DIR
    assert context.user_data["new_model"] == "gpt-test"
    assert context.user_data["dir_subs"] == ["branch", "leaf"]
    assert scanned == [tmp_path]

    query.data = "dirn:0"
    assert asyncio.run(bot.cb_dir_nav(event, context)) == bot.ASK_DIR
    assert context.user_data["dir_subs"] == ["nested"]
    assert scanned == [tmp_path, tmp_path / "branch"]

    query.data = "dirn:0"
    assert asyncio.run(bot.cb_dir_nav(event, context)) == bot.ASK_CONFIRM
    bot._pick_dir.assert_awaited_once_with(query, context, str(tmp_path / "branch" / "nested"))
    assert scanned == [tmp_path, tmp_path / "branch", tmp_path / "branch" / "nested"]
