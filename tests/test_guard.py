"""The guard is the only "never do this" for runs without a permission dialog.

Its rules are regular expressions tuned against two failure modes: missing a
catastrophic command, and firing on prose (a guard that blocks commit messages
gets switched off). Both directions are pinned here.
"""

import io
import json

import pytest

from promptpilot import config, guard


@pytest.fixture(autouse=True)
def isolated_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(guard, "_current_branch", lambda cwd: "feature/work")
    return tmp_path


@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf ~",
    "rm -fr $HOME",
    "rm -rf ${HOME}/",
    "rm -rf .git",
    "rm -rf ./repo/.git/",
    "git push --force origin feature",
    "git push -f",
    "git push --force-with-lease origin feature",
    "git push origin --delete feature",
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin HEAD:refs/heads/master",
    "git push origin :main",
    # global options between git and push used to hide the push entirely
    "git -C . push --force",
    "git -C '/path with space' push -f origin feature",
    "git -c user.name=bot push origin main",
    "git --no-pager push --delete origin feature",
    "git worktree remove ../other-task",
    "git worktree prune",
    "echo '{\"replace\":[]}' > ~/.promptpilot/guard.json",
    "printf x | tee ~/.promptpilot/guard.json",
    "rm ~/.promptpilot/claude-settings.json",
    "sed -i s/a/b/ ~/.promptpilot/guard.json",
    "sudo apt-get install nginx",
    "cd /tmp && sudo rm -rf build",
    "ls; su root",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "shutdown -h now",
    "make && reboot",
    "echo $(halt)",
])
def test_catastrophic_commands_are_blocked(command):
    assert guard.check(command, cwd="/repo", tool="Bash")


@pytest.mark.parametrize("command", [
    'git commit -m "drop sudo from the install docs"',
    'git commit -m "halt the retry loop on 429"',
    "git push origin main-fix",
    "git push origin feature/main",
    "git push origin fix/main-menu",
    "git push -u origin feature/work",
    "git -C /repo push -u origin feature/work",
    "git -C /repo status",
    "rm -rf build/ dist/",
    "rm -rf ./node_modules",
    "dd if=big.img of=/dev/null",
    "grep -rn master docs/",
    "git log main..HEAD",
    "cat .gitignore",
    "python -m pytest -q",
])
def test_ordinary_work_is_not_blocked(command):
    assert guard.check(command, cwd="/repo", tool="Bash") == ""


def test_push_from_trunk_is_blocked_even_without_a_branch_name(monkeypatch):
    seen = []
    monkeypatch.setattr(guard, "_current_branch",
                        lambda cwd: seen.append(cwd) or "main")

    assert guard.check("git push origin HEAD", cwd="/repo")
    assert guard.check("git -C /other push", cwd="/repo")
    assert guard.check("cd /third && git push", cwd="/repo")
    assert seen == ["/repo", "/other", "/third"]


def test_file_tools_cannot_edit_the_guard_config():
    payload = json.dumps({"file_path": "/home/u/.promptpilot/guard.json",
                          "content": "{\"replace\": []}"})

    assert guard.check(payload, tool="Write")
    assert guard.check(json.dumps({"file_path": "/repo/src/app.py"}), tool="Write") == ""


def test_extend_replace_and_broken_rule_files(isolated_guard):
    rules_file = isolated_guard / "guard.json"

    rules_file.write_text(json.dumps({"extend": [
        {"pattern": r"npm\s+publish", "reason": "Публикация — только руками"},
        {"pattern": "([unbalanced", "reason": "invalid regex is skipped"},
    ]}), encoding="utf-8")
    assert guard.check("npm publish") == "Публикация — только руками"
    assert guard.check("rm -rf /")  # built-ins still apply

    rules_file.write_text(json.dumps({"replace": [
        {"pattern": r"terraform\s+destroy"},
    ]}), encoding="utf-8")
    assert guard.check("terraform destroy")
    assert guard.check("rm -rf /") == ""  # replace drops the built-ins

    rules_file.write_text("{not json", encoding="utf-8")
    assert guard.check("rm -rf /")  # a broken file keeps the defaults


def test_hook_blocks_with_exit_code_2_and_logs(isolated_guard, monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "sudo reboot"},
               "cwd": "/repo"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert guard.main() == 2
    assert "ЗАПРЕЩЕНО" in capsys.readouterr().err
    assert "sudo reboot" in (isolated_guard / "guard.log").read_text(encoding="utf-8")


def test_hook_lets_ordinary_and_unparseable_input_through(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})))
    assert guard.main() == 0

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert guard.main() == 0


@pytest.mark.parametrize(("mode", "cfg", "skip", "expected"), [
    ("auto", {"supports_skills": True}, True, True),
    ("auto", {"supports_skills": True}, False, False),
    ("auto", {"kind": "claude", "executor": "herdr"}, True, True),
    ("1", {"supports_skills": True}, False, True),
    ("0", {"supports_skills": True}, True, False),
    # The hook is a Claude Code mechanism: other CLIs run unguarded. This is
    # documented in the README; keep the test so a change is deliberate.
    ("auto", {"kind": "codex"}, True, False),
    ("1", {"cmd": "agy -p {prompt}"}, True, False),
])
def test_guard_is_wired_only_into_claude_code_runs(monkeypatch, mode, cfg, skip, expected):
    monkeypatch.setattr(config, "GUARD", mode)

    assert config.guard_enabled(cfg, skip) is expected


def test_build_cmd_adds_the_hook_settings_for_autonomous_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "GUARD", "auto")

    cmd = config.build_cmd("claude", "do it", skip_permissions=True)

    assert "--dangerously-skip-permissions" in cmd
    settings = cmd[cmd.index("--settings") + 1]
    hooks = json.loads((tmp_path / "claude-settings.json").read_text(encoding="utf-8"))
    assert settings == str(tmp_path / "claude-settings.json")
    assert {entry["matcher"] for entry in hooks["hooks"]["PreToolUse"]} == {
        "Bash", "Write|Edit|MultiEdit", "mcp__.*"}
