"""Read a fresh Windows Codex terminal's completion from its native journal."""
from contextlib import closing
import json
import ntpath
from pathlib import Path
import sqlite3

from .config import codex_home, codex_user_settings
from .result_files import prompt_instruction


def windows_path(value):
    path = str(value).replace("/", "\\")
    return ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))


def journal_home(provider):
    home = codex_home(provider)
    try:
        with closing(_open_readonly(home / "state_5.sqlite")) as state:
            state.execute("SELECT id, rollout_path, cwd, created_at_ms, thread_source FROM threads LIMIT 0")
        with closing(_open_readonly(home / "logs_2.sqlite")) as logs:
            logs.execute("SELECT thread_id, ts, process_uuid FROM logs LIMIT 0")
    except (OSError, sqlite3.Error):
        return None
    return home


def prepare_prompt(task, provider, prompt, agent_args):
    """Move delivery instructions out of the first user message when we can track its turn."""
    if any("sqlite_home" in arg or "developer_instructions" in arg or arg == "resume" for arg in agent_args):
        return None, prompt
    home = journal_home(provider)
    if home is None:
        return None, prompt
    try:
        settings = codex_user_settings(home)
    except (ImportError, OSError, ValueError):
        return None, prompt
    sqlite_home = settings.get("sqlite_home")
    if sqlite_home and (not isinstance(sqlite_home, str)
                        or windows_path(Path(sqlite_home).expanduser()) != windows_path(home)):
        return None, prompt

    file_context = prompt_instruction(task)
    if not file_context or file_context not in prompt:
        return home, prompt
    base = task.prompt + file_context
    if not prompt.startswith(base):
        return None, prompt
    inherited = settings.get("developer_instructions")
    instructions = [inherited] if isinstance(inherited, str) and inherited else []
    instructions.append("For the first PromptPilot task in this Codex session only:\n" + file_context.strip())
    agent_args.extend(("-c", "developer_instructions=" + json.dumps("\n\n".join(instructions))))
    return home, task.prompt + prompt[len(base):]


def foreground_pid(process_info):
    processes = process_info.get("foreground_processes") or []
    pids = [p.get("pid") for p in processes
            if ntpath.basename(p.get("argv0") or "").lower() == "codex.exe"]
    return pids[0] if len(pids) == 1 and type(pids[0]) is int and pids[0] > 0 else None


def _open_readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.4)


class Rollout:
    def __init__(self, home, pid, launched_at, cwd, prompt):
        self.home = home
        self.pid = pid
        self.launched_at = launched_at
        self.cwd = windows_path(cwd)
        self.prompt = self._normalize(prompt)
        self.path = None
        self.thread_id = None
        self.metadata_verified = False
        self.offset = 0
        self.turn_id = None
        self.user_seen = False

    @staticmethod
    def _normalize(text):
        return text.replace("\r\n", "\n").strip()

    def _bind(self):
        with closing(_open_readonly(self.home / "state_5.sqlite")) as state:
            candidates = state.execute(
                "SELECT id, rollout_path, cwd FROM threads WHERE created_at_ms >= ? AND thread_source = 'user'",
                (int(self.launched_at * 1000),),
            ).fetchall()
        with closing(_open_readonly(self.home / "logs_2.sqlite")) as logs:
            owned = [(thread, path) for thread, path, cwd in candidates if windows_path(cwd) == self.cwd
                     and logs.execute("SELECT 1 FROM logs WHERE thread_id = ? AND ts >= ? "
                                      "AND process_uuid GLOB ? LIMIT 1",
                                      (thread, int(self.launched_at), f"pid:{self.pid}:*")).fetchone()]
        if len(owned) != 1:
            return False
        thread, raw_path = owned[0]
        path = Path(raw_path).resolve()
        if not path.is_relative_to((self.home / "sessions").resolve()):
            return False
        self.path, self.thread_id = path, thread
        return True

    def poll(self):
        """Return the exact observed turn's final text, or None while it is pending."""
        try:
            if self.path is None and not self._bind():
                return None
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break
                    self.offset = stream.tell()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    value = event.get("payload") if isinstance(event, dict) else None
                    if not isinstance(value, dict):
                        continue
                    kind = event.get("type")
                    if kind == "session_meta":
                        self.metadata_verified = value.get("id") == self.thread_id
                    elif kind == "turn_context" and self.turn_id != value.get("turn_id"):
                        self.turn_id, self.user_seen = value.get("turn_id"), False
                    elif kind == "response_item" and value.get("role") == "user":
                        items = value.get("content")
                        texts = (item.get("text") for item in items if isinstance(item, dict)) \
                            if isinstance(items, list) else ()
                        text = "\n".join(text for text in texts if isinstance(text, str))
                        self.user_seen = bool(self.turn_id and self._normalize(text) == self.prompt)
                    elif (kind == "event_msg" and value.get("type") == "task_complete"
                          and self.metadata_verified and self.user_seen and value.get("turn_id") == self.turn_id):
                        answer = value.get("last_agent_message")
                        return answer.strip() if isinstance(answer, str) else ""
        except (OSError, sqlite3.Error, ValueError):
            pass
        return None
