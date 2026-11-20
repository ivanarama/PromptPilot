"""The user-facing pipeline status must distinguish unknown TAIL from empty."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from promptpilot import config, pipeline_insights, tail_store


REPOSITORY = "owner/project"
REVIEW = {
    "review_id": "123", "review_updated_at": "2026-01-01T00:00:00Z",
    "review_body_digest": "b" * 64, "epoch_digest": "e" * 64,
}


@pytest.fixture
def profile(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", isolated_db.DB_PATH)
    profile = {"title": "Example", "repository": "Owner/Project", "queues": []}
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": profile})
    monkeypatch.setattr(pipeline_insights, "_run_profile_health_check",
                        lambda _: pytest.fail("reading status must not run a GitHub checker"))
    return profile


def checkpoint(*, revision=None, base="main", state="complete", age=0, intervals=(),
               error=None, candidates=(), coverage_delay=0):
    observed = datetime.now(timezone.utc) - timedelta(seconds=age)
    covered = observed - timedelta(seconds=coverage_delay)
    return tail_store.save_discovery(REPOSITORY, revision, {
        "base": base, "fingerprint": tail_store.query_fingerprint(REPOSITORY, base),
        "state": state, "last_complete_at": observed.isoformat(),
        "discovery_complete_through": covered.isoformat(),
        "last_error": error, "intervals": list(intervals),
    }, candidates, pass_token="observed")


def read_tail():
    return pipeline_insights.read_cached("example", [])["tail"]


def test_zero_is_only_reported_after_fresh_complete_discovery_for_this_branch(profile):
    initial = read_tail()
    assert initial["state"] == "unobserved"
    assert initial["counts"] == dict(ready=None, waiting=None, invalid=None, ambiguous=None)

    revision = checkpoint()
    known_empty = read_tail()
    assert known_empty["complete"] is True
    assert known_empty["counts"] == dict(ready=0, waiting=0, invalid=0, ambiguous=0)
    assert known_empty["completions"] == dict(empty=0, legacy=0, versioned=0)
    assert known_empty["discovery_complete_through"] is not None

    profile["base_branch"] = "stable"
    assert read_tail()["state"] == "unobserved"
    checkpoint(revision=revision, base="stable", coverage_delay=3600)
    stale = read_tail()
    assert stale["state"] == "stale"
    assert stale["counts"] == initial["counts"]
    assert stale["discovery_age_seconds"] >= 3600


def test_partial_discovery_preserves_positive_observations(profile):
    now = datetime.now(timezone.utc)
    interval = [(now - timedelta(days=31)).isoformat(), (now - timedelta(days=1)).isoformat()]
    candidates = [{"number": number, "head": "a" * 40, "base": "main",
                   "merged_at": interval[0]} for number in range(1, 6)]
    revision = checkpoint(state="incomplete", intervals=[interval], candidates=candidates)
    for number, state in enumerate(("ready", "waiting-policy", "active-elsewhere", "resolved"), start=1):
        candidate = tail_store.get_candidate(REPOSITORY, "main", number)
        proof = {"completion_kind": "empty", "completion_digest": "c" * 64} if state == "resolved" else {}
        tail_store.record_result(candidate, state=state, **REVIEW, **proof)

    partial = read_tail()
    assert partial["state"] == "incomplete"
    assert partial["uncovered_interval_count"] == 1
    assert partial["uncovered_intervals"] == [{"lower": interval[0], "upper": interval[1]}]
    assert partial["counts"] == dict(ready=1, waiting=2, invalid=None, ambiguous=None)
    assert partial["pending_proof"] == 1
    assert partial["completions"] == dict(empty=1, legacy=None, versioned=None)
    assert partial["oldest_ready_age_seconds"] >= 31 * 86400

    revision = checkpoint(revision=revision)
    assert read_tail()["complete"] is False  # One observed source still lacks classification.
    pending = tail_store.get_candidate(REPOSITORY, "main", 5)
    tail_store.record_result(pending, state="ambiguous-create", **REVIEW)
    assert read_tail()["counts"] == dict(ready=1, waiting=2, invalid=0, ambiguous=1)

    checkpoint(revision=revision, state="incomplete", intervals=[interval], error="GitHub rate limit")
    unavailable = read_tail()
    assert unavailable["state"] == "unavailable"
    assert unavailable["complete"] is False
    assert unavailable["counts"]["invalid"] is None
    assert unavailable["reason"].endswith("GitHub rate limit")


def test_last_drain_success_and_refusal_are_read_from_their_own_branch(profile):
    checkpoint()
    assert read_tail()["last_successful_drain_at"] is None
    tail_store.record_drain_event(REPOSITORY, "main", failure="GitHub lookup timed out")
    tail_store.record_drain_event(REPOSITORY, "main")
    tail_store.record_drain_event(REPOSITORY, "other", failure="other branch")
    tail = read_tail()
    failure = tail["last_drain_failure"]
    assert failure["reason"] == "GitHub lookup timed out"
    assert tail["last_successful_drain_at"] >= failure["at"]
    assert tail["complete"] is True  # This does not replace the separate discovery status.


def test_fresh_discovery_does_not_revalidate_overdue_completion(profile):
    source = {"number": 42, "head": "a" * 40, "base": "main", "merged_at": REVIEW["review_updated_at"]}
    revision = checkpoint(candidates=[source], age=31 * 86400)
    completion = {"state": "resolved", "completion_kind": "versioned", "completion_digest": "c" * 64}
    candidate = tail_store.get_candidate(REPOSITORY, "main", 42)
    tail_store.record_result(candidate, next_check_at=source["merged_at"], **REVIEW, **completion)
    checkpoint(revision=revision, candidates=[source])

    overdue = read_tail()
    assert overdue["state"] == "incomplete"
    assert overdue["recheck_due"] == 1
    assert all(value is None for value in overdue["counts"].values())
    assert overdue["completions"]["versioned"] == 1

    candidate = tail_store.get_candidate(REPOSITORY, "main", 42)
    tail_store.record_result(candidate, **REVIEW, **completion)
    verified = read_tail()
    assert verified["complete"] is True
    assert verified["recheck_due"] == 0
    assert verified["counts"] == dict(ready=0, waiting=0, invalid=0, ambiguous=0)


def test_missing_recheck_total_is_unknown(profile, monkeypatch):
    checkpoint()
    original = tail_store.backlog

    def older_backlog(*args, **kwargs):
        result = original(*args, **kwargs)
        result.pop("recheck_due", None)
        return result

    monkeypatch.setattr(tail_store, "backlog", older_backlog)
    tail = read_tail()
    assert tail["state"] == "incomplete"
    assert tail["recheck_due"] is None
    assert all(value is None for value in tail["counts"].values())


def test_storage_failure_does_not_poison_other_pipeline_status(profile, monkeypatch):
    def failed_read(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tail_store, "backlog", failed_read)
    response = pipeline_insights.read_cached("example", [])
    assert response["profile_id"] == "example"
    assert response["tail"]["state"] == "unavailable"
    assert response["tail"]["counts"]["ready"] is None
