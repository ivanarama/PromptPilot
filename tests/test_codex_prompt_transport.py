import os

from promptpilot.config import build_cmd, cmd_runs_codex, get_provider_env, load_providers
from promptpilot.worker import _remember_stream_session, format_result, parse_stream_json


def test_codex_multiline_prompt_uses_stdin_transport():
    prompt = "# Роль\n\nВыполни задачу полностью.\nИТОГ: ГОТОВО"
    providers = load_providers()

    command = build_cmd("codex", prompt)

    assert providers["codex"]["prompt_stdin"] is True
    assert command == ["codex", "exec", "--json", "-"]
    assert prompt not in command


def test_codex_model_option_is_inserted_before_stdin_marker():
    command = build_cmd("codex", "line one\nline two", model="gpt-test")

    assert command == ["codex", "exec", "--json", "--model", "gpt-test", "-"]


def test_codex_effort_is_passed_as_one_run_config_override():
    command = build_cmd("codex", "review carefully", effort="max")

    assert command == [
        "codex", "exec", "--json", "-c", 'model_reasoning_effort="max"', "-",
    ]


def test_codex_provider_effort_is_used_when_task_has_no_override(monkeypatch):
    providers = load_providers()
    providers["codex"] = {**providers["codex"], "effort": "high"}
    monkeypatch.setattr("promptpilot.config.load_providers", lambda: providers)

    command = build_cmd("codex", "triage")

    assert command[-3:] == ["-c", 'model_reasoning_effort="high"', "-"]


def test_codex_command_detection_accepts_windows_shims_and_paths():
    assert cmd_runs_codex("codex exec -")
    assert cmd_runs_codex(r'"C:\\Tools\\codex.CMD" exec -')
    assert not cmd_runs_codex("claude -p {prompt}")


def test_codex_skip_permissions_uses_codex_autonomous_flag():
    command = build_cmd("codex", "run maintenance", skip_permissions=True)

    assert command == [
        "codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-",
    ]


def test_provider_env_exposes_configured_pipeline_tools_on_path(monkeypatch, tmp_path):
    gh = tmp_path / "github" / "gh.exe"
    go = tmp_path / "golang" / "go.exe"
    gh.parent.mkdir()
    go.parent.mkdir()
    gh.touch()
    go.touch()
    monkeypatch.setenv("PP_GH_EXE", str(gh))
    monkeypatch.setenv("PP_GO_EXE", str(go))
    monkeypatch.setenv("PATH", "existing")

    env = get_provider_env("codex")

    assert env["GH_EXE"] == str(gh)
    assert env["GO_EXE"] == str(go)
    path_parts = env["PATH"].split(os.pathsep)
    assert path_parts[:2] == [str(gh.parent), str(go.parent)]
    assert "existing" in path_parts


def test_codex_resume_uses_exec_resume_with_stdin_after_session():
    command = build_cmd(
        "codex", "continue", session_id="01a06119-37cf-7522-a071-14386645fd47",
        effort="high", skip_permissions=True,
    )

    assert command == [
        "codex", "exec", "resume", "--json", "-c", 'model_reasoning_effort="high"',
        "--dangerously-bypass-approvals-and-sandbox",
        "01a06119-37cf-7522-a071-14386645fd47", "-",
    ]


def test_codex_stream_persists_session_before_completion(monkeypatch):
    seen = []
    monkeypatch.setattr("promptpilot.worker.db.set_session_id",
                        lambda task_id, session_id: seen.append((task_id, session_id)))

    _remember_stream_session(
        182, '{"type":"thread.started","thread_id":"thread-live"}\n')
    _remember_stream_session(182, '{"type":"turn.started"}\n')

    assert seen == [(182, "thread-live")]


def test_codex_jsonl_extracts_final_message_session_and_usage():
    parsed = parse_stream_json("\n".join([
        '{"type":"thread.started","thread_id":"thread-123"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"type":"reasoning","text":"hidden"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ИТОГ: ГОТОВО"}}',
        '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":80,"output_tokens":30,"reasoning_output_tokens":10}}',
    ]))

    assert parsed["text"] == "ИТОГ: ГОТОВО"
    assert parsed["meta"] == {
        "session_id": "thread-123",
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 30,
        "reasoning_output_tokens": 10,
        "total_tokens": 150,
    }
    rendered = format_result(parsed)
    assert "Tokens: 120 in / 30 out" in rendered
    assert "Cached input: 80" in rendered
    assert "Session: thread-123" in rendered
