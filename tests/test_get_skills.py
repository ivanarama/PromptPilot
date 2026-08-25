"""get_skills(): resolution order and plugin enablement.

Run: python3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from promptpilot import config  # noqa: E402


def _skill(dir_path: Path, name: str, description: str):
    """Write a subdir-style skill: <dir>/<name>/SKILL.md with frontmatter."""
    sub = dir_path / name
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n",
        encoding="utf-8",
    )


class GetSkillsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.project = root / "project"
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.project / ".claude" / "skills").mkdir(parents=True)
        # ~/.claude/plugins/marketplaces/<marketplace>/plugins/<plugin>/commands
        self.market = self.home / ".claude" / "plugins" / "marketplaces" / "mp"
        patcher = mock.patch.object(Path, "home", classmethod(lambda cls: self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _plugin(self, name: str, description: str, command: str = "shared"):
        cmds = self.market / "plugins" / name / "commands"
        cmds.mkdir(parents=True, exist_ok=True)
        (cmds / f"{command}.md").write_text(
            f"---\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
        )

    def _settings(self, enabled: dict, path: Path = None):
        path = path or self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabledPlugins": enabled}), encoding="utf-8")

    def _by_name(self, working_dir=None):
        return {s["name"]: s for s in config.get_skills(working_dir=working_dir)}

    def test_project_skill_wins_over_user_and_plugin(self):
        _skill(self.project / ".claude" / "skills", "shared", "from project")
        _skill(self.home / ".claude" / "skills", "shared", "from user")
        self._plugin("toolkit", "from plugin")
        self._settings({"toolkit@mp": True})

        found = self._by_name(str(self.project))
        self.assertEqual(found["shared"]["source"], "local")
        self.assertEqual(found["shared"]["description"], "from project")

    def test_user_skill_wins_over_plugin(self):
        _skill(self.home / ".claude" / "skills", "shared", "from user")
        self._plugin("toolkit", "from plugin")
        self._settings({"toolkit@mp": True})

        found = self._by_name()
        self.assertEqual(found["shared"]["source"], "user")

    def test_plugin_commands_hidden_when_nothing_enabled(self):
        """A marketplace clone alone enables nothing — and lists nothing."""
        self._plugin("toolkit", "from plugin", command="review-pr")

        self.assertEqual(self._by_name(), {})

    def test_enabled_plugin_is_listed(self):
        self._plugin("toolkit", "from plugin", command="review-pr")
        self._settings({"toolkit@mp": True})

        found = self._by_name()
        self.assertEqual(found["review-pr"]["source"], "plugin:toolkit")

    def test_disabled_plugin_is_not_listed(self):
        self._plugin("toolkit", "from plugin", command="review-pr")
        self._settings({"toolkit@mp": False})

        self.assertEqual(self._by_name(), {})

    def test_bare_plugin_name_key_is_honoured(self):
        """Older Claude Code spelled the key without @marketplace."""
        self._plugin("toolkit", "from plugin", command="review-pr")
        self._settings({"toolkit": True})

        self.assertIn("review-pr", self._by_name())

    def test_project_settings_can_disable_a_user_enabled_plugin(self):
        self._plugin("toolkit", "from plugin", command="review-pr")
        self._settings({"toolkit@mp": True})
        self._settings({"toolkit@mp": False},
                       self.project / ".claude" / "settings.json")

        self.assertEqual(self._by_name(str(self.project)), {})

    def test_enabled_plugins_read_from_claude_json_projects_section(self):
        self._plugin("toolkit", "from plugin", command="review-pr")
        (self.home / ".claude.json").write_text(
            json.dumps({"projects": {str(self.project): {"enabledPlugins": {"toolkit@mp": True}}}}),
            encoding="utf-8",
        )

        self.assertIn("review-pr", self._by_name(str(self.project)))
        self.assertEqual(self._by_name(), {})  # other directories: still off

    def test_broken_settings_file_does_not_break_the_list(self):
        _skill(self.home / ".claude" / "skills", "solo", "from user")
        (self.home / ".claude" / "settings.json").write_text("{ not json", encoding="utf-8")

        self.assertIn("solo", self._by_name())


if __name__ == "__main__":
    unittest.main()
