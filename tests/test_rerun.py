"""«Повторить»: клон завершённой задачи — один код для бота и веба.

Кнопка в боте была с самого начала, в вебе её не было, и клон собирался руками
в обработчике бота — список полей там отставал от TaskCreate (`effort`
терялся). Проверки закрывают обе стороны: что переносится, что намеренно нет и
кого повторять нельзя.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/_env.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # promptpilot

import _env  # noqa: E402  — своя база; импорт до promptpilot

from fastapi.testclient import TestClient  # noqa: E402

from promptpilot import api, db  # noqa: E402
from promptpilot.models import TaskCreate, TaskStatus  # noqa: E402

_env.assert_isolated()


class RerunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(api.app)

    def _finished(self, **kw):
        task = db.create_task(TaskCreate(**kw))
        db.mark_completed(task.id, "ИТОГ: ГОТОВО")
        return db.get_task(task.id)

    def test_clone_carries_the_run_settings(self):
        src = self._finished(
            prompt="/triage-issues",
            working_dir="/tmp/repo",
            provider="claude-triage",
            model="opus",
            effort="high",
            priority=3,
            skip_permissions=True,
            worktree=True,
            task_timeout=1800,
        )

        clone = db.clone_task(src.id)

        self.assertNotEqual(clone.id, src.id)
        self.assertEqual(clone.status, TaskStatus.PENDING)
        for field in ("prompt", "working_dir", "provider", "model", "effort",
                      "priority", "skip_permissions", "worktree", "task_timeout"):
            self.assertEqual(getattr(clone, field), getattr(src, field), field)

    def test_clone_is_one_off_and_starts_a_fresh_session(self):
        """Повтор серии не заводит вторую серию, повтор ответа не тянет чужую сессию."""
        src = self._finished(prompt="серия", recurrence="6h", session_id="abc123")

        clone = db.clone_task(src.id)

        self.assertIsNone(clone.recurrence)
        self.assertIsNone(clone.scheduled_at)
        self.assertIsNone(clone.session_id)
        self.assertIsNone(clone.parent_task_id)

    def test_muted_task_stays_muted(self):
        """0 — «уведомления выключены», и дефолтный чат не должен их воскрешать."""
        src = self._finished(prompt="тихая", tg_chat_id=0)

        self.assertEqual(db.clone_task(src.id).tg_chat_id, 0)

    def test_chat_override_wins(self):
        """Бот передаёт чат, в котором нажали кнопку, — он важнее исходного."""
        src = self._finished(prompt="из веба", tg_chat_id=111)

        self.assertEqual(db.clone_task(src.id, tg_chat_id=222).tg_chat_id, 222)

    def test_api_reruns_a_finished_task(self):
        src = self._finished(prompt="/review-queue", provider="claude-triage")

        r = self.client.post(f"/api/tasks/{src.id}/rerun")

        self.assertEqual(r.status_code, 201, r.text)
        clone = r.json()
        self.assertEqual(clone["prompt"], "/review-queue")
        self.assertEqual(clone["status"], "pending")
        self.assertNotEqual(clone["id"], src.id)

    def test_api_refuses_unfinished_and_unknown(self):
        pending = db.create_task(TaskCreate(prompt="ещё едет"))

        self.assertEqual(self.client.post(f"/api/tasks/{pending.id}/rerun").status_code, 409)
        self.assertEqual(self.client.post("/api/tasks/999999/rerun").status_code, 404)


if __name__ == "__main__":
    unittest.main()
