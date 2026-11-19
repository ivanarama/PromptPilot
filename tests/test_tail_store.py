from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from promptpilot import config, tail_store


REPOSITORY = "owner/project"


@pytest.fixture
def tail_db(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", isolated_db.DB_PATH)
    return isolated_db


def observation(number, head="a" * 40):
    return {"number": number, "head": head, "base": "main", "merged_at": "2026-01-01T00:00:00Z"}


def test_concurrent_page_commits_leave_only_winning_observations(tail_db):
    tail_store.save_discovery(REPOSITORY, None, {"cursor": None})
    fetched = Barrier(2)

    def commit_page(number):
        checkpoint = tail_store.load_discovery(REPOSITORY)
        fetched.wait(timeout=15)
        try:
            tail_store.save_discovery(
                REPOSITORY, checkpoint["revision"], {"cursor": str(number)},
                [observation(number)], pass_token="first-pass",
            )
        except tail_store.TailDiscoveryConflict:
            return None
        return number

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit_page, [10, 20]))
    assert results.count(None) == 1
    winner = next(number for number in results if number is not None)
    assert tail_store.load_discovery(REPOSITORY) == {"cursor": str(winner), "revision": 2}
    candidates = tail_store.backlog(REPOSITORY)["candidates"]
    assert [candidate["pr_number"] for candidate in candidates] == [winner]


def test_changed_observation_preserves_selected_review_and_completion(tail_db):
    revision = tail_store.save_discovery(REPOSITORY, None, {}, [observation(1)], pass_token="first")
    with tail_db._connect() as conn:
        conn.execute(
            """UPDATE tail_candidates SET state='resolved', review_id='123', completion_kind='empty',
                      review_updated_at='2026-01-04', review_body_digest='body', epoch_digest='epoch',
                      completion_digest='proof', next_check_at='2026-01-05', last_reason='verified'
               WHERE repository=? AND pr_number=1""",
            (REPOSITORY,),
        )
    new_head = "b" * 40
    tail_store.save_discovery(REPOSITORY, revision, {}, [observation(1, new_head)], pass_token="second")
    with tail_db._connect() as conn:
        row = conn.execute(
            """SELECT head, revision, state, review_id, review_updated_at, review_body_digest, epoch_digest,
                      completion_kind, completion_digest, next_check_at, last_reason
               FROM tail_candidates WHERE repository=? AND pr_number=1""",
            (REPOSITORY,),
        ).fetchone()
    assert tuple(row) == (
        new_head, 2, "resolved", "123", "2026-01-04", "body", "epoch", "empty", "proof", "2026-01-05", "verified",
    )


def test_backlog_age_tracks_last_complete_scan(tail_db):
    interval = ["2026-01-01T00:00:00Z", "2026-01-30T00:00:00Z"]
    fingerprint = tail_store.query_fingerprint(REPOSITORY, "main")
    revision = tail_store.save_discovery(REPOSITORY, None, {
        "base": "main", "fingerprint": fingerprint, "state": "incomplete",
        "pass_token": "internal", "same_second": [1, 2], "intervals": [interval],
    })
    backlog = tail_store.backlog(REPOSITORY)
    assert backlog["age_seconds"] is None
    assert backlog["discovery"] == {
        "base": "main", "revision": 1, "state": "incomplete", "current_interval": interval, "pending_intervals": 1,
    }
    completed = datetime.now(timezone.utc) - timedelta(days=30)
    tail_store.save_discovery(REPOSITORY, revision, {
        "base": "main", "fingerprint": fingerprint, "state": "incomplete",
        "last_error": "rate limit", "last_complete_at": completed.isoformat(),
    })
    assert tail_store.backlog(REPOSITORY)["age_seconds"] >= 30 * 86400
