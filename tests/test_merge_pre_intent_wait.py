"""A final Source collision wakes only a new, ordinary signed MERGE election."""
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

from promptpilot import pipeline_source_refresh
from promptpilot import config, entrypoint, fallback_handoff, merge_pre_intent_wait, pipeline_insights as pi
from tests.source_cost_fixtures import checker_receipt
from tests.test_fallback_handoff import health
from tests.test_merge_checkpoint import BASE, OLD, dispatch, elect
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import continuation as continuation, targeted_merge as targeted_merge


def envelope(route):
    return json.loads(route["prompt"].split("```json\n")[-1].split("\n```")[0])


@pytest.fixture
def ordinary(merge_dispatch, monkeypatch):
    f = merge_dispatch
    monkeypatch.setattr(config, "PIPELINE_SNAPSHOT_INTERVAL", 120)
    q = f.profile["queues"][0]
    q["item_blockers"] = True
    q["dispatch_gate"] = {"require_fresh_snapshot": True}
    q["execution"]["merge_confirmation"] = "bounded-v1"
    f.health = elect(f, monkeypatch, OLD, "merge")
    monkeypatch.setattr(pi, "_run_profile_health_check", lambda *_: checker_receipt(f.health))
    def publish():
        return pi.analyze("example", f.db.list_series(), use_cache=False, refresh_diagnostics=True)
    f.publish = publish
    f.data = publish()
    f.first = dispatch(f)
    assert f.first["action"] == "prompt", f.first
    monkeypatch.setenv("PP_TASK_ID", str(f.task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", f.task.started_at.isoformat())
    return f


def observe(f, monkeypatch, capsys, *, head=OLD, base=BASE):
    cmd = list(envelope(f.first)["before_intent_wait_command"])
    cmd[cmd.index("<planned_base>")] = base
    cmd[cmd.index("--from") + 1] = head
    monkeypatch.setattr(sys, "argv", ["pp", "pipelinectl", *cmd[cmd.index("merge-checkpoint"):]])
    with pytest.raises(SystemExit) as invoked:
        entrypoint.main()
    result = json.loads(capsys.readouterr().out)
    assert result["product_mutation_authorized"] is False
    assert invoked.value.code == int(result["state"] == "refused")
    return result


def leave(f, result, verdict="ПУСТО"):
    pi.release_execution_admission()
    f.db.mark_completed(f.task.id, result, verdict=verdict)
    f.db.repair_active_series_occurrences(repeat_guard_series_ids=[f.task.series_id])


def claim(f):
    f.task = f.db.get_next_runnable()
    assert f.task is not None
    f.db.set_session_id(f.task.id, "resume-session")
    f.task = f.db.get_task(f.task.id)


@pytest.mark.parametrize("reason", ["scan_in_progress", "invalidated"])
def test_wait_defers_without_provider_until_publication_then_rechecks(ordinary, monkeypatch, capsys, reason):
    f = ordinary
    if reason == "invalidated":
        pi._discard_cache("example")
    else:
        assert f.db.acquire_pipeline_scan_lease("github-default", "sampler", 120)["acquired"]
        final = pipeline_source_refresh.refresh("example", f.env, final=True)
        assert final["state"] == "wait" and final["budget"]["state"] == "scan_in_progress"
    seen = observe(f, monkeypatch, capsys)
    assert seen["reason"] == reason, seen
    leave(f, seen["result_line"])
    if reason == "scan_in_progress":
        assert f.db.release_pipeline_scan_lease("github-default", "sampler")
    sid = f.task.series_id
    assert f.db.series_action(sid, "run_now")
    claim(f)
    with monkeypatch.context() as m:
        m.setattr(pi, "_tool_preflight", lambda *_: pytest.fail("held the worker for an old snapshot"))
        m.setattr(pi, "_github_rate_limits", lambda: pytest.fail("scanned before publication"))
        m.setattr(pi, "analyze", lambda *_a, **_kw: pytest.fail("recursive refresh while waiting"))
        assert pi.dispatch_gate(f.task)["action"] == "defer"
        assert dispatch(f)["action"] == "defer"
    assert f.db.pipeline_github_budget_reservations("github-default")["count"] == 0
    assert f.db.defer_task(f.task.id, datetime.now(timezone.utc) + timedelta(minutes=8),
                           expected_started_at=f.task.started_at)
    fresh = f.publish()
    assert fresh["cache"]["token"]["revision"] > f.data["cache"]["token"]["revision"]
    claim(f)  # no run_now: publication itself made the occurrence runnable
    route = dispatch(f)
    payload = envelope(route)
    assert payload["before_intent_resume"] == {"number": 42, "head": OLD, "planned_base": BASE}
    assert route["gate_command"][-1] != f.first["gate_command"][-1]
    assert "gate-fallback" in route["gate_command"]
    assert "текущие HEAD, REVIEW, ship, CI, Source" in route["prompt"]
    pi.release_execution_admission()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert f.db.defer_task(f.task.id, deadline, expected_started_at=f.task.started_at)
    merge_pre_intent_wait.wake("example", f.profile, fresh)
    f.publish()
    assert f.db.get_next_runnable() is None  # same or additional publication cannot replay the wake


@pytest.mark.parametrize("change", ["head", "lost-ship", "new-review", "new-owner", "base", "ambiguous-result"])
def test_changed_election_or_result_never_receives_old_resume(ordinary, monkeypatch, capsys, change):
    f = ordinary
    pi._discard_cache("example")
    seen = observe(f, monkeypatch, capsys)
    if change == "ambiguous-result":
        leave(f, "ИТОГ: НУЖЕН ЧЕЛОВЕК (результат отправки неизвестен)", "НУЖЕН ЧЕЛОВЕК")
    else:
        leave(f, seen["result_line"])
    fresh_health = health("merge")
    target = fresh_health["merge_executable"][0]
    target["head"] = OLD if change in {"base", "ambiguous-result"} else "e" * 40
    if change == "base":
        target["base_ref_oid"] = "f" * 40
    if change in {"new-review", "lost-ship"}:
        fresh_health["merge_executable"] = []
        next_result = {"action": "empty", "reason": "no eligible MERGE target"}
    else:
        if change == "new-owner":
            fresh_health = health("integration-merge-recovery")
            target = fresh_health["integration_owner"]
        next_result = fallback_handoff.create({"repository": f.profile["repository"]},
                                              fresh_health, "merge", target, "fresh election")
    f.health = fresh_health
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: next_result)
    f.publish()
    assert f.db.series_action(f.task.series_id, "run_now")
    claim(f)
    route = dispatch(f)
    if change in {"new-review", "lost-ship"}:
        assert route["action"] == "complete_empty"
    else:
        assert "before_intent_resume" not in envelope(route)


def test_observer_rejects_wrong_claim_and_pending_send(ordinary, monkeypatch, capsys):
    f = ordinary
    pi._discard_cache("example")
    assert observe(f, monkeypatch, capsys, head="d" * 40)["state"] == "refused"
    assert observe(f, monkeypatch, capsys, base=OLD)["state"] == "refused"
    from promptpilot import merge_checkpoint
    f.db.set_setting(merge_checkpoint.pending_key(f.profile["repository"], {"number": 42, "head": OLD}), "pending")
    assert observe(f, monkeypatch, capsys)["state"] == "refused"
    assert not f.db.get_setting(merge_pre_intent_wait._key("example"))


def test_optional_hint_does_not_break_accepted_source_publication(ordinary, monkeypatch):
    f = ordinary
    pi.release_execution_admission()
    f.db.set_setting(merge_pre_intent_wait._key("example"), '{"source":')
    assert f.publish()["cache"]["complete"]
    def locked(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(merge_pre_intent_wait, "_wake_published", locked)
    assert f.publish()["cache"]["complete"]

def test_pre_intent_command_in_separate_native_process(ordinary, monkeypatch, tmp_path):
    f = ordinary
    pi._discard_cache("example")
    profile = tmp_path / "profiles.json"
    profile.write_text(json.dumps({"profiles": {"example": f.profile}}), encoding="utf-8")
    cmd = list(envelope(f.first)["before_intent_wait_command"])
    cmd[cmd.index("<planned_base>")] = BASE
    # The Mac build qualifies the same CLI against its packaged executable.
    executable = os.environ.get("PP_TEST_MAC_NATIVE_BINARY")
    if executable:
        assert Path(executable).is_file()
        cmd = [executable, "pipelinectl", *cmd[cmd.index("merge-checkpoint"):]]
    env = dict(os.environ, PP_DATA_DIR=str(tmp_path), PP_PIPELINE_PROFILES=str(profile),
               PP_PIPELINE_SNAPSHOT_INTERVAL="120", PP_CODEX_APP_SERVER="0", PYTHONIOENCODING="utf-8")
    result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["reason"] == "invalidated" and observed["product_mutation_authorized"] is False
    assert f.db.get_setting(merge_pre_intent_wait._key("example"))
