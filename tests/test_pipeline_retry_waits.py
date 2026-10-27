import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from promptpilot import pipeline_insights
from promptpilot.api import app
from promptpilot.models import TaskCreate


def _get(path):
    async def request():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8420") as client:
            return await client.get(path)

    response = asyncio.run(request())
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("later_schedule", [False, True])
def test_retry_reason_and_time_reach_dashboard_and_report(
        isolated_db, monkeypatch, later_schedule):
    profile = {
        "title": "Example", "repository": "owner/example",
        "queues": [{"id": "review", "title": "Review", "capacity": 1,
                    "query": "is:pr is:open", "series_contains": "Example - REVIEW"}],
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"retries": profile})
    # Cached/local views must not contact the operator's GitHub account.
    monkeypatch.setattr(pipeline_insights, "_github_rate_limits", lambda: None)
    created = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="1h"))
    running = isolated_db.get_next_runnable()
    assert running.id == created.id
    retry = datetime.now(timezone.utc) + timedelta(minutes=10)
    message = "Provider overloaded (HTTP 529), retry scheduled"
    isolated_db.mark_rate_limited(running.id, retry, message)
    expected = retry
    if later_schedule:
        expected = retry + timedelta(minutes=10)
        assert isolated_db.update_task_fields(running.id, {"scheduled_at": expected})

    dashboard = _get("/api/pipeline-insights/retries")
    queue = dashboard["queues"][0]
    assert queue["task_status"] == "rate_limited"
    assert queue["task_error"] == message
    assert datetime.fromisoformat(queue["task_next_run_at"]) == expected
    replica = queue["series_replicas"][0]
    assert replica["task_error"] == message
    assert datetime.fromisoformat(replica["task_next_run_at"]) == expected

    report = _get("/api/pipeline-insights/retries/report")
    current = report["current"]["tasks"][0]
    assert current["status"] == "rate_limited"
    assert current["error"] == message
    assert datetime.fromisoformat(current["scheduled_at"]) == expected
