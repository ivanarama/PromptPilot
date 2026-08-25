"""Расписание в боте: серии, действия над ними и сторож обрыва.

Run: python3 -m unittest discover -s tests
"""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("PP_DATA_DIR", _TMP.name)

from promptpilot import bot, db  # noqa: E402
from promptpilot.models import TaskCreate  # noqa: E402


class FakeBot:
    """Телеграм-бот, который только запоминает отправленное."""

    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})


class ScheduleTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.addClassCleanup(_TMP.cleanup)

    def setUp(self):
        with db._connect() as conn:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM settings")

    def _series_task(self, prompt="/triage-issues", chat_id=42, **kw):
        params = dict(prompt=prompt, working_dir="/repo", recurrence="4h",
                      tg_chat_id=chat_id, provider="claude", priority=4)
        params.update(kw)
        return db.create_task(TaskCreate(**params))


class NextOccurrenceTest(ScheduleTestBase):
    """Общий помощник: воркер и кнопка «Возобновить» строят вхождение одинаково."""

    def test_schedule_bound_fields_are_carried_over(self):
        task = self._series_task(machine="vm2", keep_pane=False, worktree=True,
                                 effort="low", task_timeout=900, skip_permissions=True)
        when = datetime.now(timezone.utc) + timedelta(hours=4)

        nxt = db.next_occurrence(task, when)

        self.assertEqual(nxt.machine, "vm2")
        self.assertFalse(nxt.keep_pane)
        self.assertTrue(nxt.worktree)
        self.assertEqual(nxt.effort, "low")
        self.assertEqual(nxt.task_timeout, 900)
        self.assertTrue(nxt.skip_permissions)
        self.assertEqual(nxt.recurrence, "4h")
        self.assertEqual(nxt.tg_chat_id, 42)
        self.assertEqual(nxt.scheduled_at, when)

    def test_prompt_and_chat_can_be_overridden(self):
        task = self._series_task()
        nxt = db.next_occurrence(task, datetime.now(timezone.utc),
                                 prompt="сохранённый", tg_chat_id=7)
        self.assertEqual(nxt.prompt, "сохранённый")
        self.assertEqual(nxt.tg_chat_id, 7)


class WorkerRecurrenceTest(ScheduleTestBase):
    """Воркер продлевает серию тем же помощником — рефактор ничего не потерял."""

    def test_maybe_recur_creates_the_next_occurrence(self):
        from promptpilot import worker

        task = self._series_task(machine="vm2", worktree=True, effort="low")
        db.mark_completed(task.id, "готово")

        worker._maybe_recur(db.get_task(task.id))

        nxt = [t for t in db.list_tasks(limit=10) if t.id != task.id]
        self.assertEqual(len(nxt), 1)
        self.assertEqual(nxt[0].machine, "vm2")
        self.assertTrue(nxt[0].worktree)
        self.assertEqual(nxt[0].effort, "low")
        self.assertEqual(nxt[0].tg_chat_id, 42)
        self.assertIsNotNone(nxt[0].scheduled_at)

    def test_recurrence_uses_the_stored_prompt_not_the_run_one(self):
        from promptpilot import worker

        task = self._series_task()
        db.mark_completed(task.id, "готово")
        run_copy = db.get_task(task.id)
        run_copy.prompt = "промпт с разовой припиской"

        worker._maybe_recur(run_copy)

        nxt = [t for t in db.list_tasks(limit=10) if t.id != task.id]
        self.assertEqual(nxt[0].prompt, "/triage-issues")


class SeriesLookupTest(ScheduleTestBase):
    def test_handle_prefers_the_next_occurrence(self):
        old = self._series_task()
        db.mark_completed(old.id, "готово")
        new = self._series_task()

        series = db.list_series()[0]
        self.assertEqual(bot._series_handle(series), new.id)
        self.assertEqual(bot._find_series(new.id)["prompt"], "/triage-issues")

    def test_broken_series_is_found_by_its_last_run(self):
        task = self._series_task()
        db.mark_failed(task.id, "упало")

        series = db.list_series()[0]
        self.assertTrue(series["broken"])
        self.assertEqual(bot._series_handle(series), task.id)
        self.assertIsNotNone(bot._find_series(task.id))

    def test_unknown_handle_is_not_an_error(self):
        self.assertIsNone(bot._find_series(999999))


class SeriesWhenTest(unittest.TestCase):
    """list_series отдаёт сырые ISO-строки, а не datetime."""

    def test_iso_string_is_formatted(self):
        out = bot._series_when("2026-08-25T12:00:00+00:00")
        self.assertNotEqual(out, "—")
        self.assertIn("25.08.2026", out)

    def test_empty_and_garbage_survive(self):
        self.assertEqual(bot._series_when(None), "—")
        self.assertEqual(bot._series_when("не дата"), "не дата")


class ScheduleWatchTest(ScheduleTestBase):
    """Сторож обрыва: молчание бота не должно быть неотличимо от «всё хорошо»."""

    def _tick(self, fake=None):
        fake = fake or FakeBot()
        asyncio.run(bot._schedule_watch_tick(fake))
        return fake

    def test_broken_series_is_reported_once(self):
        task = self._series_task()
        db.mark_completed(task.id, "готово")

        fake = self._tick()
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["chat_id"], 42)
        self.assertIn("оборвана", fake.sent[0]["text"])

        # второй тик молчит — иначе это спам каждые 15 минут
        self.assertEqual(len(self._tick().sent), 0)

    def test_failed_run_is_reported_too(self):
        task = self._series_task()
        db.mark_failed(task.id, "rate limit")
        self.assertEqual(len(self._tick().sent), 1)

    def test_live_series_is_silent(self):
        old = self._series_task()
        db.mark_completed(old.id, "готово")
        self._series_task()  # следующее вхождение на месте
        self.assertEqual(len(self._tick().sent), 0)

    def test_cancelled_series_is_silent(self):
        """Отменённая серия — решение человека, а не обрыв."""
        task = self._series_task()
        db.cancel_task(task.id)
        self.assertEqual(len(self._tick().sent), 0)

    def test_series_without_a_chat_is_skipped(self):
        task = self._series_task(chat_id=None)
        db.mark_completed(task.id, "готово")
        self.assertEqual(len(self._tick().sent), 0)

    def test_failed_delivery_is_retried_next_tick(self):
        task = self._series_task()
        db.mark_completed(task.id, "готово")

        self._tick(FakeBot(fail=True))
        self.assertEqual(len(self._tick().sent), 1)

    def test_a_new_break_after_a_resume_is_reported_again(self):
        first = self._series_task()
        db.mark_completed(first.id, "готово")
        self.assertEqual(len(self._tick().sent), 1)

        # серия продолжена и оборвалась снова — это новое событие
        second = self._series_task()
        db.mark_completed(second.id, "готово")
        self.assertEqual(len(self._tick().sent), 1)

    def test_marking_a_break_seen_silences_the_watcher(self):
        """Кнопки «Остановить» и «Возобновить» гасят уведомление сами."""
        task = self._series_task()
        db.mark_completed(task.id, "готово")
        series = db.list_series()[0]

        bot._mark_break_seen(series, reason="cancelled")
        self.assertEqual(len(self._tick().sent), 0)


class ScreenRenderTest(ScheduleTestBase):
    def test_empty_schedule_says_so(self):
        text, kb = bot._schedule_screen()
        self.assertIsNone(kb)
        self.assertIn("Повторяющихся задач нет", text)

    def test_broken_series_is_marked_and_offers_resume(self):
        task = self._series_task()
        db.mark_failed(task.id, "упало")

        text, kb = bot._schedule_screen()
        self.assertIn("оборвано серий: 1", text)

        card, keyboard = bot._series_card(db.list_series()[0])
        data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"ser_resume:{task.id}", data)
        self.assertNotIn(f"ser_now:{task.id}", data)

    def test_live_series_offers_run_now_and_stop(self):
        task = self._series_task()
        _, keyboard = bot._series_card(db.list_series()[0])
        data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"ser_now:{task.id}", data)
        self.assertIn(f"ser_stop:{task.id}", data)


if __name__ == "__main__":
    unittest.main()
