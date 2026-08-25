"""Адрес панели в уведомлении herdr: воркспейс/вкладка/папка вместо «w9:pC».

Run: python3 -m unittest discover -s tests
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/_env.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # promptpilot

import _env  # noqa: E402  — своя база; импорт до promptpilot

from promptpilot import bot  # noqa: E402

_env.assert_isolated()

AGENT = {
    "agent": "claude",
    "agent_status": "done",
    "pane_id": "w9:pC",
    "tab_id": "w9:tC",
    "workspace_id": "w9",
    "cwd": "/home/vibecoder5/test-herdr",
    "terminal_title_stripped": "Обработать ишью",
}

WORKSPACES = {"id": "cli:workspace:list", "result": {"workspaces": [
    {"workspace_id": "w9", "label": "PromptPilot", "number": 2},
    {"workspace_id": "w1", "label": "onebase", "number": 1},
]}}

TABS = {"id": "cli:tab:list", "result": {"tabs": [
    {"tab_id": "w9:tC", "label": "pp-kept-9999"},
    {"tab_id": "w1:t3", "label": "2"},
]}}


def _herdr_stub(responses):
    """Подменяет _herdr_json: ('workspace','list') → …, ('agent','focus',…) → …
    Неописанная команда — None, как молчащий herdr."""
    async def fake(*args, host=None, timeout=15):
        return responses.get(args[:2], None)
    return fake


class HerdrPlaceTest(unittest.TestCase):
    def setUp(self):
        bot._HERDR_PLACES.clear()   # кэш меток общий на процесс
        self.addCleanup(bot._HERDR_PLACES.clear)

    def test_place_names_workspace_tab_and_folder(self):
        ws = {"w9": "PromptPilot"}
        tabs = {"w9:tC": "pp-kept-9999"}
        with mock.patch("os.path.expanduser", return_value="/home/vibecoder5"):
            place = bot._herdr_place(AGENT, ws, tabs)
        self.assertEqual(place, "PromptPilot / pp-kept-9999 · ~/test-herdr")

    def test_numbered_tab_is_spelled_out(self):
        """Голая цифра в адресе читается как что угодно, «вкл. 2» — как вкладка."""
        place = bot._herdr_place({"workspace_id": "w1", "tab_id": "w1:t3", "cwd": ""},
                                 {"w1": "onebase"}, {"w1:t3": "2"})
        self.assertEqual(place, "onebase / вкл. 2")

    def test_place_survives_missing_maps(self):
        """herdr не ответил про воркспейсы — остаётся хотя бы папка."""
        with mock.patch("os.path.expanduser", return_value="/home/vibecoder5"):
            self.assertEqual(bot._herdr_place(AGENT, {}, {}), "~/test-herdr")

    def test_place_maps_are_cached_per_machine(self):
        calls = []

        async def fake(*args, host=None, timeout=15):
            calls.append(args[:2])
            return {"workspace": WORKSPACES, "tab": TABS}[args[0]]

        with mock.patch.object(bot, "_herdr_json", fake):
            asyncio.run(bot._herdr_place_maps("", None))
            asyncio.run(bot._herdr_place_maps("", None))
        self.assertEqual(calls, [("workspace", "list"), ("tab", "list")])

    def test_silent_herdr_is_not_cached(self):
        """Иначе один сбой ослепил бы бота на весь TTL."""
        with mock.patch.object(bot, "_herdr_json", _herdr_stub({})):
            self.assertEqual(asyncio.run(bot._herdr_place_maps("", None)), ({}, {}))
        self.assertEqual(bot._HERDR_PLACES, {})


class HerdrNotifyTest(unittest.TestCase):
    def setUp(self):
        bot._HERDR_PLACES.clear()
        self.addCleanup(bot._HERDR_PLACES.clear)
        self.sent = []

        class FakeBot:
            async def send_message(_self, chat_id, text, reply_markup=None):
                self.sent.append((chat_id, text, reply_markup))

        self.fake_bot = FakeBot()
        p = mock.patch.object(bot, "list_authorized", lambda: ["555001"])
        p.start()
        self.addCleanup(p.stop)

    def _notify(self, status="done", machine="", agent=AGENT):
        stub = _herdr_stub({("workspace", "list"): WORKSPACES, ("tab", "list"): TABS})
        with mock.patch.object(bot, "_herdr_json", stub), \
             mock.patch.object(bot, "_machine_remote", lambda m: None), \
             mock.patch("os.path.expanduser", return_value="/home/vibecoder5"):
            asyncio.run(bot._herdr_notify(self.fake_bot, agent["pane_id"], status,
                                          agent, machine))
        return self.sent[0][1], self.sent[0][2]

    def test_done_notification_says_where_the_pane_is(self):
        text, _ = self._notify()
        self.assertIn("📍 PromptPilot / pp-kept-9999 · ~/test-herdr", text)
        self.assertIn("Обработать ишью", text)

    def test_pane_id_stays_for_the_cli(self):
        """Адрес — человеку, pane_id — в `herdr agent focus` и в кнопки."""
        text, _ = self._notify()
        self.assertIn("w9:pC", text)

    def test_falls_back_to_the_pane_id_alone(self):
        """Молчащий herdr не должен лишать уведомление всякого ориентира."""
        with mock.patch.object(bot, "_herdr_json", _herdr_stub({})), \
             mock.patch.object(bot, "_machine_remote", lambda m: None):
            asyncio.run(bot._herdr_notify(self.fake_bot, "w9:pC", "done",
                                          {"pane_id": "w9:pC", "agent": "claude"}))
        self.assertIn("📍 панель w9:pC", self.sent[0][1])

    def test_focus_button_carries_machine_and_pane(self):
        _, kb = self._notify(machine="vm2")
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("hd_focus:vm2:w9:pC", data)

    def test_blocked_notification_has_a_focus_button_too(self):
        _, kb = self._notify(status="blocked")
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("hd_focus::w9:pC", data)


class HerdrFocusButtonTest(unittest.TestCase):
    """Кнопка 🎯 идёт публичным путём: callback_data → обработчик → herdr CLI."""

    def _press(self, response):
        calls = []

        async def fake(*args, host=None, timeout=15):
            calls.append(args)
            return response

        query = mock.MagicMock()
        query.data = "hd_focus::w9:pC"
        query.answer = mock.AsyncMock()
        update = mock.MagicMock(callback_query=query)
        # require_auth пропускает только личку авторизованного пользователя —
        # без этого нажатие упирается в «Нет доступа» и до herdr не доходит.
        update.effective_user.id = 555001
        update.effective_chat.type = "private"
        with mock.patch.object(bot, "_herdr_json", fake), \
             mock.patch.object(bot, "load_machines", lambda: {}), \
             mock.patch.object(bot, "is_authorized", lambda *a, **k: True):
            asyncio.run(bot.cb_herdr_focus(update, mock.MagicMock()))
        return calls, query.answer

    def test_focus_goes_to_the_pane_from_the_button(self):
        calls, answer = self._press({"result": {"agent": {"pane_id": "w9:pC"}}})
        self.assertEqual(calls, [("agent", "focus", "w9:pC")])
        self.assertIn("w9:pC", answer.await_args.args[0])
        self.assertNotIn("show_alert", answer.await_args.kwargs)

    def test_closed_pane_is_reported(self):
        """herdr отвечает {"error": …} с кодом 0 — успех виден по result."""
        _, answer = self._press({"error": {"code": "agent_not_found"}})
        self.assertTrue(answer.await_args.kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
