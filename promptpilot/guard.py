"""Hard limits for unattended runs — a PreToolUse hook, not a dialog.

A task with ``skip_permissions`` runs Claude Code with
``--dangerously-skip-permissions``: nothing asks the human any more, so
nothing stops the agent either. A PreToolUse hook does — it sees the command
before it runs and fires whatever the permission mode is. That makes it the
only place where "never do this" actually holds for an autonomous task.

Blocking is exit code 2 plus a reason on stderr; Claude Code reads that reason
and tells the model why. Everything blocked is appended to
``~/.promptpilot/guard.log``, so it is visible where an agent tried to go.

The defaults below are deliberately narrow: things that are catastrophic and
never part of legitimate work. Rules are patterns matched against the whole
command line, and ``~/.promptpilot/guard.json`` extends or replaces them::

    {"extend": [{"pattern": "npm\\\\s+publish", "reason": "Публикация — только руками"}]}
    {"replace": [{"pattern": "...", "reason": "..."}]}

This module is also the hook executable: it is invoked as
``<python> -m promptpilot guard-hook`` (or ``pp.exe guard-hook`` when frozen),
reads the hook payload on stdin and exits. It leans on nothing but the standard
library on purpose — a hook that fails to import would fail open.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Where a command actually begins: start of the line, or right after a
# separator. Without this anchor a word like "sudo" or "halt" inside a commit
# message would be blocked, and a guard that fires on prose gets switched off.
CMD = r"(?:^|[;&|]\s*|\$\(\s*|`\s*|\n\s*)"

# A branch name may contain hyphens, so \b is too loose: it makes "main-fix"
# look like "main".
TRUNK = r"(?<![\w-])(?:main|master)(?![\w-])"

# (pattern, what the model is told). Matched case-insensitively against
# "<tool name> <command or serialised arguments>".
DEFAULT_RULES = [
    # Deliberately not anchored to CMD: this one is worth a false positive.
    (r"\brm\s+-[a-z]*[rf][a-z]*\s+(?:/|~|\$HOME|\$\{HOME\})(?:\s|/?$)",
     "Удаление корня или домашнего каталога запрещено."),

    (r"\brm\s+-[a-z]*r[a-z]*f?\s+[^|;&]*\.git(?:\s|/|$)",
     "Удаление .git запрещено: это вся история репозитория."),

    (r"\bgit\s+push\b[^|;&]*(?:--force(?:-with-lease)?|--delete|\s-f\b)",
     "Форс-пуш и удаление веток на сервере запрещены: перепишешь чужую работу."),

    (r"\bgit\s+push\b[^|;&]*" + TRUNK,
     "Пуш в main/master запрещён: работай в своей ветке, вливает человек."),

    (r"\bgit\s+worktree\s+(?:remove|prune)\b",
     "Чужие worktree не трогаем — в них идёт работа других задач."),

    (CMD + r"(?:sudo|su)\s",
     "Повышение прав запрещено: задача работает от своего пользователя."),

    # /dev/null and friends are an idiom, not a wiped disk.
    (CMD + r"mkfs|\bdd\s+[^|;&]*\bof=/dev/(?!null|zero|stdout|stderr)",
     "Запись на устройство запрещена."),

    (CMD + r"(?:shutdown|reboot|poweroff|halt)\b",
     "Выключение и перезагрузка машины запрещены."),
]

# `git push origin HEAD` from main IS a push to main, and no pattern can see
# that — the branch has to be asked for separately.
TRUNK_BRANCHES = ("main", "master")


def _data_dir() -> Path:
    return Path(os.environ.get("PP_DATA_DIR", Path.home() / ".promptpilot"))


def load_rules() -> list:
    """Built-in rules, plus whatever ~/.promptpilot/guard.json says.

    A broken or unreadable file leaves the defaults in place: a guard that
    disarms itself over a stray comma is worse than no guard at all.
    """
    rules = list(DEFAULT_RULES)
    path = _data_dir() / "guard.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rules

    def parse(items):
        out = []
        for item in items or []:
            pattern, reason = item.get("pattern"), item.get("reason")
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error:
                continue
            out.append((pattern, reason or "Запрещено правилом из guard.json."))
        return out

    if isinstance(cfg, dict) and cfg.get("replace"):
        return parse(cfg["replace"])
    if isinstance(cfg, dict):
        rules += parse(cfg.get("extend"))
    elif isinstance(cfg, list):
        rules += parse(cfg)
    return rules


def _current_branch(cwd: str) -> str:
    try:
        r = subprocess.run(["git", "-C", cwd or ".", "symbolic-ref", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def check(command: str, cwd: str = "", tool: str = "") -> str:
    """Reason this command must not run, or "" when it may.

    The tool name is matched as well — a rule may name an MCP tool — but on a
    line of its own, so rules anchored to the start of a command do not mistake
    it for a prefix and quietly stop matching.
    """
    text = f"{tool}\n{command}" if tool else command
    for pattern, reason in load_rules():
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    if re.search(r"\bgit\s+push\b", text, re.IGNORECASE):
        branch = _current_branch(cwd)
        if branch in TRUNK_BRANCHES:
            return (f"Пуш из ветки {branch} запрещён: работай в своей ветке, "
                    f"вливает человек.")
    return ""


def _log(reason: str, text: str):
    path = _data_dir() / "guard.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f'{time.strftime("%F %T")}\t{reason.split(":")[0]}\t{text[:300]}\n')
    except OSError:
        pass


def main() -> int:
    """Hook entry point: payload on stdin, 2 = blocked, 0 = let it through."""
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # unparseable input is not the agent's fault — do not block work

    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input") or {}
    # Bash is judged by its command line; other tools by name and arguments.
    body = inp.get("command") or json.dumps(inp, ensure_ascii=False)

    reason = check(body, payload.get("cwd") or os.getcwd(), tool)
    if not reason:
        return 0
    _log(reason, f"{tool} {body}")
    print(f"ЗАПРЕЩЕНО. {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
