import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from promptpilot import pipeline_insights
from promptpilot.api import app
from promptpilot.models import TaskCreate


def _request(method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def _profile():
    return {
        "title": "Example pipeline", "repository": "owner/example",
        "queues": [
            {"id": "review", "title": "Review", "query": "label:review",
             "series_contains": "Example - REVIEW", "capacity": 1},
            {"id": "merge", "title": "Merge", "query": "label:ship",
             "series_contains": "Example - MERGE", "capacity": 1},
        ],
    }


def _snapshot(profile, captured_at, queues):
    return {
        "captured_at": captured_at.isoformat(),
        "profile_hash": pipeline_insights._profile_fingerprint(profile),
        "queues": queues,
    }


def test_report_snapshot_query_uses_selected_window_plus_one_day(
        isolated_db, monkeypatch):
    profile = _profile()
    observed = []

    def snapshots(profile_id, since=None, limit=1000):
        observed.append((profile_id, since, limit))
        return []

    monkeypatch.setattr(
        pipeline_insights.db, "list_pipeline_snapshots", snapshots)
    now = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)

    for hours in (24, 168, 720):
        assert pipeline_insights._report_snapshot_rows(
            "example", profile, now, hours) == []

    assert [(profile_id, now - since, limit)
            for profile_id, since, limit in observed] == [
        ("example", timedelta(hours=48), 10000),
        ("example", timedelta(hours=192), 10000),
        ("example", timedelta(hours=744), 10000),
    ]


def test_period_report_uses_only_local_history_and_deduplicates_attention(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("default report made a GitHub call")

    monkeypatch.setattr(pipeline_insights, "_github_search", forbidden)
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", forbidden)

    run = isolated_db.create_task(TaskCreate(
        prompt="Example - MERGE", recurrence="1h"))
    isolated_db.mark_completed(
        run.id,
        "ИТОГ: ГОТОВО\n\n--- Meta ---\nCost: $0.1234\nTokens: 120 in / 30 out",
        verdict="ГОТОВО")
    assert isolated_db.series_action(run.series_id, "end")

    now = datetime.now(timezone.utc)
    old_queues = {
        "review": {
            "backlog": 2, "membership_complete": True,
            "items": [
                {"key": "pr:10", "kind": "pr", "number": 10,
                 "title": "Move me", "labels": ["review"]},
                {"key": "issue:20", "kind": "issue", "number": 20,
                 "title": "Decision", "labels": ["needs-decision"]},
            ],
        },
        "merge": {"backlog": 0, "membership_complete": True, "items": []},
    }
    current_queues = {
        "review": {
            "backlog": 1, "membership_complete": True,
            "items": [
                {"key": "issue:20", "kind": "issue", "number": 20,
                 "title": "Decision", "labels": ["needs-decision", "hold"],
                 "url": "https://github.com/owner/example/issues/20"},
            ],
        },
        "merge": {
            "backlog": 2, "membership_complete": True,
            "items": [
                {"key": "pr:10", "kind": "pr", "number": 10,
                 "title": "Move me", "labels": ["ship"],
                 "url": "https://github.com/owner/example/pull/10"},
                # The same item in two cached queues must remain one attention row.
                {"key": "issue:20", "kind": "issue", "number": 20,
                 "title": "Decision", "labels": ["hold"]},
            ],
        },
    }
    isolated_db.add_pipeline_snapshot(
        "example", profile["repository"],
        _snapshot(profile, now - timedelta(hours=24), old_queues),
        now - timedelta(hours=24))
    isolated_db.add_pipeline_snapshot(
        "example", profile["repository"],
        _snapshot(profile, now - timedelta(minutes=1), current_queues),
        now - timedelta(minutes=1))
    # A separately published full cache can be newer than the append-only row
    # after a fenced snapshot write loses its lease. It must not supply backlog
    # while movement still comes from the older row.
    _cached, generation = pipeline_insights._cache_snapshot("example")
    epoch = pipeline_insights._cache_epoch()
    revision = isolated_db.increment_int_setting(
        pipeline_insights._refresh_revision_key("example"))
    assert pipeline_insights._publish_cache(
        "example", profile, generation, epoch, revision, {
            "profile_id": "example", "title": profile["title"],
            "repository": profile["repository"],
            "queues": [
                {"id": "review", "backlog": 70,
                 "membership_complete": True, "items": []},
                {"id": "merge", "backlog": 30,
                 "membership_complete": True, "items": []},
            ],
            "backlog_total": 100, "history": {},
            "health": {"state": "green", "label": "cached"},
            "bottleneck": "review", "generated_at": now.timestamp(),
        })

    report = pipeline_insights.build_period_report(
        "example", isolated_db.list_series(), hours=24)

    assert report["summary"] == {
        "backlog_current": 3,
        "backlog_delta": 1,
        "entered": 0,
        "exited": 0,
        "moved": 1,
        "runs": 1,
        "useful_runs": 1,
        "empty_or_no_change_runs": 0,
        "safe_reselections": 0,
        "errors": 0,
        "total_tokens": 150,
        "total_cost_usd": 0.1234,
        "attention": 2,
        "delivered": None,
        "merged_prs": None,
        "closed_issues": None,
    }
    assert report["coverage"]["complete"] is True
    assert report["coverage"]["membership_complete"] is True
    assert report["delivery"]["status"] == "not_requested"
    assert [item["key"] for item in report["attention"]] == [
        "pr:10", "issue:20"]
    decision = next(item for item in report["attention"]
                    if item["key"] == "issue:20")
    assert decision["labels"] == ["needs-decision", "hold"]
    assert decision["queues"] == ["merge", "review"]
    assert report["runs"]["tokens"]["total"] == 150
    assert report["runs"]["cost"] == {
        "known_runs": 1, "total_usd": 0.1234}
    assert next(queue for queue in report["queues"]
                if queue["id"] == "merge")["delta"] == 2
    rendered = pipeline_insights.render_period_report_markdown(report)
    assert "Стоимость: $0.1234 в 1 из 1 прогонов" in rendered
    assert "нет элементов с `ship`, `needs-decision` или `hold`" not in rendered
    empty_attention = {**report, "attention": []}
    assert "нет элементов с `ship`, `needs-decision` или `hold`" in \
        pipeline_insights.render_period_report_markdown(empty_attention)


def test_delivery_refresh_is_date_filtered_linked_and_exact(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    now = datetime.now(timezone.utc)
    calls = []
    admissions = []

    @contextmanager
    def admitted(profile_arg, purpose, **kwargs):
        admissions.append((profile_arg, purpose, kwargs))
        yield {"allowed": True, "enabled": True, "state": "allowed"}

    def search(repository, query):
        calls.append((repository, query))
        if "is:pr" in query:
            return {
                "count": 2, "membership_complete": True,
                "items": [
                    {"key": "pr:31", "kind": "pr", "number": 31,
                     "title": "Delivered", "url": None,
                     "merged_at": (now - timedelta(hours=2)).isoformat(),
                     "closed_at": (now - timedelta(hours=2)).isoformat()},
                    {"key": "pr:30", "kind": "pr", "number": 30,
                     "title": "Too old", "url": None,
                     "merged_at": (now - timedelta(days=2)).isoformat(),
                     "closed_at": (now - timedelta(days=2)).isoformat()},
                ],
            }
        return {
            "count": 1, "membership_complete": True,
            "items": [{
                "key": "issue:41", "kind": "issue", "number": 41,
                "title": "Closed", "url": None,
                "closed_at": (now - timedelta(hours=1)).isoformat(),
            }],
        }

    monkeypatch.setattr(pipeline_insights, "_github_search", search)
    monkeypatch.setattr(
        pipeline_insights, "_github_scan_admission", admitted)

    report = pipeline_insights.build_period_report(
        "example", [], hours=24, refresh_delivery=True)

    assert len(calls) == 2
    assert admissions[0][2] == {"budget_route": "insights"}
    assert calls[0][0] == "owner/example"
    assert "is:pr is:merged merged:>=" in calls[0][1]
    assert "is:issue is:closed closed:>=" in calls[1][1]
    assert report["delivery"]["status"] == "refreshed"
    assert report["delivery"]["exact"] is True
    assert [item["number"] for item in report["delivery"]["merged_prs"]] == [31]
    assert report["delivery"]["merged_prs"][0]["url"] == \
        "https://github.com/owner/example/pull/31"
    assert [item["number"] for item in report["delivery"]["closed_issues"]] == [41]
    assert report["summary"]["delivered"] == 2
    assert report["summary"]["merged_prs"] == 1
    assert report["summary"]["closed_issues"] == 1


def test_budget_denial_keeps_local_report_usable(isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})

    @contextmanager
    def denied(*_args, **_kwargs):
        yield {
            "allowed": False, "state": "budget_low",
            "reason": "reserved for integration", "defer_until": 123.0,
        }

    monkeypatch.setattr(pipeline_insights, "_github_scan_admission", denied)
    monkeypatch.setattr(
        pipeline_insights, "_github_search",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("denied refresh reached GitHub")))

    report = pipeline_insights.build_period_report(
        "example", [], hours=168, refresh_delivery=True)

    assert report["hours"] == 168
    assert report["queues"][0]["id"] == "review"
    assert report["delivery"]["status"] == "blocked"
    assert report["delivery"]["blocker"] == "budget_low"
    assert report["delivery"]["reason"] == "reserved for integration"
    assert report["summary"]["delivered"] is None


def test_paused_delivery_refresh_stops_before_budget_admission_or_github(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights.db, "is_paused", lambda: True)

    @contextmanager
    def forbidden_admission(*_args, **_kwargs):
        raise AssertionError("pause entered GitHub admission")
        yield

    monkeypatch.setattr(
        pipeline_insights, "_github_scan_admission", forbidden_admission)
    monkeypatch.setattr(
        pipeline_insights, "_github_search",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("pause reached GitHub search")))

    report = pipeline_insights.build_period_report(
        "example", [], hours=24, refresh_delivery=True)

    assert report["delivery"]["status"] == "blocked"
    assert report["delivery"]["blocker"] == "worker_paused"
    assert "GitHub не запрашивался" in report["delivery"]["reason"]


def test_stale_snapshot_exposes_old_movement_window_and_uncovered_tail(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    now = datetime.now(timezone.utc)
    queues = {
        "review": {"backlog": 1, "membership_complete": True, "items": []},
        "merge": {"backlog": 0, "membership_complete": True, "items": []},
    }
    isolated_db.add_pipeline_snapshot(
        "example", profile["repository"],
        _snapshot(profile, now - timedelta(hours=35), queues),
        now - timedelta(hours=35))
    incomplete_baseline = {
        **queues,
        "review": {**queues["review"], "membership_complete": False},
    }
    isolated_db.add_pipeline_snapshot(
        "example", profile["repository"],
        _snapshot(profile, now - timedelta(hours=23), incomplete_baseline),
        now - timedelta(hours=23))
    isolated_db.add_pipeline_snapshot(
        "example", profile["repository"],
        _snapshot(profile, now - timedelta(hours=10), queues),
        now - timedelta(hours=10))

    report = pipeline_insights.build_period_report(
        "example", [], hours=24)

    assert report["coverage"]["coverage_hours"] == 13
    assert report["coverage"]["fresh"] is False
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["membership_complete"] is False
    assert report["coverage"]["attention_complete"] is True
    assert report["coverage"]["snapshot_count"] == 2
    assert 9.9 <= report["coverage"]["tail_gap_hours"] <= 10.1
    assert report["coverage"]["movement_period"]["from"] == \
        (now - timedelta(hours=23)).isoformat()
    assert report["coverage"]["movement_period"]["to"] == \
        (now - timedelta(hours=10)).isoformat()
    assert report["coverage"]["movement_period"]["to"] != \
        report["period"]["to"]
    rendered = pipeline_insights.render_period_report_markdown(report)
    assert "Движение очередей рассчитано за фактическое окно" in rendered
    assert "непокрытый хвост" in rendered
    assert "нет элементов с `ship`, `needs-decision` или `hold`" in rendered
    assert "Полного снимка состава нет" not in rendered


def test_sparse_fresh_history_does_not_report_48h_delta_as_24h(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    now = datetime.now(timezone.utc)
    old_queues = {
        "review": {"backlog": 0, "membership_complete": True, "items": []},
        "merge": {"backlog": 0, "membership_complete": True, "items": []},
    }
    current_queues = {
        "review": {"backlog": 5, "membership_complete": True, "items": []},
        "merge": {"backlog": 0, "membership_complete": True, "items": []},
    }
    isolated_db.add_pipeline_snapshot(
        "example", profile["repository"],
        _snapshot(profile, now - timedelta(hours=48), old_queues),
        now - timedelta(hours=48))
    current_at = now - timedelta(seconds=1)
    isolated_db.add_pipeline_snapshot(
        "example", profile["repository"],
        _snapshot(profile, current_at, current_queues), current_at)

    report = pipeline_insights.build_period_report(
        "example", [], hours=24)

    assert report["summary"]["backlog_current"] == 5
    assert report["summary"]["backlog_delta"] is None
    assert report["summary"]["entered"] is None
    assert report["summary"]["exited"] is None
    assert all(queue["delta"] is None for queue in report["queues"])
    assert report["coverage"]["coverage_hours"] == 0.0
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["fresh"] is True
    assert 23.9 <= report["coverage"]["baseline_gap_hours"] <= 24.1
    assert report["coverage"]["movement_period"] == {
        "from": current_at.isoformat(), "to": current_at.isoformat()}
    rendered = pipeline_insights.render_period_report_markdown(report)
    assert "Движение очередей рассчитано за фактическое окно" in rendered


def test_failed_delivery_refresh_keeps_local_report(isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_search",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("GitHub unavailable")))

    report = pipeline_insights.build_period_report(
        "example", [], hours=720, refresh_delivery=True)

    assert report["hours"] == 720
    assert len(report["queues"]) == 2
    assert report["delivery"]["status"] == "failed"
    assert report["delivery"]["exact"] is False
    assert report["delivery"]["reason"] == "GitHub unavailable"


def test_report_api_supports_json_markdown_and_validates_period(
        isolated_db, monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(
        pipeline_insights, "_github_search",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("ordinary API report reached GitHub")))

    response = _request(
        "GET", "/api/pipeline-insights/example/report?hours=24")
    assert response.status_code == 200
    assert response.json()["delivery"]["status"] == "not_requested"

    markdown = _request(
        "GET",
        "/api/pipeline-insights/example/report?hours=24&format=markdown")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "# Example pipeline — итоги за 24 часа" in markdown.text
    assert "## Нужно внимание" in markdown.text
    assert "## Результат в GitHub" in markdown.text
    assert markdown.text.index("## Результат в GitHub") < \
        markdown.text.index("## Очереди") < markdown.text.index("## Нужно внимание")
    assert ("среди доступных элементов не найдено `ship`, `needs-decision` "
            "или `hold`") in markdown.text

    invalid_hours = _request(
        "GET", "/api/pipeline-insights/example/report?hours=48")
    assert invalid_hours.status_code == 400
    invalid_format = _request(
        "GET", "/api/pipeline-insights/example/report?format=html")
    assert invalid_format.status_code == 422
