from contextlib import redirect_stdout
import importlib.util
import io
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('ci_wait_observe', Path(__file__).parents[1] / 'tools/ci_wait_observe.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
HEAD = 'a' * 40


@pytest.fixture
def command(tmp_path):
    return ['--repo', 'owner/repo', '--run', '42', '--head', HEAD, '--cache', str(tmp_path / 'cache.json')]


def invoke(command, response=None, now=100):
    calls = []
    def request(path, etag=None):
        calls.append((path, etag))
        if path.endswith('/42'):
            return 200, None, dict(id=42, head_sha=HEAD, repository=dict(full_name='owner/repo'))
        return response or (200, '"etag"', dict(total_count=1, jobs=[dict(id=1, run_id=42, head_sha=HEAD, name='build', status='in_progress', conclusion=None)]))
    with patch.object(tool, 'request', request), patch.object(tool.time, 'time', return_value=now), redirect_stdout(io.StringIO()):
        value = tool.main(command)
    return value, calls


def test_public_command_shares_one_exact_observation_for_sixty_seconds(command):
    first, calls = invoke(command)
    assert len(calls) == 2 and first['github_requests'] == 2
    second, calls = invoke(command, now=159)
    assert not calls and second['cached'] and second['observed_at'] == first['observed_at']
    assert second['mutation_authorized'] is False


def test_expiry_revalidates_with_server_etag_and_preserves_observation_identity(command):
    invoke(command)
    value, calls = invoke(command, (304, '"etag"', None), now=160)
    assert calls == [('repos/owner/repo/actions/runs/42/jobs?per_page=100', '"etag"')]
    assert value['github_requests'] == 1 and value['observed_at'] == 160 and not value['cached']


@pytest.mark.parametrize('change', ['head', 'repository', 'run'])
def test_wrong_run_identity_is_rejected_before_jobs_or_cache(command, change):
    identity = dict(id=42, head_sha=HEAD, repository=dict(full_name='owner/repo'))
    if change == 'head': identity['head_sha'] = 'b' * 40
    if change == 'repository': identity['repository']['full_name'] = 'other/repo'
    if change == 'run': identity['id'] = 99
    with patch.object(tool, 'request', return_value=(200, None, identity)), pytest.raises(RuntimeError):
        tool.main(command)
    assert not Path(command[-1]).exists()


@pytest.mark.parametrize('payload', [
    dict(total_count=2, jobs=[dict(id=1, run_id=42, status='completed')]),
    dict(total_count=2, jobs=[dict(id=1, run_id=42, status='completed')] * 2),
    dict(total_count=1, jobs=[dict(id=1, run_id=99, status='completed')]),
    dict(total_count=0, jobs=[]),
])
def test_partial_duplicate_foreign_or_empty_inventory_never_finishes(command, payload):
    with pytest.raises(RuntimeError):
        invoke(command, (200, '"etag"', payload))
    assert not Path(command[-1]).exists()


def test_unsolicited_304_is_not_a_cached_success(command):
    with pytest.raises(RuntimeError):
        invoke(command, (304, None, None))


def test_finished_failures_are_diagnostic_not_a_green_verdict(command):
    value, _ = invoke(command, (200, None, dict(total_count=1, jobs=[dict(id=1, run_id=42, name='postgres', status='completed', conclusion='failure')])))
    assert value['state'] == 'finished' and value['failures'] == ['postgres']
    assert value['observation_only'] and not value['mutation_authorized'] and 'success' not in value


def test_cached_read_does_not_publish_a_new_progress_heartbeat(command):
    invoke(command)
    command += ['--task-id', '7', '--started-at', 'exact-attempt', '--progress-bin', 'pp']
    with patch.object(tool.subprocess, 'run', side_effect=AssertionError('cached data is not a fresh heartbeat')):
        value, calls = invoke(command, now=120)
    assert value['cached'] and not calls


def test_http_failure_preserves_old_cache_without_reporting_it_as_fresh(command):
    invoke(command)
    path = Path(command[-1])
    before = path.read_bytes()
    failed = subprocess.CompletedProcess([], 1, b'HTTP/2.0 403 Forbidden\r\n\r\n{"message":"denied"}', b'')
    with patch.object(tool.time, 'time', return_value=200), patch.object(tool.subprocess, 'run', return_value=failed), pytest.raises(RuntimeError):
        tool.main(command)
    assert path.read_bytes() == before


def test_new_network_observation_updates_only_the_exact_running_attempt(command):
    command += ['--task-id', '7', '--started-at', 'exact-attempt', '--progress-bin', 'existing-pp']
    with patch.object(tool.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as publish:
        value, calls = invoke(command)
    assert not value['cached'] and len(calls) == 2
    args = publish.call_args.args[0]
    assert args[:6] == ['existing-pp', 'task-progress', '--task-id', '7', '--started-at', 'exact-attempt']
    assert '--passed' not in args and '--total' not in args
