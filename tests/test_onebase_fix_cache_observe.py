import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import onebase_fix_cache_observe as late
from tools import onebase_fix_postpush as post
from tools import onebase_fix_prepush as pre


@pytest.fixture
def attempt(tmp_path):
    root = tmp_path / "fix-runs"
    root.mkdir()
    task, started, binding = 21, "2026-11-26T15:00:00+00:00", "active-worker-binding"
    now = late.time.time()
    token = dict(profile_hash="a" * 64, revision=2, epoch=1, generated_at=now - 30)
    cache = dict(available=True, complete=True, stale=False, invalidated=False, refresh_blocked=None,
                 generated_at=token["generated_at"], token=token, profile_hash=token["profile_hash"],
                 ttl_seconds=300, source="live")
    value = dict(task_id=task, task_started_at=started, state="refreshed", source_refreshed=True,
                 native_session_bound=True, existing_skill_reservation_reused=True,
                 admitted_route="skill", profile_id="onebase", queue_id="fix",
                 scope="Native-source-observation-only", product_mutation_authorized=False,
                 cache=cache, snapshot_token=token)
    source = root / "source.json"
    source.write_bytes(pre.encode(value))
    ready = dict(budget_observation_scope="running-budget-policy-v4", proof_kind="running", state="ready",
                 ready=True, profile_hash=token["profile_hash"], task_id=task, started_at=started,
                 profile_id="onebase", queue_id="fix", series_id=7, decision=dict(allowed=True, budget_route="skill"),
                 native_admission_granted=False, mutable_admission_granted=False,
                 receipt_id="budget", rate_observed_at=now - 8, generated_at=now - 2)
    inst = SimpleNamespace(root=root, data_dir=tmp_path, db=tmp_path / "promptpilot.db", prepush=pre,
                           priority_read=Mock(return_value=False),
                           working_dir=tmp_path, series=7, identity=Mock(return_value=(task, started, binding)),
                           verify_worker=Mock(), checked_origin=Mock())
    return inst, source, value, ready


def test_transient_read_and_replay_share_one_result(attempt):
    inst, source, prior, ready = attempt
    report = dict(profile_id="onebase", repository="ivanarama/onebase", cache={**prior["cache"], "source": "durable"},
                  diagnostics={"exit_code": 0})
    read = Mock(side_effect=[TimeoutError(), report])
    now = [100.0]

    def sleep(seconds):
        now[0] += seconds

    def observe(*args, **kwargs):
        return post.observe(*args, **kwargs, read=read, clock=lambda: now[0], sleep=sleep)

    kwargs = dict(budget=lambda: ready, observe=observe)
    first = late.observe_once(inst, source, 60, **kwargs)
    second = late.observe_once(inst, source, 60, **kwargs)
    assert first == second
    assert first["fresh_snapshot"] == prior["snapshot_token"]
    assert not first["publication_authorized"]
    assert read.call_count == 2
    assert len(list(inst.data_dir.rglob("observe.*.json"))) == 2


def test_reader_error_and_process_restart_cannot_open_second_window(attempt):
    inst, source, _, ready = attempt
    read = Mock(side_effect=ValueError("checker failed"))
    with pytest.raises(ValueError, match="checker failed"):
        late.observe_once(inst, source, 60, budget=lambda: ready, observe=read)
    with pytest.raises(ValueError, match="already opened"):
        late.observe_once(inst, source, 60, budget=lambda: ready, observe=read)
    assert read.call_count == 1
    assert not list(inst.data_dir.rglob("observe.result.json"))


@pytest.mark.parametrize("case", ["wrong-series", "old-budget", "bad-window", "reused-bytes"])
def test_preconditions_do_not_spend_window(attempt, case):
    inst, source, prior, ready = attempt
    seconds = 61
    if case == "wrong-series":
        ready["series_id"] = 2
    elif case == "old-budget":
        ready["rate_observed_at"] -= 100
    elif case == "bad-window":
        seconds = 301
    else:
        prior.update(state="reused", source_reused=True, source_refreshed=False, observed_at=late.time.time(),
                     mutable_admission_granted=False, existing_execution_reservation_reused=True,
                     attempt_binding_sha256="d" * 64)
        prior["cache"]["source"] = "durable"
        source.write_text(json.dumps(prior))
        inst.checked_origin.return_value = {"source_sha256": "b" * 64}
    read = Mock()
    with pytest.raises(ValueError):
        late.observe_once(inst, source, seconds, budget=lambda: ready, observe=read)
    read.assert_not_called()
    assert not list(inst.data_dir.rglob("observe.intent.json"))


def test_attempt_replaced_in_flight_cannot_publish(attempt):
    inst, source, _, ready = attempt

    def observe(*args, **kwargs):
        inst.identity.return_value = (22, "2026-11-26T15:01:00+00:00", "another-worker")
        return {"fresh_snapshot": None, "publication_authorized": False}

    with pytest.raises(ValueError, match="changed during"):
        late.observe_once(inst, source, 10, budget=lambda: ready, observe=observe)
    assert not list(inst.data_dir.rglob("observe.result.json"))
