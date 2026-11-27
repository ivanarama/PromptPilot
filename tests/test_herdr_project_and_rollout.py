import json
import os
import sqlite3
from types import SimpleNamespace

from promptpilot import codex_rollout, herdr_exec


def test_task_tab_uses_exact_project_path_even_when_another_workspace_is_focused(tmp_path, monkeypatch):
    wanted = str(tmp_path / "sample-project")
    calls = []

    def run(args):
        calls.append(args)
        if args == ["workspace", "list"]:
            return 0, {"result": {"workspaces": [
                {"workspace_id": "unrelated", "focused": True, "label": "sample-project"},
                {"workspace_id": "project", "worktree": {"checkout_path": wanted}},
            ]}}, ""
        return 0, {}, ""

    monkeypatch.setattr(herdr_exec, "_run", run)
    assert herdr_exec._create_task_tab(
        wanted, "pp-t42", ["--env", "PP_TASK_ID=42"], None, project_workspace=True)[0] == 0
    assert calls[-1][-4:] == ["--cwd", wanted, "--workspace", "project"]


def test_rollout_requires_the_foreground_process_and_exact_prompt(tmp_path):
    folder = tmp_path / "sessions"
    folder.mkdir()
    project = r"D:\Projects\sample-project"
    exact_prompt = "Ответь одним словом ОК"
    db = sqlite3.connect(tmp_path / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id, rollout_path, cwd, created_at_ms, thread_source)")
    for name in ("mine", "other"):
        db.execute("INSERT INTO threads VALUES(?, ?, ?, ?, 'user')",
                   (name, str(folder / f"{name}.jsonl"), project, 2000))
    db.commit()
    db.close()
    db = sqlite3.connect(tmp_path / "logs_2.sqlite")
    db.execute("CREATE TABLE logs (thread_id, ts, process_uuid)")
    db.executemany("INSERT INTO logs VALUES(?, 2, ?)", [("mine", "pid:42:uuid"), ("other", "pid:99:uuid")])
    db.commit()
    db.close()

    def write(path, events, final_newline=True):
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(json.dumps({"type": kind, "payload": payload}) for kind, payload in events))
            if final_newline:
                f.write("\n")

    path = folder / "mine.jsonl"
    write(path, [
        ("session_meta", {"id": "mine"}),
        ("turn_context", {"turn_id": "unrelated"}),
        ("response_item", {"role": "user", "content": [{"text": "Wrong prompt"}]}),
        ("event_msg", {"type": "task_complete", "turn_id": "unrelated", "last_agent_message": "Wrong"}),
        ("turn_context", {"turn_id": "expected"}),
        ("response_item", {"role": "user", "content": [{"text": exact_prompt}]}),
    ])
    assert codex_rollout.journal_home({"env": {"CODEX_HOME": str(tmp_path)}}) == tmp_path
    r = codex_rollout.Rollout(tmp_path, 42, 1, project, exact_prompt)
    assert r.poll() is None
    write(path, [("event_msg", {"type": "task_complete", "turn_id": "expected", "last_agent_message": "ОК"})],
          final_newline=False)
    assert r.poll() is None
    with path.open("a", encoding="utf-8") as f:
        f.write(os.linesep)
    assert r.poll() == "ОК"



def test_clean_user_turn_keeps_the_delivery_instructions_and_a_later_note(tmp_path, monkeypatch):
    task = SimpleNamespace(prompt="Prepare this report", id=42, tg_chat_id=7,
                           working_dir=str(tmp_path), started_at="start")
    delivery = codex_rollout.prompt_instruction(task)
    assert delivery
    monkeypatch.setattr(codex_rollout, "journal_home", lambda provider: tmp_path)
    prompt = task.prompt + delivery + "\n\nLatest note"
    args = []

    home, submitted = codex_rollout.prepare_prompt(task, {}, prompt, args)

    assert home == tmp_path
    assert submitted == task.prompt + "\n\nLatest note"
    assert args[0] == "-c"
    assert delivery.strip() in json.loads(args[1].removeprefix("developer_instructions="))
