"""Exercise real process pipes and Windows npm shims without calling an agent."""

import io
import json
import os
import sys

import pytest

from promptpilot import config, worker
from promptpilot.models import TaskCreate


@pytest.mark.parametrize("provider", ["claude", "claude-z"])
def test_claude_prompt_uses_stdin_with_model_effort_and_resume(
        provider, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    prompt = 'Прочитай документы\n"quoted" & %PATH% <tag>\n' * 2000
    command = config.build_cmd(
        provider, prompt, model="opus", effort="high", session_id="session-1",
        skip_permissions=True, guard=False,
    )

    assert config.load_providers()[provider]["prompt_stdin"] is True
    assert len(" ".join(command)) < 2000
    assert prompt not in command
    for option, value in [("--model", "opus"), ("--effort", "high"),
                          ("--resume", "session-1")]:
        assert command[command.index(option) + 1] == value
    assert "--dangerously-skip-permissions" in command


def fake_claude(tmp_path, monkeypatch, script):
    """Keep the built-in transport and arguments; replace only the executable."""
    program = tmp_path / "fake claude.py"
    program.write_text(script, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "claude.cmd"
        shim.write_text(f'@echo off\n"{sys.executable}" "{program}" %*\n', encoding="utf-8")
        executable = config._q(str(shim))
    else:
        import shlex
        executable = f"{shlex.quote(sys.executable)} {shlex.quote(str(program))}"
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    providers = config.load_providers()
    providers["claude"] = dict(providers["claude"])
    providers["claude"]["cmd"] = executable + " -p --verbose --output-format stream-json"
    monkeypatch.setattr(config, "load_providers", lambda: providers)
    monkeypatch.setattr(worker, "load_providers", lambda: providers)
    monkeypatch.setattr(worker, "get_provider_env", lambda _: os.environ.copy())


@pytest.mark.parametrize("detached", [False, True])
def test_worker_delivers_full_multiline_prompt_through_cli_shim(
        isolated_db, tmp_path, monkeypatch, detached, request):
    prompt = 'Прочитай всё\n"quoted" & %PATH% <tag>\n' * 2000
    fake_claude(tmp_path, monkeypatch,
                "import json, pathlib, sys\n"
                "if sys.stderr is not None:\n"
                "    sys.stderr.write('startup output ' * 10000 + '\\n'); sys.stderr.flush()\n"
                "prompt = sys.stdin.buffer.read().decode('utf-8').replace('\\r\\n', '\\n')\n"
                "pathlib.Path('received.json').write_text(json.dumps(prompt), encoding='utf-8')\n"
                "if sys.stdout is not None:\n"
                "    print(json.dumps({'type': 'system', 'session_id': 'stdin-session'}), flush=True)\n"
                "    print(json.dumps({'type': 'result', 'result': 'ИТОГ: ГОТОВО'}), flush=True)\n")
    processes = []
    if detached:
        popen = worker.subprocess.Popen

        def capture_process(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)

            def cleanup():
                if process.poll() is None:
                    if os.name == "nt":
                        with popen(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=worker.subprocess.DEVNULL,
                                   stderr=worker.subprocess.DEVNULL) as cleanup_process:
                            cleanup_process.wait(timeout=30)
                    else:
                        process.kill()
                    process.wait(timeout=10)

            request.addfinalizer(cleanup)
            return process

        monkeypatch.setattr(worker.subprocess, "Popen", capture_process)
    created = isolated_db.create_task(TaskCreate(
        prompt=prompt, provider="claude", working_dir=str(tmp_path), detached=detached,
    ))

    worker.execute_task(isolated_db.get_next_runnable())
    for process in processes:
        assert process.wait(timeout=30) == 0

    received = tmp_path / "received.json"
    assert json.loads(received.read_text(encoding="utf-8")) == prompt
    settled = isolated_db.get_task(created.id)
    assert settled.status.value == "completed", settled.error
    if not detached:
        assert settled.session_id == "stdin-session"


@pytest.mark.parametrize("cancel", [False, True])
def test_large_stdin_does_not_block_timeout_or_cancellation(
        isolated_db, tmp_path, monkeypatch, cancel):
    fake_claude(tmp_path, monkeypatch, "import time; time.sleep(60)\n")
    created = isolated_db.create_task(TaskCreate(
        prompt="large prompt\n" * 100000, provider="claude",
        working_dir=str(tmp_path), task_timeout=1,
    ))
    monkeypatch.setattr(worker.db, "is_cancel_requested", lambda _: cancel)
    worker.execute_task(isolated_db.get_next_runnable())

    settled = isolated_db.get_task(created.id)
    assert settled.status.value == ("cancelled" if cancel else "failed")
    if not cancel:
        assert "Execution timed out" in settled.error


def test_pipe_recovers_oem_error_without_corrupting_utf8_or_session(monkeypatch):
    error = "Слишком длинная командная строка.\n"
    utf8 = "Русский ответ — готово ✅\n"
    event = json.dumps({"type": "system", "session_id": "kept-session"}) + "\n"
    raw = error.encode("cp866") + utf8.encode("utf-8") + event.encode("utf-8")
    pipe = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="surrogateescape")
    sessions = []
    monkeypatch.setattr(worker.db, "set_session_id", lambda *args: sessions.append(args))
    chunks = []

    worker._read_process_pipe(pipe, chunks, 30, "cp866")

    assert "".join(chunks) == error + utf8 + event
    assert sessions == [(30, "kept-session")]
    assert pipe.closed


def test_worker_stores_readable_windows_error(isolated_db, tmp_path, monkeypatch):
    error = "Слишком длинная командная строка.\n"
    fake_claude(tmp_path, monkeypatch,
                f"import sys; sys.stderr.buffer.write({error.encode('cp866')!r}); sys.exit(1)\n")
    monkeypatch.setattr(worker, "_windows_output_encoding", lambda: "cp866")
    created = isolated_db.create_task(TaskCreate(
        prompt="test error", provider="claude", working_dir=str(tmp_path),
    ))

    worker.execute_task(isolated_db.get_next_runnable())

    settled = isolated_db.get_task(created.id)
    assert settled.status.value == "failed"
    assert settled.exit_code == 1
    assert settled.error == error


def test_custom_argv_template_does_not_inherit_stdin_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    (tmp_path / "providers.json").write_text(json.dumps({
        "claude": {"cmd": "claude -p {prompt}"},
        "claude-z": {"hidden": True},
    }), encoding="utf-8")

    providers = config.load_providers()

    assert providers["claude"]["prompt_stdin"] is False
    assert config.build_cmd("claude", "custom prompt") == ["claude", "-p", "custom prompt"]
    assert providers["claude-z"]["prompt_stdin"] is True
