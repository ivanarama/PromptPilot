"""Opted-in migration needs NEW public progress, not an executor's budget prose."""
from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from promptpilot import db, pipeline_insights as pi, pipeline_item_holds as holds
from promptpilot import merge_admission_progress as progress, project_pipeline as pp
from test_merge_progress_worker import worker_replay as worker_replay, first_failure, successor
from test_merge_admission_progress import replay as replay, inspect as inspect_progress, _change


def legacy_failure(state, *, opted=True):
    first, original = first_failure(state)
    # The genuine later intent must postdate the failed occurrence, including
    # any failed/partial acceptance. No result text is used as an exemption.
    now = datetime.now(timezone.utc)
    with db._connect(immediate=True) as c:
        c.execute("UPDATE tasks SET started_at=?,completed_at=? WHERE id=?",
                  ((now - timedelta(seconds=30)).isoformat(),
                   (now - timedelta(seconds=20)).isoformat(), first.id))
    original.pop("canonical_progress")
    original.pop("verdict")
    original["at"] = time.time() - 10
    db.set_setting(holds.PREFIX + str(first.series_id), json.dumps({"2480": original}))
    config = json.loads(state["config"].read_text(encoding="utf8"))
    if opted:
        config["legacy_merge_progress_migration"] = True
    state["config"].write_text(json.dumps(config), encoding="utf8")
    snapshot = state["archive"]["snapshot"]
    snapshot["edges"][-1]["node"]["createdAt"] = (
        now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    snapshot["updatedAt"] = now.isoformat().replace("+00:00", "Z")
    return first, original


def test_real_worker_migrates_exact_legacy_hold_once_after_new_public_intent(worker_replay):
    state = worker_replay
    first, original = legacy_failure(state)
    historical = db.get_task(first.id).model_dump()
    pending = successor(state)
    assert pi.dispatch_gate(pending) is None
    task = db.get_next_runnable()
    route = pi.execution_route(task, "canonical skill", str(state["config"].parent))
    assert route["action"] == "prompt" and route["next_already_run"]
    assert route["target"]["stage"] == "integration-merge-recovery"
    consumed = json.loads(db.get_setting(holds.PREFIX + str(first.series_id)))["2480"]
    assert consumed["progress_attempt"]["task_id"] == task.id
    assert consumed["progress_attempt"]["progress"]["intent"]["created_at"]
    assert {k: v for k, v in consumed.items() if k != "progress_attempt"} == original
    assert db.get_task(first.id).model_dump() == historical
    db.mark_completed(task.id, "ИТОГ: НЕ СМОГ (#2480 — same intent, no new progress)", verdict="НЕ СМОГ")
    db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    saved = db.get_setting(holds.PREFIX + str(first.series_id))
    assert pi.dispatch_gate(successor(state))["action"] == "defer"
    assert db.get_setting(holds.PREFIX + str(first.series_id)) == saved
    assert state["next"] == 2 and state["probe"] == 2


@pytest.mark.parametrize("kind", ["default_off", "human", "unknown_verdict", "manual_metadata",
                                 "failed_partial_tests", "old_intent", "noncompleted_failure",
                                 "foreign_series", "missing_old_task"])
def test_legacy_migration_never_releases_unproved_or_human_failure(worker_replay, kind):
    state = worker_replay
    first, original = legacy_failure(state, opted=kind != "default_off")
    pending = successor(state)
    with db._connect(immediate=True) as c:
        if kind == "human":
            c.execute("UPDATE tasks SET verdict='НУЖЕН ЧЕЛОВЕК' WHERE id=?", (first.id,))
        elif kind == "unknown_verdict":
            c.execute("UPDATE tasks SET verdict=NULL WHERE id=?", (first.id,))
        elif kind == "noncompleted_failure":
            c.execute("UPDATE tasks SET status='failed' WHERE id=?", (first.id,))
        elif kind == "foreign_series":
            c.execute("UPDATE tasks SET series_id=NULL WHERE id=?", (first.id,))
    if kind == "manual_metadata":
        original["operator_hold"] = True
    if kind == "missing_old_task":
        original["task_id"] = 999999
    if kind in {"failed_partial_tests", "old_intent"}:
        # An intent created during/before the blocked attempt is NOT a new
        # accepted preparation; never rerun partial failed acceptance for it.
        state["archive"]["snapshot"]["edges"][-1]["node"]["createdAt"] = (
            datetime.now(timezone.utc) - timedelta(
                seconds=25 if kind == "failed_partial_tests" else 15)).isoformat().replace("+00:00", "Z")
    db.set_setting(holds.PREFIX + str(first.series_id), json.dumps({"2480": original}))
    saved = db.get_setting(holds.PREFIX + str(first.series_id))
    assert pi.dispatch_gate(pending)["action"] == "defer"
    assert state["next"] == 1
    assert db.get_setting(holds.PREFIX + str(first.series_id)) == saved


def test_malformed_old_completion_is_not_a_migration_witness(worker_replay):
    state = worker_replay
    first, original = legacy_failure(state)
    pending = successor(state)
    with db._connect(immediate=True) as c:
        c.execute("UPDATE tasks SET completed_at='2026-11-01T00:00:00' WHERE id=?", (first.id,))
    assert holds.legacy_blocked_attempt(original, pending) is None


def test_actual_old_failure_changed_after_preparation_blocks_before_next(worker_replay, monkeypatch):
    state = worker_replay
    first, _ = legacy_failure(state)
    assert pi.dispatch_gate(successor(state)) is None
    task = db.get_next_runnable()

    def changed(*_a, **_k):
        with db._connect(immediate=True) as c:
            c.execute("UPDATE tasks SET verdict='НУЖЕН ЧЕЛОВЕК' WHERE id=?", (first.id,))
        return True, ""

    monkeypatch.setattr(pi, "_tool_available", changed)
    result = pi.execution_route(task, "canonical skill", str(state["config"].parent))
    assert result["action"] in {"defer", "block"} and "prompt" not in result
    assert state["next"] == 1


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", []])
def test_migration_opt_in_is_typed_without_changing_absent_config(replay, invalid):
    _, path, _ = replay
    raw = json.loads(path.read_text(encoding="utf8"))
    assert "legacy_merge_progress_migration" not in pp.load_config(str(path))
    raw["legacy_merge_progress_migration"] = invalid
    path.write_text(json.dumps(raw), encoding="utf8")
    with pytest.raises(pp.PipelineError, match="must be a boolean"):
        pp.load_config(str(path))


def test_only_opted_in_public_observer_signs_intent_server_timestamp(replay, monkeypatch, capsys):
    old = inspect_progress(replay, monkeypatch, capsys)
    old_fact = pp.decode_signed_lease(old[1]["attestation"])["facts"]
    assert set(old_fact["intent"]) == progress.INTENT_FIELDS
    _, path, _ = replay
    raw = json.loads(path.read_text(encoding="utf8"))
    raw["legacy_merge_progress_migration"] = True
    path.write_text(json.dumps(raw), encoding="utf8")
    new = inspect_progress(replay, monkeypatch, capsys)
    fact = pp.decode_signed_lease(new[1]["attestation"])["facts"]
    assert fact["intent"]["created_at"] == replay[0]["snapshot"]["edges"][-1]["node"]["createdAt"]
    assert new[2].graph_calls == old[2].graph_calls  # No new API reads/cost.
    with pytest.raises(pp.PipelineError, match="paired migration opt-in"):
        config = pp.load_config(str(path))
        config.pop("legacy_merge_progress_migration")
        progress.VerifiedProgress(new[1]["attestation"], config=config,
            config_path=path, checkout=path.parent, number=2480, head=fact["head"])


@pytest.mark.parametrize("kind", ["manual", "edited", "wrong_proof", "ci_failure", "duplicate", "cleanup"])
def test_opted_in_observer_retains_every_source_and_stop_gate(replay, monkeypatch, capsys, kind):
    _, path, _ = replay
    config = json.loads(path.read_text(encoding="utf8"))
    config["legacy_merge_progress_migration"] = True
    path.write_text(json.dumps(config), encoding="utf8")
    code, value, gh, _ = inspect_progress(
        replay, monkeypatch, capsys, change=lambda gh: _change(gh, kind))
    assert code == 2 and value["action"] == "error"
    assert gh.graph_calls <= progress.MAX_READS


def test_local_exclusion_is_not_reported_as_human_decision(monkeypatch):
    head = "a" * 40
    owner = {"number": 2480, "head": head, "stage": "integration-merge-recovery"}
    health = {"state": "yellow", "findings": [{"code": "single_flight_barrier"}],
              "integration_owner": owner}
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_a, **_k: health)
    monkeypatch.setenv("PP_PIPELINE_EXCLUDED_NUMBERS", "[2480]")
    result = pp.next_merge(None, {"fallback_handoff": "legacy"})
    assert result == {"action": "wait", "number": 2480,
                      "reason": "single-flight owner has a local admission hold"}
