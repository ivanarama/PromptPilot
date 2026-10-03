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


def prepare(task, queue, data):
    """Remember pre-launch state, return only unchanged, unexpired exclusions."""
    if queue.get("item_blockers") is not True:
        return []
    states = fingerprints(data, queue)
    if states is None:
        return []
    db.set_setting(MODE + str(task.series_id), "1")
    db.set_setting(BASELINE + str(task.series_id), json.dumps({"task_id": task.id, "states": states, "at": time.time()}))
    try:
        holds = json.loads(db.get_setting(PREFIX + str(task.series_id)) or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(holds, dict):
        return []
    excluded = []
    for number, hold in holds.items():
        if not isinstance(number, str) or not number.isdigit() or not isinstance(hold, dict):
            continue
        current_head = _review_candidate_head(data, int(number)) if number.isdigit() else None
        unchanged = (hold.get("fingerprint") == states.get(number)
                     if "fingerprint" in hold else
                     current_head is not None and hold.get("head") == current_head)
        if (unchanged
                and type(hold.get("at")) in {int, float}
                and 0 <= time.time() - hold["at"] < TTL
                and number.isdigit() and int(number) > 0):
            excluded.append(int(number))
    return sorted(excluded)


def _review_candidate_head(data, number):
    diagnostics = data.get("diagnostics") or {}
    if diagnostics.get("checker_failed"):
        return None
    candidates = diagnostics.get("review_candidates")
    if not isinstance(candidates, list):
        return None
    matching = [item.get("head", "").lower() for item in candidates
                if isinstance(item, dict) and item.get("stage") == "review"
                and item.get("number") == number and isinstance(item.get("head"), str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", item["head"])]
    return matching[0] if len(matching) == 1 else None


def register_review_target(task, number, head):
    """Attach the already-validated exact REVIEW election to this attempt.

    This only enables a negative admission hold. It never grants review or
    mutation authority, and future dispatch still requires a fresh candidate.
    """
    if (not getattr(task, "series_id", None) or type(number) is not int or number <= 0
            or not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head)):
        return
    key = BASELINE + str(task.series_id)
    try:
        baseline = json.loads(db.get_setting(key) or "{}")
    except (TypeError, ValueError):
        baseline = {}
    if baseline.get("task_id") != task.id:
        baseline = {"task_id": task.id, "states": {}, "at": time.time()}
    baseline["review_target"] = {"number": number, "head": head.lower()}
    db.set_setting(MODE + str(task.series_id), "1")
    db.set_setting(key, json.dumps(baseline))


def record(conn, series_id, task_id, verdict, result):
    """Called inside recurrence's existing transaction; return True if held.

    Only explicit unambiguous HUMAN targets with a measured pre-launch state
    qualify. UNABLE and unmeasured/ambiguous failures retain the series guard.
    """
    if str(verdict or "").strip().upper() != "НУЖЕН ЧЕЛОВЕК":
        return False
    mode = conn.execute("SELECT value FROM settings WHERE key=?", (MODE + str(series_id),)).fetchone()
    baseline = conn.execute("SELECT value FROM settings WHERE key=?", (BASELINE + str(series_id),)).fetchone()
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
        if not all(number in states for number in numbers) and not exact_target:
            return False
        row = conn.execute("SELECT value FROM settings WHERE key=?", (PREFIX + str(series_id),)).fetchone()
        holds = json.loads(row["value"]) if row else {}
        if not isinstance(holds, dict):
            return False
    except (TypeError, ValueError, KeyError):
        return False
    # Preserve the initial timestamp: recurrence repair must not extend a hold.
    for number in numbers:
        old = holds.get(number) or {}
        identity = ({"head": target["head"]} if exact_target else
                    {"fingerprint": states[number]})
        if any(old.get(key) != value for key, value in identity.items()):
            holds[number] = {**identity, "at": time.time(),
                             "reason": reason, "task_id": task_id}
    holds = {number: hold for number, hold in holds.items()
             if isinstance(hold, dict) and type(hold.get("at")) in {int, float}
             and time.time() - hold["at"] < TTL}
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (PREFIX + str(series_id), json.dumps(holds, ensure_ascii=False)))
    return True
