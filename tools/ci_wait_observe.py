"""Bounded, shared CI observations. Never an admission, review or merge proof."""
from contextlib import contextmanager
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def locked(path):
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'1')
            stream.flush()
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def atomic(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.ci-observation-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def request(endpoint, etag=None):
    command = [os.environ.get('PP_GH_EXE') or shutil.which('gh') or 'gh',
               'api', '--hostname', 'github.com', '--include', '--method', 'GET', endpoint]
    if etag is not None:
        command += ['-H', 'If-None-Match: ' + etag]
    process = subprocess.run(command, capture_output=True, timeout=20)
    raw = process.stdout.replace(b'\r\n', b'\n')
    headers, separator, body = raw.partition(b'\n\n')
    match = re.match(rb'HTTP/\S+\s+(\d{3})\b', headers)
    if not match or not separator:
        raise RuntimeError('GitHub returned no complete HTTP observation')
    status = int(match[1])
    if status not in {200, 304} or (process.returncode and status != 304):
        raise RuntimeError('GitHub observation failed with HTTP ' + str(status))
    tag = next((line.split(b':', 1)[1].strip().decode()
                for line in headers.splitlines() if line.lower().startswith(b'etag:')), None)
    return status, tag, json.loads(body) if status == 200 else None


def check_jobs(value, run, head):
    if (not isinstance(value, dict) or type(value.get('total_count')) is not int
            or not isinstance(value.get('jobs'), list)
            or value['total_count'] != len(value['jobs']) or len(value['jobs']) > 100):
        raise RuntimeError('Incomplete CI inventory; use the canonical full reader')
    seen = set()
    for job in value['jobs']:
        if (type(job.get('id')) is not int or job['id'] in seen
                or job.get('run_id') != run or job.get('head_sha', head) != head
                or job.get('status') not in {'queued', 'waiting', 'pending', 'in_progress', 'completed'}):
            raise RuntimeError('CI job identity/status is inconsistent')
        seen.add(job['id'])
    if not seen:
        raise RuntimeError('Empty CI inventory is not a completed observation')


def observe(repo, run, head, cache):
    if cache.is_symlink() or cache.parent.is_symlink():
        raise ValueError('Observation path must not be a symlink')
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    key = dict(repo=repo, run=run, head=head, code=code)
    now = time.time()
    previous = None
    try:
        value = json.loads(cache.read_bytes())
        if (value['key'] == key and digest(value['payload']) == value['payload_sha256']
                and 0 <= now - value['observed_at']):
            check_jobs(value['payload'], run, head)
            previous = value
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        pass
    if previous and now - previous['observed_at'] < 60:
        return result(previous, cached=True, requests=0)
    calls = 0
    if previous is None:
        status, _, identity = request(f'repos/{repo}/actions/runs/{run}')
        calls += 1
        if (status != 200 or identity.get('id') != run or identity.get('head_sha') != head
                or (identity.get('repository') or {}).get('full_name') != repo):
            raise RuntimeError('Requested CI run does not match repository and exact HEAD')
    etag = previous.get('etag') if previous else None
    if etag is not None and (not isinstance(etag, str) or '\r' in etag or '\n' in etag):
        raise RuntimeError('Invalid cached ETag')
    status, new_etag, payload = request(f'repos/{repo}/actions/runs/{run}/jobs?per_page=100', etag)
    calls += 1
    if status == 304:
        if previous is None or etag is None:
            raise RuntimeError('Unsolicited 304 cannot supply missing CI data')
        payload = previous['payload']
    check_jobs(payload, run, head)
    record = dict(version=1, key=key, payload=payload, payload_sha256=digest(payload),
                  etag=new_etag or (etag if status == 304 else None),
                  observed_at=time.time())
    atomic(cache, record)
    return result(record, cached=False, requests=calls)


def result(record, *, cached, requests):
    jobs = record['payload']['jobs']
    return dict(observation_only=True, mutation_authorized=False,
                source=record['key'], observed_at=record['observed_at'],
                cached=cached, github_requests=requests,
                state='pending' if any(j['status'] != 'completed' for j in jobs) else 'finished',
                failures=[j['name'] for j in jobs if j.get('conclusion') in {'failure', 'timed_out', 'cancelled'}],
                jobs=[{k: j.get(k) for k in ['id', 'name', 'status', 'conclusion']} for j in jobs])


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--run', required=True, type=int)
    parser.add_argument('--head', required=True)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--task-id', type=int)
    parser.add_argument('--started-at')
    parser.add_argument('--progress-bin')
    args = parser.parse_args(arguments)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo) or args.run <= 0 or not re.fullmatch(r'[0-9a-f]{40}', args.head):
        parser.error('exact repository, run id and HEAD required')
    if any(v is not None for v in [args.task_id, args.started_at, args.progress_bin]) and not all(v is not None for v in [args.task_id, args.started_at, args.progress_bin]):
        parser.error('progress requires the exact existing task attempt and CLI')
    args.cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = args.cache.with_suffix('.lock')
    if lock.is_symlink():
        raise ValueError('Observation lock must not be a symlink')
    with locked(lock):
        value = observe(args.repo, args.run, args.head, args.cache)
    if args.task_id and not value['cached'] and value['state'] == 'pending':
        names = ', '.join(j['name'] for j in value['jobs'] if j['status'] != 'completed')
        reason = ('CI run ' + str(args.run) + ': ' + names)[:490]
        subprocess.run([args.progress_bin, 'task-progress', '--task-id', str(args.task_id),
                        '--started-at', args.started_at, '--stage', 'review', '--status', 'waiting',
                        '--wait-reason', reason], check=True, capture_output=True, timeout=15)
    print(json.dumps(value, ensure_ascii=False))
    return value


if __name__ == '__main__':
    main()
