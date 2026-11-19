from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
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


REVIEW = {"review_id": "123", "review_updated_at": "2026-01-01T00:00:00Z",
          "review_body_digest": "b" * 64, "epoch_digest": "e" * 64}


@pytest.mark.parametrize("kind", ["empty", "legacy", "versioned"])
def test_local_completion_leaves_residue_only_for_the_proved_version(tail_db, kind):
    discovery = tail_store.save_discovery(REPOSITORY, None, {}, [observation(1)], pass_token="p1")
    candidate = tail_store.get_candidate(REPOSITORY, "main", 1)
    tail_store.record_result(candidate, state="resolved", completion_kind=kind, completion_digest="c" * 64,
                             next_check_at="2000-01-01T00:00:00Z", **REVIEW)
    completed = tail_store.backlog(REPOSITORY)
    assert completed["counts"] == {} and completed["candidates"] == []
    assert completed["completions"] == {kind: 1}
    refresh, = tail_store.due_candidates(REPOSITORY, "main")
    assert refresh["state"] == "resolved"

    tail_store.save_discovery(REPOSITORY, discovery, {}, [observation(1, head="f" * 40)], pass_token="p2")
    with pytest.raises(tail_store.TailCandidateConflict):
        tail_store.record_result(refresh, state="resolved", completion_kind=kind, completion_digest="d" * 64,
                                 **REVIEW)
    reopened = tail_store.backlog(REPOSITORY)
    assert reopened["completions"] == {} and reopened["counts"] == {"pending-proof": 1}
    assert reopened["candidates"][0]["completion_digest"] is None
    stale, = tail_store.due_candidates(REPOSITORY, "main")
    assert stale["head"] == "f" * 40 and stale["review_id"] == REVIEW["review_id"]
    # A broken review stays visible, retaining the last known review for diagnosis.
    tail_store.record_result(stale, state="invalid-source", reason="selected review was deleted")
    invalid, = tail_store.backlog(REPOSITORY)["candidates"]
    assert (invalid["state"], invalid["review_id"], invalid["completion_kind"]) == ("invalid-source", "123", None)
    assert tail_store.due_candidates(REPOSITORY, "main") == []
    replacement = dict(REVIEW, review_id="124")
    tail_store.record_result(tail_store.get_candidate(REPOSITORY, "main", 1), state="ready", **replacement)
    ready, = tail_store.due_candidates(REPOSITORY, "main")
    assert ready["state"] == "ready" and ready["review_id"] == "124"


def test_due_candidates_prioritize_age_bound_retries_and_filter_the_base(tail_db):
    data = [observation(n) for n in range(1, 12)]
    data[0]["merged_at"] = "2026-02-01T00:00:00Z"
    data[-1]["base"] = "stable"
    tail_store.save_discovery(REPOSITORY, None, {}, data, pass_token="p")
    for n in (7, 8, 9, 10):
        row = tail_store.get_candidate(REPOSITORY, "main", n)
        tail_store.record_result(row, state="waiting-policy", reason="hold",
                                 next_check_at=f"2000-01-{11-n:02d}T00:00:00Z")
    batch = tail_store.due_candidates(REPOSITORY, "main", 5)
    assert [row["pr_number"] for row in batch] == [2, 3, 4, 5, 10]
    # Even a one-PR batch can revisit an overdue policy wait.
    assert tail_store.due_candidates(REPOSITORY, "main", 1)[0]["pr_number"] == 10
    only_stable, = tail_store.due_candidates(REPOSITORY, "stable")
    assert only_stable["pr_number"] == 11
    assert tail_store.get_candidate(REPOSITORY, "main", 11) is None


def test_candidate_classification_and_transition_event_share_one_transaction(tail_db):
    tail_store.save_discovery(REPOSITORY, None, {}, [observation(1)], pass_token="p")
    a = tail_store.get_candidate(REPOSITORY, "main", 1)
    b, = tail_store.due_candidates(REPOSITORY, "main")
    with tail_db._connect() as conn:
        conn.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON pipeline_snapshots
                        BEGIN SELECT RAISE(ABORT, 'fixture disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture disk failure"):
        tail_store.record_result(a, state="ready", **REVIEW)
    assert tail_store.get_candidate(REPOSITORY, "main", 1) == a
    with tail_db._connect() as conn:
        conn.execute("DROP TRIGGER fail_event")
    assert tail_store.record_result(a, state="ready", **REVIEW) == a["revision"] + 1
    with pytest.raises(tail_store.TailCandidateConflict):
        tail_store.record_result(b, state="waiting-policy", reason="hold")
    event, = tail_db.list_pipeline_snapshots("tail:" + REPOSITORY)
    assert event["payload"]["number"] == 1 and event["payload"]["revision"] == a["revision"] + 1
    assert event["payload"]["review_body_digest"] == REVIEW["review_body_digest"]


def test_old_index_is_readable_before_migration_without_modifying_it(tail_db):
    # Keep the stage A candidate schema and one observation, as an older
    # binary would have left them. Use only ADD COLUMN for the upgrade.
    path = tail_db.DB_PATH
    with sqlite3.connect(path) as conn:
        sql, = conn.execute("SELECT sql FROM sqlite_schema WHERE name='tail_candidates'").fetchone()
        old = sql.replace("    classified_revision INTEGER,\n", "")
        assert old != sql
        conn.execute("DROP TABLE tail_candidates")
        conn.execute(old)
        conn.execute("DELETE FROM schema_migrations WHERE version=?", (tail_db.TAIL_CLASSIFICATION_SCHEMA_VERSION,))
        conn.execute("INSERT INTO tail_discovery VALUES ('owner/project',1,'{}','2026-01-01')")
        conn.execute("""INSERT INTO tail_candidates
                        (repository,pr_number,merged_at,head,base,last_observed_at,pass_token)
                        VALUES ('owner/project',1,'2026-01-01T00:00:00Z',?,'main','2026-01-01','old')""",
                     ("a" * 40,))
    before, = tail_store.due_candidates(REPOSITORY, "main")
    assert before["state"] == "pending-proof" and tail_store.backlog(REPOSITORY)["counts"] == {"pending-proof": 1}
    with sqlite3.connect(path) as conn:
        assert "classified_revision" not in [row[1] for row in conn.execute("PRAGMA table_info(tail_candidates)")]
    tail_db.init_db()
    tail_db.init_db()
    after, = tail_store.due_candidates(REPOSITORY, "main")
    assert after == before
    tail_store.record_result(after, state="ready", **REVIEW)
    assert tail_store.get_candidate(REPOSITORY, "main", 1)["state"] == "ready"
