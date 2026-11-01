"""Signed positive typed MERGE with durable one-send ownership.

The ordinary merge/cleanup transaction is shared; its proof remains explicitly
typed. An uncertain compare-and-merge is observed, never automatically resent.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .pipeline_errors import PipelineError
from .review_routing_policy import ROLES
from .review_routing_positive import CONTRACT, capability, live
from .review_routing_wire import _fields, _integer, hash64

PURPOSE = 'typed-clean-merge-v1'
FIELDS = ('version protocol purpose stage repository number head base config_sha256 '
          'reader_sha256 source ordinary issued_at expires_at nonce')


def _pp():
    from . import project_pipeline
    return project_pipeline


def _wire(value, config, *, current=True):
    pp = _pp()
    _fields(value, FIELDS)
    if (type(value['version']) is not int or value['version'] != 1
            or value['protocol'] != CONTRACT or value['purpose'] != PURPOSE
            or value['stage'] != 'merge' or value['repository'] != config['repository']):
        raise PipelineError('unsupported positive typed MERGE request')
    _integer(value['number'], 1, 2147483647)
    for name in ('head', 'base'):
        if not isinstance(value[name], str) or not re.fullmatch('[0-9a-f]{40}', value[name]):
            raise PipelineError('invalid positive typed MERGE ' + name)
    hash64(value['config_sha256'])
    _fields(value['reader_sha256'], ' '.join(ROLES))
    for digest in value['reader_sha256'].values():
        hash64(digest)
    ordinary = value['ordinary']
    _fields(ordinary, 'version stage repository number head snapshot proof')
    if (type(ordinary['version']) is not int or ordinary['version'] != 1 or ordinary['stage'] != 'merge'
            or type(ordinary['number']) is not int
            or any(ordinary[name] != value[name] for name in ('repository', 'number', 'head'))
            or ordinary['proof'] != value['source']
            or not isinstance(value['source'], dict)
            or value['source'].get('protocol') != 'review-routing-v1'
            or value['source'].get('kind') != 'positive-review'):
        raise PipelineError('positive typed MERGE request changes its actual proof')
    hash64(ordinary['snapshot'])
    issued, expires = value['issued_at'], value['expires_at']
    if (type(issued) is not int or type(expires) is not int
            or issued < 0 or not 0 < expires - issued <= 1800
            or not isinstance(value['nonce'], str) or not re.fullmatch('[0-9a-f]{32}', value['nonce'])):
        raise PipelineError('invalid positive typed MERGE lifetime/nonce')
    if pp.digest(config) != value['config_sha256'] or pp._configured_replica_count() != 1:
        raise PipelineError('positive typed MERGE configuration or single lane changed')
    if current and (issued > time.time() + 60 or expires <= time.time()):
        raise PipelineError('positive typed MERGE request expired or is not yet valid')


def _base(gh, config, snapshot, expected=None):
    base = snapshot['baseRefOid']
    if expected is not None and base != expected:
        raise PipelineError('positive typed MERGE base changed after its signed request')
    ref = gh.json('api', f"repos/{config['repository']}/git/ref/heads/{config['base_branch']}")
    if (ref.get('object') or {}).get('sha') != base:
        raise PipelineError('positive typed MERGE main changed during its snapshot')
    compared = gh.json('api', f"repos/{config['repository']}/compare/{base}...{snapshot['headRefOid']}")
    if (compared.get('status') not in {'ahead', 'identical'}
            or (compared.get('base_commit') or {}).get('sha') != base
            or (compared.get('merge_base_commit') or {}).get('sha') != base):
        raise PipelineError('positive typed MERGE HEAD does not contain the current main')


def _ci(gh, config, head):
    required = config.get('required_checks')
    if (not isinstance(required, list) or not required
            or any(not isinstance(name, str) or not name for name in required)
            or len(set(required)) != len(required)):
        raise PipelineError('positive typed MERGE requires explicit mandatory CI checks')
    checks = gh.json('api', f"repos/{config['repository']}/commits/{head}/check-runs?per_page=100")
    if (not isinstance(checks, dict) or type(checks.get('total_count')) is not int or not 0 <= checks['total_count'] <= 100
            or not isinstance(checks.get('check_runs'), list)
            or len(checks['check_runs']) != checks['total_count']):
        raise PipelineError('positive typed MERGE CI snapshot is incomplete')
    for name in required:
        matching = [item for item in checks['check_runs']
                    if isinstance(item, dict) and item.get('name') == name
                    and isinstance(item.get('app'), dict) and item['app'].get('id') == 15368]
        if not matching:
            raise PipelineError('positive typed MERGE mandatory GitHub Actions check is missing: ' + name)
        if any(type(item.get('id')) is not int or item['id'] <= 0 for item in matching):
            raise PipelineError('positive typed MERGE CI producer IDs are incomplete')
        latest = max(matching, key=lambda item: item['id'])
        if latest.get('head_sha') != head or latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
            raise PipelineError('positive typed MERGE mandatory CI is not successful: ' + name)


def _candidate(health, ordinary, context):
    queue = health.get('merge_executable')
    if (not isinstance(queue, list) or not queue
            or any(queue[0].get(name) != ordinary[name] for name in ('number', 'head'))
            or queue[0].get('stage') != 'merge' or health.get('integration_owner') is not None):
        raise PipelineError('positive typed MERGE lost its first executable single-flight slot')
    candidates = health.get('positive_merge_candidates')
    if not isinstance(candidates, list) or not any(
            item == dict(stage='merge', number=ordinary['number'], head=ordinary['head'], source=context['source'])
            for item in candidates):
        raise PipelineError('positive typed MERGE checker source differs from the independent proof')


def issue(gh, config, health, ordinary, *, config_path):
    pp = _pp()
    if not isinstance(config_path, str) or not config_path:
        raise PipelineError('positive typed MERGE requires its effective configuration path')
    config_path = str(Path(config_path).resolve())
    context = live(gh, config, ordinary['number'], purpose='merge', health=health)
    _candidate(health, ordinary, context)
    if context['source'] != ordinary['proof']:
        raise PipelineError('positive typed MERGE source changed before request issuance')
    _base(gh, config, context['snapshot'])
    _ci(gh, config, ordinary['head'])
    ttl = config.get('review_lease_seconds', 7200)
    if type(ttl) is not int or not 300 <= ttl <= 28800:
        raise PipelineError('invalid configured positive typed MERGE lifetime')
    now = int(time.time())
    value = dict(version=1, protocol=CONTRACT, purpose=PURPOSE, stage='merge',
                 repository=config['repository'], number=ordinary['number'], head=ordinary['head'],
                 base=context['snapshot']['baseRefOid'], config_sha256=pp.digest(config),
                 reader_sha256=context['reader_sha256'], source=context['source'], ordinary=ordinary,
                 issued_at=now, expires_at=now + min(ttl, 1800), nonce=secrets.token_hex(16))
    _wire(value, pp.load_config(config_path))
    return pp.encode_signed_lease(value)


@contextmanager
def _store():
    root = Path(os.environ.get('PP_DATA_DIR', Path.home() / '.promptpilot'))
    signing = Path(os.environ.get('PP_PIPELINE_LEASE_KEY_FILE', root / 'pipeline-lease.key'))
    if not signing.is_absolute():
        raise PipelineError('positive typed MERGE requires an absolute persistent signing-key path')
    directory = signing.parent / 'typed-merge-sends-v1'
    directory.mkdir(parents=True, exist_ok=True)
    path, marker = directory / 'receipts.sqlite3', directory / 'initialized.json'
    if path.is_symlink() or marker.is_symlink() or marker.exists() and not path.is_file():
        raise PipelineError('positive typed MERGE established receipt store is missing or unsafe')
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=0, isolation_level=None)
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('BEGIN IMMEDIATE')
        if not marker.exists():
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'receipts' in tables and connection.execute('SELECT 1 FROM receipts LIMIT 1').fetchone():
                raise PipelineError('positive typed MERGE initialization marker was lost')
            connection.execute('CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), uuid TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS receipts (target TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, ticket TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO metadata VALUES (1,?)', (secrets.token_hex(16),))
            identity = connection.execute('SELECT uuid FROM metadata WHERE id=1').fetchone()[0]
            connection.commit()
            try:
                with marker.open('x', encoding='utf-8') as output:
                    json.dump(dict(protocol=CONTRACT, uuid=identity), output)
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                pass
            connection.execute('BEGIN IMMEDIATE')
        expected = json.loads(marker.read_text(encoding='utf-8'))
        actual = connection.execute('SELECT uuid FROM metadata WHERE id=1').fetchone()
        if (actual is None or not isinstance(actual[0], str) or not re.fullmatch('[0-9a-f]{32}', actual[0])
                or expected != dict(protocol=CONTRACT, uuid=actual[0])):
            raise PipelineError('positive typed MERGE receipt-store identity changed')
        connection.rollback()
        yield connection
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise PipelineError('positive typed MERGE receipt store unavailable: ' + str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()


def complete(gh, config, token, *, config_path):
    pp = _pp()
    value = pp.decode_signed_lease(token)
    if pp.encode_lease(value) != token.split('.', 1)[0]:
        raise PipelineError('positive typed MERGE request is not canonical signed JSON')
    _wire(value, config)
    if not isinstance(config_path, str) or not config_path:
        raise PipelineError('positive typed MERGE requires its effective configuration path')
    config_path = str(Path(config_path).resolve())

    def fresh():
        effective = pp.load_config(config_path)
        _wire(value, effective)
        pp.ensure_identity(gh, effective)
        health = pp.run_health(effective, config_path=config_path)
        policy = capability(effective, health)
        if policy['consumer_sha256'] != value['reader_sha256']:
            raise PipelineError('positive typed MERGE reader trees changed')
        context = live(gh, effective, value['number'], purpose='merge', health=health)
        _candidate(health, value['ordinary'], context)
        if context['source'] != value['source']:
            raise PipelineError('positive typed MERGE current canonical source changed')
        _base(gh, effective, context['snapshot'], value['base'])
        _ci(gh, effective, value['head'])
        status, checks = pp.pr_checks(gh, effective, value['number'])
        if status.get('mergeStateStatus') != 'CLEAN' or status.get('mergeable') != 'MERGEABLE' or not pp.checks_ready(effective, checks)[0]:
            raise PipelineError('positive typed MERGE is no longer clean and ready')
        final = live(gh, effective, value['number'], purpose='merge', health=health)
        if (final['source'] != value['source'] or final['snapshot']['baseRefOid'] != value['base']
                or final['reader_sha256'] != value['reader_sha256']):
            raise PipelineError('positive typed MERGE changed during final CI/transport work')
        pp.ensure_identity(gh, effective)
        if capability(effective, health)['consumer_sha256'] != value['reader_sha256']:
            raise PipelineError('positive typed MERGE reader trees changed during final transport work')
        _wire(value, pp.load_config(config_path))
        return effective

    def guard(phase):
        fresh()
        if phase == 'put':
            # The independent lifetime target survives ticket/cache replacement.
            # Spend before the final gates, never infer a safe retry from expiry.
            with _store() as connection:
                connection.execute('BEGIN IMMEDIATE')
                target = pp.digest(value['source'])
                if connection.execute('SELECT 1 FROM receipts WHERE target=?', (target,)).fetchone():
                    raise PipelineError('positive typed MERGE was already attempted; use read-only outcome recovery')
                connection.execute('INSERT INTO receipts VALUES (?,?,?)', (target, value['nonce'], pp.canonical(value)))
                connection.commit()
            fresh()  # Repeat every live gate after SQLite writer admission.

    guard('begin')
    return pp._complete_ordinary_merge(gh, config, value['ordinary'], config_path=config_path, typed_guard=guard)
