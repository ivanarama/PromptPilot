from promptpilot.config import build_cmd, cmd_runs_codex, load_providers


def test_codex_multiline_prompt_uses_stdin_transport():
    prompt = "# Роль\n\nВыполни задачу полностью.\nИТОГ: ГОТОВО"
    providers = load_providers()

    command = build_cmd("codex", prompt)

    assert providers["codex"]["prompt_stdin"] is True
    assert command == ["codex", "exec", "-"]
    assert prompt not in command


def test_codex_model_option_is_inserted_before_stdin_marker():
    command = build_cmd("codex", "line one\nline two", model="gpt-test")

    assert command == ["codex", "exec", "--model", "gpt-test", "-"]


def test_codex_effort_is_passed_as_one_run_config_override():
    command = build_cmd("codex", "review carefully", effort="max")

    assert command == [
        "codex", "exec", "-c", 'model_reasoning_effort="max"', "-",
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
