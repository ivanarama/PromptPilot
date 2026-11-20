"""Hand an ordinary MERGE's external Actions wait back to the scheduler.

This module reads GitHub. The canonical MERGE procedure owns the diagnosis,
its scheduling and product gates, and the single authorized rerun request.
"""

import json
import math
import os
import re
import sqlite3
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from . import fallback_handoff, merge_checkpoint, project_pipeline as pp


POLL_SECONDS = 120
WAIT_SECONDS = 35 * 60
WAITING = {"waiting", "unknown"}
TERMINAL = {"success", "failed", "timeout", "superseded", "attempt_changed"}
PENDING = {"queued", "in_progress", "waiting", "pending", "requested"}
FAILURES = {"failure", "timed_out", "cancelled"}
WORKFLOW = ".github/workflows/ci.yml"
ORDINARY_STAGES = {"merge", "integration-merge-ready", "legacy-integration-merge-ready"}
UNRESOLVED = {"failed", "timeout", "attempt_changed"}


def _require(condition, reason):
    if not condition:
        raise pp.PipelineError(reason)


def _id(value):
    return type(value) is int and 0 < value < 2**63


def _key(repository, number, head):
    # Independent of the config, workflow, and task. A fresh election cannot
    # issue a second rerun for another run on the same unchanged PR.
    return "pipeline_merge_ci_wait:v1:" + pp.digest([repository, number, head])


def _receipt_key(series_id):
    return f"pipeline_merge_ci_wait:task:v1:{series_id}"


def _get(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def _put(conn, key, record):
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (key, json.dumps(record, sort_keys=True)))


def _load(conn, repository, target):
    number, head = target["number"], target["head"]
    record = _get(conn, _key(repository, number, head))
    if record is not None:
        _require(isinstance(record, dict) and record["version"] == 1
                 and (record["repository"], record["number"], record["head"]) == (repository, number, head)
                 and _id(record["run_id"]) and _id(record["attempt"]) and _id(record["workflow_id"])
                 and re.fullmatch(r"[0-9a-f]{64}", record["config_sha256"])
                 and isinstance(record["base_branch"], str) and record["base_branch"]
                 and record["state"] in WAITING | TERMINAL and math.isfinite(record["deadline"])
                 and (record["claimed_at"] is None or math.isfinite(record["claimed_at"])),
                 "corrupt external CI journal")
    return record


def active_wait(repository, target):
    from . import db

    if not target:
        return None
    with db._connect() as conn:
        record = _load(conn, repository, target)
    # An exact-head election can return after an operator restores ship or a
    # previously closed PR is reopened. Reobserve its run before trusting it.
    return record if record and record["state"] != "success" else None


def series_wait(series_id, repository):
    """An unsigned 'wait' election can reference this journal only for a fresh PR readback."""
    from . import db

    with db._connect() as conn:
        conn.execute("BEGIN")
        hint = _get(conn, f"pipeline_merge_ci_wait:series:v1:{series_id}")
        if not hint or hint["repository"] != repository:
            return None
        record = _load(conn, repository, hint)
    return record if record and record["state"] not in {"success", "superseded"} else None


def _status(gh, record):
    repo, number, head = (record[k] for k in ("repository", "number", "head"))
    prefix = "repos/" + repo
    pr = gh.json("api", f"{prefix}/pulls/{number}")
    base = record["base_branch"]
    _require(pr["number"] == number and pr["base"]["repo"]["full_name"] == repo,
             "Actions observation returned another pull request")
    labels = {item["name"] for item in pr["labels"]}
    if (pr["state"] != "open" or pr["draft"] is not False or pr["merged"] is not False or pr["head"]["sha"] != head
            or pr["base"]["ref"] != base or "ship" not in labels
            or labels & {"hold", "needs-decision", "manual", "changes-requested"}):
        return None
    run = gh.json("api", f"{prefix}/actions/runs/{record['run_id']}")
    head_ref, head_repo = pr["head"]["ref"], pr["head"]["repo"]
    _require(run["id"] == record["run_id"] and _id(run["workflow_id"]) and _id(run["run_attempt"])
             and run["repository"]["full_name"] == repo and run["path"] == WORKFLOW
             and run["event"] in {"pull_request", "workflow_dispatch", "push"}
             and run["head_sha"] == head and run["head_branch"] == head_ref
             and run["head_repository"]["full_name"] == head_repo["full_name"]
             and (not record.get("workflow_id") or record["workflow_id"] == run["workflow_id"]),
             "Actions run does not match this CI workflow, repository and PR head")
    # workflow_dispatch can have a PR association too (#2731). Do not accept
    # an arbitrary workflow which happens to mention the same commit.
    matches = [p for p in run["pull_requests"] if p["number"] == number]
    _require(len(matches) == 1 and matches[0]["head"]["sha"] == head
             and matches[0]["head"]["ref"] == head_ref and matches[0]["base"]["ref"] == base
             and matches[0]["head"]["repo"]["id"] == head_repo["id"]
             and matches[0]["base"]["repo"]["id"] == pr["base"]["repo"]["id"],
             "Actions run lacks the elected PR association")
    _require((run["status"] in PENDING and run["conclusion"] is None)
             or (run["status"] == "completed" and isinstance(run["conclusion"], str) and run["conclusion"]),
             "Actions run has incomplete status")
    return run


def _started(run):
    stamp = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
    _require(stamp.tzinfo is not None, "Actions run start time needs a timezone")
    return stamp.timestamp()


def _observation(record, run, now):
    result = dict(run_id=record["run_id"], attempt=record["attempt"], deadline=record["deadline"])
    if run is None:
        return dict(result, state="superseded")
    if run["run_attempt"] > record["attempt"]:
        return dict(result, state="attempt_changed")
    if run["run_attempt"] < record["attempt"]:
        _require(record["claimed_at"] is not None and run["run_attempt"] == record["attempt"] - 1,
                 "unexpected earlier Actions attempt")
        state = "unknown"
    elif run["status"] == "completed":
        # A terminal fact already on GitHub wins over a late local poll.
        return dict(result, state="success" if run["conclusion"] == "success" else "failed")
    else:
        state = "waiting"
        result["deadline"] = min(result["deadline"], _started(run) + WAIT_SECONDS)
    return dict(result, state="timeout" if now >= result["deadline"] else state)


def observe_run(gh, record, *, now=None):
    """Bounded readback for a scheduler-admitted poll. No task or GitHub writes."""
    run = _status(gh, record)
    return _observation(record, run, time.time() if now is None else now)


def _observed(record, observation):
    _require(observation["state"] in WAITING | TERMINAL
             and observation["run_id"] == record["run_id"] and observation["attempt"] == record["attempt"]
             and math.isfinite(observation["deadline"]) and observation["deadline"] <= record["deadline"],
             "CI readback differs from the saved wait")
    return dict(record, state=observation["state"], deadline=observation["deadline"])


def save_observation(record, observation):
    """Publish only a readback for the record seen before network I/O."""
    from . import db

    updated = _observed(record, observation)
    with db._connect(immediate=True) as conn:
        if _load(conn, record["repository"], record) != record:
            return False
        _put(conn, _key(record["repository"], record["number"], record["head"]), updated)
    return True


def defer_time(task, *, conn=None):
    """Schedule a completed empty turn's next poll while preserving its report and usage."""
    from . import db

    if not getattr(task, "series_id", None) or not getattr(task, "started_at", None):
        return None
    with (nullcontext(conn) if conn is not None else db._connect()) as reader:
        if conn is None:
            reader.execute("BEGIN")
        try:
            notice = _get(reader, _receipt_key(task.series_id))
            if not notice or (notice["task_id"], notice["started_at"]) != (task.id, task.started_at.isoformat()):
                return None
            row = reader.execute("SELECT merge_checkpoint_gate FROM tasks WHERE id=? AND series_id=? "
                                 "AND started_at=? AND status='completed' AND verdict='ПУСТО'",
                                 (task.id, task.series_id, notice["started_at"])).fetchone()
            if not row or row[0] != notice["gate"]:
                return None
            record = _load(reader, notice["repository"], notice)
            if not record or record["state"] not in WAITING or (record["run_id"], record["attempt"]) != (
                    notice["run_id"], notice["attempt"]):
                return None
            when = min(time.time() + POLL_SECONDS, notice["observed_at"] + POLL_SECONDS, record["deadline"])
            return datetime.fromtimestamp(when, timezone.utc)
        except (pp.PipelineError, ValueError, TypeError, KeyError, OverflowError):
            # A damaged optional wake receipt must not disable ordinary recurrence.
            return None


def _check_failure(gh, record, config):
    path = f"repos/{record['repository']}/actions/runs/{record['run_id']}/attempts/1/jobs?per_page=100"
    answer = gh.json("api", path)
    jobs = answer["jobs"]
    _require(isinstance(jobs, list) and len(jobs) == answer["total_count"] <= 100
             and any(j["name"] in config["required_checks"] and j["status"] == "completed"
                     and j["conclusion"] in FAILURES and j["head_sha"] == record["head"]
                     and j["run_id"] == record["run_id"] and j["run_attempt"] == 1 for j in jobs),
             "no failed required check in the exact original attempt")


def adapter_config_path(command):
    _require(command and command[-2:] == ["next", "merge"], "expected the configured MERGE adapter")
    options = command[:-2]
    flags = [index for index, arg in enumerate(options) if arg == "--config" or arg.startswith("--config=")]
    _require(len(flags) <= 1, "ambiguous MERGE adapter config")
    if not flags:
        return "pipelinectl.json"
    index = flags[0]
    if options[index] == "--config":
        _require(index + 1 < len(options), "MERGE adapter config path is missing")
        path = options[index + 1]
    else:
        path = options[index].partition("=")[2]
    _require(path and not path.startswith("-"), "invalid MERGE adapter config path")
    return path


def diagnose(gh, config, config_path, number, head):
    """Elect a signed ordinary fallback after an exact CI run needs investigation."""
    from .review_routing_policy import require_legacy

    def wait(reason, state="unavailable"):
        return {"action": "wait", "number": number, "state": state, "reason": reason}

    try:
        _require(_id(number) and merge_checkpoint.SHA.fullmatch(head), "invalid CI target")
        _require(number not in pp.admission_exclusions() and pp._configured_replica_count() == 1,
                 "CI diagnosis requires an available ordinary MERGE target")
        repo = config["repository"]
        record = active_wait(repo, {"number": number, "head": head})
        _require(record and record["state"] in UNRESOLVED, "no unresolved exact Actions attempt")
        _require(config.get("fallback_handoff") == "target-v1"
                 and record["config_sha256"] == fallback_handoff.config_digest(config),
                 "CI diagnosis config no longer matches its exact Actions journal")
        pp.ensure_identity(gh, config)
        _require(not pp.pending_merge_intents(gh, config), "pending merge cleanup takes precedence")
        health = pp.run_health(config, config_path=config_path)
        _require(fallback_handoff.config_digest(config) == record["config_sha256"],
                 "MERGE configuration changed during diagnosis")
        fallback_handoff.validate_health(health)
        queue = health["merge_executable"]
        _require(queue and queue[0]["number"] == number and queue[0]["head"] == head
                 and queue[0]["stage"] in ORDINARY_STAGES,
                 "the first executable MERGE target changed")
        observed = observe_run(gh, record)
        if observed["state"] != record["state"]:
            _require(save_observation(record, observed), "CI journal changed during diagnosis")
            return wait("Actions readback changed, re-elect before diagnosis", observed["state"])
        require_legacy(gh, config, number)
        _require(active_wait(repo, record) == record
                 and fallback_handoff.config_digest(pp.load_config(config_path)) == record["config_sha256"],
                 "CI journal or configuration changed before the handoff")
        return fallback_handoff.create(config, health, "merge", queue[0],
                                       f"Actions run {record['run_id']} attempt {record['attempt']} requires diagnosis")
    except (RuntimeError, OSError, KeyError, ValueError, TypeError, sqlite3.Error) as exc:
        return wait(str(exc))


def _apply(args, env):
    from . import db, pipeline_insights as pi

    raw = env.get("PP_TASK_ID", "")
    marker = env.get("PP_TASK_STARTED_AT")
    _require(re.fullmatch(r"[1-9][0-9]{0,18}", raw) and _id(int(raw)) and marker
             and _id(args.number) and _id(args.run_id) and _id(args.attempt)
             and merge_checkpoint.SHA.fullmatch(args.head), "invalid CI task or target identity")
    task_id = int(raw)
    task = db.get_task(task_id)
    binding = pi._matching_queue(task) if task else None
    _require(binding and binding[0] == args.profile and binding[2]["id"] == "merge"
             and binding[2].get("item_blockers") is True, "CI wait needs an ordinary managed MERGE")
    profile, queue = binding[1:]
    policy = pi._github_budget_policy(profile)
    execution = queue["execution"]
    _require(policy and policy.get("costs") and str(execution.get("mode", "auto")).lower() == "auto"
             and str(execution.get("stage") or queue["id"]).lower() == "merge"
             and execution.get("merge_confirmation") == "bounded-v1"
             and profile["github_budget"].get("source_cost_protocol") == "source-graphql-cost-v1",
             "CI wait needs costed managed MERGE admission")
    lease, token = merge_checkpoint._gate_file(task_id, args.lease_file)
    repo, target = lease["repository"], lease["target"]
    _require(not lease.get("target_reservation") and repo == profile["repository"]
             and target["stage"] in ORDINARY_STAGES
             and (args.number, args.head) == (target["number"], target["head"]),
             "CI wait target differs from the signed ordinary election")
    command = pi._tool_command(queue["execution"], "merge")
    path = Path(adapter_config_path(command))
    if not path.is_absolute():
        path = Path(task.working_dir or ".").resolve() / path
    config = pp.load_config(str(path))
    gate = pp.digest([marker, token])

    def guard(conn):
        fallback_handoff.validate_lease(lease, "merge")
        _require(pi._matching_queue(task) == binding
                 and fallback_handoff.config_digest(config) == lease["config_sha256"]
                 and fallback_handoff.config_digest(pp.load_config(str(path))) == lease["config_sha256"],
                 "MERGE configuration changed")
        state = db._pipeline_source_refresh_state(conn, policy["lease_scope"], task_id, args.profile, "merge",
                                                 policy["costs"])["guard"]
        row = conn.execute("SELECT merge_checkpoint_gate FROM tasks WHERE id=?", (task_id,)).fetchone()
        _require(state["route"] == "fallback_targeted" and state["started_at"] == marker and row[0] == gate,
                 "CI wait lost its admitted election")
        return state

    with db._connect() as conn:
        conn.execute("BEGIN")
        admitted = guard(conn)
        previous = _load(conn, repo, target)
    claiming = args.operation == "claim"
    if previous:
        _require(lease["config_sha256"] == previous["config_sha256"], "previous CI journal has another config")
        if claiming and previous["claimed_at"] is not None:
            return dict(state="already_claimed", run_id=previous["run_id"], attempt=previous["attempt"])
        if previous["run_id"] != args.run_id:
            _require(previous["state"] in TERMINAL and previous["claimed_at"] is None,
                     "another CI run owns the waiting record")
    same = previous if previous and previous["run_id"] == args.run_id else None
    record = same or dict(version=1, repository=repo, number=args.number, head=args.head, run_id=args.run_id,
                          attempt=args.attempt, config_sha256=lease["config_sha256"], base_branch=config["base_branch"],
                          claimed_at=None, deadline=time.time() + WAIT_SECONDS)
    if same and not claiming:
        _require(args.attempt == same["attempt"], "observe must name the journaled Actions attempt")
    gh = pp.GitHub(timeout_seconds=config["github_timeout_seconds"])
    run = _status(gh, record)
    now = time.time()
    if not run:
        if not same:
            return {"state": "superseded"}
        updated = dict(record, state="superseded")
    elif claiming:
        _require(args.attempt == run["run_attempt"] == 1 and run["status"] == "completed"
                 and run["conclusion"] in FAILURES, "claim requires the failed original attempt")
        _check_failure(gh, record, config)
        now = time.time()
        updated = dict(record, state="unknown", attempt=2, workflow_id=run["workflow_id"],
                       claimed_at=now, deadline=now + WAIT_SECONDS)
    else:
        if not same:
            _require(run["run_attempt"] == args.attempt, "Actions attempt differs from observation")
            record["workflow_id"] = run["workflow_id"]
            record["deadline"] = min(now, _started(run)) + WAIT_SECONDS
            # An existing external attempt > 1 already consumed the allowed rerun.
            if args.attempt > 1:
                record["claimed_at"] = min(now, _started(run))
        updated = _observed(record, _observation(record, run, now))

    with db._connect(immediate=True) as conn:
        _require(guard(conn) == admitted and _load(conn, repo, target) == previous,
                 "MERGE CI record or admission changed concurrently; reread before continuing")
        _put(conn, _key(repo, args.number, args.head), updated)
        _put(conn, f"pipeline_merge_ci_wait:series:v1:{task.series_id}",
             dict(repository=repo, number=args.number, head=args.head))
        if updated["state"] in WAITING:
            _put(conn, _receipt_key(task.series_id),
                 dict(repository=repo, number=args.number, head=args.head, run_id=updated["run_id"],
                      attempt=updated["attempt"], task_id=task_id, started_at=marker, gate=gate, observed_at=now))
        else:
            conn.execute("DELETE FROM settings WHERE key=?", (_receipt_key(task.series_id),))
    granted = claiming and updated["state"] == "unknown"
    return dict(state="claimed" if granted else updated["state"], run_id=updated["run_id"],
                attempt=updated["attempt"], deadline=updated["deadline"], send_allowed=granted)


def add_arguments(parser):
    parser.add_argument("operation", choices=("claim", "observe"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--lease-file", required=True)


def execute(args):
    try:
        result = _apply(args, os.environ)
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError, AttributeError, OSError, sqlite3.Error) as exc:
        result = {"state": "refused", "reason": str(exc)}
    result.setdefault("send_allowed", False)
    result["product_mutation_authorized"] = False
    print(json.dumps(result, ensure_ascii=False))
    return int(result["state"] == "refused")
