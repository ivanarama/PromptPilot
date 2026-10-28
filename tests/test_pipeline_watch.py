from types import SimpleNamespace
import json

from promptpilot.pipeline_watch import summarize, read_merges, delivery_metrics


def test_watch_counts_cached_input_once_and_uses_completed_runs_not_merges():
    result = summarize([{
        "id": 1, "status": "completed", "verdict": "ГОТОВО",
        "completed_at": "2026-09-27T10:02:00+00:00",
        "result": "Tokens: 100 in / 20 out\nCached input: 90\nReasoning output: 10",
    }], [], "2026-09-27T10:00:00+00:00", "2026-09-27T10:03:00+00:00")
    assert result["productive_runs"] == 1
    assert result["completed_run_tokens"] == {
        "input": 100, "cached_input": 90, "uncached_input": 10, "output": 20}
    assert "confirmed_merges" not in result


def test_watch_reports_timeout_and_automatic_pause():
    result = summarize([{
        "id": 7, "status": "running", "series_id": 4,
        "started_at": "2026-09-27T10:00:00+00:00", "task_timeout": 60,
    }], [{"id": 4, "paused": True, "auto_pause_reason": "proof invalid"}],
        "2026-09-27T10:00:00+00:00", "2026-09-27T10:03:00+00:00")
    assert len(result["alerts"]) == 2
    assert result["running"][0]["age_minutes"] == 3


def test_watch_compares_instants_not_timestamp_strings():
    result = summarize([
        {"id": 1, "completed_at": "2026-09-27T10:00:00Z"},
        {"id": 2, "completed_at": "2026-09-27T12:59:59+03:00"},
        {"id": 3, "completed_at": "2026-09-27T11:00:00Z"},
    ], [], "2026-09-27T10:00:00+00:00", "2026-09-27T10:01:00Z")
    assert result["completed_runs"] == 1


def test_delivery_ratios_unknown_when_zero_or_github_failed():
    report = {"completed_runs": 4, "completed_run_tokens": {"uncached_input": 100, "output": 20}}
    assert delivery_metrics(report, {}, True)["completed_runs_per_merge"] is None
    assert delivery_metrics(report, {1: {}}, False)["completed_runs_per_merge"] is None
    result = delivery_metrics(report, {1: {}}, True)
    assert result["completed_runs_per_merge"] == 4
    assert result["reported_done_is_delivery"] is False


def test_merge_read_paginates_and_uses_instant_boundary(monkeypatch):
    rows = [{"number": n, "updated_at": "2026-09-27T10:00:00Z",
             "merged_at": "2026-09-27T10:00:00Z"} for n in range(100)]
    pages = [rows, [{"number": 101, "merged_at": "2026-09-27T10:00:00Z"}]]
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(pages.pop(0)))
    monkeypatch.setattr("promptpilot.pipeline_watch.subprocess.run", run)
    assert len(read_merges("gh", "owner/repo", "2026-09-27T10:00:00+00:00")) == 101
    assert "page=2" in commands[1][-1]
