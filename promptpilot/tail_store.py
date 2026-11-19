"""Local checkpoints and candidate observations for read-only TAIL discovery."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from . import config
from .pipeline_errors import PipelineError


class TailDiscoveryConflict(PipelineError):
    """Another invocation already committed a newer checkpoint."""


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


def backlog(repository, limit=100, after=0, *, base="main"):
    """Read a consistent bounded page; even an uninitialized store stays read-only."""
    if not 1 <= limit <= 1000 or after < 0:
        raise PipelineError("TAIL backlog requires limit 1..1000 and a nonnegative page key")
    if not isinstance(base, str) or not base:
        raise PipelineError("TAIL backlog requires a base branch")
    result = {
        "repository": repository, "base": base, "discovery": None, "age_seconds": None,
        "counts": {}, "candidates": [], "next_after": None,
    }
    # Importing db initializes the schema. Keep inspection read-only even for
    # a new operator directory or a database created by an earlier version.
    if not config.DB_PATH.exists():
        return result
    uri = config.DB_PATH.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tail_discovery'"
        ).fetchone():
            return result
        row = conn.execute(
            "SELECT * FROM tail_discovery WHERE repository = ?", (repository,),
        ).fetchone()
        if row is None:
            return result
        result["counts"] = dict(conn.execute(
            "SELECT state, count(*) FROM tail_candidates WHERE repository = ? AND base = ? GROUP BY state",
            (repository, base),
        ))
        rows = conn.execute(
            """SELECT pr_number, merged_at, head, base, last_observed_at, revision,
                      state, review_id, review_updated_at, review_body_digest, epoch_digest,
                      completion_kind, completion_digest, next_check_at, last_reason
               FROM tail_candidates
               WHERE repository = ? AND base = ? AND state != 'resolved' AND pr_number > ?
               ORDER BY pr_number LIMIT ?""",
            (repository, base, after, limit + 1),
        ).fetchall()
        result["candidates"] = [dict(row) for row in rows[:limit]]
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
