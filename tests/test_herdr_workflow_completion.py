from promptpilot.herdr_exec import _closing_workflow_verdict


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
