"""Bounded, local admission holds; these never grant mutation authority."""
import hashlib
import json
import re
import time

from . import db

PREFIX = "pipeline_item_holds:v1:"
MODE = "pipeline_item_hold_mode:v1:"
BASELINE = "pipeline_item_baseline:v1:"
TTL = 24 * 3600


def complete_members(queue):
    """Admission must use full membership, never the dashboard's short list."""
    if not isinstance(queue, dict) or queue.get("membership_complete") is not True:
        return None
    backlog = queue.get("backlog")
    if type(backlog) is not int or backlog < 0:
        return None
    members = queue.get("admission_items")
    if isinstance(members, list) and len(members) == backlog:
        return members
    # Older compatible caches have no admission projection. Accept the display
    # list only when its size proves it was not truncated; otherwise fall back
    # to the project's fresh target election without exclusions.
    members = queue.get("items")
    if isinstance(members, list) and len(members) == backlog:
        return members
    return None


def fingerprints(data, queue_config=None):
    cache = data.get("cache") or {}
    if cache.get("complete") is not True or cache.get("stale") or cache.get("refresh_blocked"):
        return None
    items = {}
    for queue in data.get("queues") or []:
        for item in complete_members(queue) or []:
            if not isinstance(item, dict) or not item.get("updated_at"):
                continue
            number = item.get("number")
            if type(number) is not int or number <= 0:
                continue
            items[str(number)] = item
    diagnostics = data.get("diagnostics") or {}
    # REVIEW's project-owned election also includes re-review targets that are
    # deliberately absent from the ordinary GitHub search (for example a PR
    # carrying needs-decision after an owner review-again marker). These are
    # still exact, versioned targets. A hold only removes work from admission;
    # it never grants permission to review or mutate a PR.
    stage = str((queue_config or {}).get("id") or "").lower()
    execution = (queue_config or {}).get("execution") or {}
    if isinstance(execution, dict):
        stage = str(execution.get("stage") or stage).lower()
    if stage == "review" and not diagnostics.get("checker_failed"):
        candidates = diagnostics.get("review_candidates")
        if isinstance(candidates, list):
            seen = set()
            ambiguous = set()
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("stage") != "review":
                    continue
                number, head = candidate.get("number"), candidate.get("head")
                if (type(number) is not int or number <= 0
                        or not isinstance(head, str)
                        or not re.fullmatch(r"[0-9a-fA-F]{40}", head)):
                    continue
                key = str(number)
                if key in seen:
                    ambiguous.add(key)
                    continue
                seen.add(key)
                items.setdefault(key, {"number": number, "kind": "pr",
                                       "updated_at": head.lower(), "head": head.lower()})
            for key in ambiguous:
                items.pop(key, None)
    if stage in {"fix", "merge"} and not diagnostics.get("checker_failed"):
        # A completed handoff can move a PR out of this stage's search before
        # the next attempt. Measure all versioned diagnostic PRs as negative
        # admission evidence, including human_waiting and review_candidates.
        # This cannot add a PR to any executable allowlist.
        versions = {}
        for field in ("review_candidates", "content_review_candidates", "review_backlog",
                      "fix_candidates", "merge_candidates", "merge_executable",
                      "human_waiting", "reviewed_waiting_ship"):
            for candidate in diagnostics.get(field) or []:
                if not isinstance(candidate, dict):
                    continue
                number, head = candidate.get("number"), candidate.get("head")
                if (type(number) is int and number > 0 and isinstance(head, str)
                        and re.fullmatch(r"[0-9a-fA-F]{40}", head)):
                    versions.setdefault(str(number), set()).add(head.lower())
        for key, heads in versions.items():
            if len(heads) != 1:
                items.pop(key, None)
                continue
            head = next(iter(heads))
            items.setdefault(key, {"number": int(key), "kind": "pr",
                                   "updated_at": head, "head": head})
    result = {}
    for number, item in items.items():
        witnesses = []
        for field, value in diagnostics.items():
            if isinstance(value, list):
                witnesses.extend((field, row) for row in value if isinstance(row, dict)
                                 and (row.get("number") == int(number) or row.get("pr") == int(number)
                                      or row.get("issue") == int(number)))
        encoded = json.dumps([item, sorted(witnesses, key=lambda pair: json.dumps(pair, sort_keys=True))],
                             sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        result[number] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return result


def prepare(task, queue, data, *, verified_progress=None):
    """Remember pre-launch state, return only unchanged, unexpired exclusions."""
    if queue.get("item_blockers") is not True:
        return []
    states = fingerprints(data, queue)
    if states is None:
        return []
    db.set_setting(MODE + str(task.series_id), "1")
    # FIX is a skill election, not the REVIEW/MERGE tool election. An explicit
    # opt-in permits negative admission evidence for only the fresh checker's
    # measured PR reworks. This never authorizes a write or a broader intake.
    error_states = {}
    diagnostics = data.get("diagnostics") or {}
    if (queue.get("id") == "fix" and queue.get("item_error_holds") is True
            and not diagnostics.get("checker_failed")):
        for item in diagnostics.get("fix_candidates") or []:
            if (isinstance(item, dict) and item.get("stage") == "review"
                    and type(item.get("number")) is int
                    and isinstance(item.get("head"), str)
                    and re.fullmatch(r"[0-9a-fA-F]{40}", item["head"])
                    and str(item["number"]) in states):
                error_states[str(item["number"])] = states[str(item["number"])]
    progress = _progress_facts(verified_progress)
    started = _running_attempt(task)
    baseline = {
        "task_id": task.id, "states": states, "error_states": error_states,
        "canonical_progress": progress if started is not None else {},
        "attempt_started_at": started,
        "at": time.time()}
    db.set_setting(BASELINE + str(task.series_id), json.dumps(baseline))
    if progress and started is not None:
        db.set_setting(_attempt_key(task.id), json.dumps(baseline))
    try:
        holds = json.loads(db.get_setting(PREFIX + str(task.series_id)) or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(holds, dict):
        return []
    excluded, released = [], {}
    for number, hold in holds.items():
        if not isinstance(number, str) or not number.isdigit() or not isinstance(hold, dict):
            continue
        current_head = _candidate_head(data, int(number), hold.get("stage", "review"))
        unchanged = (hold.get("fingerprint") == states.get(number)
                     if "fingerprint" in hold else
                     current_head is not None and hold.get("head") == current_head)
        if unchanged and _progress_available(hold, progress.get(number), task, started):
            from .project_pipeline import digest
            released[number] = digest(hold)
            unchanged = False
        if (unchanged
                and type(hold.get("at")) in {int, float}
                and 0 <= time.time() - hold["at"] < TTL
                and number.isdigit() and int(number) > 0):
            excluded.append(int(number))
    if released:
        key = BASELINE + str(task.series_id)
        baseline["progress_releases"] = released
        db.set_setting(key, json.dumps(baseline))
        if progress and started is not None:
            db.set_setting(_attempt_key(task.id), json.dumps(baseline))
    return sorted(excluded)


def _progress_facts(values):
    from .merge_admission_progress import VerifiedProgress, pp
    facts = {}
    for number, value in (values or {}).items():
        if type(number) is not int or not isinstance(value, VerifiedProgress):
            continue
        try:
            observed = value.facts
            if observed["number"] == number:
                facts[str(number)] = observed
        except pp.PipelineError:
            continue
    return facts


def _running_attempt(task, conn=None):
    """No pending task or executor-supplied string can claim a real attempt."""
    ident, series = getattr(task, "id", None), getattr(task, "series_id", None)
    started = getattr(task, "started_at", None)
    if type(ident) is not int or type(series) is not int or not hasattr(started, "isoformat"):
        return None
    if conn is None:
        with db._connect() as owned:
            return _running_attempt(task, owned)
    row = conn.execute("SELECT status,series_id,started_at FROM tasks WHERE id=?", (ident,)).fetchone()
    if (not row or row["status"] != "running" or row["series_id"] != series
            or row["started_at"] != started.isoformat()):
        return None
    return row["started_at"]


def _progress_available(hold, current, task, started):
    from .merge_admission_progress import advanced
    if (hold.get("verdict") != "НЕ СМОГ"
            or hold.get("last_progress_verdict", "НЕ СМОГ") != "НЕ СМОГ" or hold.get("stage") != "merge"
            or not advanced(hold.get("canonical_progress"), current)):
        return False
    consumed = hold.get("progress_attempt")
    # A pending dispatch may observe availability, but does not consume it.
    # Once consumed, only that exact running occurrence may pass again.
    return consumed is None or (isinstance(consumed, dict)
        and set(consumed) == {"task_id", "started_at", "progress", "at", "hold_sha256"} and started is not None
        and consumed.get("task_id") == getattr(task, "id", None)
        and consumed.get("started_at") == started and consumed.get("progress") == current)


def _attempt_key(task_id):
    return BASELINE + "attempt:" + str(task_id)


def _task_identity(task):
    return all(type(value) is int and value > 0 for value in (
        getattr(task, "id", None), getattr(task, "series_id", None)))


def released_progress(task):
    try:
        baseline = json.loads(db.get_setting(_attempt_key(task.id))
                              or db.get_setting(BASELINE + str(task.series_id)) or "{}")
        releases = baseline.get("progress_releases") or {}
        return dict(releases) if baseline.get("task_id") == task.id and isinstance(releases, dict) else {}
    except (TypeError, ValueError):
        return {}


def progress_attempt_blocked(task, data):
    """A racing second claimed task must stop before even calling next."""
    if not _task_identity(task):
        return False
    from .merge_admission_progress import validate_facts, pp
    try:
        candidates = (data.get("diagnostics") or {}).get("merge_executable") or []
        if len(candidates) != 1 or candidates[0].get("stage") != "integration-merge-recovery":
            return False
        holds = json.loads(db.get_setting(PREFIX + str(task.series_id)) or "{}")
        item = candidates[0]
        hold = holds.get(str(item["number"]))
        if (not isinstance(hold, dict) or hold.get("stage") != "merge" or hold.get("head") != item["head"]
                or type(hold.get("at")) not in {int, float} or not 0 <= time.time() - hold["at"] < TTL):
            return False
        validate_facts(hold.get("canonical_progress"))
        consumed = hold.get("progress_attempt")
        if consumed is None:
            return False
        started = _running_attempt(task)
        return not (isinstance(consumed, dict) and consumed.get("task_id") == task.id
                    and started is not None and consumed.get("started_at") == started)
    except (pp.PipelineError, TypeError, ValueError, KeyError):
        return False


def unproved_progress_hold(task, data, verified_progress):
    """A known held integration owner cannot fall through to next or an LLM.

    Cache rows identify only the negative restriction; positive release still
    requires the separately verified canonical transition. Unrelated targets
    keep their existing election/exclusion policy.
    """
    if not _task_identity(task):
        return False
    try:
        items = (data.get("diagnostics") or {}).get("merge_executable") or []
        if len(items) != 1 or items[0].get("stage") != "integration-merge-recovery":
            return False
        item = items[0]
        holds = json.loads(db.get_setting(PREFIX + str(task.series_id)) or "{}")
        hold = holds.get(str(item["number"]))
        if (not isinstance(hold, dict) or "canonical_progress" not in hold
                or hold.get("stage") != "merge" or hold.get("head") != item["head"]
                or type(hold.get("at")) not in {int, float}
                or not 0 <= time.time() - hold["at"] < TTL):
            return False
        return not _progress_available(hold, _progress_facts(verified_progress).get(str(item["number"])),
                                       task, _running_attempt(task))
    except (TypeError, ValueError, KeyError, AttributeError):
        return True


def claim_progress_attempt(task, verified_progress, released):
    """Consume a proven transition atomically BEFORE potentially mutating next.

    No refund follows a crash, wrong election or unknown result. The task's
    normal exact-target election and every project gate remain independent.
    """
    current = _progress_facts(verified_progress)
    if not current:
        return not verified_progress and not released
    from .merge_admission_progress import advanced
    from .project_pipeline import digest
    if {n for n, v in current.items() if v["phase"] == "pending-base-sync"} != set(released):
        return False
    with db._connect(immediate=True) as conn:
        started = _running_attempt(task, conn)
        if started is None:
            return False
        key = PREFIX + str(task.series_id)
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        try:
            holds = json.loads(row["value"]) if row else {}
        except (TypeError, ValueError):
            return False
        if not isinstance(holds, dict):
            return False
        changed = False
        for number in released:
            fact = current[number]
            hold = holds.get(number)
            if (not isinstance(hold, dict) or hold.get("head") != fact["head"]
                    or hold.get("stage") != "merge" or hold.get("verdict") != "НЕ СМОГ"
                    or type(hold.get("at")) not in {int, float}
                    or not 0 <= time.time() - hold["at"] < TTL
                    or not advanced(hold.get("canonical_progress"), fact)):
                return False
            expected = released[number]
            consumed = hold.get("progress_attempt")
            same_consumed = (isinstance(consumed, dict) and consumed.get("task_id") == task.id
                and consumed.get("started_at") == started and consumed.get("progress") == fact
                and consumed.get("hold_sha256") == expected
                and digest({k: v for k, v in hold.items() if k != "progress_attempt"}) == expected)
            if digest(hold) != expected and not same_consumed:
                return False
            if not _progress_available(hold, fact, task, started):
                return False
            if hold.get("progress_attempt") is None:
                hold["progress_attempt"] = {"task_id": task.id, "started_at": started,
                                             "progress": fact, "at": time.time(), "hold_sha256": expected}
                changed = True
        if changed:
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                         (key, json.dumps(holds, ensure_ascii=False)))
    return True


def _candidate_head(data, number, stage):
    diagnostics = data.get("diagnostics") or {}
    if diagnostics.get("checker_failed"):
        return None
    candidates = diagnostics.get("review_candidates" if stage == "review" else "merge_executable")
    if not isinstance(candidates, list):
        return None
    matching = [item.get("head", "").lower() for item in candidates
                if isinstance(item, dict) and (item.get("stage") == "review"
                                               if stage == "review" else True)
                and item.get("number") == number and isinstance(item.get("head"), str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", item["head"])]
    return matching[0] if len(matching) == 1 else None


def register_review_target(task, number, head):
    """Attach the already-validated exact REVIEW election to this attempt.

    This only enables a negative admission hold. It never grants review or
    mutation authority, and future dispatch still requires a fresh candidate.
    """
    register_target(task, number, head, "review")


def register_target(task, number, head, stage, *, config_sha256=None):
    """Remember a validated tool election, never an executor's claimed target."""
    if (stage not in {"review", "merge"}
            or not getattr(task, "series_id", None) or type(number) is not int or number <= 0
            or not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head)):
        return
    key = BASELINE + str(task.series_id)
    try:
        baseline = json.loads(db.get_setting(_attempt_key(task.id)) or db.get_setting(key) or "{}")
    except (TypeError, ValueError):
        baseline = {}
    if baseline.get("task_id") != task.id:
        baseline = {"task_id": task.id, "states": {}, "at": time.time()}
    if stage == "merge":
        measured = (baseline.get("canonical_progress") or {}).get(str(number))
        if (not isinstance(measured, dict) or measured.get("head") != head.lower()
                or measured.get("config_sha256") != config_sha256):
            baseline["canonical_progress"] = {}
    baseline["review_target"] = {"number": number, "head": head.lower(), "stage": stage}
    db.set_setting(MODE + str(task.series_id), "1")
    db.set_setting(key, json.dumps(baseline))
    if db.get_setting(_attempt_key(task.id)):
        db.set_setting(_attempt_key(task.id), json.dumps(baseline))


def _hold_matches_identity(hold, identity):
    # Older exact REVIEW holds omitted stage; prepare() treats them as review.
    return all(hold.get(key, "review" if key == "stage" else None) == value
               for key, value in identity.items())


def _baseline_progress(conn, measured, target, task_id, series_id):
    """Only facts measured before next for this actual claimed occurrence."""
    from .merge_admission_progress import validate_facts, pp
    value = (measured.get("canonical_progress") or {}).get(str(target.get("number")))
    try:
        validate_facts(value)
    except (pp.PipelineError, ValueError, TypeError):
        return None
    row = conn.execute("SELECT series_id,started_at FROM tasks WHERE id=?", (task_id,)).fetchone()
    if (not row or row["series_id"] != series_id or not row["started_at"]
            or measured.get("attempt_started_at") != row["started_at"]
            or target.get("stage") != "merge" or value["head"] != target.get("head")
            or value["number"] != target.get("number")):
        return None
    return value


def record(conn, series_id, task_id, verdict, result):
    """Called inside recurrence's existing transaction; return True if held.

    Explicit HUMAN targets require measured state. UNABLE requires an exact tool
    election or an opted-in, fresh checker-measured FIX rework. Unscoped failures
    retain the series guard; an executor cannot add a target to its baseline.
    """
    outcome = str(verdict or "").strip().upper()
    if outcome not in {"НУЖЕН ЧЕЛОВЕК", "НЕ СМОГ"}:
        return False
    mode = conn.execute("SELECT value FROM settings WHERE key=?", (MODE + str(series_id),)).fetchone()
    baseline = (conn.execute("SELECT value FROM settings WHERE key=?", (_attempt_key(task_id),)).fetchone()
                or conn.execute("SELECT value FROM settings WHERE key=?", (BASELINE + str(series_id),)).fetchone())
    if not mode or mode["value"] != "1" or not baseline:
        return False
    details = db._pipeline_blocker_details(verdict, result)
    if not details:
        return False
    reason = details["reason"]
    numbers = set(re.findall(r"(?<![\w#])#([1-9][0-9]*)\b", reason))
    if not numbers:
        return False
    if len(numbers) > 1:
        entries = reason.split(";")
        matches = [re.match(r"^\s*#([1-9][0-9]*)\s+[—–-]\s+\S", entry) for entry in entries]
        if len(entries) != len(numbers) or not all(matches) or {m[1] for m in matches} != numbers:
            return False
    try:
        measured = json.loads(baseline["value"])
        if measured.get("task_id") != task_id or not 0 <= time.time() - measured["at"] < TTL:
            return False
        states = measured["states"]
        target = measured.get("review_target") or {}
        exact_target = (len(numbers) == 1
                        and str(target.get("number")) in numbers
                        and isinstance(target.get("head"), str)
                        and re.fullmatch(r"[0-9a-f]{40}", target["head"]))
        if target and not exact_target:
            return False
        measured_error = (len(numbers) == 1
                          and measured.get("error_states", {}).get(next(iter(numbers)))
                          == states.get(next(iter(numbers)))
                          and states.get(next(iter(numbers))) is not None)
        if outcome == "НЕ СМОГ" and not (exact_target or measured_error):
            return False
        if not all(number in states for number in numbers) and not exact_target:
            return False
        row = conn.execute("SELECT value FROM settings WHERE key=?", (PREFIX + str(series_id),)).fetchone()
        holds = json.loads(row["value"]) if row else {}
        if not isinstance(holds, dict):
            return False
    except (TypeError, ValueError, KeyError):
        return False
    progress = _baseline_progress(conn, measured, target, task_id, series_id) if exact_target else None
    if progress:
        number = next(iter(numbers))
        old = holds.get(number)
        consumed = old.get("progress_attempt") if isinstance(old, dict) else None
        if (isinstance(consumed, dict) and consumed.get("task_id") == task_id
                and consumed.get("started_at") == measured.get("attempt_started_at")
                and consumed.get("progress") == progress
                and type(old.get("at")) in {int, float} and 0 <= time.time() - old["at"] < TTL):
            # Consume once, retaining the original hold's age, identity and reason.
            # A second recurrence repair of this same occurrence is idempotent.
            old.update(canonical_progress=progress, last_progress_task_id=task_id,
                       last_progress_verdict=outcome, last_progress_reason=reason)
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                         (PREFIX + str(series_id), json.dumps(holds, ensure_ascii=False)))
            return True
    for number in numbers:
        old = holds.get(number) or {}
        identity = ({"head": target["head"], "stage": target.get("stage", "review")}
                    if exact_target else {"fingerprint": states[number]})
        if (isinstance(old, dict)
                and _hold_matches_identity(old, identity)
                and old.get("task_id") not in (None, task_id)
                and type(old.get("at")) in {int, float}
                and 0 <= time.time() - old["at"] < TTL):
            # A different occurrence re-elected a still-held measured target.
            # Exact elections and fingerprint exclusions retain the same guard.
            return False
    # Preserve the initial timestamp: recurrence repair must not extend a hold.
    for number in numbers:
        old = holds.get(number) or {}
        identity = ({"head": target["head"], "stage": target.get("stage", "review")} if exact_target else
                    {"fingerprint": states[number]})
        live = (isinstance(old, dict) and type(old.get("at")) in {int, float}
                and 0 <= time.time() - old["at"] < TTL)
        old = old if isinstance(old, dict) else {}
        if (not _hold_matches_identity(old, identity)
                or (old.get("task_id") != task_id and not live)):
            holds[number] = {**identity, "at": time.time(),
                             "reason": reason, "task_id": task_id}
            if progress and outcome == "НЕ СМОГ":
                holds[number].update(canonical_progress=progress, verdict=outcome)
    holds = {number: hold for number, hold in holds.items()
             if isinstance(hold, dict) and type(hold.get("at")) in {int, float}
             and time.time() - hold["at"] < TTL}
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (PREFIX + str(series_id), json.dumps(holds, ensure_ascii=False)))
    return True
