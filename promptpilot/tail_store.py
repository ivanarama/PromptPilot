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


def _checkpoint_payload(checkpoint):
    # The revision belongs to the SQLite row even if the caller reused a loaded dict.
    content = {key: value for key, value in checkpoint.items() if key != "revision"}
    return json.dumps(content, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _keep_budget(checkpoint, remaining, reset_at, now):
    """Merge independent GitHub receipts; an active exhaustion bars further reads."""
    if remaining is None or not reset_at:
        return False
    saved = checkpoint.get("budget_remaining")
    saved_reset = checkpoint.get("budget_reset_at")
    if saved is not None and saved_reset:
        if saved_reset == reset_at and saved <= remaining:
            return False
        if saved == 0 and saved_reset > now and (remaining != 0 or saved_reset >= reset_at):
            return False
        if saved_reset > reset_at and not (remaining == 0 and reset_at > now):
            return False
    checkpoint.update(budget_remaining=remaining, budget_reset_at=reset_at)
    return True


def load_discovery(repository, *, budget=None):
    """Read the latest checkpoint, optionally recording another actual request's quota."""
    from . import db

    with db._connect(immediate=budget is not None) as conn:
        row = conn.execute(
            "SELECT revision, checkpoint_json FROM tail_discovery WHERE repository = ?",
            (repository,),
        ).fetchone()
        if row is None:
            return None
        checkpoint = _checkpoint(row)
        if budget is not None:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if _keep_budget(checkpoint, *budget, now):
                # Metering is independent of page CAS. A slow response may
                # report exhaustion even after another scan finished the page.
                conn.execute("UPDATE tail_discovery SET checkpoint_json = ? WHERE repository = ?",
                             (_checkpoint_payload(checkpoint), repository))
        return checkpoint


def save_discovery(repository, expected_revision, checkpoint, observations=(), *, pass_token=None):
    """Commit a checkpoint and its page together, or leave both untouched."""
    from . import db

    content = dict(checkpoint)
    now = db._now()
    with db._connect(immediate=True) as conn:
        if expected_revision is not None:
            saved = conn.execute("""SELECT checkpoint_json FROM tail_discovery
                                    WHERE repository = ? AND revision = ?""",
                                 (repository, expected_revision)).fetchone()
            if saved is not None:
                prior = json.loads(saved["checkpoint_json"])
                _keep_budget(content, prior.get("budget_remaining"), prior.get("budget_reset_at"),
                             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        payload = _checkpoint_payload(content)
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
    if content.get("budget_remaining") is not None:
        checkpoint.update(budget_remaining=content["budget_remaining"], budget_reset_at=content["budget_reset_at"])
    return revision


def pass_snapshot(repository, token, revision):
    """Canonical digest of the observed set, independent of remote page order."""
    from . import db

    digest = hashlib.sha256()
    count = 0
    with db._connect() as conn:
        # Another scanner can already be refreshing pass_token on candidates
        # for the next pass. Read this checkpoint and its set in one snapshot.
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM tail_discovery WHERE repository = ? AND revision = ?",
                            (repository, revision)).fetchone():
            raise TailDiscoveryConflict("TAIL verification checkpoint changed")
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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tail_candidates)")}
    has_revision = "classified_revision" in columns
    current = "classified_revision = revision" if has_revision else "0"
    recovery = f"CASE WHEN {current} THEN recovery_json END" if "recovery_json" in columns else "NULL"
    select = f"""SELECT repository, pr_number, base, head, merged_at, last_observed_at, revision,
                      CASE WHEN {current} THEN state ELSE 'pending-proof' END AS state,
                      review_id, review_updated_at, review_body_digest, epoch_digest,
                      CASE WHEN {current} THEN completion_kind END AS completion_kind,
                      CASE WHEN {current} THEN completion_digest END AS completion_digest,
                      CASE WHEN {current} THEN next_check_at END AS next_check_at,
                      CASE WHEN {current} THEN last_reason END AS last_reason,
                      {recovery} AS recovery_json
               FROM tail_candidates WHERE repository = ? AND base = ?"""
    return select, has_revision


def _candidate(row):
    candidate = dict(row)
    evidence = candidate.pop("recovery_json")
    if evidence is not None:
        try:
            candidate["recovery"] = json.loads(evidence)
        except ValueError as exc:
            raise sqlite3.DatabaseError("invalid local TAIL recovery evidence") from exc
    return candidate


def get_candidate(repository, base, number):
    """Read a single observed identity, including a completion due for revalidation."""
    with _read_index() as conn:
        if conn is None:
            return None
        candidates, _ = _projection(conn)
        row = conn.execute(candidates + " AND pr_number = ?", (repository, base, number)).fetchone()
        return _candidate(row) if row is not None else None


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
            return [_candidate(row) for row in rows]
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
        return [_candidate(retries[0] if due < merged else primary[0])]
    # Reserve one place for a due retry, then use spare capacity if fresh
    # work runs out. Failed checks move their next_check_at forward.
    retry_slots = min(len(retries), max(1, limit - len(primary)))
    return [_candidate(row) for row in primary[:limit - retry_slots] + retries[:retry_slots]]


def record_result(candidate, *, state, reason=None, review_id=None, review_updated_at=None,
                  review_body_digest=None, epoch_digest=None, completion_kind=None,
                  completion_digest=None, next_check_at=None, recovery=None):
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
    # An ordinary check of the same review can coexist with a verified
    # recovery observation. A changed or unprovable review cannot inherit it.
    if recovery is None and known and state != "invalid-source":
        prior = tuple(candidate.get(key) for key in
                      ("review_id", "review_updated_at", "review_body_digest", "epoch_digest"))
        if prior == version:
            recovery = candidate.get("recovery")
    if recovery is not None and not known:
        raise PipelineError("TAIL recovery requires a complete selected review version")
    recovery_json = (
        json.dumps(recovery, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if recovery else None
    )

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
                completion_kind = ?, completion_digest = ?, next_check_at = ?, last_reason = ?, recovery_json = ?
            WHERE repository = ? AND base = ? AND pr_number = ? AND head = ? AND merged_at = ?
              AND revision = ?""",
            (revision, revision, state, *version, completion_kind, completion_digest, next_check_at, reason, recovery_json,
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


def _drain_key(repository, base):
    return "tail-drain-v1:" + query_fingerprint(repository, base)


def _drain_info(conn, repository, base):
    # Some old indexes predate the rest of the operator database.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        return {}
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_drain_key(repository, base),)).fetchone()
    if row is None:
        return {}
    try:
        info = json.loads(row["value"])
        if isinstance(info, dict):
            return info
    except ValueError:
        pass
    raise sqlite3.DatabaseError("invalid local TAIL drain diagnostics")


def record_drain_event(repository, base, *, failure=None, completed_at=None):
    """Keep the latest verified completion and latest failure independently."""
    from . import db

    with db._connect(immediate=True) as conn:
        info = _drain_info(conn, repository, base)
        now = datetime.now(timezone.utc).isoformat()
        if failure is None:
            if completed_at is not None:
                stamp = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise sqlite3.DatabaseError("TAIL completion is missing its timezone")
                now = stamp.astimezone(timezone.utc).isoformat()
            previous = info.get("last_successful_drain_at")
            if previous is None or datetime.fromisoformat(previous) < datetime.fromisoformat(now):
                info["last_successful_drain_at"] = now
        else:
            info["last_drain_failure"] = {"at": now, "reason": str(failure)[:1000]}
        conn.execute("""INSERT INTO settings (key, value) VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                     (_drain_key(repository, base), json.dumps(info, separators=(",", ":"))))


def backlog(repository, limit=100, after=0, *, base="main"):
    """Read a consistent bounded page; even an uninitialized store stays read-only."""
    if not 1 <= limit <= 1000 or after < 0:
        raise PipelineError("TAIL backlog requires limit 1..1000 and a nonnegative page key")
    if not isinstance(base, str) or not base:
        raise PipelineError("TAIL backlog requires a base branch")
    result = {
        "repository": repository, "base": base, "discovery": None, "age_seconds": None,
        "oldest_ready_at": None, "last_successful_drain_at": None, "last_drain_failure": None,
        "recheck_due": None,
        "counts": {}, "completions": {}, "candidates": [], "next_after": None,
    }
    with _read_index() as conn:
        if conn is None:
            return result
        checkpoint_row = conn.execute(
            "SELECT * FROM tail_discovery WHERE repository = ?", (repository,),
        ).fetchone()
        if checkpoint_row is None:
            return result
        candidates, has_revision = _projection(conn)
        if has_revision:
            result["recheck_due"], = conn.execute(
                """SELECT count(*) FROM tail_candidates
                   WHERE repository = ? AND base = ? AND classified_revision = revision
                     AND state != 'ready' AND next_check_at <= ?""",
                (repository, base, datetime.now(timezone.utc).isoformat())).fetchone()
        counts = conn.execute(f"""SELECT state, completion_kind, count(*), min(merged_at) FROM ({candidates})
                                  GROUP BY state, completion_kind""", (repository, base))
        for state, kind, count, oldest in counts:
            if state == "resolved":
                result["completions"][kind] = count
            else:
                result["counts"][state] = count
                if state == "ready":
                    result["oldest_ready_at"] = oldest
        rows = conn.execute(f"""SELECT * FROM ({candidates})
                                WHERE state != 'resolved' AND pr_number > ?
                                ORDER BY pr_number LIMIT ?""", (repository, base, after, limit + 1)).fetchall()
        for row in rows[:limit]:
            candidate = _candidate(row)
            del candidate["repository"]
            result["candidates"].append(candidate)
        result.update(_drain_info(conn, repository, base))
        if len(rows) > limit:
            result["next_after"] = rows[limit - 1]["pr_number"]

        # A repository keeps one current checkpoint. Old observations survive
        # a branch or query change; only a compatible scan can attest freshness.
        checkpoint = _checkpoint(checkpoint_row)
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
        progress["uncovered_intervals"] = [
            {"lower": lower, "upper": upper} for lower, upper in intervals[-100:][::-1]
        ]
        result["discovery"] = progress
        completed_at = progress.get("last_complete_at")
        if completed_at:
            completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            elapsed = datetime.now(timezone.utc) - completed
            result["age_seconds"] = max(0, int(elapsed.total_seconds()))
    return result
