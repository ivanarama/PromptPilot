from pathlib import Path

from promptpilot import api


def test_web_index_is_kept_in_memory_after_startup(monkeypatch, tmp_path):
    # PyInstaller one-file extracts package data under a temporary directory.
    # A long-running macOS launchd process must keep serving the UI even if the
    # OS later cleans that directory.
    monkeypatch.setattr(api, "STATIC_DIR", tmp_path / "already-cleaned")

    response = api.index()

    assert response.status_code == 200
    assert response.media_type == "text/html"
    assert b"<title>PromptPilot</title>" in response.body


def test_web_pipeline_report_has_local_first_period_controls():
    html = (
        Path(__file__).parents[1] / "promptpilot" / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="pipelineReport"' in html
    assert "setPipelineView('report')" in html
    assert "[24,168,720]" in html
    assert "function loadPipelineReport(profile, hours=24, refresh=false)" in html
    assert (
        "pipeline-insights/${encodeURIComponent(profile)}/report?${query}"
        in html
    )
    assert "${refresh ? '&refresh=true' : ''}" in html
    assert "Собираю отчёт из локальной истории" in html
    assert "Уточнить результат в GitHub" in html


def test_web_pipeline_report_labels_exact_and_incomplete_data_honestly():
    html = (
        Path(__file__).parents[1] / "promptpilot" / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "const deliveryExact = delivery.exact === true" in html
    assert "Локальный отчёт не обращался к GitHub" in html
    assert "Выводы предварительные" in html
    assert "coverage.tail_gap_hours" in html
    assert "coverage.movement_period" in html
    assert "coverage.attention_complete" in html
    assert "Влито PR / закрыто issues" in html
    assert '<div class="pipeline-metric-label">Стоимость</div>' in html
    assert "cost.total_usd" in html
    assert "Результат в GitHub" in html
    assert "ship, needs-decision или hold" in html
    assert "function pipelineReportUrl(value)" in html
    assert "url.protocol === 'https:' || url.protocol === 'http:'" in html
