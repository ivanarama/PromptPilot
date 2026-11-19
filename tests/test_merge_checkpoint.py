"""Exercise an ordinary MERGE from its signed dispatch through a new Native claim."""
import json
import sys
from types import SimpleNamespace

import pytest

from promptpilot import config, entrypoint, fallback_handoff, pipeline_insights as pi
from promptpilot.models import TaskCreate
from tests.test_fallback_handoff import HEAD, health
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import continuation as continuation, targeted_merge as targeted_merge


OLD, BASE, TREE = "b" * 40, "c" * 40, "d" * 40


def elect(f, monkeypatch, head, stage):
    current = health(stage)
    for item in current["merge_executable"]:
        item["head"] = head
    config = {"repository": f.profile["repository"]}
    target = current["merge_executable"][0]
    preflight = fallback_handoff.create(config, current, "merge", target, "project fallback")
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: preflight)
    return current


def run_cli(monkeypatch, capsys, f, route, operation, *, intent=101, old=OLD, head=HEAD,
            marker=None, gate_file=None):
    command = ["pp", "pipelinectl", "merge-checkpoint", operation, "--profile", "example",
               "--number", "42", "--to", head, "--lease-file", gate_file or route["gate_command"][-1]]
    if operation == "record":
        command += ["--from", old, "--base", BASE, "--tree", TREE, "--intent", str(intent)]
    monkeypatch.setenv("PP_TASK_ID", str(f.task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", marker or f.task.started_at.isoformat())
    monkeypatch.setattr(sys, "argv", command)
    with pytest.raises(SystemExit) as invoked:
        entrypoint.main()
    result = json.loads(capsys.readouterr().out)
    assert result["product_mutation_authorized"] is False
    assert invoked.value.code == int(result["state"] == "refused")
    return result


def dispatch(f):
    return pi.execution_route(f.task, f.task.prompt, retain_budget=True)


def new_claim(f):
    pi.release_execution_admission()
    f.db.create_task(TaskCreate(prompt=f.task.prompt, priority=1, recurrence="1h", task_timeout=600))
    task = f.db.get_next_runnable()
    f.db.set_session_id(task.id, "recovery-session")
    f.task = f.db.get_task(task.id)


def start(f, monkeypatch, stage="merge"):
    f.profile["queues"][0]["item_blockers"] = True
    elect(f, monkeypatch, OLD, stage)
    route = dispatch(f)
    assert route["action"] == "prompt", route
    return route


@pytest.mark.parametrize("sampler_interval", [120, 0])
def test_saved_update_survives_new_claim_and_waits_for_fresh_source(
        merge_dispatch, monkeypatch, capsys, sampler_interval):
    f = merge_dispatch
    monkeypatch.setattr(config, "PIPELINE_SNAPSHOT_INTERVAL", sampler_interval)
    initial = start(f, monkeypatch)
    assert run_cli(monkeypatch, capsys, f, initial, "record", marker="wrong")["state"] == "refused"
    assert run_cli(monkeypatch, capsys, f, initial, "record", old="e" * 40)["state"] == "refused"
    saved = run_cli(monkeypatch, capsys, f, initial, "record")
    assert saved["state"] == "recorded" and saved["checkpoint"]["intent"] == 101
    assert run_cli(monkeypatch, capsys, f, initial, "record") == saved
    assert run_cli(monkeypatch, capsys, f, initial, "record", intent=102)["state"] == "refused"
    new_claim(f)
    source = elect(f, monkeypatch, HEAD, "integration-merge-recovery")
    source["integration_owner"]["base_sync_candidate"] = {
        "from": OLD, "to": HEAD, "base": BASE, "source": "head_parents"}
    cached = {"repository": f.profile["repository"], "diagnostics": source,
              "cache": {"profile_hash": pi._profile_fingerprint(f.profile), "complete": True,
                        "stale": True, "available": True},
              "queues": [{"id": "merge", "membership_complete": True, "items": []}]}
    monkeypatch.setattr(pi, "read_cached", lambda *_: cached)
    unavailable = dispatch(f)
    if sampler_interval:
        assert unavailable["action"] == "defer" and "fresh Source" in unavailable["reason"]
        assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0
    else:
        assert unavailable["action"] == "prompt" and '"merge_checkpoint"' not in unavailable["prompt"]
        assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 1
        new_claim(f)
    cached["cache"]["stale"] = False
    recovery = dispatch(f)
    assert recovery["action"] == "prompt" and '"merge_checkpoint"' in recovery["prompt"]
    assert run_cli(monkeypatch, capsys, f, recovery, "read") == dict(saved, state="found")
    assert run_cli(monkeypatch, capsys, f, recovery, "read",
                   gate_file=initial["gate_command"][-1])["state"] == "refused"
    new_claim(f)
    source["integration_owner"]["base_sync_candidate"]["base"] = "e" * 40
    changed = dispatch(f)
    assert changed["action"] == "prompt" and '"merge_checkpoint"' not in changed["prompt"]


@pytest.mark.parametrize("stage", ["integration-merge-ready", "legacy-integration-merge-ready"])
def test_another_base_update_after_integration_review(merge_dispatch, monkeypatch, capsys, stage):
    f = merge_dispatch
    initial = start(f, monkeypatch, stage)
    assert run_cli(monkeypatch, capsys, f, initial, "read", head=OLD)["state"] == "refused"
    assert run_cli(monkeypatch, capsys, f, initial, "record", old="e" * 40)["state"] == "refused"
    saved = run_cli(monkeypatch, capsys, f, initial, "record")
    assert saved["state"] == "recorded"
    new_claim(f)
    elect(f, monkeypatch, HEAD, "integration-merge-recovery")
    monkeypatch.setattr(config, "PIPELINE_SNAPSHOT_INTERVAL", 0)
    recovery = dispatch(f)
    assert recovery["action"] == "prompt"
    assert run_cli(monkeypatch, capsys, f, recovery, "read") == dict(saved, state="found")


def test_crash_after_push_before_local_record_can_reconstruct_from_fresh_claim(
        merge_dispatch, monkeypatch, capsys):
    f = merge_dispatch
    initial = start(f, monkeypatch)
    # The server already updated the ref when the original lease expired.
    with monkeypatch.context() as expired:
        now = fallback_handoff.time.time()
        expired.setattr(fallback_handoff, "time", SimpleNamespace(time=lambda: now + fallback_handoff.TTL + 1))
        assert run_cli(monkeypatch, capsys, f, initial, "record")["state"] == "refused"
    new_claim(f)
    elect(f, monkeypatch, HEAD, "integration-merge-recovery")
    route = dispatch(f)
    assert route["action"] == "prompt" and '"merge_checkpoint"' not in route["prompt"]
    assert run_cli(monkeypatch, capsys, f, route, "read")["state"] == "missing"
    # Even a valid signed gate copied to this occurrence was not elected for it.
    other_config = {"repository": f.profile["repository"], "base_branch": "another"}
    current = health("integration-merge-recovery")
    other = fallback_handoff.create(other_config, current, "merge", current["integration_owner"], "recovery")
    other_path = pi._lease_argument(f.task, other["handoff"]["lease"], "gate")[-1]
    assert run_cli(monkeypatch, capsys, f, route, "read", gate_file=other_path)["state"] == "refused"
    assert run_cli(monkeypatch, capsys, f, route, "record")["state"] == "recorded"
    assert run_cli(monkeypatch, capsys, f, route, "read")["state"] == "found"
    assert run_cli(monkeypatch, capsys, f, route, "read", head="e" * 40)["state"] == "refused"
    f.db.set_setting(f"cancel_task:{f.task.id}", "1")
    assert run_cli(monkeypatch, capsys, f, route, "read")["state"] == "refused"
