"""Replay the normal signed MERGE dispatch on each side of GitHub's send boundary."""

import hashlib
import json
import time

import pytest

from promptpilot import fallback_handoff, merge_checkpoint, merge_done_proof, merge_finish_proof
from promptpilot import pipeline_insights as pi, pipeline_item_holds as holds, project_pipeline as pp
from tests.source_cost_fixtures import observed_rates
from tests.test_bounded_merge_finish import native_recovery as native_recovery
from tests.test_github_budget import _limits
from tests.test_merge_checkpoint import BASE, HEAD, OLD, TREE, dispatch, elect, new_claim, run_cli
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import continuation as continuation, targeted_merge as targeted_merge


@pytest.mark.parametrize(("managed", "record", "verdict", "held"), [
    (True, True, "НЕ СМОГ", False), (False, True, "НЕ СМОГ", True),
    (True, False, "НЕ СМОГ", True), (True, True, "НУЖЕН ЧЕЛОВЕК", True),
])
def test_recovery_reconstructs_checkpoint_then_provider_stops(merge_dispatch, monkeypatch, capsys,
                                                              managed, record, verdict, held):
    f = merge_dispatch
    queue = f.profile["queues"][0]
    queue["item_blockers"] = True
    if managed:
        queue["execution"]["merge_confirmation"] = "bounded-v1"
    health = elect(f, monkeypatch, HEAD, merge_checkpoint.RECOVERY)
    health["integration_owner"]["base_sync_candidate"] = {"from": OLD, "to": HEAD, "base": BASE, "source": "head_parents"}
    data = {"profile_id": "example", "repository": f.profile["repository"], "diagnostics": health,
            "cache": pi._cache_metadata("durable", time.time(), 2, 3, 2, pi._profile_fingerprint(f.profile)),
            "queues": [{"id": "merge", "membership_complete": True, "backlog": 1,
                        "items": [{"number": 42, "head": HEAD, "kind": "pr", "updated_at": HEAD}]}]}
    monkeypatch.setattr(pi, "read_cached", lambda *_: data)
    route = dispatch(f)
    assert route["action"] == "prompt" and route["target"]["head"] == HEAD
    if managed:
        assert "checkpoint_record_command" in route["prompt"] and "Не публикуй done" in route["prompt"]
    if record:
        saved = run_cli(monkeypatch, capsys, f, route, "record")
        assert saved["state"] == "recorded" and (saved.get("next_step") == "native-confirmation") == managed
        assert saved["checkpoint"]["tree"] == TREE
        assert run_cli(monkeypatch, capsys, f, route, "read")["checkpoint"] == saved["checkpoint"]
    # The free-form report is identical with and without the native transfer.
    report = f"ИТОГ: {verdict} (#42 — Source изменился после gate; done не подтверждён)"
    f.db.mark_completed(f.task.id, report, verdict=verdict)
    assert f.db.repair_active_series_occurrences(repeat_guard_series_ids=[f.task.series_id]) == [f.task.series_id]
    s = f.db.get_series(f.task.series_id)
    successor = f.db.get_task(s["next_task_id"])
    assert not s["paused"] and successor.status.value == "pending"
    exact = json.loads(f.db.get_setting(holds.PREFIX + str(f.task.series_id)) or "{}").get("42")
    assert bool(exact and exact["head"] == HEAD) == held
    assert (42 in holds.prepare(successor, queue, data)) == held


@pytest.mark.parametrize("pre_send_failure", ["revision", "allowance"])
def test_full_recovery_commits_only_at_http_and_reconciles_lost_response(native_recovery, monkeypatch, pre_send_failure):
    f, snapshot, config = native_recovery
    target = {"number": 42, "head": HEAD, "stage": merge_checkpoint.RECOVERY}
    key = merge_checkpoint.pending_key(config["repository"], target)
    token = {"epoch": 0, "revision": 1, "generated_at": time.time(),
             "profile_hash": snapshot["cache"]["profile_hash"]}
    revision_key = pi._published_revision_key("example", token["profile_hash"])
    snapshot["cache"]["token"] = token
    f.db.set_setting(revision_key, "1")
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(core=2500)))
    body = "canonical done for this checkpoint"
    gates, sent = [], []

    def prepare(gh, conf, cp, **kwargs):
        observed = f.db.pipeline_github_budget_reservations("github-default")["items"]
        assert len(observed) == 1 and observed[0]["route"] == "merge_full_confirmation"
        assert cp["to"] == target["head"]
        return body, None, "immutable digest"

    def gate(gh, conf, stage, lease, **kwargs):
        gates.append(f.task.id)
        return {"action": "validated", "repository": config["repository"], "stage": "merge",
                "target": target, "mutation_authorized": False}

    class Backend:
        def json(self, *args, input_value):
            assert args == ("api", f"repos/{config['repository']}/issues/42/comments", "--method", "POST", "--input", "-")
            record = json.loads(f.db.get_setting(key))
            assert record["proof_kind"] == "legacy-general-done-v1"
            assert record["body_sha256"] == hashlib.sha256(input_value["body"].encode()).hexdigest()
            sent.append(input_value["body"])
            raise pp.PipelineError("response lost after acceptance")

    monkeypatch.setattr(pp, "GitHub", lambda **kw: Backend())
    monkeypatch.setattr(merge_done_proof, "prepare", prepare)
    monkeypatch.setattr(fallback_handoff, "gate", gate)
    actual_post = pp.post_comment

    def no_http(gh, *args):
        assert not f.db.get_setting(key)
        if pre_send_failure == "revision":
            f.db.set_setting(revision_key, "2")
        else:
            gh.rest["publication"] = 0
        return actual_post(gh, *args)

    monkeypatch.setattr(pp, "post_comment", no_http)
    first = dispatch(f)
    assert first["action"] == "defer" and not sent and len(gates) == 1, first
    assert not f.db.get_setting(key)
    if pre_send_failure == "revision":
        assert first["state"] == "re-elect-before-send"
    monkeypatch.setattr(pp, "post_comment", actual_post)
    token["revision"] = 2
    f.db.set_setting(revision_key, "2")
    new_claim(f)
    snapshot["queues"][0]["task_id"] = f.task.id
    second = dispatch(f)
    assert second["action"] == "defer" and "response lost" in second["reason"], second
    assert sent == [body] and len(set(gates)) == 2 and f.db.get_setting(key)
    new_claim(f)
    snapshot["queues"][0]["task_id"] = f.task.id
    monkeypatch.setattr(merge_finish_proof, "observe_receipt", lambda *_a, **_kw: 502)
    third = dispatch(f)
    assert third["action"] == "complete_empty" and third["verdict"] == "ГОТОВО", third
    assert sent == [body] and len(gates) == 2 and not f.db.get_setting(key)


@pytest.mark.parametrize("drift", [False, True])
def test_only_current_immutable_proof_failure_reaches_operator(native_recovery, monkeypatch, drift):
    f, snapshot, config = native_recovery
    profile_hash = snapshot["cache"]["profile_hash"]
    key = pi._published_revision_key("example", profile_hash)
    snapshot["cache"]["token"] = {"epoch": 0, "revision": 1, "generated_at": time.time(), "profile_hash": profile_hash}
    f.db.set_setting(key, "1")
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(core=2500)))
    calls = []

    def proof(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            return "body", None, "digest"
        if drift:
            f.db.set_setting(key, "2")
        raise merge_done_proof.UnsupportedProof("unsupported pushed conflict file mode")

    monkeypatch.setattr(merge_done_proof, "prepare", proof)
    result = dispatch(f)
    if drift:
        assert result["action"] == "defer" and result["state"] == "re-elect-before-send"
    else:
        assert result["action"] == "block" and "#42" in result["reason"] and "file mode" in result["reason"]
    assert not merge_checkpoint.blocks_fallback(config["repository"], {"number": 42, "head": HEAD})


@pytest.mark.parametrize("node_matches", [True, False])
def test_full_success_requires_addressed_readback_of_the_post_response(native_recovery, monkeypatch, node_matches):
    f, snapshot, config = native_recovery
    profile_hash = snapshot["cache"]["profile_hash"]
    snapshot["cache"]["token"] = {"epoch": 0, "revision": 1, "generated_at": time.time(), "profile_hash": profile_hash}
    f.db.set_setting(pi._published_revision_key("example", profile_hash), "1")
    monkeypatch.setattr(pi, "_github_rate_limits", lambda: observed_rates(_limits(core=2500)))
    target = {"stage": merge_checkpoint.RECOVERY, "number": 42, "head": HEAD}
    monkeypatch.setattr(merge_done_proof, "prepare", lambda *a, **kw: ("exact", None, "digest"))
    monkeypatch.setattr(fallback_handoff, "gate", lambda *a, **kw: {
        "action": "validated", "repository": config["repository"], "stage": "merge",
        "target": target, "mutation_authorized": False})
    posts = []

    class Backend:
        def json(self, *args, input_value):
            posts.append(input_value)
            return {"id": 502, "node_id": "posted", "body": "exact", "user": {"login": config["trusted_account"]}}

    monkeypatch.setattr(pp, "GitHub", lambda **kw: Backend())
    def readback(gh, record, *, config, expected_node_id):
        assert gh.sent and expected_node_id == "posted" and record["intent"] == 101
        return 502 if node_matches else None

    monkeypatch.setattr(merge_finish_proof, "observe_receipt", readback)
    result = dispatch(f)
    assert posts == [{"body": "exact"}]
    assert merge_checkpoint.blocks_fallback(config["repository"], target) == (not node_matches)
    if node_matches:
        assert result["verdict"] == "ГОТОВО" and result["completion"]["done"] == 502
    else:
        assert result["action"] == "defer" and "canonical receipt" in result["reason"]
