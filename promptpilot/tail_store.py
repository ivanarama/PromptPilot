"""Local TAIL discovery checkpoints, candidate observations, and admission results."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3

from . import config
from .pipeline_errors import PipelineError


class TailDiscoveryConflict(PipelineError):
    """Another invocation already committed a newer checkpoint."""


class TailCandidateConflict(PipelineError):
    """Discovery or another classifier changed the candidate during the remote read."""


def query_fingerprint(repository, base):
    # A protocol change invalidates both cursor resumption and reported progress.
    query = json.dumps(["merged-tail-v2", repository, base], ensure_ascii=True)
    return hashlib.sha256(query.encode("ascii")).hexdigest()


def _checkpoint(row):
    checkpoint = json.loads(row["checkpoint_json"])
    checkpoint["revision"] = row["revision"]
    return checkpoint


def load_discovery(repository):
    from . import db

    with db._connect() as conn:
        row = conn.execute(
            "SELECT revision, checkpoint_json FROM tail_discovery WHERE repository = ?",
            (repository,),
        ).fetchone()
        return _checkpoint(row) if row is not None else None


def save_discovery(repository, expected_revision, checkpoint, observations=(), *, pass_token=None):
    """Commit a checkpoint and its page together, or leave both untouched."""
    from . import db

    # The revision is owned by the transaction even when callers reuse a loaded dict.
    content = {k: v for k, v in checkpoint.items() if k != "revision"}
    payload = json.dumps(content, separators=(",", ":"), sort_keys=True, allow_nan=False)
    now = db._now()
    with db._connect(immediate=True) as conn:
        if expected_revision is None:
            revision = 1
            result = conn.execute(
                """INSERT INTO tail_discovery(repository, revision, checkpoint_json, updated_at)
                   VALUES (?, ?, ?, ?) ON CONFLICT(repository) DO NOTHING""",
                (repository, revision, payload, now),
            )
        else:
            revision = expected_revision + 1
            result = conn.execute(
                """UPDATE tail_discovery SET revision = ?, checkpoint_json = ?, updated_at = ?
                   WHERE repository = ? AND revision = ?""",
                (revision, payload, now, repository, expected_revision),
            )
        if result.rowcount != 1:
            raise TailDiscoveryConflict("TAIL discovery changed during this request")

        for item in observations:
            if not pass_token:
                raise PipelineError("TAIL page is missing its verification pass")
            # Discovery may refresh observations. Only the later proof checker can
            # select a review, change processing state, or record a completion.
            conn.execute(
                """INSERT INTO tail_candidates
                   (repository, pr_number, merged_at, head, base, last_observed_at, pass_token)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(repository, pr_number) DO UPDATE SET
                     revision = revision + (head != excluded.head OR base != excluded.base
                                            OR merged_at != excluded.merged_at),
                     merged_at = excluded.merged_at, head = excluded.head, base = excluded.base,
                     last_observed_at = excluded.last_observed_at, pass_token = excluded.pass_token""",
                (repository, item["number"], item["merged_at"], item["head"], item["base"], now, pass_token),
            )
    return revision


def pass_snapshot(repository, token):
    """Canonical digest of the observed set, independent of remote page order."""
    from . import db

    digest = hashlib.sha256()
    count = 0
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT pr_number, merged_at, head, base FROM tail_candidates
               WHERE repository = ? AND pass_token = ? ORDER BY pr_number""",
            (repository, token),
        )
        for row in rows:
            record = json.dumps(tuple(row), separators=(",", ":")).encode("utf-8")
            digest.update(record + b"\n")
            count += 1
    return count, digest.hexdigest()


@contextmanager
def _read_index():
    # Importing db initializes the schema. Old and new operator directories
    # must also support inspection without writes.
    if not config.DB_PATH.exists():
        yield None
        return
    uri = config.DB_PATH.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tail_discovery'"
        ).fetchone()
        yield conn if exists else None


def _projection(conn):
    # Stage A already recorded local observations but did not classify them.
    # Expose a changed observation as pending until its exact revision is
    # checked again. Keep the previously selected review available for diagnosis.
    has_revision = any(row["name"] == "classified_revision" for row in conn.execute(
        "PRAGMA table_info(tail_candidates)"))
    current = "classified_revision = revision" if has_revision else "0"
    select = f"""SELECT repository, pr_number, base, head, merged_at, last_observed_at, revision,
                      CASE WHEN {current} THEN state ELSE 'pending-proof' END AS state,
                      review_id, review_updated_at, review_body_digest, epoch_digest,
                      CASE WHEN {current} THEN completion_kind END AS completion_kind,
                      CASE WHEN {current} THEN completion_digest END AS completion_digest,
                      CASE WHEN {current} THEN next_check_at END AS next_check_at,
                      CASE WHEN {current} THEN last_reason END AS last_reason
               FROM tail_candidates WHERE repository = ? AND base = ?"""
    return select, has_revision


def get_candidate(repository, base, number):
    """Read a single observed identity, including a completion due for revalidation."""
    with _read_index() as conn:
        if conn is None:
            return None
        candidates, _ = _projection(conn)
        row = conn.execute(candidates + " AND pr_number = ?", (repository, base, number)).fetchone()
        return dict(row) if row is not None else None


def due_candidates(repository, base, limit=5):
    """Select a bounded batch; this read grants no lease or permission to write."""
    if type(limit) is not int or not 1 <= limit <= 5:
        raise PipelineError("TAIL drain limit must be between 1 and 5")
    now = datetime.now(timezone.utc).isoformat()
    with _read_index() as conn:
        if conn is None:
            return []
        candidates, has_revision = _projection(conn)
        if not has_revision:
            rows = conn.execute(candidates + " ORDER BY merged_at, pr_number LIMIT ?", (repository, base, limit))
            return [dict(row) for row in rows]
        # Filter on the stored columns so SQLite can walk the two bounded
        # indexes instead of sorting eligible work from the full archive.
        primary = conn.execute(candidates + """
            AND (classified_revision IS NOT revision OR state IN ('ready', 'pending-proof'))
            AND (classified_revision IS NOT revision OR next_check_at IS NULL
                 OR (state = 'ready' AND next_check_at <= ?))
            ORDER BY merged_at, pr_number LIMIT ?""", (repository, base, now, limit)).fetchall()
        retries = conn.execute(candidates + """
            AND classified_revision = revision AND state != 'ready' AND tail_candidates.next_check_at <= ?
            ORDER BY tail_candidates.next_check_at, pr_number LIMIT ?""", (repository, base, now, limit)).fetchall()
    if limit == 1 and primary and retries:
        due = datetime.fromisoformat(retries[0]["next_check_at"])
        merged = datetime.fromisoformat(primary[0]["merged_at"].replace("Z", "+00:00"))
        return [dict(retries[0] if due < merged else primary[0])]
    # Reserve one place for a due retry, then use spare capacity if fresh
    # work runs out. Failed checks move their next_check_at forward.
    retry_slots = min(len(retries), max(1, limit - len(primary)))
    return [dict(row) for row in primary[:limit - retry_slots] + retries[:retry_slots]]


def record_result(candidate, *, state, reason=None, review_id=None, review_updated_at=None,
                  review_body_digest=None, epoch_digest=None, completion_kind=None,
                  completion_digest=None, next_check_at=None):
    """CAS the classifier's result for the observed PR and append its public metadata locally."""
    from . import db

    if state not in {"pending-proof", "ready", "waiting-policy", "active-elsewhere",
                     "ambiguous-create", "invalid-source", "resolved"}:
        raise PipelineError("unknown TAIL candidate state")
    version = (review_id, review_updated_at, review_body_digest, epoch_digest)
    known = any(value is not None for value in version)
    if known or state in {"resolved", "ready"}:
        if (not isinstance(review_id, str) or not review_id or not isinstance(review_updated_at, str)
                or not review_updated_at or any(not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value)
                                               for value in (review_body_digest, epoch_digest))):
            raise PipelineError("TAIL classification requires a complete selected review version")
    if state == "resolved":
        if (completion_kind not in {"empty", "legacy", "versioned"} or not isinstance(completion_digest, str)
                or not re.fullmatch("[0-9a-f]{64}", completion_digest)):
            raise PipelineError("TAIL resolution requires an exact completion proof")
    elif completion_kind is not None or completion_digest is not None:
        raise PipelineError("unfinished TAIL candidate cannot have a completion proof")

    now = datetime.now(timezone.utc)
    if next_check_at is not None:
        try:
            when = datetime.fromisoformat(next_check_at.replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError
        except (AttributeError, TypeError, ValueError) as exc:
            raise PipelineError("TAIL next check must be a timezone-aware timestamp") from exc
        next_check_at = when.astimezone(timezone.utc).isoformat()
    elif state != "ready":
        delay = timedelta(days=1) if state == "resolved" else timedelta(minutes=15)
        next_check_at = (now + delay).isoformat()

    revision = candidate["revision"] + 1
    with db._connect(immediate=True) as conn:
        result = conn.execute("""
            UPDATE tail_candidates SET revision = ?, classified_revision = ?, state = ?,
                review_id = coalesce(?, review_id), review_updated_at = coalesce(?, review_updated_at),
                review_body_digest = coalesce(?, review_body_digest), epoch_digest = coalesce(?, epoch_digest),
                completion_kind = ?, completion_digest = ?, next_check_at = ?, last_reason = ?
            WHERE repository = ? AND base = ? AND pr_number = ? AND head = ? AND merged_at = ?
              AND revision = ?""",
            (revision, revision, state, *version, completion_kind, completion_digest, next_check_at, reason,
             candidate["repository"], candidate["base"], candidate["pr_number"], candidate["head"],
             candidate["merged_at"], candidate["revision"]))
        if result.rowcount != 1:
            raise TailCandidateConflict("TAIL candidate changed during classification")
        event = {"kind": "tail-classification", "number": candidate["pr_number"], "revision": revision,
                 "state": state, "review_body_digest": review_body_digest,
                 "epoch_digest": epoch_digest, "completion_kind": completion_kind,
                 "completion_digest": completion_digest}
        conn.execute(
            """INSERT INTO pipeline_snapshots(profile_id, repository, captured_at, payload_json)
               VALUES (?, ?, ?, ?)""",
            ("tail:" + candidate["repository"], candidate["repository"], now.isoformat(),
             json.dumps(event, separators=(",", ":"))))
    return revision


def backlog(repository, limit=100, after=0, *, base="main"):
    """Read a consistent bounded page; even an uninitialized store stays read-only."""
    if not 1 <= limit <= 1000 or after < 0:
        raise PipelineError("TAIL backlog requires limit 1..1000 and a nonnegative page key")
    if not isinstance(base, str) or not base:
        raise PipelineError("TAIL backlog requires a base branch")
    result = {
        "repository": repository, "base": base, "discovery": None, "age_seconds": None,
        "counts": {}, "completions": {}, "candidates": [], "next_after": None,
    }
    with _read_index() as conn:
        if conn is None:
            return result
        row = conn.execute(
            "SELECT * FROM tail_discovery WHERE repository = ?", (repository,),
        ).fetchone()
        if row is None:
            return result
        candidates, _ = _projection(conn)
        counts = conn.execute(f"""SELECT state, completion_kind, count(*) FROM ({candidates})
                                  GROUP BY state, completion_kind""", (repository, base))
        for state, kind, count in counts:
            if state == "resolved":
                result["completions"][kind] = count
            else:
                result["counts"][state] = count
        rows = conn.execute(f"""SELECT * FROM ({candidates})
                                WHERE state != 'resolved' AND pr_number > ?
                                ORDER BY pr_number LIMIT ?""", (repository, base, after, limit + 1)).fetchall()
        result["candidates"] = [{key: row[key] for key in row.keys() if key != "repository"} for row in rows[:limit]]
        if len(rows) > limit:
            result["next_after"] = rows[limit - 1]["pr_number"]

        # A repository keeps one current checkpoint. Old observations survive
        # a branch or query change; only a compatible scan can attest freshness.
        checkpoint = _checkpoint(row)
        if checkpoint.get("fingerprint") != query_fingerprint(repository, base):
            return result
        # The cursor, verification token and seen-node set belong to the
        # checkpoint. Backlog readers need interval bounds and progress only.
        fields = ("revision", "generation", "state", "base", "lower", "upper", "mode", "pass_number",
                  "scanned", "source_count", "matched", "verify_pending", "budget_remaining",
                  "budget_reset_at", "last_error", "last_complete_at", "discovery_complete_through")
        progress = {key: checkpoint[key] for key in fields if key in checkpoint}
        intervals = checkpoint.get("intervals", [])
        progress["current_interval"] = intervals[-1] if intervals else None
        progress["pending_intervals"] = len(intervals)
        result["discovery"] = progress
        completed_at = progress.get("last_complete_at")
        if completed_at:
            completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            elapsed = datetime.now(timezone.utc) - completed
            result["age_seconds"] = max(0, int(elapsed.total_seconds()))
    return result
