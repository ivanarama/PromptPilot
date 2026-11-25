import json
import time

import pytest

from promptpilot import pipeline_insights as pi
from tests.source_cost_fixtures import observed_rates
from tests.test_fallback_handoff import HEAD
from tests.test_github_budget import _limits
from tests.test_merge_checkpoint import (
    BASE,
    OLD,
    dispatch,
    elect,
    new_claim,
    run_cli,
    start,
)
from tests.test_merge_final_source import aged_waiter, merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import (
    continuation as continuation,
    targeted_merge as targeted_merge,
)


@pytest.fixture
def recovery(merge_dispatch, monkeypatch, capsys):
    f = merge_dispatch
    execution = f.profile["queues"][0]["execution"]
    execution["merge_confirmation"] = "bounded-v1"
    f.profile["github_budget"]["costs"]["tool_preflight"]["core"] = 600
    initial = start(f, monkeypatch)
    assert run_cli(monkeypatch, capsys, f, initial, "record")["state"] == "recorded"
    new_claim(f)
    current = elect(f, monkeypatch, HEAD, "integration-merge-recovery")
    current["integration_owner"]["base_sync_candidate"] = {
        "from": OLD,
        "base": BASE,
        "to": HEAD,
        "source": "head_parents",
    }
    snapshot = {
        "repository": f.profile["repository"],
        "diagnostics": current,
        "cache": {
            "complete": True,
            "stale": False,
            "available": True,
            "profile_hash": pi._profile_fingerprint(f.profile),
        },
        "queues": [{"id": "merge", "membership_complete": True}],
    }
    monkeypatch.setattr(pi, "read_cached", lambda *_: snapshot)
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(core=1094)))
    return f, snapshot


def test_checked_pr_uses_only_the_bounded_executor_at_1094_core(recovery, monkeypatch):
    f, snapshot = recovery

    def finish(
        task,
        profile_id,
        profile,
        queue,
        admission,
        command,
        working_dir,
        handoff,
        checkpoint,
    ):
        rows = f.db.pipeline_github_budget_reservations("github-default")
        assert rows["count"] == 1 and rows["items"][0]["route"] == "merge_confirmation"
        assert rows["totals"]["core"] < 1094 - 250 - 50
        assert checkpoint["number"] == 42 and checkpoint["to"] == HEAD
        return {"action": "complete_empty", "mode": "tool", "verdict": "ГОТОВО"}

    from promptpilot import merge_confirmation

    monkeypatch.setattr(merge_confirmation, "execute", finish)
    route = dispatch(f)
    assert route["action"] == "complete_empty", route
    pi.release_execution_admission()
    snapshot["diagnostics"]["integration_owner"]["base_sync_candidate"]["base"] = "e" * 40
    refused = dispatch(f)
    assert refused["action"] == "defer" and "matching fresh Source" in refused["reason"]
    assert not f.db.pipeline_github_budget_reservations("github-default")["items"]


def test_checked_pr_yields_to_aged_waiter(recovery):
    f, _ = recovery
    aged_waiter(f)
    route = dispatch(f)
    assert route["action"] == "defer" and route["github_budget"]["state"] == "fairness_waiter"
    assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0


@pytest.mark.parametrize("stale", [True, False], ids=["signed-recovery", "excluded-owner-wait"])
def test_exact_held_owner_reports_reason_and_rechecks_after_resolution(recovery, monkeypatch, stale):
    from promptpilot import merge_confirmation, pipeline_item_holds

    f, snapshot = recovery
    key = pipeline_item_holds.PREFIX + str(f.task.series_id)
    reason = "#42 — cannot verify pushed base-sync"
    record = json.dumps({"42": {"stage": "merge", "head": HEAD, "at": time.time(),
                                "reason": reason, "task_id": 100}})
    f.db.set_setting(key, record)
    snapshot["cache"]["stale"] = stale
    original = pi._tool_preflight
    probes, confirmations = [], []

    def probe(*args, env_extra=None):
        probes.append(env_extra)
        if env_extra:
            assert json.loads(env_extra["PP_PIPELINE_EXCLUDED_NUMBERS"]) == [42]
            return {"action": "wait", "number": 42, "reason": "single-flight owner awaits human resolution"}
        return original(*args)

    def confirm(*args, **kwargs):
        confirmations.append(1)
        return {"action": "complete_empty", "verdict": "ГОТОВО"}

    monkeypatch.setattr(pi, "_tool_preflight", probe)
    monkeypatch.setattr(merge_confirmation, "execute", confirm)
    wait = dispatch(f)
    assert wait["action"] == "defer" and wait["defer_for"] == "10m"
    assert reason in wait["reason"] and HEAD[:8] in wait["reason"]
    assert bool(probes[0]) is not stale and len(probes) == 1
    assert not confirmations and f.db.get_setting(key) == record
    assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0

    f.db.set_setting(key, "{}")
    snapshot["cache"]["stale"] = False
    assert dispatch(f)["verdict"] == "ГОТОВО"
    assert len(probes) == 2 and confirmations == [1]


@pytest.mark.parametrize("head,age", [(OLD, 0), (HEAD, 24 * 3600)])
def test_other_head_or_expired_hold_does_not_block_confirmation(recovery, monkeypatch, head, age):
    from promptpilot import merge_confirmation, pipeline_item_holds

    f, _ = recovery
    key = pipeline_item_holds.PREFIX + str(f.task.series_id)
    f.db.set_setting(key, json.dumps({"42": {"stage": "merge", "head": head, "at": time.time() - age}}))
    monkeypatch.setattr(merge_confirmation, "execute", lambda *_: {"action": "complete_empty", "verdict": "ГОТОВО"})
    assert dispatch(f)["verdict"] == "ГОТОВО"


@pytest.fixture
def native_recovery(merge_dispatch, monkeypatch, capsys, tmp_path):
    import json
    from promptpilot import fallback_handoff, project_pipeline as pp
    from tests.test_fallback_handoff import health

    f = merge_dispatch
    queue = f.profile["queues"][0]
    queue["item_blockers"] = True
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(
        json.dumps(
            {
                "repository": f.profile["repository"],
                "trusted_account": "owner",
                "health_command": ["checker"],
                "sync_base_before_health": True,
                "fallback_handoff": "target-v1",
            }
        )
    )
    project = pp.load_config(str(config_path))
    monkeypatch.chdir(tmp_path)
    queue["execution"].update(
        merge_confirmation="bounded-v1",
        command=["pipeline", "--config", str(config_path), "next", "merge"],
    )
    f.profile["github_budget"]["costs"]["tool_preflight"]["core"] = 600

    def election(stage, head):
        state = health(stage)
        state["merge_executable"][0]["head"] = head
        handoff = fallback_handoff.create(
            project, state, "merge", state["merge_executable"][0], "elect"
        )
        monkeypatch.setattr(pi, "_tool_preflight", lambda *_: handoff)
        return state

    election("merge", OLD)
    initial = dispatch(f)
    assert run_cli(monkeypatch, capsys, f, initial, "record")["state"] == "recorded"
    new_claim(f)
    state = election("integration-merge-recovery", HEAD)
    state["integration_owner"]["base_sync_candidate"] = {
        "from": OLD,
        "base": BASE,
        "to": HEAD,
        "source": "head_parents",
    }
    snapshot = {
        "repository": project["repository"],
        "diagnostics": state,
        "cache": {
            "complete": True,
            "stale": False,
            "available": True,
            "profile_hash": pi._profile_fingerprint(f.profile),
        },
        "queues": [{"id": "merge", "membership_complete": True}],
    }
    state["exit_code"] = 0
    snapshot.update(profile_id="example", runtime={"state": "online", "paused": False})
    snapshot["queues"][0].update(
        task_id=f.task.id,
        task_status="running",
        replica_status={"valid": True},
        admission_items=[{"number": 42}],
    )
    monkeypatch.setattr(pi, "read_cached", lambda *_: snapshot)
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(core=1094)))
    monkeypatch.setattr(pp, "GitHub", lambda **_: object())
    return f, snapshot, project


def test_long_preparation_refreshes_once_within_held_scan_lease(native_recovery, monkeypatch):
    from promptpilot import merge_finish_proof, pipeline_source_refresh
    from tests.source_cost_fixtures import checker_receipt

    f, snapshot, _ = native_recovery
    seen = []

    def checker(_):
        # Public continuation commands never receive this tool's allowance.
        with pytest.raises(ValueError, match="exact existing execution reservation"):
            pipeline_source_refresh.refresh("example", {"PP_TASK_ID": str(f.task.id)}, final=True)
        seen.append(pi._scan_lease_context.lease.token)
        snapshot["cache"]["stale"] = False
        return checker_receipt(snapshot["diagnostics"])

    monkeypatch.setattr(pi, "_run_profile_health_check", checker)
    analyze = pi._analyze_without_budget

    def publish(*args, **kwargs):
        value = analyze(*args, **kwargs)
        snapshot["cache"]["token"] = value["cache"]["token"]
        return value

    monkeypatch.setattr(pi, "_analyze_without_budget", publish)

    def observed(gh, config, checkpoint, *, prepare_source, validate_attempt, **kwargs):
        seen.append(pi._scan_lease_context.lease.token)
        snapshot["cache"]["stale"] = True
        validate_attempt()
        prepare_source()
        assert len(seen) == 2 and seen[0] == seen[1]
        assert (
            f.db.pipeline_github_budget_reservations("github-default")["items"][0].get(
                "final_source_cost"
            )
            is None
        )
        validate_attempt()
        with pytest.raises(RuntimeError, match="repeated final Source"):
            prepare_source()
        return {
            "action": "completed",
            "stage": "merge-confirmation",
            "number": 42,
            "head": HEAD,
            "intent": 101,
            "done": 501,
            "published": False,
        }

    monkeypatch.setattr(merge_finish_proof, "confirm", observed)
    route = dispatch(f)
    assert route["action"] == "complete_empty" and route["verdict"] == "ПУСТО", route
    assert len(seen) == 2 and len(f.db.list_pipeline_snapshots("example")) == 1


def test_general_recovery_rechecks_full_price_after_narrow_proof(native_recovery, monkeypatch):
    from promptpilot import merge_finish_proof

    f, _, _ = native_recovery
    low = False

    def limits():
        return observed_rates(_limits(core=1094 if low else 1800))

    monkeypatch.setattr(pi, "_github_rate_limits", limits)

    def outside(*args, **kwargs):
        nonlocal low
        low = True
        raise merge_finish_proof.OutsideScope("not a first-hop intent")

    monkeypatch.setattr(merge_finish_proof, "confirm", outside)
    route = dispatch(f)
    assert (
        low
        and route["action"] == "defer"
        and route["github_budget"]["requested_cost"]["core"] == 1050
    ), route
    assert not f.db.pipeline_github_budget_reservations("github-default")["items"]


def test_uncertain_send_holds_only_its_exact_fallback_through_new_claim(
    native_recovery, monkeypatch
):
    import hashlib
    import time
    from promptpilot import fallback_handoff, merge_checkpoint, merge_finish_proof, project_pipeline as pp
    from tests.test_fallback_handoff import health

    f, snapshot, project = native_recovery
    digest = hashlib.sha256(b"exact-comment").hexdigest()
    raw = snapshot["cache"]
    raw["token"] = {
        "epoch": 0,
        "revision": 1,
        "generated_at": time.time(),
        "profile_hash": raw["profile_hash"],
    }
    f.db.set_setting(pi._published_revision_key("example", raw["profile_hash"]), "1")
    monkeypatch.setattr(
        fallback_handoff,
        "gate",
        lambda gh, config, stage, token, **kw: {
            "action": "validated",
            "mutation_authorized": False,
            "target": {"stage": "integration-merge-recovery", "number": 42, "head": HEAD},
        },
    )

    class UncertainBackend:
        def json(self, *args, **kwargs):
            assert args[-4:] == ("--method", "POST", "--input", "-")
            raise RuntimeError("connection dropped after POST")

    monkeypatch.setattr(pp, "GitHub", lambda **kw: UncertainBackend())

    def lose_response(gh, config, checkpoint, *, gate, claim_send, prepare_source, **kwargs):
        prepare_source()
        gate()
        assert claim_send(42, HEAD, 101, digest)
        return pp.post_comment(gh, config, 42, "exact-comment")

    monkeypatch.setattr(merge_finish_proof, "confirm", lose_response)
    route = dispatch(f)
    assert route["action"] == "defer" and "connection dropped after POST" in route["reason"]
    key = merge_checkpoint.pending_key(project["repository"], {"number": 42, "head": HEAD})
    assert f.db.get_setting(key)
    new_claim(f)
    f.profile["queues"][0]["execution"].pop("merge_confirmation")
    snapshot["cache"]["profile_hash"] = pi._profile_fingerprint(f.profile)
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits()))
    monkeypatch.setattr(merge_finish_proof, "observe_delivery", lambda *a, **kw: None)
    retry = dispatch(f)
    assert retry["action"] == "defer" and "not yet visible" in retry["reason"]
    pi.release_execution_admission()
    other = health("merge")
    target = other["merge_executable"][0]
    target.update(number=43, head="e" * 40)
    fresh = fallback_handoff.create(project, other, "merge", target, "another PR")
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: fresh)
    assert dispatch(f)["action"] == "prompt"
    assert f.db.get_setting(key)


def test_confirmation_respects_other_reservations_at_real_floor(recovery):
    from promptpilot.models import TaskCreate

    f, _ = recovery
    f.db.create_task(TaskCreate(prompt="other profile"))
    other = f.db.get_next_runnable()
    assert f.db.reserve_pipeline_github_budget(
        "github-default",
        token="other-reservation",
        task_id=other.id,
        task_started_at=other.started_at,
        profile_id="other",
        queue_id="fix",
        route="skill",
        cost={"core": 650, "graphql": 0, "search": 0},
        limits=_limits(),
        minimum_remaining={"core": 250, "graphql": 100, "search": 2},
    )["allowed"]
    result = dispatch(f)
    assert result["action"] == "defer" and result["github_budget"]["state"] == "budget_in_flight"
    assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 1


@pytest.mark.parametrize("current_head", [True, False])
def test_waiting_for_review_reconciles_a_previous_send_without_a_provider(
    native_recovery, monkeypatch, current_head
):
    import json
    from promptpilot import merge_checkpoint, merge_finish_proof

    f, _, project = native_recovery
    target = {"number": 42, "head": HEAD}
    key = merge_checkpoint.pending_key(project["repository"], target)
    record = {
        "repository": project["repository"],
        "number": 42,
        "to": HEAD,
        "from": OLD,
        "base": BASE,
        "tree": "d" * 40,
        "intent": 101,
        "body_sha256": "f" * 64,
    }
    f.db.set_setting(key, json.dumps(record))
    seen = []

    def observe(gh, saved, *, config):
        assert gh.phase == "receipt" and saved == record
        rows = f.db.pipeline_github_budget_reservations("github-default")
        assert rows["count"] == 1 and rows["items"][0]["route"] == "merge_receipt"
        seen.append(saved["number"])
        return 501, current_head

    monkeypatch.setattr(merge_finish_proof, "observe_delivery", observe)
    monkeypatch.setattr(
        pi,
        "_tool_preflight",
        lambda *_: {"action": "wait", "reason": "owner awaits integration review"},
    )
    route = dispatch(f)
    assert seen == [42] and route["action"] == "complete_empty"
    assert route["verdict"] == ("ГОТОВО" if current_head else "ПУСТО")
    assert f.db.get_setting(key) is None


def test_final_source_checker_failure_preserves_causal_stop(native_recovery, monkeypatch):
    from promptpilot import merge_finish_proof

    f, snapshot, _ = native_recovery
    monkeypatch.setattr(
        pi,
        "_run_profile_health_check",
        lambda *_: {"checker_failed": True, "summary": "broken checker"},
    )

    def failing(*a, prepare_source, **kwargs):
        snapshot["cache"]["stale"] = True
        prepare_source()
        pytest.fail("failed checker admitted a mutation")

    monkeypatch.setattr(merge_finish_proof, "confirm", failing)
    refused = dispatch(f)
    assert refused["action"] == "defer"
    event = f.db.get_pipeline_refresh_status("example", strict=True)
    assert (
        event["source_checker_failure_revisions"][pi._profile_fingerprint(f.profile)]
        == event["revision"]
    )
