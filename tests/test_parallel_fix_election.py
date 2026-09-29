"""Opt-in FIX target election primitives; no worker route is enabled yet."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from threading import Barrier

import pytest

from promptpilot import project_pipeline as pipelinectl
from promptpilot.models import TaskCreate


HEAD_A = "a" * 40
HEAD_B = "b" * 40
DIGEST = "c" * 40
CONFIG = {
    "repository": "owner/repo", "parallel_fix_enabled": True,
    "target_reservation_ttl_seconds": 300,
}


def _attempt(db, monkeypatch, name):
    db.create_task(TaskCreate(prompt=name))
    task = db.get_next_runnable()
    monkeypatch.setenv("PP_TASK_ID", str(task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", task.started_at.astimezone(
        timezone.utc).isoformat())
    monkeypatch.setenv("PP_PROVIDER_OWNERSHIP_KIND", "headless")
    return task


def test_fix_election_requires_opt_in_and_versioned_issue():
    issue = {"stage": "fix-issue", "number": 7, "updated_at": "2026-09-29"}
    with pytest.raises(pipelinectl.PipelineError, match="disabled"):
        pipelinectl._elect_fix_candidate({"repository": "owner/repo"}, [issue])
    with pytest.raises(pipelinectl.PipelineError, match="unversioned"):
        pipelinectl._ordered_fix_candidates([issue])
    assert pipelinectl._fix_candidate_key({
        **issue, "eligibility_digest": DIGEST,
    }) == ("fix-issue", 7, DIGEST)


def test_fix_election_prioritizes_pr_rework_and_preserves_issue_order(
        isolated_db, monkeypatch):
    candidates = [
        {"stage": "fix-issue", "number": 3, "eligibility_digest": DIGEST},
        {"stage": "review", "number": 12, "head": HEAD_B},
        {"stage": "review", "number": 10, "head": HEAD_A},
    ]
    _attempt(isolated_db, monkeypatch, "first FIX")
    first, reservation = pipelinectl._elect_fix_candidate(CONFIG, candidates)
    assert first["number"] == 10
    assert (reservation["stage"], reservation["number"], reservation["head"]) == (
        "fix-pr", 10, HEAD_A)

    _attempt(isolated_db, monkeypatch, "second FIX")
    second, reservation = pipelinectl._elect_fix_candidate(CONFIG, candidates)
    assert second["number"] == 12
    assert reservation["stage"] == "fix-pr"

    _attempt(isolated_db, monkeypatch, "third FIX")
    third, reservation = pipelinectl._elect_fix_candidate(CONFIG, candidates)
    assert third["number"] == 3
    assert reservation["stage"] == "fix-issue"


def test_fix_election_same_attempt_never_retargets_after_revision_change(
        isolated_db, monkeypatch):
    _attempt(isolated_db, monkeypatch, "FIX")
    first = {"stage": "review", "number": 10, "head": HEAD_A}
    _, reservation = pipelinectl._elect_fix_candidate(CONFIG, [first])
    repeated, renewed = pipelinectl._elect_fix_candidate(CONFIG, [first])
    assert repeated == first
    assert renewed["token"] == reservation["token"]
    with pytest.raises(pipelinectl.PipelineError, match="no longer eligible"):
        pipelinectl._elect_fix_candidate(CONFIG, [
            {"stage": "review", "number": 10, "head": HEAD_B},
            {"stage": "fix-issue", "number": 11,
             "eligibility_digest": DIGEST},
        ])


def test_fix_election_concurrent_attempts_do_not_duplicate_target(
        isolated_db, monkeypatch):
    tasks = [_attempt(isolated_db, monkeypatch, f"FIX {index}")
             for index in range(2)]
    barrier = Barrier(2)
    candidate = {"stage": "review", "number": 10, "head": HEAD_A}

    def elect(task):
        # Environment variables are process-global; exercise the concurrent
        # reservation boundary directly with each exact task attempt.
        barrier.wait()
        return isolated_db.reserve_pipeline_target(
            "owner/repo", "fix-pr", 10, HEAD_A, task.id, 300,
            task_started_at=task.started_at.astimezone(timezone.utc).isoformat(),
            ownership_kind="headless",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(elect, tasks))
    assert len([result for result in results if result is not None]) == 1
    assert pipelinectl._fix_candidate_key(candidate) == ("fix-pr", 10, HEAD_A)
