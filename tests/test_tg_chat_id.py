"""Кому уходят уведомления: дефолт PP_TG_CHAT_ID, «молчать» и правка адресата.

Run: python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/_env.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # promptpilot

import _env  # noqa: E402  — своя база; импорт до promptpilot

from promptpilot import api, db  # noqa: E402
from promptpilot.models import TaskCreate  # noqa: E402

_env.assert_isolated()

# Дефолт подставляется из модульной переменной, а не из окружения на импорте:
# модули тестов делят процесс, и кто импортировал конфиг первым — тот и задал
# всем PP_TG_CHAT_ID. Патчим там, куда значение уже попало.
DEFAULT_CHAT = 555001


class TgChatIdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        patcher = mock.patch.object(db, "TG_CHAT_ID", DEFAULT_CHAT)
        patcher.start()
        self.addCleanup(patcher.stop)
        with db._connect() as conn:
            conn.execute("DELETE FROM tasks")

    def test_task_without_chat_id_gets_the_configured_default(self):
        """Веб-задача: браузер chat id не знает, его подставляет сервер."""
        task = db.create_task(TaskCreate(prompt="из веба"))
        self.assertEqual(task.tg_chat_id, DEFAULT_CHAT)

    def test_zero_means_muted_and_survives_the_default(self):
        task = db.create_task(TaskCreate(prompt="молча", tg_chat_id=0))
        self.assertEqual(task.tg_chat_id, 0)

    def test_explicit_chat_id_wins(self):
        """Задача из бота несёт свой чат — дефолт её не трогает."""
        task = db.create_task(TaskCreate(prompt="из бота", tg_chat_id=777))
        self.assertEqual(task.tg_chat_id, 777)

    def test_no_default_configured_keeps_the_old_silence(self):
        with mock.patch.object(db, "TG_CHAT_ID", 0):
            task = db.create_task(TaskCreate(prompt="без дефолта"))
        self.assertIsNone(task.tg_chat_id)

    def test_muted_task_is_not_queued_for_notification(self):
        muted = db.create_task(TaskCreate(prompt="молча", tg_chat_id=0))
        loud = db.create_task(TaskCreate(prompt="из веба"))
        db.mark_completed(muted.id, "готово")
        db.mark_completed(loud.id, "готово")

        pending = {t.id for t in db.get_pending_notifications()}
        self.assertIn(loud.id, pending)
        self.assertNotIn(muted.id, pending)

    def test_chat_id_is_editable_on_a_queued_task(self):
        """Дозаправить серию адресатом, не пересоздавая её."""
        task = db.create_task(TaskCreate(prompt="серия", tg_chat_id=0, recurrence="6h"))
        self.assertTrue(db.update_task_fields(task.id, {"tg_chat_id": 42}))
        self.assertEqual(db.get_task(task.id).tg_chat_id, 42)

    def test_chat_id_is_editable_while_the_task_runs(self):
        """Адресат читается в момент доставки, значит на ходу это ещё осмысленно."""
        task = db.create_task(TaskCreate(prompt="идёт"))
        with db._connect() as conn:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task.id,))

        self.assertTrue(db.update_task_fields(task.id, {"tg_chat_id": 42}))
        self.assertEqual(db.get_task(task.id).tg_chat_id, 42)

    def test_other_fields_are_still_frozen_while_running(self):
        task = db.create_task(TaskCreate(prompt="идёт"))
        with db._connect() as conn:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task.id,))

        self.assertFalse(db.update_task_fields(task.id, {"priority": 1}))
        # Смешанная правка идущей задачи тоже не проходит — целиком, не наполовину.
        self.assertFalse(db.update_task_fields(task.id, {"tg_chat_id": 42, "priority": 1}))
        self.assertEqual(db.get_task(task.id).priority, 5)

    def test_completed_task_is_not_editable(self):
        task = db.create_task(TaskCreate(prompt="готово"))
        db.mark_completed(task.id, "ok")
        self.assertFalse(db.update_task_fields(task.id, {"tg_chat_id": 42}))

    def test_recurrence_carries_the_muted_state_forward(self):
        """Продление серии не должно воскрешать выключенные уведомления."""
        first = db.create_task(TaskCreate(prompt="серия", tg_chat_id=0, recurrence="6h"))
        second = db.create_task(TaskCreate(
            prompt=first.prompt, recurrence=first.recurrence, tg_chat_id=first.tg_chat_id))
        self.assertEqual(second.tg_chat_id, 0)


class ApiPatchTest(unittest.TestCase):
    """PATCH /api/tasks/<id> принимает tg_chat_id — раньше поле молча игнорировалось."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        db.init_db()
        cls.client = TestClient(api.app)

    def setUp(self):
        for module, attr in ((db, "TG_CHAT_ID"), (api, "TG_CHAT_ID")):
            patcher = mock.patch.object(module, attr, DEFAULT_CHAT)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_patch_sets_and_clears_the_chat(self):
        task = db.create_task(TaskCreate(prompt="из веба"))

        r = self.client.patch(f"/api/tasks/{task.id}", json={"tg_chat_id": 4242})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.get_task(task.id).tg_chat_id, 4242)

        r = self.client.patch(f"/api/tasks/{task.id}", json={"tg_chat_id": 0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.get_task(task.id).tg_chat_id, 0)

    def test_config_endpoint_reports_the_default(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tg_chat_id"], DEFAULT_CHAT)


if __name__ == "__main__":
    unittest.main()
