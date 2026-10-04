# -*- coding: utf-8 -*-
"""P24: парсер берёт ПОСЛЕДНЮЮ строку AUDIT_VERDICT.

Отчёты ревью цитируют прошлые аудиты и вывод гейта («гейт PASS на …»,
{{previous_review}} с их AUDIT_VERDICT) — первый матч может быть ЦИТАТОЙ.
Инциденты 2026-10-04: каскад видел ложный «PASS + открытый blocker» и
открывал человеческий блокер, хотя финальный вердикт был REVISION_REQUIRED."""
from promptpilot import workflows as W


REPORT_QUOTED_PASS = (
    "Сводка: предыдущий раунд\n"
    "AUDIT_FINDINGS_JSON: []\n"
    "AUDIT_VERDICT: PASS\n"          # цитата прошлого аудита (embedded context)
    "\n... мой новый аудит ...\n"
    'AUDIT_FINDINGS_JSON: [{"fingerprint":"F-1","severity":"medium",'
    '"category":"contract","title":"расхождение","status":"open",'
    '"payload":{"path":"API_V1.md","line":10,"reproduction":"x","expected":"y"}}]\n'
    "AUDIT_VERDICT: REVISION_REQUIRED"
)


def test_last_verdict_line_wins_over_quoted_earlier_one():
    d = W.parse_reviewer_report(REPORT_QUOTED_PASS)
    assert d is not None
    assert d.verdict.value == "REVISION_REQUIRED"
    assert len(d.findings) == 1


def test_single_verdict_still_parses():
    d = W.parse_reviewer_report(
        "Аудит.\nAUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS")
    assert d is not None and d.verdict.value == "PASS"


def test_last_findings_block_wins_too():
    # цитата с пустыми находками раньше — финальные находки не теряются
    d = W.parse_reviewer_report(REPORT_QUOTED_PASS)
    assert d.findings[0].fingerprint == "F-1"
