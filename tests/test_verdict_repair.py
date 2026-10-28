import json
import subprocess as stdlib_subprocess
from types import SimpleNamespace

import promptpilot.worker as worker
from promptpilot.worker import VERDICT_REPAIR_PROMPT, _repair_verdict

TASK = SimpleNamespace(id=1, model="glm-5.3-flash", effort=None)


def _fake_run(stdout="", stderr="", returncode=0, capture=None):
    calls = []

    def run(cmd, **kwargs):
        if capture is not None:
            capture.append((cmd, kwargs))
        return stdlib_subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run, calls


def test_repair_prompt_carries_report_tail_and_contract():
    prompt = VERDICT_REPAIR_PROMPT.format(tail="...body of the report")

    assert "...body of the report" in prompt
    assert "ИТОГ: ГОТОВО" in prompt
    assert "Не запускай никаких инструментов" in prompt


def test_repair_verdict_parses_plain_output(monkeypatch):
    run, _ = _fake_run(stdout="Отчёт готов.\nИТОГ: ГОТОВО — сделано\n")
    monkeypatch.setattr(worker.subprocess, "run", run)

    verdict = _repair_verdict(TASK, "claude", {}, "хвост отчёта", "/tmp")

    assert verdict == "ГОТОВО"


def test_repair_verdict_parses_stream_json_output(monkeypatch):
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-9"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ИТОГ: УЖЕ СДЕЛАНО — уже исправлено"}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False}),
    ])
    run, _ = _fake_run(stdout=stream)
    monkeypatch.setattr(worker.subprocess, "run", run)

    verdict = _repair_verdict(TASK, "claude", {}, "хвост", "/tmp")

    assert verdict == "УЖЕ СДЕЛАНО"


def test_repair_verdict_uses_stdin_transport_for_stdin_providers(monkeypatch):
    calls = []
    run, _ = _fake_run(stdout="ИТОГ: НЕ СМОГ — не вышло\n", capture=calls)
    monkeypatch.setattr(worker.subprocess, "run", run)

    verdict = _repair_verdict(TASK, "codex", {"prompt_stdin": True}, "хвост", "/tmp")

    assert verdict == "НЕ СМОГ"
    cmd, kwargs = calls[0]
    assert cmd[-1] == "-"
    assert kwargs["input"].startswith("Предыдущий ответ не содержал")
    assert "хвост" in kwargs["input"]


def test_repair_verdict_swallows_provider_failures(monkeypatch):
    def run(cmd, **kwargs):
        raise stdlib_subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(worker.subprocess, "run", run)

    assert _repair_verdict(TASK, "claude", {}, "хвост", "/tmp") == ""


def test_repair_verdict_returns_empty_when_model_stays_silent(monkeypatch):
    run, _ = _fake_run(stdout="Никакой строки вердикта, просто болтовня.\n")
    monkeypatch.setattr(worker.subprocess, "run", run)

    assert _repair_verdict(TASK, "claude", {}, "хвост", "/tmp") == ""
