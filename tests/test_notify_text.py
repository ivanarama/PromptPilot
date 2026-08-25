"""Уведомление о завершении: чем задача была и чем кончилась.

Run: python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/_env.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # promptpilot

import _env  # noqa: E402  — своя база; импорт до promptpilot

from promptpilot import bot  # noqa: E402

_env.assert_isolated()


def _task(prompt="/review-queue", verdict=None):
    return SimpleNamespace(id=95, prompt=prompt, verdict=verdict)


class HeadlineTest(unittest.TestCase):
    def test_stage_name_is_the_headline(self):
        """У конвейера все прогоны на одну директорию — этап различает промпт."""
        self.assertEqual(bot._task_headline(_task()), "/review-queue")

    def test_only_the_first_line(self):
        self.assertEqual(bot._task_headline(_task("почини #1122\nподробности ниже")),
                         "почини #1122")

    def test_long_prompt_is_clipped(self):
        head = bot._task_headline(_task("разобрать заявку " + "очень длинная " * 10))
        self.assertLessEqual(len(head), 40)
        self.assertTrue(head.endswith("…"))

    def test_empty_prompt_is_not_an_error(self):
        self.assertEqual(bot._task_headline(_task("")), "")


class VerdictSuffixTest(unittest.TestCase):
    def test_verdict_rides_next_to_the_status(self):
        """«Выполнена» = агент дошёл до конца; нужен ли человек — говорит вердикт."""
        self.assertEqual(bot._verdict_suffix(_task(verdict="НУЖЕН ЧЕЛОВЕК")),
                         " — 🟡 НУЖЕН ЧЕЛОВЕК")

    def test_no_verdict_adds_nothing(self):
        self.assertEqual(bot._verdict_suffix(_task()), "")

    def test_unknown_verdict_still_shows(self):
        self.assertEqual(bot._verdict_suffix(_task(verdict="ЧТО-ТО")), " — • ЧТО-ТО")


if __name__ == "__main__":
    unittest.main()
