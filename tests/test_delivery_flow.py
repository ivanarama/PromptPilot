import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from promptpilot import pipeline_insights
from promptpilot.api import app
from promptpilot.models import TaskCreate


def seed(db, monkeypatch, *, age=0):
    profile = {'title': 'Project', 'repository': 'owner/project', 'queues': [
        {'id': stage, 'title': stage.upper(), 'query': 'is:open',
         'series_contains': 'Project - ' + stage.upper(), 'capacity': 1}
        for stage in ('fix', 'review', 'merge')]}
    monkeypatch.setattr(pipeline_insights, '_profiles', lambda: {'flow': profile})
    monkeypatch.setattr(db, 'worker_runtime_status', lambda **_: {'state': 'online', 'paused': False})

    def forbidden(*_, **__):
        raise AssertionError('cached delivery explanation attempted a GitHub request')

    monkeypatch.setattr(pipeline_insights, '_github_search', forbidden)
    monkeypatch.setattr(pipeline_insights, '_github_rate_limits', forbidden)
    pipeline_insights.invalidate_cache()
    now = datetime.now(timezone.utc)
    _cached, generation = pipeline_insights._cache_snapshot('flow')
    epoch = pipeline_insights._cache_epoch()
    revision = db.increment_int_setting(pipeline_insights._refresh_revision_key('flow'))
    assert pipeline_insights._publish_cache('flow', profile, generation, epoch, revision, {
        'profile_id': 'flow', 'title': 'Project', 'repository': 'owner/project',
        'generated_at': (now - timedelta(seconds=age)).timestamp(), 'history': {},
        'health': {}, 'backlog_total': 2, 'runtime': {},
        'queues': [{'id': stage, 'title': stage.upper(), 'backlog': 2 if stage == 'fix' else 0}
                   for stage in ('fix', 'review', 'merge')],
        'diagnostics': {'fix_candidates': [{'number': 10, 'review_depth': 4}],
                        'human_waiting': [{'number': 10, 'review_depth': 4}, {'number': 11, 'review_depth': 1}]}})


def request():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8420') as client:
            response = await client.get('/api/pipeline-insights/flow')
            assert response.status_code == 200, response.text
            return response.json()
    return asyncio.run(run())


def test_cached_endpoint_explains_empty_merge_and_current_work(isolated_db, monkeypatch):
    seed(isolated_db, monkeypatch)
    task = isolated_db.create_task(TaskCreate(prompt='Project - FIX', recurrence='1h'))
    assert isolated_db.get_next_runnable().id == task.id
    flow = request()['delivery_flow']
    assert flow['reason_code'] == 'fix'
    assert flow['queue_observation_fresh']
    assert flow['active'][0]['task_id'] == task.id
    assert flow['active'][0]['stage'] == 'fix'
    assert flow['active'][0]['elapsed_seconds'] >= 0
    assert flow['review_rounds'] == [{'number': 10, 'rounds': 4}]


def test_stale_snapshot_does_not_claim_current_empty_merge(isolated_db, monkeypatch):
    seed(isolated_db, monkeypatch, age=3600)
    flow = request()['delivery_flow']
    assert flow['reason_code'] == 'unknown'
    assert not flow['queue_observation_fresh']


def test_pause_takes_precedence_over_cached_backlog(isolated_db, monkeypatch):
    seed(isolated_db, monkeypatch)
    isolated_db.set_setting('worker_paused', '1')
    assert request()['delivery_flow']['reason_code'] == 'paused'
