"""Explicit transport through actual API and temporary SQLite, never execution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json

import httpx
import pytest

from promptpilot import api, pipeline_insights, pipeline_scope_delivery as delivery
from promptpilot.models import TaskCreate


def call(method, path, body=None, headers=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False), base_url='http://127.0.0.1:8420') as client:
            return await client.request(method, path, json=body, headers=headers)
    return asyncio.run(run())


def snapshot(db):
    with db._connect() as c:
        return {name: [dict(r) for r in c.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                for name in ('tasks', 'task_series', 'settings', 'pipeline_target_reservations')}


@pytest.fixture
def prepared(isolated_db, monkeypatch):
    db = isolated_db
    monkeypatch.setattr(api, 'API_TOKEN', '')
    monkeypatch.setattr(api, 'ALLOWED_HOSTS', [])
    monkeypatch.setattr(api, 'ALLOWED_ORIGINS', [])
    def forbidden(*args, **kwargs):
        pytest.fail('scope transport must not enter admission or GitHub')
    monkeypatch.setattr(pipeline_insights, 'read_cached', forbidden)
    monkeypatch.setattr(pipeline_insights, '_github_rate_limits', forbidden)
    target = db.create_task(TaskCreate(prompt='Original consumer\n', provider='codex', recurrence='30m', rights='read'))
    other = db.create_task(TaskCreate(prompt='Other queue', provider='codex', recurrence='1h', priority=1))
    assert db.get_next_runnable().id == other.id
    with db._connect() as c:
        c.execute('UPDATE task_series SET paused=1 WHERE id=?', (target.series_id,))
        c.execute("UPDATE tasks SET scheduled_at='2100-01-01',next_run_at='2100-01-02',budget_wait_scope='keep-wait',budget_wait_started_at='keep-time',pipeline_priority_restore=7 WHERE id=?", (target.id,))
        c.executemany('INSERT INTO settings(key,value) VALUES(?,?)', [
            ('worker_paused', '1'), (f'pipeline_item_holds:v1:{target.series_id}', '{"77":{"reason":"preserve"}}'),
            (f'pipeline_item_hold_mode:v1:{target.series_id}', '1'), ('pipeline_github_budget_reservations:v1:fixture', '{"keep":true}')])
    url = f'/api/schedule/{target.series_id}/scope-delivery'
    state = call('GET', url).json()
    assert state['ready'] and state['paused']
    request = {k: state[k] for k in ('series_revision', 'pending_id', 'pending_revision')}
    request.update(request_nonce='1' * 32, addition='\nExact opaque scope reference\n')
    return {'db': db, 'task': target, 'other': other, 'url': url, 'body': request}


def test_read_state_is_private_projection_without_writes(prepared):
    x = prepared
    before = snapshot(x['db'])
    result = call('GET', x['url'])
    assert result.status_code == 200 and result.json()['pending_id'] == x['task'].id
    assert 'Original consumer' not in result.text and 'rights' not in result.text
    assert snapshot(x['db']) == before


def test_atomic_delivery_changes_only_both_prompts_and_own_receipt(prepared):
    x = prepared
    before = snapshot(x['db'])
    response = call('POST', x['url'], x['body'])
    assert response.status_code == 200, response.text
    result, after = response.json(), snapshot(x['db'])
    assert result['delivery_state'] == 'delivered' and result['paused'] is True
    assert len(after['tasks']) == len(before['tasks'])
    for old, new in zip(before['tasks'], after['tasks']):
        expected = dict(old, prompt=old['prompt'] + x['body']['addition']) if old['id'] == x['task'].id else old
        assert new == expected
    for old, new in zip(before['task_series'], after['task_series']):
        expected = dict(old, prompt=old['prompt'] + x['body']['addition'], updated_at=result['delivered_at']) if old['id'] == x['task'].series_id else old
        assert new == expected
    key = delivery.PREFIX + str(x['task'].series_id) + ':' + x['body']['request_nonce']
    assert [r for r in after['settings'] if r['key'] != key] == before['settings']
    assert after['pipeline_target_reservations'] == before['pipeline_target_reservations']
    assert 'Exact opaque scope' not in response.text
    assert call('GET', x['url'] + '/' + x['body']['request_nonce']).json() == result


@pytest.mark.parametrize('change', ['future', 'pending', 'rights', 'backoff', 'started', 'session', 'running', 'rate_limited', 'missing', 'ambiguous'])
def test_stale_or_noncanonical_consumer_refuses_without_any_side_effect(prepared, change):
    x = prepared
    with x['db']._connect() as c:
        if change == 'future':
            c.execute("UPDATE task_series SET prompt='new future' WHERE id=?", (x['task'].series_id,))
        elif change == 'ambiguous':
            c.execute("INSERT INTO tasks(prompt,status,created_at,series_id) VALUES('duplicate','pending','2026-01-01',?)", (x['task'].series_id,))
        else:
            statements = {'pending': "prompt='changed'", 'rights': "rights='full'", 'backoff': "next_run_at='2200-01-01'", 'started': "started_at='2026-01-01'", 'session': "session_id='owned-session'", 'running': "status='running'", 'rate_limited': "status='rate_limited'", 'missing': "status='completed'"}
            c.execute('UPDATE tasks SET ' + statements[change] + ' WHERE id=?', (x['task'].id,))
    before = snapshot(x['db'])
    response = call('POST', x['url'], x['body'])
    assert response.status_code == 409
    assert snapshot(x['db']) == before


def test_guarded_series_alias_is_not_a_new_execution_channel(prepared):
    x = prepared
    with x['db']._connect() as c:
        c.execute('INSERT INTO settings(key,value) VALUES(?,?)', (f'workorder_no_recurrence:v1:{x["task"].series_id}', str(x['task'].series_id)))
    before = snapshot(x['db'])
    assert call('GET', x['url']).json()['blocked_reason'] == 'guarded_series'
    assert call('POST', x['url'], x['body']).status_code == 409
    assert snapshot(x['db']) == before


def test_existing_receipt_replay_never_touches_later_consumer_or_scope(prepared):
    x = prepared
    first = call('POST', x['url'], x['body']).json()
    with x['db']._connect() as c:
        c.execute("UPDATE tasks SET status='completed' WHERE id=?", (x['task'].id,))
    x['db'].create_task(TaskCreate(prompt='Later consumer', provider='codex', series_id=x['task'].series_id))
    before = snapshot(x['db'])
    assert call('POST', x['url'], x['body']).json() == first
    assert call('POST', x['url'], dict(x['body'], addition='different scope')).status_code == 409
    assert snapshot(x['db']) == before


def test_lost_commit_ack_recovers_same_nonce_without_repeating_delivery(prepared, monkeypatch):
    x = prepared
    original, first = delivery._connection, [True]
    @contextmanager
    def unknown(*, readonly):
        with original(readonly=readonly) as c:
            yield c
        if not readonly and first[0]:
            first[0] = False
            raise OSError('commit applied, response lost')
    monkeypatch.setattr(delivery, '_connection', unknown)
    assert call('POST', x['url'], x['body']).status_code == 500
    receipt = call('GET', x['url'] + '/' + x['body']['request_nonce'])
    assert receipt.status_code == 200
    before = snapshot(x['db'])
    assert call('POST', x['url'], x['body']).json() == receipt.json()
    assert snapshot(x['db']) == before


def test_competing_scope_requests_cannot_both_append(prepared):
    x = prepared
    bodies = [x['body'], dict(x['body'], request_nonce='2' * 32, addition='other scope')]
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda body: call('POST', x['url'], body), bodies))
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_auth_cross_site_and_validation_do_not_expose_scope_text(prepared, monkeypatch):
    x = prepared
    secret = 'scope-text-must-not-echo-lease-secret'
    monkeypatch.setattr(api, 'API_TOKEN', 'operator-token')
    assert call('GET', x['url']).status_code == 401
    auth = {'Authorization': 'Bearer operator-token'}
    assert call('GET', x['url'], headers=auth).status_code == 200
    before = snapshot(x['db'])
    rejected = call('POST', x['url'], x['body'], headers={**auth, 'Sec-Fetch-Site': 'cross-site'})
    assert rejected.status_code == 403
    bad = call('POST', x['url'], dict(x['body'], addition=secret, pending_id={'secret': secret}, scope_file='/not/a/capability'), headers=auth)
    assert bad.status_code == 422 and secret not in bad.text and 'scope_file' not in bad.text
    assert snapshot(x['db']) == before


@pytest.mark.parametrize('target', ['settings', 'foreign_task', 'foreign_series'])
def test_trigger_writes_cannot_mutate_other_controls_or_queues(prepared, target):
    x = prepared
    with x['db']._connect() as c:
        action = {'settings': "DELETE FROM settings WHERE key LIKE 'pipeline_item_holds:%'",
                  'foreign_task': f"UPDATE tasks SET note='foreign mutation' WHERE id={x['other'].id}",
                  'foreign_series': f"UPDATE task_series SET title='foreign mutation' WHERE id={x['other'].series_id}"}[target]
        c.execute('CREATE TRIGGER hostile_scope_update AFTER UPDATE OF prompt ON tasks BEGIN ' + action + '; END')
    before = snapshot(x['db'])
    assert call('POST', x['url'], x['body']).status_code == 409
    assert snapshot(x['db']) == before


def test_missing_database_is_not_created_by_state_receipt_or_delivery(prepared, tmp_path, monkeypatch):
    x = prepared
    missing = tmp_path / 'absent-directory' / 'absent.sqlite'
    monkeypatch.setattr(x['db'], 'DB_PATH', missing)
    assert call('GET', x['url']).status_code == 409
    assert call('GET', x['url'] + '/' + x['body']['request_nonce']).status_code == 409
    assert call('POST', x['url'], x['body']).status_code == 409
    assert not missing.parent.exists()


def test_malformed_receipt_is_not_echoed_or_overwritten(prepared):
    x = prepared
    assert call('POST', x['url'], x['body']).status_code == 200
    key = delivery.PREFIX + str(x['task'].series_id) + ':' + x['body']['request_nonce']
    with x['db']._connect() as c:
        value = json.loads(c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()['value'])
        value['unexpected_private_text'] = 'never-return-this-scope-secret'
        c.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), key))
    before = snapshot(x['db'])
    response = call('GET', x['url'] + '/' + x['body']['request_nonce'])
    assert response.status_code == 409 and 'never-return-this-scope-secret' not in response.text
    assert call('POST', x['url'], x['body']).status_code == 409
    assert snapshot(x['db']) == before


@pytest.mark.parametrize('addition', [' ' * 2, '界' * 22000], ids=['blank', 'utf8_over_limit'])
def test_empty_or_oversized_utf8_scope_is_not_written(prepared, addition):
    x = prepared
    before = snapshot(x['db'])
    assert call('POST', x['url'], dict(x['body'], addition=addition)).status_code == 409
    assert snapshot(x['db']) == before


@pytest.mark.parametrize('hostile_trigger', [False, True], ids=['commit', 'trigger_refused'])
def test_callable_only_authorizer_runtime_keeps_atomic_delivery(prepared, monkeypatch, hostile_trigger):
    """Python 3.10 accepts a callback, but cannot unregister it with None."""
    x = prepared
    original = delivery._connection

    class CallableOnlyAuthorizer:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def set_authorizer(self, callback):
            if not callable(callback):
                raise TypeError('authorizer_callback must be callable')
            return self.connection.set_authorizer(callback)

    @contextmanager
    def callable_only_connection(*, readonly):
        with original(readonly=readonly) as connection:
            yield CallableOnlyAuthorizer(connection)

    if hostile_trigger:
        with x['db']._connect() as c:
            c.execute("CREATE TRIGGER legacy_runtime_hostile AFTER UPDATE OF prompt ON tasks "
                      f"BEGIN UPDATE tasks SET note='forbidden' WHERE id={x['other'].id}; END")
    before = snapshot(x['db'])
    monkeypatch.setattr(delivery, '_connection', callable_only_connection)
    response = call('POST', x['url'], x['body'])
    assert response.status_code == (409 if hostile_trigger else 200), response.text
    if hostile_trigger:
        assert snapshot(x['db']) == before
    else:
        result = response.json()
        assert result['delivery_state'] == 'delivered'
        assert call('GET', x['url'] + '/' + x['body']['request_nonce']).json() == result
