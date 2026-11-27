import os

import pytest

from promptpilot import herdr_exec


@pytest.mark.parametrize("unchanged", [True, False])
def test_owned_codex_prompt_is_pasted_once_and_only_submitted_to_same_idle_pane(monkeypatch, unchanged):
    started = {"pane_id": "p1", "terminal_id": "term", "agent_status": "idle", "state_change_seq": 12}
    current = dict(started, terminal_id="term" if unchanged else "replacement")
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["agent", "prompt"]:
            return 1, {"error": {"code": "agent_prompt_stalled"}}, "stalled"
        if args[:2] == ["agent", "get"]:
            return 0, {"result": {"agent": current}}, ""
        return 0, {}, ""

    monkeypatch.setattr(herdr_exec, "_run", run)
    status, _ = herdr_exec._submit_owned_windows_codex("task", "p1", "line 1\nline 2", started, None)

    assert calls[0][:4] == ["agent", "prompt", "task", "line 1\nline 2"]
    assert calls[1] == ["agent", "get", "task"]
    if unchanged:
        assert status == "working"
        assert calls[2:] == [["agent", "send-keys", "task", "right", "enter"]]
    else:
        assert status == "__error__"
        assert len(calls) == 2



def test_windows_herdr_gets_native_codex_when_only_npm_shim_is_on_path(tmp_path, monkeypatch):
    shim = tmp_path / "codex.cmd"
    executable = (tmp_path / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
                  / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(herdr_exec.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(herdr_exec.shutil, "which",
                        lambda command, path: str(shim) if command == "codex.cmd" else None)
    assert herdr_exec._windows_codex_pane_path(str(tmp_path)).split(os.pathsep) == [
        str(executable.parent), str(tmp_path),
    ]
