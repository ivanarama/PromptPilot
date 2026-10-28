"""The herdr bridge must not announce old completions after a bot restart."""

import asyncio

from promptpilot import bot


def test_watch_skips_existing_done_agents_but_reports_new_completion(monkeypatch):
    agents = [
        {"pane_id": "w1:pA", "name": "t15", "agent_status": "done", "completion_seq": 49},
        {"pane_id": "w1:pC", "name": "t17", "agent_status": "done", "completion_seq": 52},
        {"pane_id": "w1:pD", "name": "t18", "agent_status": "done", "completion_seq": 55},
    ]
    sent = []

    async def agent_list(*_args, **_kwargs):
        return {"result": {"agents": agents}}

    async def notify(_bot, pane, status, *_args, **_kwargs):
        sent.append((pane, status))

    monkeypatch.setattr(bot, "_herdr_watch_targets", lambda: [("", None)])
    monkeypatch.setattr(bot, "_herdr_json", agent_list)
    monkeypatch.setattr(bot, "_herdr_notify", notify)

    notified, initialized = {}, set()
    asyncio.run(bot._herdr_watch_tick(None, notified, initialized))
    assert sent == []

    agents[0]["agent_status"] = "working"
    asyncio.run(bot._herdr_watch_tick(None, notified, initialized))
    agents[0]["agent_status"] = "done"
    agents[0]["completion_seq"] = 60
    asyncio.run(bot._herdr_watch_tick(None, notified, initialized))
    assert sent == [("w1:pA", "done")]

    asyncio.run(bot._herdr_watch_tick(None, notified, initialized))
    assert sent == [("w1:pA", "done")]
