"""Resume private evidence for an elected ordinary OneBase FIX before its first push.

No operation contacts GitHub or grants publication or test credit.
"""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


SHA = re.compile("[0-9a-f]{40}")
DIGEST = re.compile("[0-9a-f]{64}")
REPO = "ivanarama/onebase"
WAIT = {"scan_in_progress", "rate_snapshot_stale", "cache_invalidated"}
FLAGS = {"publication_authorized": False, "acceptance_reuse_authorized": False}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def private(root, name):
    p = Path(name)
    require(p.is_absolute() and p.resolve(strict=True) == p and p.is_relative_to(root) and p.is_file(),
            "expected a regular file in the private FIX evidence directory")
    return p


def read_json(root, name):
    return json.loads(private(root, name).read_bytes())


def unique(path, value):
    """Publish complete bytes once, without overwriting a competing producer."""
    require(path.resolve() == path, "unexpected receipt path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encode(value))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True
    finally:
        os.unlink(temp)


def same_or_new(path, value):
    if not unique(path, value):
        require(path.read_bytes() == encode(value), "conflicting evidence for this election")


def task_env(root, *, series_id=2):
    task = os.environ.get("PP_TASK_ID", "")
    started = os.environ.get("PP_TASK_STARTED_AT")
    require(re.fullmatch("[1-9][0-9]{0,18}", task), "requires the native FIX worker environment")
    if not started:
        with closing(sqlite3.connect((root.parent / "promptpilot.db").as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT started_at FROM tasks WHERE id=? AND series_id=? AND status='running'",
                               (int(task), series_id)).fetchone()
            require(row and row[0], "ordinary FIX task is not running")
            started = row[0]
    return int(task), started


def native(root, task, started, *, origin=None, source_binding=None, series_id=2, working_dir=None):
    """Bind the private hint to an actual local invocation, and preserve the normal guards."""
    database = root.parent / "promptpilot.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND series_id=?", (task, series_id)).fetchone()
        series = conn.execute("SELECT * FROM task_series WHERE id=?", (series_id,)).fetchone()
        directory = str(working_dir or root.parent.parent / "Projects/onebase-automation/fix")
        require(series and series["title"] == "OneBase - FIX - исправление одобренного"
                and series["working_dir"] == directory and row and row["working_dir"] == directory
                and row["started_at"] == started and (row["session_id"] or row["herdr_pane"])
                and not row["machine"] and not row["detached"], "wrong native FIX occurrence")
        if origin is not None:
            lines = (row["result"] or "").partition("--- Meta ---")[0].strip().splitlines()
            require(row["status"] == "completed" and row["verdict"] == "ПУСТО" and lines
                    and lines[-1].strip() in {f"ИТОГ: ПУСТО — нет допуска: {origin}",
                        f"ИТОГ: ПУСТО (нет допуска: {origin}; кандидат сохранён)",
                        f"ИТОГ: ПУСТО (нет допуска: {origin}; исправление сохранено)"},
                    "origin did not finish in a clean pre-send scan wait")
            return
        now = time.time()
        start = datetime.fromisoformat(started)
        require(row["status"] == "running" and not series["paused"] and not series["ended_at"]
                and start.tzinfo is not None and 0 <= now - start.timestamp() < row["task_timeout"]
                and not conn.execute("SELECT 1 FROM settings WHERE key IN ('worker_paused',?) AND value='1'",
                                     (f"cancel_task:{task}",)).fetchone(), "native FIX is no longer active")
        prefix = "pipeline_github_budget_reservations:v1:"
        own = []
        for rowset in conn.execute("SELECT key,value FROM settings WHERE substr(key,1,?)=?", (len(prefix), prefix)):
            payload = json.loads(rowset["value"])
            require(payload["version"] == 1, "unexpected native reservation format")
            own.extend((rowset["key"], r) for r in payload["reservations"] if r["task_id"] == task)
        require(len(own) == 1 and own[0][0] == prefix + digest(b"github-default")
                and own[0][1]["task_started_at"] == started and own[0][1]["profile_id"] == "onebase"
                and own[0][1]["queue_id"] == "fix" and own[0][1]["route"] == "skill", "native FIX reservation changed")
        # A different session or edit to any task execution input invalidates the ticket.
        execution_keys = ("prompt", "working_dir", "provider", "model", "effort", "rights", "skip_permissions",
                "machine", "detached", "herdr_target", "worktree", "worktree_path", "worktree_branch",
                "task_timeout")
        if source_binding is not None:
            # SQLite stores the execution booleans as integers, as in task_execution_fingerprint.
            execution = json.dumps({k: row[k] for k in execution_keys}, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":")).encode()
            bound = [task, started, row["session_id"], row["herdr_pane"], own[0][1]["token"],
                     digest(execution), row["series_id"], row["priority"]]
            require(digest(encode(bound)) == source_binding, "reused Source belongs to another FIX attempt")
        keys = (*execution_keys, "priority", "session_id", "herdr_pane", "started_at")
        return digest(encode([{k: row[k] for k in keys}, own[0][1]]))


def priority_scan_deferred(cache):
    budget = cache.get("refresh_budget")
    return (cache.get("refresh_blocked") == "priority_waiter" and isinstance(budget, dict)
            and budget.get("budget_route") == "insights" and budget.get("state") == "priority_waiter"
            and budget.get("allowed") is False)


def valid_deferred_receipt(source):
    if "deferred_refresh" not in source:
        return True
    deferred = source["deferred_refresh"]
    return (source.get("state") == "reused" and isinstance(deferred, dict)
            and set(deferred) == {"refresh_blocked", "refresh_budget"} and priority_scan_deferred(deferred)
            and set(deferred["refresh_budget"]) == {"state", "budget_route", "allowed"})


def source_token(source, task, started):
    token = source["snapshot_token"]
    cache = source["cache"]
    reused = source["state"] == "reused"
    active = (source["state"] == "refreshed" and source["source_refreshed"] is True
              and not source.get("source_reused") and cache["source"] == "live")
    if reused:
        stamp, observed = token["generated_at"], source["observed_at"]
        start = datetime.fromisoformat(started)
        require(start.tzinfo and type(stamp) in (int, float) and type(observed) in (int, float)
                and start.timestamp() <= observed <= time.time() and 0 <= observed - stamp < 300
                and source["source_refreshed"] is False and source["source_reused"] is True
                and cache["source"] == "durable" and source["mutable_admission_granted"] is False
                and source["existing_execution_reservation_reused"] is True
                and DIGEST.fullmatch(source["attempt_binding_sha256"]), "invalid reused FIX Source receipt")
    require((active or reused)
            and source["native_session_bound"] is True and source["existing_skill_reservation_reused"] is True
            and source["admitted_route"] == "skill" and source["queue_id"] == "fix"
            and source["profile_id"] == "onebase" and source["task_id"] == task
            and source["task_started_at"] == started and source["product_mutation_authorized"] is False
            and source["scope"] == "Native-source-observation-only"
            and cache["available"] is True and cache["complete"] is True and cache["stale"] is False
            and cache["invalidated"] is False and not cache.get("refresh_blocked") and valid_deferred_receipt(source)
            and cache["token"] == token and cache["generated_at"] == token["generated_at"]
            and cache["ttl_seconds"] == 300 and cache["profile_hash"] == token["profile_hash"]
            and DIGEST.fullmatch(token["profile_hash"])
            and all(type(token[k]) is int and token[k] >= 0 for k in ("epoch", "revision")),
            "expected this ordinary FIX attempt's successful native Source receipt")
    return token


def check_source_election(source, election, task, started, *, passive=False):
    if source["state"] == "reused":
        require(source["fix_pr_heads"].get(str(election["number"])) == election["head"]
                and election.get("task_id") == task and election.get("task_started_at") == started
                and (passive or election.get("snapshot_token") == source["snapshot_token"]),
                "current PR, HEAD and FIX election must match the reused Source")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def read_cached_fix_queue():
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open("http://127.0.0.1:8420/api/pipeline-insights/onebase?refresh=false", timeout=10) as response:
        return json.load(response)


def current_fix_token(pr, head=None, *, report=None, priority_read=None):
    if report is None:
        report = read_cached_fix_queue()
    cache = report["cache"]
    latest = cache["token"]
    require(report["repository"] == REPO and report["profile_id"] == "onebase"
            and cache["source"] in ("memory", "durable") and cache["available"] is True and cache["complete"] is True
            and cache["stale"] is False and cache["invalidated"] is False
            and (not cache.get("refresh_blocked") or (priority_read and priority_read(report)))
            and cache["ttl_seconds"] == 300 and cache["generated_at"] == latest["generated_at"]
            and 0 <= time.time() - latest["generated_at"] < 300, "current FIX cache is unavailable or stale")
    require(latest["profile_hash"] == cache["profile_hash"], "current FIX cache profile changed")
    queues = [q for q in report["queues"] if q["id"] == "fix"]
    require(len(queues) == 1 and queues[0]["membership_complete"] is True, "incomplete FIX queue")
    items = [i for i in queues[0]["admission_items"] if i["kind"] == "pr" and i["number"] == pr]
    require(len(items) == 1 and items[0]["key"] == f"pr:{pr}"
            and items[0]["url"] == f"https://github.com/{REPO}/pull/{pr}"
            and "changes-requested" in items[0]["labels"] and not {"ship", "hold", "manual"}.intersection(items[0]["labels"]),
            "PR is no longer admitted to the FIX queue")
    if head is not None:
        diagnostics = report["diagnostics"]
        heads = [(item.get("head") or item.get("source_head")) for item in diagnostics["fix_candidates"]
                 if item["stage"] in ("review", "fix-pr") and item["number"] == pr]
        require(diagnostics.get("exit_code") == 0 and not diagnostics.get("errors")
                and not diagnostics.get("checker_failed") and heads == [head]
                and all(q["membership_complete"] is True for q in report["queues"]),
                "current FIX diagnostics do not confirm the elected HEAD")
    return latest


def passive_records(root, task, started):
    return root.parent / "onebase-fix-passive-observations" / f"{task}-{digest(started.encode())[:24]}"


def passive_result(root, source_bytes, original, task, started, guard):
    """Read the immutable result of the one observation opened by this attempt."""
    records = passive_records(root, task, started)
    binding = {"task": task, "started": started, "native": guard,
               "source_sha256": digest(source_bytes), "original": original}
    require(read_json(root.parent, records / "observe.intent.json") == binding,
            "passive Source does not belong to this FIX occurrence")
    witness = read_json(root.parent, records / "observe.result.json")
    require(witness["binding"] == binding and witness["publication_authorized"] is False,
            "passive Source does not belong to this FIX occurrence")
    return witness["fresh_snapshot"]


def passive_source_token(root, source_file, original, task, started, guard, election, pr):
    """Confirm the task's single passive observation against its current election and the live local cache."""
    source_bytes = private(root, source_file).read_bytes()
    source = json.loads(source_bytes)
    require(source_token(source, task, started) == original, "original Source receipt changed")
    observed = passive_result(root, source_bytes, original, task, started, guard)
    latest = current_fix_token(pr, election["head"] if source["state"] == "reused" else None)
    require(isinstance(observed, dict)
            and original["profile_hash"] == observed["profile_hash"] == latest["profile_hash"]
            and all(type(token[k]) is int for k in ("epoch", "revision") for token in (observed, latest))
            and all(original[k] <= observed[k] <= latest[k] for k in ("epoch", "revision", "generated_at")),
            "passive Source does not continue this occurrence's original snapshot")
    require(election.get("task_id") == task and election.get("task_started_at") == started
            and election.get("snapshot_token") == latest, "repeat the FIX election on the current snapshot")
    require(native(root, task, started) == guard, "FIX execution or reservation changed during passive readback")
    return latest


def budget_now(budget, task, started, profile, now, *, series_id=2):
    require(budget["budget_observation_scope"] == "running-budget-policy-v4" and budget["proof_kind"] == "running"
            and budget["state"] == "ready" and budget["ready"] is True
            and budget["profile_hash"] == profile and budget["task_id"] == task
            and budget["started_at"] == started and budget["profile_id"] == "onebase"
            and budget["queue_id"] == "fix" and budget["series_id"] == series_id
            and budget["decision"]["allowed"] is True and budget["decision"]["budget_route"] == "skill"
            and budget["native_admission_granted"] is False and budget["mutable_admission_granted"] is False
            and budget["receipt_id"] and 0 <= now - budget["rate_observed_at"] < 60
            and budget["rate_observed_at"] <= budget["generated_at"] <= now,
            "expected the current ready budget checkpoint for this FIX attempt")


def selected(data, pr, source):
    """Stable selection across producers; additional service metadata is not part of election identity."""
    decision = data["decision_id"]
    proof = data["proof"]
    canonical = {k: proof[k] for k in ("review_id", "review_node", "claim_id", "claim_node",
                                         "completion_id", "completion_node", "outcome")}
    require(type(pr) is int and pr > 0 and SHA.fullmatch(source) and data["number"] == pr
            and data["head"] == source and data["owner"] == "FIX" and DIGEST.fullmatch(data["epoch"])
            and all(type(canonical[k]) is int and canonical[k] > 0 for k in ("review_id", "claim_id", "completion_id"))
            and all(isinstance(canonical[k], str) and canonical[k] for k in (
                "review_node", "claim_node", "completion_node", "outcome"))
            and ((decision is None and data["decision_sha256"] is None)
                 or (re.fullmatch("[1-9][0-9]*", str(decision)) and DIGEST.fullmatch(data["decision_sha256"]))),
            "incomplete ordinary FIX election")
    return {"number": pr, "head": source, "owner": "FIX", "epoch": data["epoch"], "proof": canonical,
            "decision_id": None if decision is None else str(decision), "decision_sha256": data["decision_sha256"]}


def refs(pr_data, number, source):
    head, base = pr_data["head"], pr_data["base"]
    require(pr_data["number"] == number and pr_data["state"] == "open" and not pr_data["merged"]
            and base["repo"]["full_name"] == REPO and base["ref"] == "main" and SHA.fullmatch(base["sha"])
            and head["sha"] == source and head["repo"]["full_name"] and head["ref"]
            and (head["repo"]["full_name"] == REPO or pr_data.get("maintainer_can_modify") is True),
            "PR identity, protected base or formal source differs")
    return {"fork": head["repo"]["full_name"], "branch": head["ref"], "base": base["sha"]}


def evidence(root, outcome_path):
    outcome_path = private(root, outcome_path)
    folder = outcome_path.parent
    o = read_json(root, outcome_path)
    pr, src, head = o["pr"], o["source"], o["candidate"]
    require(o["state"] == "availability_wait" and o["stage"] == "fix-approved" and o["reason"] in WAIT
            and o["pushed"] is False and o["github_mutations"] is False and SHA.fullmatch(head)
            and head != src and SHA.fullmatch(o["tree"]), "not an unsent FIX candidate waiting for budget")
    # Fixed sibling inputs come from the original private run, never the PR body.
    manifest = private(folder, o["acceptance_manifest"])
    bundle = private(folder, o["bundle"])
    selection_path = private(folder, o.get("selection", folder / "selected-target.json"))
    source_receipt = private(folder, o.get("source_receipt", folder / "source.stdout"))
    stale_path = private(folder, o.get("precheck_budget", folder / "precheck-budget.json"))
    m = read_json(folder, manifest)
    require((m["source"], m["head"], m["tree"]) == (src, head, o["tree"]), "candidate manifest differs")
    files = m["files"]
    require(isinstance(files, dict) and all(str(p) in files for p in (
        bundle, selection_path, source_receipt)), "required manifest entries missing")
    for name, expected in files.items():
        require(DIGEST.fullmatch(expected) and file_hash(private(folder, name)) == expected, "acceptance artifact changed")
    git = "/usr/bin/git" if os.name == "posix" else "git"
    advertised = subprocess.check_output([git, "bundle", "list-heads", str(bundle)], cwd=folder, timeout=30)
    heads = [line.split(maxsplit=1)[0] for line in advertised.decode().splitlines()]
    require(heads and set(heads) == {head}, "bundle does not advertise the exact checked candidate")
    election = read_json(folder, selection_path)
    sel = selected(election, pr, src)
    attempt = o.get("attempt") or read_json(folder, folder / "attempt.json")
    task, started = attempt["task_id"], attempt["started_at"]
    require(re.fullmatch("[1-9][0-9]{0,18}", str(task)) and started and attempt["scope"] == "fix-approved"
            and attempt["github_mutations"] is False, "unexpected originating attempt")
    task = int(task)
    initial_source = read_json(folder, source_receipt)
    token = source_token(initial_source, task, started)
    check_source_election(initial_source, election, task, started)
    budget = read_json(folder, stale_path)
    stop_files = [stale_path]
    if o["reason"] == "cache_invalidated":
        budget_now(budget, task, started, token["profile_hash"], budget["generated_at"])
        source_wait = [private(folder, o[k]) for k in ("source_wait", "source_cache", "observer_start", "observer_exit")]
        observation, source, start, status = (read_json(folder, p) for p in source_wait)
        cache = source["cache"]
        require(status == 3 and str(start["task"]) == str(task) and start["started_at"] == started
                and type(start["seconds"]) is int and 1 <= start["seconds"] <= 300
                and observation["fresh_snapshot"] is None and observation["publication_authorized"] is False
                and source["repository"] == REPO and source["profile_id"] == "onebase"
                and cache["complete"] is True and cache["invalidated"] is True
                and cache.get("refresh_blocked") in (None, "", "scan_in_progress")
                and cache["profile_hash"] == token["profile_hash"]
                and not source["diagnostics"].get("checker_failed") and source["diagnostics"]["exit_code"] == 0,
                "missing completed, task-bound passive wait for an invalidated Source")
        stop_files += source_wait
    else:
        require(budget["state"] == "refused" and budget["ready"] is False and budget["reason"] == "rate_snapshot_stale"
                and budget["task_id"] == task and budget["started_at"] == started and budget["profile_id"] == "onebase"
                and budget["queue_id"] == "fix" and budget["profile_hash"] == token["profile_hash"],
                "missing original stale budget checkpoint")
        if o["reason"] == "scan_in_progress":
            wait_path = private(folder, o["budget_observation"])
            wait = read_json(folder, wait_path)
            require(wait["state"] == "wait" and wait["scope"] == "Native-budget-observation-only"
                    and wait["admitted_route"] == "skill" and wait["budget"]["allowed"] is False
                    and wait["budget"]["state"] == o["reason"] and wait["budget"]["budget_route"] == "insights"
                    and wait["product_mutation_authorized"] is False and wait["rate_snapshot_refreshed"] is False,
                    "missing typed, definite pre-send budget wait")
            stop_files.append(wait_path)
    pr_path = private(folder, o.get("pr_read", folder / f"pr-{pr}.json"))
    branches = refs(read_json(folder, pr_path), pr, src)
    return {"outcome": str(outcome_path), "manifest": str(manifest), "bundle": str(bundle),
            "source": src, "candidate": head, "tree": o["tree"], "pr": pr, "origin_task": task,
            "origin_started": started, "wait_reason": o["reason"], "selection": sel, "refs": branches, "source_token": token,
            **({"source_binding": initial_source["attempt_binding_sha256"]} if initial_source["state"] == "reused" else {}),
            "evidence_sha256": {str(p): file_hash(p) for p in (
                outcome_path, manifest, pr_path, *stop_files)}}


def directory(root, selection):
    return root / "pre-push-v1" / str(selection["number"]) / selection["head"] / digest(encode(selection))


def capture(root, args, task, started):
    """Write the reusable pointer only after the original attempt proves it never entered send."""
    native(root, task, started)
    manifest = read_json(root, args.manifest)
    election = read_json(root, args.selection)
    reason = (read_json(root, args.budget_wait)["budget"]["state"] if args.budget_wait
              else "cache_invalidated" if args.source_wait else "rate_snapshot_stale")
    require(bool(args.source_wait) == bool(args.source_cache) == bool(args.observer_start) == bool(args.observer_exit),
            "passive Source wait requires the original stdout, final cache, start record and exit status")
    require(not args.budget_wait or reason == "scan_in_progress", "unsupported final Native budget wait")
    outcome = {"state": "availability_wait", "stage": "fix-approved", "reason": reason,
               "pr": election["number"], "source": manifest["source"], "candidate": manifest["head"],
               "tree": manifest["tree"], "pushed": False, "github_mutations": False,
               "attempt": {"task_id": task, "started_at": started, "scope": "fix-approved", "github_mutations": False},
               "acceptance_manifest": str(args.manifest), "bundle": str(args.bundle), "selection": str(args.selection),
               "source_receipt": str(args.source_receipt), "precheck_budget": str(args.precheck_budget),
               "pr_read": str(args.pr_read)}
    for field in ("budget_wait", "source_wait", "source_cache", "observer_start", "observer_exit"):
        if getattr(args, field):
            outcome["budget_observation" if field == "budget_wait" else field] = str(getattr(args, field))
    args.outcome = args.manifest.parent / "fix-prepush-outcome.json"
    same_or_new(args.outcome, outcome)
    return record(root, args, task, started)


def record(root, args, task, started):
    native(root, task, started)
    data = evidence(root, args.outcome)
    if "source_binding" in data:
        native(root, task, started, source_binding=data["source_binding"])
    require((data["origin_task"], data["origin_started"]) == (task, started), "outcome belongs to another attempt")
    folder = directory(root, data["selection"])
    require(not (folder.parent / "push-attempt.json").exists(), "push already claimed, reconcile by readback")
    path = folder / "attempts" / f"{task}.json"
    same_or_new(path, data)
    return {"state": "recorded", "record": str(path), **FLAGS}


def seed(root, args):
    require(not any(os.environ.get(k) for k in ("PP_TASK_ID", "PP_TASK_STARTED_AT")),
            "seeding requires the release operator session")
    data = evidence(root, args.outcome)
    require(DIGEST.fullmatch(args.legacy_sha256) and digest(encode(data)) == args.legacy_sha256,
            "operator-pinned legacy evidence digest differs")
    native(root, data["origin_task"], data["origin_started"], origin=data["wait_reason"])
    folder = directory(root, data["selection"])
    require(not (folder.parent / "push-attempt.json").exists(), "push already claimed, reconcile by readback")
    for path in (folder / "attempts").glob("*.json"):
        incumbent = read_json(root, path)
        require(incumbent["candidate"] == data["candidate"] and incumbent["tree"] == data["tree"],
                "conflicting existing candidate at operator import")
    same_or_new(folder / "record.json", data)
    return {"state": "recorded", "candidate": data["candidate"], "record": str(folder / "record.json"), **FLAGS}


def matching_record(root, folder, selection, task, started, token, current):
    """Retain interrupted attempts for audit without blocking a later fully proven capture."""
    paths = list((folder / "attempts").glob("*.json"))
    seed = folder / "record.json"
    if seed.exists():
        paths.append(seed)
    choices = {}
    errors = []
    for path in paths:
        try:
            data = read_json(root, path)
            origin = data["origin_task"]
            require(type(origin) is int and origin < task and data["origin_started"] != started
                    and (path == seed or path.name == f"{origin}.json") and data["selection"] == selection,
                    "stored election or task identity differs")
            native(root, origin, data["origin_started"], origin=data["wait_reason"])
            previous = data["source_token"]
            require(token["profile_hash"] == previous["profile_hash"] and token["epoch"] >= previous["epoch"]
                    and token["revision"] > previous["revision"] and token["generated_at"] > previous["generated_at"],
                    "a later native FIX election and Source observation are required")
            require((current["fork"], current["branch"]) == (data["refs"]["fork"], data["refs"]["branch"]),
                    "fork or ref changed")
            require(data == evidence(root, data["outcome"]), "original checked evidence changed")
        except (OSError, TypeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            continue
        key = data["candidate"], data["tree"]
        if key not in choices or choices[key][1]["origin_task"] < origin:
            choices[key] = path, data
    require(len(choices) <= 1, "conflicting verified candidates for this election; reconcile before publication")
    if choices:
        return next(iter(choices.values()))
    if errors:
        raise ValueError("no completed candidate with verified evidence: " + errors[0])
    return None


def read(root, args, task, started):
    source = read_json(root, args.source_receipt)
    token = source_token(source, task, started)
    guard = native(root, task, started, source_binding=source.get("attempt_binding_sha256") if source["state"] == "reused" else None)
    election = read_json(root, args.selection)
    selection = selected(election, args.pr, args.source)
    folder = directory(root, selection)
    passive = getattr(args, "passive_source", False)
    check_source_election(source, election, task, started, passive=passive)
    if passive:
        token = passive_source_token(root, args.source_receipt, token, task, started, guard, election, args.pr)
    elif source["state"] == "reused":
        require(current_fix_token(args.pr, args.source) == token
                and native(root, task, started, source_binding=source["attempt_binding_sha256"]) == guard,
                "reused Source or FIX attempt changed since observation")
    else:
        require(0 <= time.time() - token["generated_at"] < 300, "current election requires fresh Source")
    budget_now(read_json(root, args.budget_receipt), task, started, token["profile_hash"], time.time())
    current = refs(read_json(root, args.pr_read), args.pr, args.source)
    intent = folder.parent / "push-attempt.json"
    if intent.exists():
        return {"state": "reconcile", "prior_claim": read_json(root, intent), **FLAGS}
    match = matching_record(root, folder, selection, task, started, token, current)
    if match is None:
        return {"state": "missing", **FLAGS}
    record_path, data = match
    ticket = folder / f"task-{task}.json"
    receipt = {"task": task, "started": started, "native_guard": guard, "profile_hash": token["profile_hash"],
               "record": str(record_path), "record_sha256": digest(encode(data)), "current_base": current["base"]}
    same_or_new(ticket, receipt)
    return {"state": "candidate", **{k: data[k] for k in ("pr", "source", "candidate", "tree", "bundle", "manifest")},
            "previous_pr_base": data["refs"]["base"], "current_pr_base": current["base"],
            "pr_base_changed": current["base"] != data["refs"]["base"], "ticket": str(ticket), **FLAGS}


def claim(root, args, task, started):
    ticket = private(root, args.ticket)
    txn = read_json(root, ticket)
    data = read_json(root, txn["record"])
    folder = directory(root, data["selection"])
    require(ticket == folder / f"task-{task}.json" and txn["task"] == task and txn["started"] == started
            and Path(txn["record"]) in {folder / "record.json", folder / "attempts" / f"{data['origin_task']}.json"}
            and txn["record_sha256"] == digest(encode(data)),
            "resume ticket is not bound to this exact election")
    checkout = args.worktree.resolve(strict=True)
    git = "/usr/bin/git" if os.name == "posix" else "git"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    def g(*cmd):
        return subprocess.check_output([git, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
                                        *cmd], cwd=checkout, env=env, timeout=30).decode().strip()
    require(Path(g("rev-parse", "--show-toplevel")).resolve() == checkout
            and g("show", "-s", "--no-show-signature", "--format=%H %P %T", "HEAD").split() == [data["candidate"], data["source"], data["tree"]]
            and not g("status", "--porcelain", "--untracked-files=normal"), "restored worktree or commit differs")
    require(native(root, task, started) == txn["native_guard"], "FIX execution or reservation changed")
    budget_now(read_json(root, args.budget_receipt), task, started, txn["profile_hash"], time.time())
    # From this point a push may have happened. Every later attempt reconciles the remote ref.
    require(unique(folder.parent / "push-attempt.json", {"task": task, "candidate": data["candidate"],
                                                          "ticket": str(ticket)}), "push already claimed, reconcile by readback")
    return {"state": "claimed", "candidate": data["candidate"], **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    save = sub.add_parser("record")
    save.add_argument("--outcome", type=Path, required=True)
    new = sub.add_parser("capture", description=(
        "Capture a checked ordinary FIX whose first push was definitely never attempted. All input files must be "
        "under the same private run directory as the manifest. Manifest JSON contains source, head, tree and files "
        "(absolute paths mapped to SHA256), including the bundle, election, native Source receipt and all original "
        "acceptance journals. Normalize the actual canonical election into number, head, owner='FIX', epoch, "
        "decision_id, decision_sha256 and proof. Use a positive decimal decision ID (integer or string) and "
        "its SHA256, or JSON null for both when the election has no separate owner decision. The proof object "
        "contains review_id, review_node, claim_id, claim_node, completion_id, completion_node and outcome. "
        "Use canonical numeric IDs and original node IDs; preserve the review identity on every election. "
        "Attach the complete current REST PR and this task's successful native Source response. A sole stale "
        "budget checkpoint requires no wait flag. A subsequent terminal budget-refresh scan_in_progress requires "
        "--budget-wait. For a ready budget and exhausted passive Source wait, provide --source-wait plus "
        "--source-cache, --observer-start and --observer-exit. Finish the ordinary terminal result with "
        "'ИТОГ: ПУСТО — нет допуска: REASON', using rate_snapshot_stale, scan_in_progress or cache_invalidated."))
    new.add_argument("--before-send", action="store_true", required=True)
    for key in ("manifest", "bundle", "selection", "source-receipt", "pr-read", "precheck-budget"):
        new.add_argument("--" + key, type=Path, required=True)
    stops = new.add_mutually_exclusive_group()
    stops.add_argument("--budget-wait", type=Path, help="original terminal budget-refresh JSON")
    stops.add_argument("--source-wait", type=Path, help="stdout JSON from the exhausted permitted passive observer")
    for key in ("source-cache", "observer-start", "observer-exit"):
        new.add_argument("--" + key, type=Path, help="required with --source-wait")
    bootstrap = sub.add_parser("seed", description="Operator-only import after reconciling complete prior send history.")
    bootstrap.add_argument("--outcome", type=Path, required=True)
    bootstrap.add_argument("--legacy-sha256", required=True)
    resume = sub.add_parser("read")
    resume.add_argument("--passive-source", action="store_true", help=(
        "Use this occurrence's recorded one-time passive observation and repeated election. "
        "Bind selection.task_id, task_started_at and snapshot_token to the current cached FIX queue."))
    resume.add_argument("--pr", type=int, required=True)
    resume.add_argument("--source", required=True)
    for key in ("selection", "source-receipt", "budget-receipt", "pr-read"):
        resume.add_argument("--" + key, type=Path, required=True)
    send = sub.add_parser("claim-push")
    for key in ("ticket", "worktree", "budget-receipt"):
        send.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = Path.home() / ".promptpilot" / "fix-runs"
        require(root.resolve() == root, "unexpected private FIX directory")
        actions = {"capture": capture, "record": record, "read": read, "claim-push": claim}
        result = seed(root, args) if args.action == "seed" else actions[args.action](root, args, *task_env(root))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "refused", "reason": str(exc), **FLAGS}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
