"""Canonical repository spelling survives worker, owned START and recovery gates."""
# ruff: noqa: F811 -- imported pytest fixtures are resolved by parameter name.
import base64
import copy
from contextlib import redirect_stdout
import hashlib
import hmac
from io import StringIO
import json
from pathlib import Path
import pytest
from promptpilot import project_pipeline as pp, pipeline_insights as insights
from promptpilot import review_routing_fix_request as author_requests
from promptpilot import review_routing_pipeline_request as pipeline_requests
from promptpilot.review_routing_execution import OneUseStart
from test_review_routing_adapter import adapter as original_adapter  # noqa: F401
from test_review_routing_stage_guard import setup_guard  # noqa: F401
from test_review_routing_fix import author_bridge as original_author_bridge, ledger  # noqa: F401
from test_review_routing_pipeline_fix import pipeline_case  # noqa: F401

@pytest.fixture(params=['owner/repo', 'Owner/MixedCase'])
def adapter(original_adapter, request):
    peer, health, path, report, run = original_adapter
    repo = request.param
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['repository'] = repo
    path.write_text(json.dumps(raw), encoding='utf-8')
    state = peer.current['data']['repository']
    state['nameWithOwner'] = repo
    for name in ['baseRepository', 'headRepository']:
        state['pullRequest'][name]['nameWithOwner'] = repo
    original = peer.json

    def canonical_peer(*args, input_value=None):
        if args in [('api', 'repos/' + repo), ('api', 'repos/owner/repo')]:
            return {'full_name': repo, 'node_id': 'R_BASE'}
        mapped = tuple((a.replace('repos/' + repo, 'repos/owner/repo') if isinstance(a, str) else a for a in args))
        return original(*mapped, input_value=input_value)
    peer.json = canonical_peer
    return original_adapter

@pytest.fixture
def author_bridge(original_author_bridge):
    peer, health, path, task, run, database = original_author_bridge
    repo = pp.load_config(str(path))['repository']
    original = peer.json

    def transport_alias(*args, input_value=None):
        mapped = tuple((a.replace('repos/' + repo, 'repos/owner/repo') if isinstance(a, str) else a for a in args))
        return original(*mapped, input_value=input_value)
    peer.json = transport_alias
    if (Path(task.working_dir) / '.git').is_dir():
        from promptpilot.process_tree import run_owned
        result = run_owned(['git', '-C', task.working_dir, 'remote', 'set-url', 'origin',
                            'https://github.com/' + repo + '.git'],
                           capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
    return original_author_bridge

def cli(path, *args):
    stream = StringIO()
    with redirect_stdout(stream):
        code = pp.run(['--config', str(path), *args])
    return (code, json.loads(stream.getvalue()))

def test_public_author_delivery_twice(author_bridge):
    peer, health, path, task, run, database = author_bridge
    repo = pp.load_config(str(path))['repository']
    code, selected = cli(path, 'next', 'fix')
    assert code == 0 and selected['action'] == 'author-handoff', selected
    value = author_requests.decode(selected['lease'])
    assert value['repository'] == repo and value['target_reservation']['repository'] == repo.lower()
    for _ in range(2):
        code, result = cli(path, 'complete', 'fix', '--lease', selected['lease'])
        assert code == 0 and result['action'] == 'handed-off', result
    state = ledger(peer, path)
    assert state.budget_used == 1 and len(state.attempts) == 1
    assert set(state.attempt_phases(state.attempts[0])) == {'assigned', 'handoff-done'}
    assert len(peer.assignment_posts) == 1

@pytest.mark.parametrize('executor', ['author', 'pipeline'])
def test_public_worker_routing(author_bridge, monkeypatch, executor):
    peer, health, path, task, run, database = author_bridge
    repo = pp.load_config(str(path))['repository']
    if executor == 'pipeline':
        from promptpilot.review_routing_pipeline_policy import CONTRACT
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['technical_fix_execution'] = dict(enabled=True, protocol='review-routing-v1', executor=dict(kind='pipeline', login='owner'))
        path.write_text(json.dumps(raw), encoding='utf-8')
        health['fix_execution_contract'] = copy.deepcopy(CONTRACT)
    queue = dict(id='fix', execution=dict(mode='auto', command=['pipelinectl', '--config', str(path), 'next', 'fix'], review_routing_protocol='review-routing-v1'))
    monkeypatch.setattr(insights, '_matching_queue', lambda _task: ('private', {'repository': repo}, queue))
    monkeypatch.setattr(insights, '_tool_available', lambda *args: (True, ''))
    monkeypatch.setattr(insights, 'load_providers', lambda: {})
    calls = []
    preflights = []

    def public_cli(_execution, command, _directory, *, env_extra=None):
        calls.append([c for c in command if not c.startswith('ey')])
        with monkeypatch.context() as environment:
            for name, value in (env_extra or {}).items():
                environment.setenv(name, value)
            stream = StringIO()
            with redirect_stdout(stream):
                code = pp.run(command[1:])
            result = json.loads(stream.getvalue())
        preflights.append({k: v for k, v in result.items() if k != 'lease'})
        assert code == 0 or result['action'] == 'error', result
        return result
    monkeypatch.setattr(insights, '_tool_preflight', public_cli)
    route = insights.execution_route(task, 'Private FIX', task.working_dir)
    if executor == 'author':
        assert route['action'] == 'complete_empty' and route['verdict'] == 'ПЕРЕДАНО АВТОРУ', route
        assert len(calls) == 2 and len(peer.assignment_posts) == 1
    else:
        assert route['action'] == 'prompt' and 'technical_fix' in route, route

def test_public_pipeline_start_mixed_case(pipeline_case):
    peer, health, path, task, database, config = pipeline_case
    repo = config['repository']
    code, selected = cli(path, 'next', 'fix')
    assert code == 0 and selected['action'] == 'pipeline-fix', selected
    code, prepared = cli(path, 'prepare-fix-start', 'fix', '--lease', selected['lease'])
    state = ledger(peer, path)
    assert code == 0 and prepared['action'] == 'pipeline-start', prepared
    from promptpilot.review_routing_start_request import decode
    assert decode(prepared['lease'])['scope']['repository'] == repo
    assert state.budget_used == 1 and 'started' in state.attempt_phases(state.attempts[0])

@pytest.mark.parametrize('original_author_bridge', ['real-source'], indirect=True)
def test_owned_start_preserves_canonical_identity_and_invokes_one_factory(pipeline_case):
    import subprocess
    import sys
    from promptpilot.process_tree import OwnedProcess
    from promptpilot.review_routing_start_request import decode
    from promptpilot.review_routing_workspace import Workspace
    peer, health, path, task, database, config = pipeline_case
    code, selected = cli(path, 'next', 'fix')
    assert code == 0, selected
    code, prepared = cli(path, 'prepare-fix-start', 'fix', '--lease', selected['lease'])
    assert code == 0, prepared
    value = decode(prepared['lease'])
    scope = value['scope']
    Workspace(database.DB_PATH, scope, task.working_dir).prepare()
    code, refused = cli(path, 'gate-fix-start', 'fix', '--lease', prepared['lease'])
    assert code == 2 and 'consumed' in refused['error'], refused
    gates = []
    factories = []

    def final(bound):
        code, allowed = cli(path, 'gate-fix-start', 'fix', '--lease', prepared['lease'])
        assert code == 0 and allowed['action'] == 'start-allowed', allowed
        gates.append(allowed)
        return True

    def factory():
        factories.append(True)
        return OwnedProcess.start([sys.executable, '-B', '-c', 'print("review72-harmless-owned-child")'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    store = OneUseStart(database.DB_PATH, create=False, allow_new=False)
    tree = store.invoke(scope, final_guard=final, factory=factory)
    try:
        store.observe_started(scope, tree.process.pid)
        output, error = tree.process.communicate(timeout=10)
        assert tree.process.returncode == 0 and b'review72-harmless-owned-child' in output
    finally:
        store.confirm_stopped(scope, tree)
        tree.close()
    with pytest.raises(pp.PipelineError):
        store.invoke(scope, final_guard=final, factory=factory)
    assert len(gates) == len(factories) == 1

def test_settlement_repository_key_schema(author_bridge):
    import secrets
    from uuid import uuid4
    from promptpilot import review_routing_settlement_request as settlements
    peer, health, path, task, run, database = author_bridge
    code, selected = cli(path, 'next', 'fix')
    assert code == 0, selected
    request = author_requests.decode(selected['lease'])
    identity = dict(request['identity'], head_sha='b' * 40)
    reservation = dict(request['target_reservation'], head='b' * 40)
    value = dict(version=1, protocol='review-routing-v1', purpose=settlements.PURPOSE, repository=request['repository'], identity=identity, author=request['author'], attempt=request['attempt'], attempt_id='1003', owner=str(uuid4()), config_sha256=request['config_sha256'], reader_sha256=request['reader_sha256'], pipeline_replicas=request['pipeline_replicas'], target_reservation=reservation, evidence_sha256='e' * 64, issued_at=request['issued_at'], expires_at=request['issued_at'] + 300, nonce=secrets.token_hex(16))
    token = settlements.encode(value)
    assert settlements.decode(token) == value
    assert value['repository'] == identity['base_repository']
    assert reservation['repository'] == value['repository'].lower()

@pytest.mark.parametrize('executor', ['author', 'pipeline'])
def test_signed_schema_and_malformed_json(author_bridge, executor):
    peer, health, path, task, run, database = author_bridge
    repo = pp.load_config(str(path))['repository']
    if executor == 'pipeline':
        from promptpilot.review_routing_pipeline_policy import CONTRACT
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['technical_fix_execution'] = dict(enabled=True, protocol='review-routing-v1', executor=dict(kind='pipeline', login='owner'))
        path.write_text(json.dumps(raw), encoding='utf-8')
        health['fix_execution_contract'] = copy.deepcopy(CONTRACT)
    code, selected = cli(path, 'next', 'fix')
    assert code == 0, selected
    decoder = author_requests.decode if executor == 'author' else pipeline_requests.decode
    original = decoder(selected['lease'])
    assert original['repository'] == repo
    before = list(peer.posts)
    mutations = {'repository-other': lambda v: v.update(repository='other/MixedCase'), 'repository-null': lambda v: v.update(repository=None), 'repository-list': lambda v: v.update(repository=[]), 'repository-int': lambda v: v.update(repository=7), 'repository-bool': lambda v: v.update(repository=True), 'canonical-case-only': lambda v: v.update(repository=v['repository'].swapcase()), 'identity-node': lambda v: v['identity'].update(base_repository_id='R_OTHER'), 'identity-head': lambda v: v['identity'].update(head_ref='other'), 'author-login': lambda v: v['author'].update(login='other'), 'author-null': lambda v: v.update(author=None), 'number-bool': lambda v: v.update(number=True), 'source-head': lambda v: v['attempt']['source'].update(head='f' * 40), 'reservation-other': lambda v: v['target_reservation'].update(repository='other/repo'), 'reservation-number-bool': lambda v: v['target_reservation'].update(number=True), 'reservation-task-bool': lambda v: v['target_reservation'].update(task_id=True), 'reservation-stage': lambda v: v['target_reservation'].update(stage='review'), 'reservation-head': lambda v: v['target_reservation'].update(head='e' * 40), 'ownership': lambda v: v['target_reservation'].update(ownership_kind='herdr'), 'herdr-pane': lambda v: v['target_reservation'].update(herdr_pane_id='foreign'), 'timestamp-naive': lambda v: v['target_reservation'].update(reserved_at='2026-11-01T12:00:00'), 'lifetime-bool': lambda v: v.update(expires_at=True), 'limit-bool': lambda v: v.update(current_limit=True), 'extra-field': lambda v: v.update(extra=True)}
    command = ('complete', 'fix') if executor == 'author' else ('prepare-fix-start', 'fix')
    rejected = []
    for name, mutate in mutations.items():
        value = copy.deepcopy(original)
        mutate(value)
        token = pp.encode_signed_lease(value)
        code, result = cli(path, *command, '--lease', token)
        assert code == 2 and result['action'] == 'error', (name, result)
        assert peer.posts == before and (not peer.assignment_posts), name
        rejected.append(dict(probe=name, error=result['error']))
    raw = pp.canonical(original).decode('utf-8')
    malformed = {'duplicate-version': raw[:-1] + ',"version":1}', 'noncanonical-whitespace': ' ' + raw, 'invalid-json': '{"version":1,', 'array-root': '[1]', 'invalid-utf8': b'{"version":1,"repository":"\xff"}'}
    for name, payload in malformed.items():
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        body = base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
        signature = base64.urlsafe_b64encode(hmac.new(pp.pipeline_lease_key(create=False), body.encode('ascii'), hashlib.sha256).digest()).decode('ascii').rstrip('=')
        code, result = cli(path, *command, '--lease', body + '.' + signature)
        assert code == 2 and result['action'] == 'error', (name, result)
        assert peer.posts == before and (not peer.assignment_posts)
        rejected.append(dict(probe=name, error=result['error']))
