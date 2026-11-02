from datetime import datetime, timedelta, timezone
import json

from click.testing import CliRunner

from promptpilot.cli import cli
from promptpilot.models import TaskCreate
from promptpilot import task_progress
from test_delivery_flow import seed, request


def running(db):
    task = db.create_task(TaskCreate(prompt='Project - FIX', recurrence='1h'))
    assert db.get_next_runnable().id == task.id
    return db.get_task(task.id)


def report(task, *arguments):
    return CliRunner().invoke(cli, ['task-progress', '--task-id', str(task.id),
        '--started-at', task.started_at.isoformat(), '--stage', 'Race UI2', *arguments])


def test_cli_progress_reaches_cached_dashboard_without_github(isolated_db, monkeypatch):
    seed(isolated_db, monkeypatch)
    task = running(isolated_db)
    assert report(task, '--passed', '414', '--total', '469', '--skipped', '5').exit_code == 0
    item = request()['delivery_flow']['active'][0]
    assert item['task_id'] == task.id
    p = item['progress']
    assert p['stage'] == 'Race UI2' and p['status'] == 'running'
    assert p['tests'] == dict(passed=414, failed=0, skipped=5, total=469)
    assert not p['stale']
    first = p['stage_started_at']
    assert report(task, '--status', 'waiting', '--wait-reason', 'Ожидание PostgreSQL').exit_code == 0
    p = request()['delivery_flow']['active'][0]['progress']
    assert p['stage_started_at'] == first
    assert p['wait_reason'] == 'Ожидание PostgreSQL'


def test_bad_counts_and_ended_attempt_cannot_publish(isolated_db):
    task = running(isolated_db)
    for args in [('--passed', '4', '--total', '3'), ('--passed', '1'),
                 ('--status', 'waiting'), ('--stage', ''), ('--failed', '1'),
                 ('--passed', '1', '--total', '1', '--failed', '1')]:
        assert report(task, *args).exit_code != 0
    isolated_db.mark_completed(task.id, 'ИТОГ: ПУСТО')
    assert report(task).exit_code != 0


def test_attempt_identity_malformed_and_oversize_records_are_ignored(isolated_db):
    task = running(isolated_db)
    assert report(task).exit_code == 0
    now = datetime.now(timezone.utc)
    path = task_progress._path(task.id, task.started_at)
    good = json.loads(path.read_text(encoding='utf-8'))
    for key, value in [('task_id', task.id + 1), ('tests', dict(passed=True, failed=0, skipped=0, total=1)),
                       ('updated_at', (now + timedelta(hours=1)).isoformat())]:
        path.write_text(json.dumps(dict(good, **{key: value})), encoding='utf-8')
        assert task_progress.observe(task.id, task.started_at, now) is None
    for raw in ['{', ' ' * 16385]:
        path.write_text(raw, encoding='utf-8')
        assert task_progress.observe(task.id, task.started_at, now) is None
    assert task_progress.observe(task.id, task.started_at + timedelta(seconds=1), now) is None


def test_old_heartbeat_is_explicitly_stale(isolated_db):
    task = running(isolated_db)
    assert report(task).exit_code == 0
    value = task_progress.observe(task.id, task.started_at, datetime.now(timezone.utc) + timedelta(minutes=4))
    assert value['stale'] and value['age_seconds'] >= 240
