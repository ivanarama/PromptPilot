import json

from promptpilot.config import BUILTIN_PROVIDERS
from promptpilot.worker import detect_unrecognized_model, format_result


def _stream_output(model: str) -> str:
    return "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}),
        f"[claude-code:unrecognized_model] {json.dumps({'model': model, 'query_source': 'sdk'})}",
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Ответ."}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": "Ответ.",
                    "modelUsage": {"glm-4.7": {"input_tokens": 10, "output_tokens": 5}}}),
    ])


def test_unrecognized_model_detected_on_stderr_only():
    # The bridge prints the marker outside the stream-json flow (commonly
    # stderr); stdout stays clean. Issue #81.
    assert detect_unrecognized_model(_stream_output("GLM-5.3-Flash")) == "GLM-5.3-Flash"
    assert detect_unrecognized_model("") is None
    assert detect_unrecognized_model(json.dumps({"type": "system"})) is None


def test_unrecognized_model_survives_malformed_marker_payload():
    assert detect_unrecognized_model("[claude-code:unrecognized_model] not-json") == "unknown"


def test_format_result_warns_about_silent_fallback():
    parsed = {"text": "Ответ.", "meta": {"model": "glm-4.7", "unrecognized_model": "GLM-5.3-Flash"}}

    rendered = format_result(parsed)

    assert "GLM-5.3-Flash" in rendered
    assert "не распознана" in rendered
    assert "Model: glm-4.7" in rendered


def test_format_result_without_warning_is_unchanged():
    parsed = {"text": "Ответ.", "meta": {"model": "glm-5.3"}}

    rendered = format_result(parsed)

    assert "не распознана" not in rendered
    assert rendered.startswith("Ответ.")


def test_claude_z_defaults_track_current_glm_line():
    # BUILTIN, not load_providers(): the local ~/.promptpilot config may
    # override env for a specific z.ai plan, the shipped default must still
    # be a current model (issue #81: shipped glm-4.7 is retired).
    env = BUILTIN_PROVIDERS["claude-z"]["env"]

    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "glm-5.3"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "glm-5.3"
