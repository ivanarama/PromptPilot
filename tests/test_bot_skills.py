"""Скилы проекта из бота: рабочая папка, кандидаты и `/имя-скила` как промпт.

Run: python3 -m unittest discover -s tests
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PP_DATA_DIR", tempfile.mkdtemp())

from promptpilot import bot  # noqa: E402


def _make_skill(project: Path, name: str = "triage-issues", flat: bool = False):
    if flat:
        d = project / ".claude" / "commands"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
    else:
        d = project / ".claude" / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
    return project


class HasLocalSkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_subdir_layout(self):
        self.assertTrue(bot._has_local_skills(_make_skill(self.root / "a")))

    def test_flat_layout(self):
        self.assertTrue(bot._has_local_skills(_make_skill(self.root / "b", flat=True)))

    def test_plain_directory_has_none(self):
        plain = self.root / "c"
        plain.mkdir()
        self.assertFalse(bot._has_local_skills(plain))

    def test_missing_directory_is_not_an_error(self):
        self.assertFalse(bot._has_local_skills(self.root / "nope"))


class HistoryCandidatesTest(unittest.TestCase):
    """Без PP_PROJECTS_ROOT кандидаты берутся из рабочих папок прошлых задач."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.with_skills = _make_skill(self.root / "pipeline")
        self.without = self.root / "plain"
        self.without.mkdir()

    def test_only_dirs_with_skills_are_offered(self):
        hist = [str(self.without), str(self.with_skills)]
        with mock.patch.object(bot, "PROJECTS_ROOT", ""), \
             mock.patch.object(bot.db, "recent_working_dirs", return_value=hist):
            self.assertEqual(bot._skill_history_dirs(), [str(self.with_skills)])

    def test_db_failure_is_not_fatal(self):
        with mock.patch.object(bot.db, "recent_working_dirs", side_effect=RuntimeError):
            self.assertEqual(bot._skill_history_dirs(), [])

    def test_projects_root_entries_are_not_duplicated(self):
        with mock.patch.object(bot, "PROJECTS_ROOT", str(self.root)), \
             mock.patch.object(bot.db, "recent_working_dirs",
                               return_value=[str(self.with_skills)]):
            self.assertEqual(bot._skill_history_dirs(), [])


class DefaultWorkdirTest(unittest.TestCase):
    """`/skills` больше не спрашивает скилы «ниоткуда»."""

    def _ctx(self, **user_data):
        return SimpleNamespace(user_data=dict(user_data))

    def test_browsed_project_wins(self):
        ctx = self._ctx(skills_workdir="/browsed", new_dir="/wizard")
        self.assertEqual(bot._default_skills_workdir(1, ctx), "/browsed")

    def test_wizard_dir_is_next(self):
        ctx = self._ctx(new_dir="/wizard")
        self.assertEqual(bot._default_skills_workdir(1, ctx), "/wizard")

    def test_last_wizard_settings_then_history(self):
        with mock.patch.object(bot, "_load_last_settings", return_value={"new_dir": "/last"}):
            self.assertEqual(bot._default_skills_workdir(1, self._ctx()), "/last")

        with mock.patch.object(bot, "_load_last_settings", return_value=None), \
             mock.patch.object(bot.db, "recent_working_dirs", return_value=["/recent"]):
            self.assertEqual(bot._default_skills_workdir(1, self._ctx()), "/recent")

    def test_nothing_known_is_not_an_error(self):
        with mock.patch.object(bot, "_load_last_settings", return_value=None), \
             mock.patch.object(bot.db, "recent_working_dirs", return_value=[]):
            self.assertIsNone(bot._default_skills_workdir(1, self._ctx()))


class ProjectButtonTest(unittest.TestCase):
    def test_button_is_shown_even_without_projects_root(self):
        """Отсутствие настройки не должно выглядеть как отсутствие функции."""
        skills = [{"name": "x", "description": "", "argument_hint": "", "source": "user"}]
        with mock.patch.object(bot, "PROJECTS_ROOT", ""):
            _, keyboard = bot._build_skills_message(skills, "Скилы:", show_proj_btn=True)
        data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("skills_proj_picker", data)


class PromptCommandFilterTest(unittest.TestCase):
    """Шаг промпта принимает /имя-скила, но не крадёт команды бота."""

    def _reserved(self, text):
        return re.match(bot._RESERVED_CMD_RE, text) is not None

    def test_skill_names_pass_through_to_the_prompt(self):
        for text in ("/triage-issues", "/fix-approved 1122", "/merge-shepherd"):
            self.assertFalse(self._reserved(text), text)

    def test_bot_commands_stay_commands(self):
        for text in ("/cancel", "/skip", "/tasks", "/add", "/skills",
                     "/help@promptpilot_bot", "/stats now"):
            self.assertTrue(self._reserved(text), text)

    def test_prefix_collision_is_not_reserved(self):
        """`/addressbook` — это скил, а не команда /add."""
        self.assertFalse(self._reserved("/addressbook"))


if __name__ == "__main__":
    unittest.main()
