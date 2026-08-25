from promptpilot.herdr_exec import (
    _closing_workflow_verdict,
    _has_running_background_task,
    _trim_transcript,
)


def test_closing_workflow_verdict_ignores_echoed_contract_examples():
    transcript = """Prompt:
ИТОГ: ГОТОВО — example
ИТОГ: УЖЕ СДЕЛАНО — example
ИТОГ: НУЖЕН ЧЕЛОВЕК — example
ИТОГ: НЕ СМОГ — example
</promptpilot-workflow-contract>
"""

    assert _closing_workflow_verdict(transcript) == ""


def test_closing_workflow_verdict_accepts_only_final_line():
    assert _closing_workflow_verdict(
        "Проверки завершены.\nИТОГ: ГОТОВО — задача выполнена"
    ) == "ГОТОВО"
    assert _closing_workflow_verdict(
        "ИТОГ: ГОТОВО — промежуточно\nНо работа продолжается"
    ) == ""
    assert _closing_workflow_verdict(
        "  ИТОГ: ГОТОВО — задача выполнена, изменения и\n"
        "  проверки перечислены выше"
    ) == "ГОТОВО"


def test_agy_background_task_indicator_blocks_idle_completion():
    assert _has_running_background_task(
        "● [16:02:06] python -m pytest -q running"
    )
    assert not _has_running_background_task(
        "● [16:02:06] python -m pytest -q completed"
    )


def test_trim_transcript_uses_agy_greater_than_prompt_marker():
    prompt = "Recovery/finalization only. Inspect the completed work."
    transcript = """Old answer that must not be returned
─────────────────────────────────────────────────────
> Recovery/finalization only. Inspect the completed work.

● Bash(git status --short)

Проверка завершена, дерево чистое.
────────
Вторая секция отчёта также завершена.
ИТОГ: ГОТОВО — задача выполнена
─────────────────────────────────────────────────────
>
? for shortcuts               Gemini 3.7 Flash · high
"""

    cleaned = _trim_transcript(transcript, prompt)

    assert "Old answer" not in cleaned
    assert "Проверка завершена" in cleaned
    assert "Вторая секция отчёта" in cleaned
    assert cleaned.endswith("ИТОГ: ГОТОВО — задача выполнена")


def test_trim_transcript_drops_wrapped_workflow_contract_examples():
    prompt = """Final workflow registration only. Do not modify files.

<promptpilot-workflow-contract version="w1-verdict-v1">
ИТОГ: ГОТОВО — example
ИТОГ: НЕ СМОГ — example
</promptpilot-workflow-contract>"""
    transcript = """> Final workflow registration only. Do not modify
  files.
  <promptpilot-workflow-contract
  version="w1-verdict-v1">
  ИТОГ: ГОТОВО — example
  ИТОГ: НЕ СМОГ — example
  </promptpilot-workflow-contract>

● Bash(git status --short)
  Дерево чистое.
  ИТОГ: УЖЕ СДЕЛАНО — проверка завершена
─────────────────────────────────────────────────────
>
"""

    cleaned = _trim_transcript(transcript, prompt)

    assert "example" not in cleaned
    assert cleaned.startswith("● Bash")
    assert cleaned.endswith("ИТОГ: УЖЕ СДЕЛАНО — проверка завершена")
