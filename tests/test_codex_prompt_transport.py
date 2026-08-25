from promptpilot.config import build_cmd, load_providers


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
