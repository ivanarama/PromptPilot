import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from promptpilot import fallback_handoff, herdr_exec, pipeline_insights, worker, workflows
from promptpilot import project_pipeline as pipelinectl
from promptpilot.models import TaskCreate


HEAD_A = "a" * 40
HEAD_B = "b" * 40


@pytest.fixture(autouse=True)
def _enough_worker_slots(monkeypatch):
    monkeypatch.setattr(pipeline_insights, "CONCURRENCY", 2)


def _candidate(number, head):
    return {"number": number, "head": head, "stage": "review", "review_depth": 0}


def _health(*candidates):
    values = list(candidates)
    return {
        "state": "green", "findings": [], "integration_owner": None,
        "review_candidates": values,
        "content_review_candidates": values,
        "merge_executable": [],
    }


def _set_replica_attempt_env(monkeypatch, task, *, ownership_kind="headless"):
    monkeypatch.setenv("PP_TASK_ID", str(task.id))
    monkeypatch.setenv(
        "PP_TASK_STARTED_AT", task.started_at.astimezone(timezone.utc).isoformat())
    monkeypatch.setenv("PP_PROVIDER_OWNERSHIP_KIND", ownership_kind)


def _reserve_attempt(db_module, task, *, number=10, head=HEAD_A,
                     ownership_kind="headless"):
    return db_module.reserve_pipeline_target(
        "owner/repo", "review", number, head, task.id, 300,
        task_started_at=task.started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind=ownership_kind,
    )


def _replica_series(series_id, directory, *, title="Project - REVIEW",
                    status="pending", paused=False, broken=False):
    return {
        "id": series_id, "title": title, "working_dir": str(directory),
        "ended": False, "paused": paused, "broken": broken,
        "next_task_id": series_id + 100, "next_status": status,
        "effective_recurrence": "15m", "failure_rate": 0,
        "empty_rate": 0, "avg_duration_seconds": 300,
        "temporary_recurrence": None, "temporary_empty_count": 0,
    }


def test_target_reservation_is_atomic_idempotent_and_skips_to_next(isolated_db):
    barrier = __import__("threading").Barrier(2)

    def reserve(task_id):
        barrier.wait()
        return isolated_db.reserve_pipeline_target(
            "Owner/Repo", "review", 10, HEAD_A, task_id, 300)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(reserve, (101, 102)))

    winners = [item for item in (first, second) if item is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, winner["task_id"], 300
    )["token"] == winner["token"]
    loser = 102 if winner["task_id"] == 101 else 101
    assert isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 11, HEAD_B, loser, 300
    )["number"] == 11


@pytest.mark.parametrize(("stage", "head"), [
    ("pre-review-validation", HEAD_A),
    ("review", HEAD_B),
])
def test_same_pr_cannot_be_reserved_across_stage_or_head_transition(
        isolated_db, stage, head):
    first = isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, 101, 300)

    assert first is not None
    assert isolated_db.reserve_pipeline_target(
        "owner/repo", stage, 10, head, 102, 300) is None


def test_one_task_cannot_reserve_targets_in_two_repositories(isolated_db):
    first = isolated_db.reserve_pipeline_target(
        "owner/first", "review", 10, HEAD_A, 101, 300)

    assert first is not None
    assert isolated_db.reserve_pipeline_target(
        "owner/second", "review", 20, HEAD_B, 101, 300) is None
    assert len(isolated_db.list_pipeline_target_reservations()) == 1


def test_herdr_descriptor_is_bound_exactly_before_provider_start(isolated_db):
    isolated_db.create_task(TaskCreate(prompt="bind Herdr"))
    task = isolated_db.get_next_runnable()
    reservation = _reserve_attempt(
        isolated_db, task, ownership_kind="herdr")
    assert reservation["herdr_session_state"] == "reserved"

    creating = isolated_db.begin_pipeline_target_herdr_session(reservation)
    owned = isolated_db.bind_pipeline_target_herdr_session(
        creating, "pane-1", "tab-1", "workspace-1")
    repeated = isolated_db.bind_pipeline_target_herdr_session(
        owned, "pane-1", "tab-1", "workspace-1")

    assert owned["herdr_session_state"] == "owned"
    assert (
        owned["herdr_pane_id"], owned["herdr_tab_id"],
        owned["herdr_workspace_id"],
    ) == ("pane-1", "tab-1", "workspace-1")
    assert repeated["token"] == owned["token"]
    assert isolated_db.bind_pipeline_target_herdr_session(
        owned, "renamed-pane", "tab-1", "workspace-1") is None


def test_target_reservation_ttl_and_terminal_release(isolated_db):
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    first = isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, 101, 60, now=now)
    assert isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, 102, 60,
        now=now + timedelta(seconds=59)) is None
    replacement = isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, 102, 60,
        now=now + timedelta(seconds=61))
    assert replacement["task_id"] == 102
    assert isolated_db.renew_pipeline_target_reservation(
        first, 60, now=now + timedelta(seconds=61)) is None
    isolated_db.release_pipeline_target_reservations(102)

    task = isolated_db.create_task(TaskCreate(prompt="terminal"))
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 12, "c" * 40, task.id, 300)
    isolated_db.mark_completed(task.id, "done")
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review") == []


def test_running_owner_keeps_expired_target_fenced_across_system_sleep(
        isolated_db):
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    created = isolated_db.create_task(TaskCreate(prompt="sleeping provider"))
    running = isolated_db.get_next_runnable()
    assert running.id == created.id
    reservation = isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id, 60, now=now)

    assert isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id + 1, 60,
        now=now + timedelta(hours=1)) is None
    renewed = isolated_db.renew_pipeline_target_reservation(
        reservation, 60, now=now + timedelta(hours=1))
    assert renewed is not None
    assert renewed["task_id"] == running.id

    isolated_db.mark_failed(running.id, "provider stopped")
    replacement = isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id + 1, 60,
        now=now + timedelta(hours=1, seconds=61))
    assert replacement["task_id"] == running.id + 1


def test_deleting_task_releases_target_reservation(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="delete reserved task"))
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, task.id, 300)

    assert isolated_db.delete_task(task.id) is True
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review") == []


def test_running_task_cannot_be_deleted_or_release_reservation(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="running reserved task"))
    running = isolated_db.get_next_runnable()
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id, 300)

    assert isolated_db.delete_task(running.id) is False
    assert isolated_db.get_task(running.id).status.value == "running"
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review")[0]["task_id"] == running.id


def test_running_reserved_task_cannot_be_reset_until_provider_stops(isolated_db):
    isolated_db.create_task(TaskCreate(prompt="protected reset"))
    running = isolated_db.get_next_runnable()
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id, 300)

    assert isolated_db.reset_task(running.id) is False
    assert isolated_db.get_task(running.id).status.value == "running"
    isolated_db.release_pipeline_target_reservations(running.id)
    assert isolated_db.reset_task(running.id) is True


def test_reset_before_preflight_fences_old_attempt_from_new_claim(isolated_db):
    isolated_db.create_task(TaskCreate(prompt="slow preflight"))
    old_attempt = isolated_db.get_next_runnable()
    old_started_at = old_attempt.started_at

    # Reset is still allowed before election owns a target, but the delayed
    # preflight Future must not reserve or finalize the replacement attempt.
    assert isolated_db.reset_task(old_attempt.id) is True
    new_attempt = isolated_db.get_next_runnable()
    assert new_attempt.started_at != old_started_at

    assert isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, old_attempt.id, 300,
        task_started_at=old_started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind="headless",
    ) is None
    new_reservation = _reserve_attempt(isolated_db, new_attempt)
    assert new_reservation is not None

    assert isolated_db.mark_completed(
        old_attempt.id, "stale future",
        expected_started_at=old_started_at,
    ) is False
    fresh = isolated_db.get_task(new_attempt.id)
    assert fresh.status.value == "running"
    assert fresh.started_at == new_attempt.started_at
    assert isolated_db.list_pipeline_target_reservations()[0]["token"] == \
        new_reservation["token"]


@pytest.mark.parametrize("transition", [
    "failed", "rate_limited", "deferred", "cancelled", "attempt_failed",
])
def test_every_attempt_exit_releases_target_reservation(isolated_db, transition):
    task = isolated_db.create_task(TaskCreate(prompt=f"exit {transition}"))
    running = isolated_db.get_next_runnable()
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id, 300)
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    if transition == "failed":
        isolated_db.mark_failed(running.id, "failed")
    elif transition == "rate_limited":
        isolated_db.mark_rate_limited(running.id, retry_at, "limited")
    elif transition == "deferred":
        isolated_db.defer_task(running.id, retry_at, "deferred")
    elif transition == "cancelled":
        isolated_db.mark_cancelled(running.id, "cancelled")
    else:
        assert isolated_db.fail_running_attempt(
            running.id, running.started_at, "crashed") is True

    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review") == []


def test_worker_outer_exception_path_releases_target(
        isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="attempt"))
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, task.id, 300)
    monkeypatch.setattr(
        worker, "_execute_task_inner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(worker, "_recur_after_run", lambda _task: None)
    monkeypatch.setattr(workflows, "sync_task", lambda _task_id: None)
    monkeypatch.setattr(workflows, "advance_linked_task", lambda _task_id: None)

    with pytest.raises(RuntimeError, match="boom"):
        worker.execute_task(task)

    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review") == []


def test_poll_exception_stops_provider_before_terminal_reservation_release(
        isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="owned provider"))
    task = isolated_db.get_next_runnable()
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, task.id, 300)
    events = []

    class Pipe:
        def readline(self):
            return ""

        def close(self):
            return None

    class Process:
        stdin = None
        stdout = Pipe()
        stderr = Pipe()
        returncode = None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(["provider"], timeout)

        def kill(self):
            events.append("kill")

    class Tree:
        process = Process()

        def terminate(self):
            events.append("terminate")

        def close(self):
            events.append("close")

    monkeypatch.setattr(worker.OwnedProcess, "start", lambda *_args, **_kwargs: Tree())
    monkeypatch.setattr(worker, "build_cmd", lambda *_args, **_kwargs: [sys.executable])
    monkeypatch.setattr(worker, "load_providers", lambda: {})
    monkeypatch.setattr(worker, "get_provider_env", lambda _provider: os.environ.copy())
    monkeypatch.setattr(
        isolated_db, "is_cancel_requested",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("poll failed")))
    monkeypatch.setattr(workflows, "sync_task", lambda _task_id: None)
    monkeypatch.setattr(workflows, "advance_linked_task", lambda _task_id: None)

    with pytest.raises(RuntimeError, match="poll failed") as caught:
        worker.execute_task(task)

    assert events[:2] == ["terminate", "close"]
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review")
    assert worker._fail_stuck(task, caught.value) is True
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review") == []


def test_target_heartbeat_runs_independently_of_provider_poll(
        isolated_db, monkeypatch):
    isolated_db.create_task(TaskCreate(prompt="long admission"))
    running = isolated_db.get_next_runnable()
    reservation = isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id, 300,
        task_started_at=running.started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind="headless")
    real_renew = isolated_db.renew_pipeline_target_reservation
    renewals = []

    def observed_renew(*args, **kwargs):
        renewals.append(time.monotonic())
        return real_renew(*args, **kwargs)

    monkeypatch.setattr(
        isolated_db, "renew_pipeline_target_reservation", observed_renew)
    heartbeat = worker._pipeline_target_heartbeater(
        reservation, running.id,
        task_started_at=running.started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind="headless", ttl_seconds=300, interval_seconds=0.01)
    heartbeat.start()
    time.sleep(0.045)
    heartbeat.stop()
    isolated_db.mark_failed(running.id, "done")

    assert len(renewals) >= 2


def test_quarantine_heartbeat_does_not_wait_for_cleanup_backoff(
        isolated_db, monkeypatch):
    isolated_db.create_task(TaskCreate(prompt="quarantined"))
    running = isolated_db.get_next_runnable()
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, running.id, 300)
    recoveries = {}
    error = worker.ProviderOwnershipError("uncertain", lambda: "still live")
    worker._queue_stuck_recovery(recoveries, running, "lane", error)
    item = next(iter(recoveries.values()))
    item["retry_at"] = 10_000
    renewals = []
    monkeypatch.setattr(
        worker, "_renew_quarantined_target",
        lambda task_id: renewals.append(task_id) or 1,
    )

    worker._drain_stuck_recoveries(recoveries, now=100)

    assert renewals == [running.id]
    assert recoveries
    assert worker._occupied_worker_slots({}, recoveries) == 1


def test_reservation_schema_has_versioned_migration(isolated_db):
    with sqlite3.connect(isolated_db.DB_PATH) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='pipeline_target_reservations'"
        ).fetchone()
        versions = {row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations WHERE version IN (?, ?, ?, ?)",
            (
                isolated_db.PIPELINE_TARGET_RESERVATION_SCHEMA_VERSION,
                isolated_db.PIPELINE_TARGET_ATTEMPT_SCHEMA_VERSION,
                isolated_db.PIPELINE_TARGET_OWNERSHIP_SCHEMA_VERSION,
                isolated_db.PIPELINE_TARGET_HERDR_SCHEMA_VERSION,
            ),
        )}
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(pipeline_target_reservations)")}
    assert table == ("pipeline_target_reservations",)
    assert versions == {
        isolated_db.PIPELINE_TARGET_RESERVATION_SCHEMA_VERSION,
        isolated_db.PIPELINE_TARGET_ATTEMPT_SCHEMA_VERSION,
        isolated_db.PIPELINE_TARGET_OWNERSHIP_SCHEMA_VERSION,
        isolated_db.PIPELINE_TARGET_HERDR_SCHEMA_VERSION,
    }
    assert {
        "task_started_at", "ownership_kind", "herdr_session_state",
        "herdr_pane_id", "herdr_tab_id", "herdr_workspace_id",
    } <= columns


def test_review_election_reserves_then_selects_next_candidate(
        isolated_db, monkeypatch):
    config = {
        "repository": "owner/repo", "review_completion_gate": "target-v1",
        "fallback_handoff": "target-v1", "review_lease_seconds": 300,
        "target_reservation_ttl_seconds": 300,
    }
    health = _health(_candidate(10, HEAD_A), _candidate(11, HEAD_B))
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    attempts = []
    for index in range(4):
        isolated_db.create_task(TaskCreate(prompt=f"replica {index}"))
        attempts.append(isolated_db.get_next_runnable())

    _set_replica_attempt_env(monkeypatch, attempts[0])
    first, first_reservation, first_health = pipelinectl._elect_review_candidate(
        config, health, health["review_candidates"])
    _set_replica_attempt_env(monkeypatch, attempts[1])
    second, second_reservation, second_health = pipelinectl._elect_review_candidate(
        config, health, health["review_candidates"])
    isolated_db.release_pipeline_target_reservations(attempts[0].id)
    repeated, repeated_reservation, _ = pipelinectl._elect_review_candidate(
        config, health, health["review_candidates"])
    _set_replica_attempt_env(monkeypatch, attempts[2])
    replacement_first, _, _ = pipelinectl._elect_review_candidate(
        config, health, health["review_candidates"])
    _set_replica_attempt_env(monkeypatch, attempts[3])
    exhausted, _, _ = pipelinectl._elect_review_candidate(
        config, health, health["review_candidates"])

    assert first["number"] == 10
    assert first_reservation["task_id"] == attempts[0].id
    assert first_health["review_candidates"][0]["number"] == 10
    assert second["number"] == 11
    assert second_reservation["task_id"] == attempts[1].id
    assert second_health["review_candidates"][0]["number"] == 11
    assert repeated["number"] == 11
    assert repeated_reservation["token"] == second_reservation["token"]
    assert replacement_first["number"] == 10
    assert exhausted is None


def test_expired_owned_reservation_fails_without_retargeting(
        isolated_db, monkeypatch):
    config = {
        "repository": "owner/repo", "review_completion_gate": "target-v1",
        "fallback_handoff": "target-v1", "review_lease_seconds": 300,
        "target_reservation_ttl_seconds": 300,
    }
    health = _health(_candidate(10, HEAD_A), _candidate(11, HEAD_B))
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    isolated_db.create_task(TaskCreate(prompt="owner"))
    owner = isolated_db.get_next_runnable()
    _set_replica_attempt_env(monkeypatch, owner)
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, owner.id, 300,
        task_started_at=owner.started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind="headless")
    real_reserve = isolated_db.reserve_pipeline_target
    first_call = True

    def steal_before_renew(*args, **kwargs):
        nonlocal first_call
        if first_call:
            first_call = False
            isolated_db.release_pipeline_target_reservations(owner.id)
            real_reserve("owner/repo", "review", 10, HEAD_A, 999, 300)
            return None
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(isolated_db, "reserve_pipeline_target", steal_before_renew)

    with pytest.raises(
            pipelinectl.PipelineError, match="reservation was lost"):
        pipelinectl._elect_review_candidate(
            config, health, health["review_candidates"])
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo")[0]["task_id"] == 999


def test_repeated_next_cannot_retarget_when_owned_target_left_queue(
        isolated_db, monkeypatch):
    config = {
        "repository": "owner/repo", "review_completion_gate": "target-v1",
        "fallback_handoff": "target-v1", "review_lease_seconds": 300,
        "target_reservation_ttl_seconds": 300,
    }
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    isolated_db.create_task(TaskCreate(prompt="owner"))
    owner = isolated_db.get_next_runnable()
    _set_replica_attempt_env(monkeypatch, owner)
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, owner.id, 300,
        task_started_at=owner.started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind="headless")
    changed = _health(_candidate(11, HEAD_B))

    with pytest.raises(pipelinectl.PipelineError, match="no longer eligible"):
        pipelinectl._elect_review_candidate(
            config, changed, changed["review_candidates"])

    owned = isolated_db.list_pipeline_target_reservations(
        repository="owner/repo")
    assert [(item["number"], item["head"]) for item in owned] == [(10, HEAD_A)]


def test_replicated_lease_without_reservation_is_rejected(monkeypatch):
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    monkeypatch.setenv("PP_TASK_ID", "101")

    with pytest.raises(pipelinectl.PipelineError, match="no target reservation"):
        pipelinectl._validate_lease_reservation({
            "stage": "review", "repository": "owner/repo", "number": 10,
            "head": HEAD_A, "pipeline_replicas": 2,
        }, {"repository": "owner/repo"})


def test_fallback_gate_requires_live_exact_task_reservation(
        isolated_db, monkeypatch, tmp_path):
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(tmp_path / "lease.key"))
    config = {
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["health"], "fallback_handoff": "target-v1",
        "review_completion_gate": "target-v1", "review_lease_seconds": 300,
        "target_reservation_ttl_seconds": 300,
    }
    target = _candidate(10, HEAD_A)
    health = _health(target)
    isolated_db.create_task(TaskCreate(prompt="gate owner"))
    owner = isolated_db.get_next_runnable()
    _set_replica_attempt_env(monkeypatch, owner)
    reservation = isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, owner.id, 300,
        task_started_at=owner.started_at.astimezone(timezone.utc).isoformat(),
        ownership_kind="headless")
    envelope = fallback_handoff.create(
        config, health, "review", target, "full review",
        target_reservation=reservation)
    monkeypatch.setattr(pipelinectl, "run_health", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(pipelinectl, "ensure_identity", lambda *_args: None)

    validated = fallback_handoff.gate(
        object(), config, "review", envelope["handoff"]["lease"])
    assert validated["action"] == "validated"

    isolated_db.release_pipeline_target_reservations(owner.id)
    with pytest.raises(pipelinectl.PipelineError, match="expired or was released"):
        fallback_handoff.gate(
            object(), config, "review", envelope["handoff"]["lease"])


def test_replica_working_dirs_are_required_existing_and_distinct(tmp_path):
    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    first.mkdir()
    second.mkdir()
    queue = {"series_contains": "Project - REVIEW", "replicas": 2}
    valid = pipeline_insights._queue_replica_status(queue, [
        _replica_series(1, first), _replica_series(2, second),
    ])
    duplicate = pipeline_insights._queue_replica_status(queue, [
        _replica_series(1, first), _replica_series(2, first),
    ])
    missing = pipeline_insights._queue_replica_status(queue, [
        _replica_series(1, first), _replica_series(2, tmp_path / "missing"),
    ])

    assert valid["valid"] is True
    assert duplicate["valid"] is False
    assert any("один working_dir" in issue for issue in duplicate["issues"])
    assert missing["valid"] is False
    assert any("не существует" in issue for issue in missing["issues"])


def test_replica_working_dirs_use_filesystem_identity_not_path_spelling(
        monkeypatch, tmp_path):
    first = tmp_path / "Review"
    second = tmp_path / "review-alias"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        pipeline_insights, "_local_directory_identity",
        lambda _path: (7, 42),
    )

    status = pipeline_insights._queue_replica_status(
        {"series_contains": "Project - REVIEW", "replicas": 2},
        [_replica_series(1, first), _replica_series(2, second)],
    )

    assert status["valid"] is False
    assert any("один working_dir" in issue for issue in status["issues"])


@pytest.mark.parametrize(("field", "value", "message"), [
    ("worktree", True, "worktree"),
    ("detached", True, "detached"),
    ("herdr_target", "shared-user-tab", "herdr_target"),
])
def test_replica_status_rejects_unowned_active_occurrence_lifetime(
        tmp_path, field, value, message):
    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    first.mkdir()
    second.mkdir()
    replicas = [_replica_series(1, first), _replica_series(2, second)]
    replicas[0][field] = value
    queue = {"series_contains": "Project - REVIEW", "replicas": 2}

    status = pipeline_insights._queue_replica_status(queue, replicas)
    projection = pipeline_insights._queue_replica_projection(
        queue, replicas, capacity=1)

    assert status["valid"] is False
    assert any(message in issue for issue in status["issues"])
    assert projection["parallel_capacity"] == 0


def test_series_projection_exposes_active_occurrence_lifetime_flags(
        isolated_db, tmp_path):
    created = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW unsafe", working_dir=str(tmp_path),
        recurrence="15m", worktree=True, detached=True,
        herdr_target="shared-user-tab",
    ))

    series = isolated_db.get_series(created.series_id)

    assert series["worktree"] is True
    assert series["detached"] is True
    assert series["herdr_target"] == "shared-user-tab"


def test_replica_projection_keeps_per_run_capacity_separate(tmp_path):
    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    first.mkdir()
    second.mkdir()
    queue = {"series_contains": "Project - REVIEW", "replicas": 2}
    projection = pipeline_insights._queue_replica_projection(queue, [
        _replica_series(1, first), _replica_series(2, second),
    ], capacity=2)

    assert projection["replica_count"] == 2
    assert projection["replicas_active"] == 2
    assert projection["parallel_capacity"] == 4
    assert len(projection["series_replicas"]) == 2


def test_replica_projection_has_no_capacity_without_active_series(tmp_path):
    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    first.mkdir()
    second.mkdir()
    queue = {"series_contains": "Project - REVIEW", "replicas": 2}
    projection = pipeline_insights._queue_replica_projection(queue, [
        _replica_series(1, first, paused=True),
        _replica_series(2, second, broken=True),
    ], capacity=2)

    recommendation = pipeline_insights._replica_recommendation(
        queue, 8, projection, 8)
    assert projection["replicas_active"] == 0
    assert projection["parallel_capacity"] == 0
    assert pipeline_insights._replica_runs_needed(8, projection) is None
    assert recommendation["eta_hours"] is None
    assert recommendation["throughput_per_hour"] == 0


@pytest.mark.parametrize("series", [
    [],
    [{"paused": True}],
    [{"broken": True}],
])
def test_legacy_single_series_has_zero_capacity_when_not_active(
        tmp_path, series):
    values = []
    for index, overrides in enumerate(series, start=1):
        item = _replica_series(index, tmp_path, **{
            key: value for key, value in overrides.items()
            if key in {"paused", "broken"}
        })
        values.append(item)
    queue = {"series_contains": "Project - REVIEW"}

    projection = pipeline_insights._queue_replica_projection(
        queue, values, capacity=1)
    recommendation = pipeline_insights._replica_recommendation(
        queue, 5, projection, 8)

    assert projection["replica_count"] == 1
    assert projection["replicas_active"] == 0
    assert projection["parallel_capacity"] == 0
    assert pipeline_insights._replica_runs_needed(5, projection) is None
    assert recommendation["eta_hours"] is None
    assert "нет активных" in recommendation["recommendation"]


def test_invalid_replica_set_has_zero_capacity_and_is_hard_bottleneck(tmp_path):
    first = tmp_path / "review-1"
    first.mkdir()
    queue = {"series_contains": "Project - REVIEW", "replicas": 2}
    projection = pipeline_insights._queue_replica_projection(
        queue, [_replica_series(1, first)], capacity=2)
    recommendation = pipeline_insights._replica_recommendation(
        queue, 8, projection, 8)
    stalled = {"backlog": 8, "eta_hours": None, "runs_needed": None}
    healthy = {"backlog": 1, "eta_hours": 1, "runs_needed": 1}

    assert projection["replica_status"]["valid"] is False
    assert projection["parallel_capacity"] == 0
    assert "конфигурация" in recommendation["recommendation"]
    assert pipeline_insights._bottleneck_rank(stalled) == float("inf")
    assert max([healthy, stalled], key=pipeline_insights._bottleneck_rank) is stalled


def test_cached_dashboard_health_is_red_for_invalid_replica_set(
        isolated_db, monkeypatch, tmp_path):
    first = tmp_path / "review-1"
    first.mkdir()
    profile = {
        "target_clear_hours": 8,
        "queues": [{
            "id": "review", "title": "Review",
            "series_contains": "Project - REVIEW", "replicas": 2,
        }],
    }
    saved = {
        "profile_id": "p", "backlog_total": 4,
        "queues": [{"id": "review", "title": "Review", "backlog": 4}],
        "history": {"5h": {
            "complete": True, "runs": {}, "backlog_delta": -1,
            "exited": 1, "churn_items": 0,
        }},
    }
    series = [_replica_series(1, first)]
    monkeypatch.setattr(
        pipeline_insights, "_pipeline_runtime",
        lambda *_args, **_kwargs: {"required": True, "state": "online", "stalled": []},
    )

    result = pipeline_insights._refresh_local_state(
        saved, profile, series, source="durable", generated_at=time.time(),
        entry_epoch=0, entry_revision=1, current_epoch=0)

    assert result["health"]["state"] == "red"
    assert result["health"]["label"] == "невалидная конфигурация реплик"
    assert "review" in result["health"]["reason"]


def test_replica_set_is_invalid_when_worker_has_too_few_slots(
        monkeypatch, tmp_path):
    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(pipeline_insights, "CONCURRENCY", 1)
    queue = {"series_contains": "Project - REVIEW", "replicas": 2}

    projection = pipeline_insights._queue_replica_projection(queue, [
        _replica_series(1, first), _replica_series(2, second),
    ], capacity=1)

    assert projection["parallel_capacity"] == 0
    assert any("PP_CONCURRENCY" in issue
               for issue in projection["replica_status"]["issues"])


def test_legacy_queue_still_uses_only_first_matching_series(tmp_path):
    series = [
        _replica_series(1, tmp_path, title="Project - REVIEW first"),
        _replica_series(2, tmp_path, title="Project - REVIEW accidental duplicate"),
    ]

    assert [item["id"] for item in pipeline_insights._series_replicas_for_queue(
        {"series_contains": "Project - REVIEW"}, series)] == [1]


def test_group_wake_latches_successors_for_running_replicas(
        isolated_db, monkeypatch, tmp_path):
    first = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 1", working_dir=str(tmp_path), recurrence="4h"))
    second = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 2", working_dir=str(tmp_path), recurrence="4h"))
    assert isolated_db.get_next_runnable().id == first.id
    assert isolated_db.get_next_runnable().id == second.id
    monkeypatch.setattr(
        isolated_db, "_pipeline_cache_guard_matches", lambda _conn, _guard: True)

    woken = isolated_db.wake_series_group_once(
        [first.series_id, second.series_id], "wake:review", "snapshot-a",
        cache_guard={})

    assert woken == [first.series_id, second.series_id]
    assert isolated_db.get_setting(
        f"pipeline_series_wake_intent:v1:{first.series_id}") == "1"
    assert isolated_db.get_setting(
        f"pipeline_series_wake_intent:v1:{second.series_id}") == "1"
    assert isolated_db.wake_series_group_once(
        [first.series_id, second.series_id], "wake:review", "snapshot-a",
        cache_guard={}) == []


def test_group_wake_recreates_replica_in_terminal_recurrence_gap(
        isolated_db, monkeypatch, tmp_path):
    first = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 1", working_dir=str(tmp_path), recurrence="4h"))
    second = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 2", working_dir=str(tmp_path), recurrence="4h"))
    running = isolated_db.get_next_runnable()
    assert running.id == first.id
    assert isolated_db.mark_completed(first.id, "done")
    monkeypatch.setattr(
        isolated_db, "_pipeline_cache_guard_matches", lambda _conn, _guard: True)

    woken = isolated_db.wake_series_group_once(
        [first.series_id, second.series_id], "wake:review", "snapshot-gap",
        cache_guard={})

    assert woken == [first.series_id, second.series_id]
    first_live = [
        task for task in isolated_db.list_tasks()
        if task.series_id == first.series_id and task.status.value == "pending"
    ]
    assert len(first_live) == 1
    assert first_live[0].scheduled_at <= datetime.now(timezone.utc)
    assert isolated_db.wake_series_group_once(
        [first.series_id, second.series_id], "wake:review", "snapshot-gap",
        cache_guard={}) == []


def test_group_wake_does_not_restart_cancelled_replica(
        isolated_db, monkeypatch, tmp_path):
    cancelled = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 1", working_dir=str(tmp_path), recurrence="4h"))
    pending = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 2", working_dir=str(tmp_path), recurrence="4h"))
    assert isolated_db.cancel_task(cancelled.id)
    monkeypatch.setattr(
        isolated_db, "_pipeline_cache_guard_matches", lambda _conn, _guard: True)

    woken = isolated_db.wake_series_group_once(
        [cancelled.series_id, pending.series_id], "wake:review", "snapshot-cancel",
        cache_guard={})

    assert woken == [pending.series_id]
    cancelled_tasks = [
        task for task in isolated_db.list_tasks()
        if task.series_id == cancelled.series_id
    ]
    assert len(cancelled_tasks) == 1
    assert cancelled_tasks[0].id == cancelled.id
    assert cancelled_tasks[0].status.value == "cancelled"
    assert isolated_db.get_setting("wake:review") == "snapshot-cancel"


def test_adaptive_cadence_updates_every_replica(isolated_db, tmp_path):
    first = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 1", working_dir=str(tmp_path), recurrence="30m"))
    second = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 2", working_dir=str(tmp_path), recurrence="30m"))
    matching = [isolated_db.get_series(first.series_id),
                isolated_db.get_series(second.series_id)]
    queue = {"adaptive_cadence": {
        "idle_recurrence": "30m", "busy_recurrence": "10m",
        "backlog_above": 0,
    }}

    status = pipeline_insights._reconcile_adaptive_cadence(queue, matching, 5)

    assert status["series_count"] == 2
    assert status["effective_recurrence"] == "10m"
    assert isolated_db.get_series(first.series_id)["effective_recurrence"] == "10m"
    assert isolated_db.get_series(second.series_id)["effective_recurrence"] == "10m"


def test_run_now_and_event_wake_address_every_replica(monkeypatch, tmp_path):
    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    first.mkdir()
    second.mkdir()
    queue = {
        "id": "review", "series_contains": "Project - REVIEW", "replicas": 2,
        "wake_when": {"field": "review_candidates"},
    }
    profile = {
        "repository": "owner/repo",
        "priority_control": {"trusted_account": "owner"},
        "queues": [queue],
    }
    series = [_replica_series(1, first), _replica_series(2, second)]
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"p": profile})
    monkeypatch.setattr(
        pipeline_insights, "_gh_api_json",
        lambda args, _input=None: {"login": "owner"} if args == ["user"] else
        {"state": "open", "pull_request": {"url": "x"}, "labels": []})
    run_now = []
    monkeypatch.setattr(
        pipeline_insights.db, "series_action",
        lambda series_id, action: run_now.append((series_id, action)) or True)

    result = pipeline_insights.set_item_priority(
        "p", "review", "pr", 10, "auto", True, series)

    assert result["series_woken_ids"] == [1, 2]
    assert run_now == [(1, "run_now"), (2, "run_now")]

    cache = {
        "complete": True, "stale": False,
        "token": {
            "profile_hash": pipeline_insights._profile_fingerprint(profile),
            "epoch": 0, "revision": 1, "generated_at": time.time(),
        },
    }
    group_wakes = []
    monkeypatch.setattr(pipeline_insights.db, "is_paused", lambda: False)
    monkeypatch.setattr(
        pipeline_insights.db, "wake_series_group_once",
        lambda ids, key, fingerprint, **_kwargs:
        group_wakes.append((ids, key, fingerprint)) or ids)

    assert pipeline_insights._wake_ready_queues(
        "p", profile, {"cache": cache, "diagnostics": {
            "review_candidates": [{"number": 10}],
        }}, series) == ["review"]
    assert group_wakes[0][0] == [1, 2]


def test_execution_route_passes_replica_identity_to_preflight(
        isolated_db, monkeypatch, tmp_path):
    first_dir = tmp_path / "review-1"
    second_dir = tmp_path / "review-2"
    first_dir.mkdir()
    second_dir.mkdir()
    first = isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 1", working_dir=str(first_dir), recurrence="15m"))
    isolated_db.create_task(TaskCreate(
        prompt="Project - REVIEW 2", working_dir=str(second_dir), recurrence="15m"))
    task = isolated_db.get_next_runnable()
    assert task.id == first.id
    queue = {
        "id": "review", "series_contains": "Project - REVIEW", "replicas": 2,
        "execution": {"mode": "auto", "command": ["pipelinectl", "next", "review"]},
    }
    monkeypatch.setattr(
        pipeline_insights, "_matching_queue", lambda _task: ("p", {}, queue))
    monkeypatch.setattr(pipeline_insights, "_tool_available", lambda *_args: (True, ""))
    monkeypatch.setattr(pipeline_insights, "load_providers", lambda: {})
    captured = {}

    def preflight(_execution, _command, _working_dir, *, env_extra=None):
        captured.update(env_extra or {})
        return {"action": "wait", "reason": "all targets reserved"}

    monkeypatch.setattr(pipeline_insights, "_tool_preflight", preflight)
    route = pipeline_insights.execution_route(task, "skill", str(first_dir))

    assert route["action"] == "complete_empty"
    assert captured == {
        "PP_TASK_ID": str(task.id),
        "PP_TASK_STARTED_AT": task.started_at.astimezone(timezone.utc).isoformat(),
        "PP_PIPELINE_REPLICAS": "2",
        "PP_PROVIDER_OWNERSHIP_KIND": "headless",
        "PP_DATA_DIR": str(isolated_db.DB_PATH.resolve().parent),
        "PP_PIPELINE_LEASE_KEY_FILE": str(
            (isolated_db.DB_PATH.resolve().parent / "pipeline-lease.key").resolve()),
    }


def test_worker_propagates_replica_mode_into_herdr_provider(monkeypatch, tmp_path):
    data_dir = tmp_path / "scheduler-data"
    started_at = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    started_iso = started_at.isoformat()
    task = SimpleNamespace(
        id=41, series_id=7, prompt="Project - REVIEW",
        provider="test-provider", working_dir=None, machine=None,
        keep_pane=False, started_at=started_at,
    )
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(
        pipeline_insights, "execution_route",
        lambda *_args, **_kwargs: {
            "action": "prompt", "mode": "tool", "prompt": "run",
            "profile_id": "project", "queue_id": "review",
            "pipeline_replicas": 2,
            "pipeline_data_dir": str(data_dir),
            "pipeline_lease_key_file": str(data_dir / "pipeline-lease.key"),
            "pipeline_task_started_at": started_iso,
            "pipeline_provider_ownership_kind": "herdr",
            "pipeline_target_reservation": {
                "task_id": 41, "task_started_at": started_iso,
                "ownership_kind": "herdr", "token": "1" * 32,
            },
        },
    )
    heartbeat_calls = []

    class Heartbeat:
        reservation = {"token": "1" * 32}

        def start(self):
            heartbeat_calls.append("start")
            return self

        def __call__(self, *, force=False):
            heartbeat_calls.append(("touch", force))

    monkeypatch.setattr(
        worker, "_pipeline_target_heartbeater",
        lambda *_args, **_kwargs: Heartbeat(),
    )
    monkeypatch.setattr(
        worker, "load_providers",
        lambda: {"test-provider": {"executor": "herdr", "env": {"KEEP": "1"}}},
    )
    captured = {}

    def provider(_task, provider_cfg, **kwargs):
        captured.update(provider_cfg["env"])
        captured["strict_owned_session"] = provider_cfg["strict_owned_session"]
        kwargs["target_heartbeat"]()

    monkeypatch.setattr(worker, "_execute_herdr_task", provider)

    worker._execute_task_body(task)

    assert captured == {
        "KEEP": "1", "PP_PIPELINE_REPLICAS": "2",
        "PP_TASK_STARTED_AT": started_iso,
        "PP_PROVIDER_OWNERSHIP_KIND": "herdr",
        "PP_PIPELINE_TARGET_TOKEN": "1" * 32,
        "PP_DATA_DIR": os.path.realpath(str(data_dir)),
        "PP_PIPELINE_LEASE_KEY_FILE": os.path.realpath(
            str(data_dir / "pipeline-lease.key")),
        "strict_owned_session": True,
    }
    assert heartbeat_calls == ["start", ("touch", False)]


@pytest.mark.parametrize("unsafe_field, unsafe_value", [
    ("detached", True), ("herdr_target", "user-session"),
])
def test_execution_route_blocks_unowned_replica_lifetimes(
        monkeypatch, tmp_path, unsafe_field, unsafe_value):
    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    first.mkdir()
    second.mkdir()
    queue = {
        "id": "review", "series_contains": "Project - REVIEW", "replicas": 2,
        "execution": {"mode": "auto", "stage": "review"},
    }
    task = SimpleNamespace(
        id=101, series_id=1, machine=None, worktree=False, detached=False,
        herdr_target=None,
    )
    setattr(task, unsafe_field, unsafe_value)
    monkeypatch.setattr(
        pipeline_insights, "_matching_queue", lambda _task: ("p", {}, queue))
    monkeypatch.setattr(
        pipeline_insights.db, "list_series", lambda: [
            _replica_series(1, first), _replica_series(2, second),
        ])

    route = pipeline_insights.execution_route(task, "skill", str(first))

    assert route["action"] == "block"
    assert unsafe_field in route["reason"]


def test_strict_herdr_wait_error_closes_owned_session(monkeypatch):
    calls = []

    def fake_run(args, host=None, timeout=None):
        calls.append(args)
        if args[:2] == ["tab", "create"]:
            return 0, {"result": {
                "root_pane": {"pane_id": "pane-1"},
                "tab": {"tab_id": "tab-1"},
            }}, ""
        if args[:2] == ["agent", "start"]:
            return 0, {"result": {"agent": {"agent_status": "idle"}}}, ""
        if args[:2] == ["agent", "prompt"]:
            return 1, {"error": {"code": "timeout"}}, "still working"
        if args[:2] == ["agent", "wait"]:
            return 1, {"error": {"code": "rpc_error"}}, "rpc failed"
        if args[:2] == ["tab", "close"]:
            return 0, {}, ""
        if args[:2] == ["tab", "list"]:
            return 0, {"result": {"tabs": []}}, ""
        if args[:2] == ["agent", "list"]:
            return 0, {"result": {"agents": []}}, ""
        raise AssertionError(args)

    task = SimpleNamespace(
        id=1, herdr_target=None, model=None, effort=None, session_id=None,
        skip_permissions=False, detached=False, working_dir=".", worktree=False,
    )
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda _host: None)
    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", lambda *_args: None)
    monkeypatch.setattr(herdr_exec, "_run", fake_run)
    monkeypatch.setattr(herdr_exec, "guard_enabled", lambda *_args: False)

    outcome = herdr_exec.run_in_herdr(
        task, {"kind": "codex", "strict_owned_session": True},
        prompt_override="review",
    )

    assert outcome["ok"] is False
    assert outcome.get("ownership_uncertain") is not True
    assert any(args[:2] == ["tab", "close"] for args in calls)


def test_strict_herdr_stale_cleanup_failure_is_ownership_uncertain(monkeypatch):
    task = SimpleNamespace(
        id=1, herdr_target=None, model=None, effort=None, session_id=None,
        skip_permissions=False, detached=False, working_dir=".", worktree=False,
    )
    attempts = []
    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda _host: None)

    def fail_stale(*_args):
        attempts.append("close")
        raise herdr_exec.HerdrError("stale provider still live")

    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", fail_stale)

    outcome = herdr_exec.run_in_herdr(
        task, {"kind": "codex", "strict_owned_session": True},
        prompt_override="review",
    )

    assert outcome["ownership_uncertain"] is True
    assert callable(outcome["_ownership_cleanup"])
    assert "stale provider still live" in outcome["error"]
    assert outcome["_ownership_cleanup"]()
    assert len(attempts) == 2


def test_strict_herdr_cleanup_exception_is_ownership_uncertain(monkeypatch):
    task = SimpleNamespace(
        id=1, herdr_target=None, model=None, effort=None, session_id=None,
        skip_permissions=False, detached=False, working_dir=".", worktree=False,
    )

    def fake_run(args, host=None, timeout=None):
        if args[:2] == ["tab", "create"]:
            return 0, {"result": {
                "root_pane": {"pane_id": "pane-1"},
                "tab": {"tab_id": "tab-1"},
            }}, ""
        if args[:2] == ["agent", "start"]:
            return 1, {"error": {"code": "start_failed"}}, "start failed"
        raise AssertionError(args)

    monkeypatch.setattr(herdr_exec, "_ensure_server", lambda _host: None)
    monkeypatch.setattr(herdr_exec, "_close_stale_tabs", lambda *_args: None)
    monkeypatch.setattr(herdr_exec, "_run", fake_run)
    monkeypatch.setattr(
        herdr_exec, "_close_owned_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("close exploded")),
    )

    outcome = herdr_exec.run_in_herdr(
        task, {"kind": "codex", "strict_owned_session": True},
        prompt_override="review",
    )

    assert outcome["ownership_uncertain"] is True
    assert callable(outcome["_ownership_cleanup"])
    assert "close exploded" in outcome["error"]


def test_uncertain_provider_cleanup_keeps_reservation_until_confirmed(
        isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="uncertain provider"))
    task = isolated_db.get_next_runnable()
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, task.id, 300)
    attempts = iter(["still live", ""])
    error = worker.ProviderOwnershipError(
        "herdr close not confirmed", lambda: next(attempts))

    assert worker._fail_stuck(task, error) is False
    assert isolated_db.get_task(task.id).status.value == "running"
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review")

    assert worker._fail_stuck(task, error) is True
    assert isolated_db.get_task(task.id).status.value == "failed"
    assert isolated_db.list_pipeline_target_reservations(
        repository="owner/repo", stage="review") == []


@pytest.mark.parametrize(("terminal_intent", "expected_status"), [
    ({"ok": True, "rate_limited": False, "error": "", "output": "done"},
     "completed"),
    ({"ok": False, "rate_limited": False, "cancelled": True,
      "cancel_note": "cancelled", "error": ""}, "cancelled"),
    ({"ok": False, "rate_limited": True, "retry_reason": "rate_limit",
      "error": "429"}, "rate_limited"),
    ({"ok": False, "rate_limited": False, "env_failure": "HTTP 503",
      "error": "503"}, "rate_limited"),
])
def test_terminal_intent_survives_two_close_failures_until_quarantine_cleanup(
        isolated_db, monkeypatch, terminal_intent, expected_status):
    isolated_db.create_task(TaskCreate(prompt="strict herdr outcome"))
    task = isolated_db.get_next_runnable()
    reservation = _reserve_attempt(
        isolated_db, task, ownership_kind="herdr")
    reservation = isolated_db.begin_pipeline_target_herdr_session(reservation)
    cleanup_attempts = ["immediate close failed", "strict finally failed"]

    def cleanup():
        cleanup_attempts.append("quarantine cleanup succeeded")
        return ""

    monkeypatch.setattr(
        herdr_exec, "run_in_herdr",
        lambda *_args, **_kwargs: {
            "ok": False, "rate_limited": False,
            "error": "owned session close failed twice",
            "ownership_uncertain": True,
            "_terminal_intent": dict(terminal_intent),
            "_ownership_cleanup": cleanup,
        },
    )

    with pytest.raises(worker.ProviderOwnershipError) as caught:
        worker._execute_herdr_task(task, {"strict_owned_session": True})

    assert isolated_db.get_task(task.id).status.value == "running"
    assert isolated_db.list_pipeline_target_reservations()[0]["token"] == \
        reservation["token"]
    assert worker._fail_stuck(task, caught.value) is True
    assert cleanup_attempts == [
        "immediate close failed", "strict finally failed",
        "quarantine cleanup succeeded",
    ]
    assert isolated_db.get_task(task.id).status.value == expected_status
    assert isolated_db.list_pipeline_target_reservations() == []


def test_terminal_continuation_cas_false_keeps_exact_attempt_quarantined(
        isolated_db):
    isolated_db.create_task(TaskCreate(prompt="delayed CAS"))
    task = isolated_db.get_next_runnable()
    reservation = _reserve_attempt(
        isolated_db, task, ownership_kind="herdr")
    allow_commit = False

    def commit_terminal():
        if not allow_commit:
            return False
        return isolated_db.mark_completed(
            task.id, "done", expected_started_at=task.started_at)

    error = worker.ProviderOwnershipError(
        "cleanup settled", lambda: "", on_cleaned=commit_terminal)

    assert worker._fail_stuck(task, error) is False
    assert isolated_db.get_task(task.id).status.value == "running"
    assert isolated_db.list_pipeline_target_reservations()[0]["token"] == \
        reservation["token"]

    allow_commit = True
    assert worker._fail_stuck(task, error) is True
    assert isolated_db.get_task(task.id).status.value == "completed"
    assert isolated_db.list_pipeline_target_reservations() == []


def test_headless_success_survives_first_terminal_database_failure(isolated_db):
    isolated_db.create_task(TaskCreate(prompt="headless success"))
    task = isolated_db.get_next_runnable()
    _reserve_attempt(isolated_db, task, ownership_kind="headless")
    attempts = 0

    def commit_success():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return isolated_db.mark_completed(
            task.id, "done", expected_started_at=task.started_at)

    with pytest.raises(worker.ProviderOwnershipError) as caught:
        worker._commit_terminal_or_defer(
            task, "headless successful outcome", commit_success)

    assert isolated_db.get_task(task.id).status.value == "running"
    assert worker._fail_stuck(task, caught.value) is True
    assert isolated_db.get_task(task.id).status.value == "completed"
    assert attempts == 2


def test_headless_cancel_survives_transient_tree_cleanup_failure(isolated_db):
    isolated_db.create_task(TaskCreate(prompt="headless cancel"))
    task = isolated_db.get_next_runnable()
    _reserve_attempt(isolated_db, task, ownership_kind="headless")

    class Tree:
        def __init__(self):
            self.close_calls = 0
            self.process = SimpleNamespace(kill=lambda: None)

        def terminate(self):
            return None

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("temporary close failure")

    tree = Tree()
    worker._register_provider_tree(task.id, tree)
    error = worker.ProviderOwnershipError(
        "cancel cleanup pending", lambda: "",
        on_cleaned=lambda: isolated_db.mark_cancelled(
            task.id, "cancelled", expected_started_at=task.started_at),
    )

    assert worker._fail_stuck(task, error) is False
    assert isolated_db.get_task(task.id).status.value == "running"
    assert worker._fail_stuck(task, error) is True
    assert isolated_db.get_task(task.id).status.value == "cancelled"


def test_restart_cleans_reserved_provider_before_requeue_and_release(
        isolated_db, monkeypatch):
    isolated_db.create_task(TaskCreate(prompt="restart cleanup"))
    task = isolated_db.get_next_runnable()
    _reserve_attempt(isolated_db, task, ownership_kind="herdr")
    events = []

    def cleanup_factory(recovered_task, reservation):
        assert recovered_task.id == task.id
        assert reservation["ownership_kind"] == "herdr"

        def cleanup():
            assert isolated_db.get_task(task.id).status.value == "running"
            assert isolated_db.task_has_live_pipeline_target_reservation(task.id)
            events.append("provider stopped")
            return ""

        return cleanup

    monkeypatch.setattr(worker, "_recovered_provider_cleanup", cleanup_factory)

    keep, quarantines = worker._reconcile_reserved_running_tasks(set())

    assert events == ["provider stopped"]
    assert keep == set()
    assert quarantines == []
    assert isolated_db.get_task(task.id).status.value == "pending"
    assert isolated_db.list_pipeline_target_reservations() == []


def test_restart_before_herdr_creation_requeues_without_false_quarantine(
        isolated_db):
    isolated_db.create_task(TaskCreate(prompt="crash before Herdr begin"))
    task = isolated_db.get_next_runnable()
    reservation = _reserve_attempt(
        isolated_db, task, ownership_kind="herdr")
    assert reservation["herdr_session_state"] == "reserved"

    keep, quarantines = worker._reconcile_reserved_running_tasks(set())

    assert keep == set()
    assert quarantines == []
    assert isolated_db.get_task(task.id).status.value == "pending"
    assert isolated_db.list_pipeline_target_reservations() == []


def test_restart_preserves_cancel_requested_before_worker_crash(isolated_db):
    created = isolated_db.create_task(TaskCreate(
        prompt="cancel before crash", recurrence="15m"))
    task = isolated_db.get_next_runnable()
    _reserve_attempt(isolated_db, task, ownership_kind="herdr")
    wake_key = f"pipeline_series_wake_intent:v1:{created.series_id}"
    isolated_db.set_setting(wake_key, "1")
    assert isolated_db.request_cancel(task.id) is True

    keep, quarantines = worker._reconcile_reserved_running_tasks(set())

    assert keep == set()
    assert quarantines == []
    assert isolated_db.get_task(task.id).status.value == "cancelled"
    assert isolated_db.is_cancel_requested(task.id) is False
    assert isolated_db.get_setting(wake_key) is None
    assert isolated_db.list_pipeline_target_reservations() == []


def test_restart_closes_exact_herdr_ids_even_after_labels_are_renamed(
        isolated_db, monkeypatch):
    isolated_db.create_task(TaskCreate(prompt="renamed Herdr"))
    task = isolated_db.get_next_runnable()
    reservation = _reserve_attempt(
        isolated_db, task, ownership_kind="herdr")
    reservation = isolated_db.begin_pipeline_target_herdr_session(reservation)
    reservation = isolated_db.bind_pipeline_target_herdr_session(
        reservation, "pane-stable", "tab-stable", "workspace-stable")
    events = []
    monkeypatch.setattr(
        herdr_exec, "_ensure_server", lambda host: events.append(("ensure", host)))
    monkeypatch.setattr(
        herdr_exec, "_close_owned_session",
        lambda name, args, host, **kwargs:
        events.append((name, args, host, kwargs)) or "",
    )
    monkeypatch.setattr(
        herdr_exec, "_close_stale_tabs",
        lambda *_args: pytest.fail("mutable label sweep was used"),
    )

    cleanup = worker._recovered_provider_cleanup(task, reservation)

    assert cleanup() == ""
    assert events == [
        ("ensure", None),
        (f"recovered task #{task.id}",
         ["workspace", "close", "workspace-stable"],
         None, {"pane_id": "pane-stable"}),
    ]


def test_restart_cleanup_failure_quarantines_then_requeues_without_failure(
        isolated_db, monkeypatch):
    isolated_db.create_task(TaskCreate(prompt="restart retry"))
    task = isolated_db.get_next_runnable()
    reservation = _reserve_attempt(
        isolated_db, task, ownership_kind="headless")
    outcomes = iter(["provider still alive", ""])
    projections = []
    monkeypatch.setattr(
        worker, "_recovered_provider_cleanup",
        lambda _task, _reservation: lambda: next(outcomes),
    )
    monkeypatch.setattr(
        workflows, "sync_task",
        lambda task_id: projections.append(("sync", task_id)),
    )
    monkeypatch.setattr(
        workflows, "advance_linked_task",
        lambda task_id: projections.append(("advance", task_id)),
    )

    keep, quarantines = worker._reconcile_reserved_running_tasks(set())

    assert keep == {task.id}
    assert len(quarantines) == 1
    assert isolated_db.get_task(task.id).status.value == "running"
    assert isolated_db.list_pipeline_target_reservations()[0]["token"] == \
        reservation["token"]

    recovered_task, error = quarantines[0]
    assert worker._fail_stuck(recovered_task, error) is True
    assert isolated_db.get_task(task.id).status.value == "pending"
    assert isolated_db.list_pipeline_target_reservations() == []
    assert projections == [("sync", task.id), ("advance", task.id)]


@pytest.mark.parametrize("ownership_kind", ["headless", "herdr"])
def test_restart_uses_persisted_ownership_kind_despite_provider_config_drift(
        isolated_db, monkeypatch, ownership_kind):
    provider = "now-headless" if ownership_kind == "herdr" else "now-herdr"
    isolated_db.create_task(TaskCreate(prompt="config drift", provider=provider))
    task = isolated_db.get_next_runnable()
    _reserve_attempt(isolated_db, task, ownership_kind=ownership_kind)
    observed = []
    monkeypatch.setattr(
        worker, "_recovered_provider_cleanup",
        lambda _task, reservation: (
            lambda: observed.append(reservation["ownership_kind"]) or ""),
    )

    worker._reconcile_reserved_running_tasks(set())

    assert observed == [ownership_kind]


def test_recovered_herdr_cleanup_starts_server_before_closing_tabs(
        isolated_db, monkeypatch):
    isolated_db.create_task(TaskCreate(prompt="herdr restart"))
    task = isolated_db.get_next_runnable()
    reservation = _reserve_attempt(
        isolated_db, task, ownership_kind="herdr")
    reservation = isolated_db.begin_pipeline_target_herdr_session(reservation)
    events = []
    monkeypatch.setattr(
        herdr_exec, "_ensure_server",
        lambda host: events.append(("ensure", host)),
    )
    monkeypatch.setattr(
        herdr_exec, "_close_stale_tabs",
        lambda task_id, host: events.append(("close", task_id, host)),
    )

    cleanup = worker._recovered_provider_cleanup(task, reservation)

    assert cleanup() == ""
    assert events == [("ensure", None), ("close", task.id, None)]


def test_macos_orphan_scan_requires_exact_unguessable_reservation_marker(
        monkeypatch):
    started_at = "2026-09-17T12:00:00+00:00"
    token = "a" * 32

    class FakeOS:
        name = "posix"

        @staticmethod
        def listdir(_path):
            raise OSError("no procfs")

        @staticmethod
        def getpgid(pid):
            return pid + 1000

        @staticmethod
        def getpgrp():
            return 9999

    output = "\n".join([
        # Untrusted argv can contain the public task id and attempt, but it
        # cannot predict the random reservation token.
        f"101 provider --prompt PP_TASK_ID=7 PP_TASK_STARTED_AT={started_at}",
        f"102 provider PP_TASK_ID=7 PP_TASK_STARTED_AT={started_at} "
        f"PP_PIPELINE_TARGET_TOKEN={token}",
    ])
    monkeypatch.setattr(worker, "os", FakeOS)
    monkeypatch.setattr(
        worker.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""),
    )

    groups, error = worker._marked_task_process_groups(7, started_at, token)

    assert error == ""
    assert groups == {1102}


def test_registered_provider_cleanup_retry_keeps_boundary_and_reservation(
        isolated_db):
    isolated_db.create_task(TaskCreate(prompt="tree retry"))
    task = isolated_db.get_next_runnable()
    isolated_db.reserve_pipeline_target(
        "owner/repo", "review", 10, HEAD_A, task.id, 300)

    class Tree:
        def __init__(self):
            self.terminate_calls = 0
            self.close_calls = 0
            self.process = SimpleNamespace(kill=lambda: None)

        def terminate(self):
            self.terminate_calls += 1
            if self.terminate_calls == 1:
                raise OSError("temporary terminate failure")

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("temporary close failure")

    tree = Tree()
    worker._register_provider_tree(task.id, tree)

    assert worker._fail_stuck(task, RuntimeError("boom")) is False
    assert isolated_db.get_task(task.id).status.value == "running"
    assert isolated_db.task_has_live_pipeline_target_reservation(task.id)
    assert worker._fail_stuck(task, RuntimeError("boom")) is True
    assert tree.terminate_calls == 2
    assert tree.close_calls == 2
    assert isolated_db.get_task(task.id).status.value == "failed"


def test_duplicate_provider_registration_closes_new_tree(isolated_db):
    first_events = []
    second_events = []

    class Tree:
        def __init__(self, events):
            self.events = events
            self.process = SimpleNamespace(kill=lambda: events.append("kill"))

        def terminate(self):
            self.events.append("terminate")

        def close(self):
            self.events.append("close")

    first = Tree(first_events)
    second = Tree(second_events)
    worker._register_provider_tree(777, first)
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            worker._register_provider_tree(777, second)
        assert second_events == ["terminate", "close"]
        assert first_events == []
    finally:
        assert worker._close_registered_provider_tree(777) is True


def test_scheduler_requires_one_lane_per_replica(monkeypatch):
    profile = {
        "queues": [{
            "id": "review", "series_contains": "Project - REVIEW", "replicas": 2,
        }],
        "scheduler": {"lanes": [{"id": "one", "queues": ["review"]}]},
    }
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"p": profile})
    with pytest.raises(ValueError, match="2 реплики"):
        pipeline_insights.worker_lane_policy()

    profile["scheduler"]["lanes"].append({"id": "two", "queues": ["review"]})
    assert len(pipeline_insights.worker_lane_policy()["lanes"]) == 2
