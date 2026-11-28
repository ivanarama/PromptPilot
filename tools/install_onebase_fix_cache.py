"""Install the initial Source observation for an ordinary scheduled OneBase FIX.

Run from the reviewed PromptPilot checkout. Each release has an immutable config and
helpers. Only the series and its never-started occurrences get new instructions.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile


TOOLS = Path(__file__).resolve().parent
PRODUCTION_BUDGET_SHA = "4581b1924a8daf50e68ff41cc00f6c3f454810b440aa88e110c833ab0c0149fd"
TITLE = "OneBase - FIX - исправление одобренного"
V1_FILES = (
    "onebase_fix_cache.py",
    "onebase_fix_cache_guard.py",
    "onebase_fix_cache_observe.py",
    "onebase_fix_prepush.py",
    "onebase_fix_postpush.py",
)
FILES = (*V1_FILES, "source_graphql_cost.py", "onebase_fix_lint_source.py")
BEGIN, END = "<!-- onebase-fix-cache -->", "<!-- /onebase-fix-cache -->"
OLD_BEGIN = "<!-- onebase-fix-source-binding-20261126 -->"
OLD_END = "<!-- /onebase-fix-source-binding-20261126 -->"
DIGEST = re.compile(r"[0-9a-f]{64}")
ROOT = "onebase-fix-cache"
STATE = "onebase_fix_cache_install:v1:"
LIVE = "status IN ('pending','running','rate_limited')"
NEVER_STARTED = (
    "status='pending' AND started_at IS NULL AND session_id IS NULL "
    "AND herdr_pane IS NULL AND retry_count=0 AND COALESCE(machine,'')='' AND NOT detached"
)


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def pin(path, digest, *, executable=False, worker_owned=False):
    p = Path(path).expanduser().resolve(strict=True)
    need(
        p.is_file() and (not executable or os.access(p, os.X_OK)),
        f"expected executable or regular file: {p}",
    )
    if worker_owned and os.name == "posix":
        need(p.stat().st_uid == os.geteuid(), f"worker must own the Native executable: {p}")
    value = hashlib.sha256()
    with p.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    need(
        isinstance(digest, str) and DIGEST.fullmatch(digest) and value.hexdigest() == digest,
        f"SHA-256 mismatch: {p}",
    )
    return {"path": str(p), "sha256": digest}


def connection(data_dir, write=False):
    path = (Path(data_dir) / "promptpilot.db").resolve(strict=True)
    c = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def series(c, working_dir):
    rows = c.execute(
        "SELECT id,title,prompt,paused,working_dir FROM task_series WHERE title=? "
        "AND working_dir=? AND ended_at IS NULL",
        (TITLE, str(working_dir)),
    ).fetchall()
    need(
        len(rows) == 1,
        "expected exactly one ordinary OneBase FIX series in this working directory",
    )
    return rows[0]


def check_profile(path, expected=None):
    from promptpilot.pipeline_budget_status import policy_sha256
    from promptpilot.source_graphql_cost import health_contract

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    profiles = payload.get("profiles", payload)
    profile = profiles["onebase"]
    owners = [
        (name, queue["id"])
        for name, configured in profiles.items()
        for queue in configured["queues"]
        if queue.get("series_contains") and queue["series_contains"].casefold() in TITLE.casefold()
    ]
    need(
        profile["repository"] == "ivanarama/onebase"
        and profile["priority_control"]["trusted_account"] == "ivanarama"
        and owners == [("onebase", "fix")]
        and profile["github_budget"].get("source_cost_protocol") == "source-graphql-cost-v1",
        "configured OneBase costed FIX profile required",
    )
    health_contract(profile)
    actual = policy_sha256(profiles)
    need(
        expected is None or expected == actual,
        f"configured budget policy differs from approved SHA-256 (actual {actual})",
    )
    return actual


def bootstrap(args):
    """Create a fresh single-queue profile and its first paused recurrence."""
    data = args.data_dir.expanduser().resolve()
    work = args.working_dir.expanduser().resolve(strict=True)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["PP_DATA_DIR"] = str(data)
    profiles_path = data / "pipeline_profiles.json"
    need(
        Path(os.environ.get("PP_PIPELINE_PROFILES", profiles_path)).resolve() == profiles_path,
        "bootstrap requires the default profile path in the selected data directory",
    )
    contract = pin(work / ".claude/skills/review-queue/SKILL.md", args.review_sha256)
    skill = pin(work / ".claude/skills/fix-approved/SKILL.md", args.fix_sha256)
    checker = pin(args.checker, args.checker_sha256, executable=True)
    budget = json.loads(args.budget.read_text(encoding="utf-8-sig"))
    need(
        budget.get("source_cost_protocol") == "source-graphql-cost-v1",
        "supply a reviewed costed github_budget object",
    )
    command = [
        checker["path"],
        "-json",
        "-transport",
        "graphql",
        "-technical-handoff",
        "-owner",
        "ivanarama",
        "-repo",
        "ivanarama/onebase",
        "-contract",
        contract["path"],
    ]
    excludes = " -label:hold -label:manual"
    profile = {
        "title": "OneBase",
        "repository": "ivanarama/onebase",
        "target_clear_hours": 8,
        "priority_control": {"trusted_account": "ivanarama"},
        "github_budget": budget,
        "queues": [
            {
                "id": "fix",
                "title": "FIX",
                "capacity": 1,
                "series_contains": "OneBase - FIX",
                "queries": [
                    "is:pr is:open label:changes-requested -label:ship" + excludes,
                    "is:issue is:open label:ready-fix -label:in-work -label:needs-decision -label:plan-needed -label:plan-in-review"
                    + excludes,
                    "is:issue is:open label:approved -label:in-work -label:plan-needed -label:plan-in-review"
                    + excludes,
                ],
                "backlog_diagnostic_field": "fix_candidates",
            }
        ],
        "health_check": {
            "command": command,
            "working_dir": str(work),
            "timeout_seconds": 180,
            "cache_seconds": 300,
            "source_graphql_cost": {
                "protocol": "source-graphql-cost-v1",
                "executable_sha256": checker["sha256"],
                "contract_sha256": contract["sha256"],
            },
        },
    }
    prompt = (
        TITLE
        + "\nRead CLAUDE.md in the OneBase checkout in full. Run the ordinary FIX procedure at "
        + shlex.quote(skill["path"])
        + " after verifying its SHA-256 "
        + skill["sha256"]
        + ". Obtain a current task-bound Source at the managed checkpoint before the ordinary election. "
        "Follow every live ownership, hold, budget and mutation gate in the procedure."
    )
    raw = json_bytes({"profiles": {"onebase": profile}})
    # Link an already-complete file, so concurrent setup never publishes a partial
    # profile and an existing operator configuration cannot be overwritten.
    from promptpilot import db
    from promptpilot.models import TaskCreate

    need(
        db.parse_recurrence(args.recurrence) is not None and args.task_timeout > 0,
        "invalid recurrence or timeout",
    )
    with tempfile.TemporaryDirectory(dir=data) as tmp:
        src = Path(tmp) / "profile.json"
        src.write_bytes(raw)
        budget_sha = check_profile(src)
        src.chmod(0o600)
        with db._connect(immediate=True) as conn:
            rows = conn.execute("SELECT * FROM task_series").fetchall()
            need(
                not rows
                or (
                    len(rows) == 1
                    and rows[0]["prompt"] == prompt
                    and rows[0]["paused"]
                    and rows[0]["working_dir"] == str(work)
                    and rows[0]["base_recurrence"] == args.recurrence
                    and rows[0]["task_timeout"] == args.task_timeout
                    and rows[0]["priority"] == 1
                ),
                "bootstrap is for a fresh data directory; use prepare with your existing profile",
            )
            if profiles_path.exists():
                need(
                    profiles_path.read_bytes() == raw,
                    "existing profile differs; use prepare for an existing installation",
                )
            else:
                os.link(src, profiles_path)
            if rows:
                sid = rows[0]["id"]
            else:
                task = db._insert_task(
                    conn,
                    TaskCreate(
                        prompt=prompt,
                        working_dir=str(work),
                        provider="codex",
                        priority=1,
                        recurrence=args.recurrence,
                        task_timeout=args.task_timeout,
                    ),
                )
                sid = task.series_id
                conn.execute("UPDATE task_series SET paused=1 WHERE id=?", (sid,))
    return {
        "state": "paused",
        "series_id": sid,
        "profile": str(profiles_path),
        "budget_policy_sha256": budget_sha,
    }


def prepare(args):
    from tools.onebase_fix_lint_source import discover_scope

    data, work = (
        args.data_dir.expanduser().resolve(strict=True),
        args.working_dir.expanduser().resolve(strict=True),
    )
    need(
        work.is_dir() and data.is_dir(),
        "expected existing PromptPilot and OneBase directories",
    )
    with closing(connection(data)) as c:
        row = series(c, work)
    profile_path = Path(os.environ.get("PP_PIPELINE_PROFILES", data / "pipeline_profiles.json")).resolve(
        strict=True
    )
    check_profile(profile_path, args.budget_policy_sha256)
    native = pin(args.native, args.native_sha256, executable=True, worker_owned=True)
    need(
        bool(args.cache_native) == bool(args.cache_native_sha256),
        "cache Native path and SHA must be supplied together",
    )
    cached = (
        pin(args.cache_native, args.cache_native_sha256, executable=True, worker_owned=True)
        if args.cache_native
        else native
    )
    need(
        bool(args.legacy_runtime_guard) == bool(args.legacy_runtime_guard_sha256),
        "legacy guard path and SHA must be supplied together",
    )
    caps = subprocess.run(
        [cached["path"], "pipelinectl", "source-refresh", "--help"],
        capture_output=True,
        timeout=60,
        check=True,
    )
    need(
        b"--reuse-fix-cache-only" in caps.stdout and b"--allow-deferred-insights-read" in caps.stdout,
        "cache Native does not implement priority-aware read-only FIX observation",
    )
    caps = subprocess.run(
        [cached["path"], "pipelinectl", "budget-status", "--help"],
        capture_output=True,
        timeout=60,
        check=True,
    )
    need(
        b"--policy-sha256" in caps.stdout and b"--series-id" in caps.stdout,
        "cache Native does not implement the task-bound running budget check",
    )
    # Python sources have the same Git bytes in checkouts using LF or CRLF.
    sources = {name: TOOLS / name for name in FILES}
    sources["source_graphql_cost.py"] = TOOLS.parent / "promptpilot" / "source_graphql_cost.py"
    assets = {name: path.read_text(encoding="utf-8").encode("utf-8") for name, path in sources.items()}
    python = Path(sys.executable).resolve(strict=True)
    config = {
        "version": 2,
        "data_dir": str(data),
        "working_dir": str(work),
        "series_id": row["id"],
        "profiles_path": str(profile_path),
        "budget_policy_sha256": args.budget_policy_sha256,
        "python": str(python),
        "native": native,
        "cache_native": cached,
        "files": {name: sha(raw) for name, raw in assets.items()},
    }
    if args.worker_plist:
        config["worker_plist"] = str(args.worker_plist.expanduser().resolve(strict=True))
    if args.legacy_runtime_guard:
        config["legacy_runtime_guard"] = pin(args.legacy_runtime_guard, args.legacy_runtime_guard_sha256)
    lint_scope = discover_scope(row["prompt"])
    if lint_scope is not None:
        config["lint_source_scope"] = lint_scope
    assets["config.json"] = json_bytes(config)
    sign = sha(assets["config.json"])
    parent = data / ROOT
    parent.mkdir(mode=0o700, exist_ok=True)
    need(parent.resolve() == parent, "unexpected release directory")
    target = parent / sign
    scratch = data / "fix-runs"
    scratch.mkdir(mode=0o700, exist_ok=True)
    need(scratch.resolve() == scratch, "unexpected private FIX evidence directory")
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=parent) as tmp:
            stage = Path(tmp) / "release"
            stage.mkdir(mode=0o700)
            for name, raw in assets.items():
                f = stage / name
                with f.open("xb") as fd:
                    fd.write(raw)
                    fd.flush()
                    os.fsync(fd.fileno())
                if os.name == "posix":
                    f.chmod(0o400)
            try:
                stage.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    conf = target / "config.json"
    read_release(conf, sign)
    return {"config": str(conf), "config_sha256": sign, "series_id": row["id"]}


def read_release(conf, sign):
    need(
        DIGEST.fullmatch(sign or "") and conf.is_absolute() and conf.resolve(strict=True) == conf,
        "expected pinned absolute config",
    )
    raw = conf.read_bytes()
    need(sha(raw) == sign, "release config changed")
    cfg = json.loads(raw)
    layouts = {1: V1_FILES, 2: FILES}
    need(
        type(cfg["version"]) is int and cfg["version"] in layouts
        and conf == Path(cfg["data_dir"]) / ROOT / sign / "config.json"
        and set(cfg["files"]) == set(layouts[cfg["version"]]),
        "release layout does not match the config",
    )
    check_profile(Path(cfg["profiles_path"]), cfg["budget_policy_sha256"])
    for name, digest in cfg["files"].items():
        f = conf.parent / name
        need(
            f.resolve(strict=True) == f and sha(f.read_bytes()) == digest,
            f"installed helper changed: {name}",
        )
    for name in ("native", "cache_native", "legacy_runtime_guard"):
        if name in cfg:
            info = cfg[name]
            need(
                pin(
                    info["path"],
                    info["sha256"],
                    executable=name != "legacy_runtime_guard",
                    worker_owned=name != "legacy_runtime_guard",
                )
                == info,
                f"release dependency changed: {name}",
            )
    return cfg


def note(cfg, conf, sign):
    command = lambda script: shlex.join(
        [
            cfg["python"],
            "-IB",
            str(conf.with_name(script)),
            "--config",
            str(conf),
            "--config-sha",
            sign,
        ]
    )
    return "\n".join(
        [
            BEGIN,
            f"<!-- pp:onebase-fix-cache config-sha256={sign} -->",
            "For this ordinary FIX, keep all repository, owner, target, runtime and budget checks. "
            "At the first permitted task-bound Source request, verify the SHA-256 of the launcher "
            f"({cfg['files'][FILES[0]]}) and config ({sign}). Run in the original worker session and "
            "save the original stdout, stderr and exit status in the task's private fix-runs directory:",
            command(FILES[0]),
            *(["A reused receipt may record that a background insights scan yielded to a priority waiter. "
               "Use the validated snapshot only within this FIX observation. The scan remains deferred."]
              if cfg["version"] >= 2 else []),
            "An active refreshed result follows the existing Source procedure. On state=reused with "
            "source_reused=true and source_refreshed=false run the unchanged ordinary election once. "
            "Record the task ID, actual start and snapshot_token in its normalized selection. Obtain the live "
            "unfiltered REST PR and current remote ref. Before PR work or claiming any attempt, run:",
            command(FILES[1]) + " --source-receipt FILE --selection FILE --pr-read FILE",
            "A later publication of the same PR and HEAD may bind through this attempt's successful passive "
            "observation and a fresh matching ordinary election. Keep its original Source bytes. "
            "Only the current elected PR and matching HEAD can continue. A ready observation gives no test credit "
            "or permission to write. If the selected issue lacks a PR HEAD, an eligible PR changed HEAD, or "
            "the snapshot no longer binds the same eligible selection, preserve the original receipt. "
            "Use the existing permission and budget for at most one full refresh by running:",
            command(FILES[0]) + " --refresh-after-reused FILE --selection FILE --target-read FILE",
            "Use the actual selection and fresh unfiltered REST target. Recheck the same target and all original "
            "gates after refresh. A refusal, timeout, ambiguous send or ineligible target keeps its ordinary stop. "
            "A second election, full refresh or new owner permission cannot be inferred from this result.",
            "At an existing ordinary passive Source checkpoint, after a successful observation by this task, "
            "use this recorder once within the remaining allowed window. It obtains the current task-bound budget locally:",
            command(FILES[2]) + " --source-receipt FILE --seconds N",
            "The recorder can additionally check an existing --budget-receipt FILE. Use the original task and publication timestamp. Repeat live Source, budget, ownership, target and "
            "ref checks before any mutation. Existing target-specific owner conditions and narrower limits apply.",
            "If an existing ordinary post-push policy independently permits its own observation, use its "
            "dedicated observer only at that post-push checkpoint with the same eligibility and time limits. "
            f"Verify its helper SHA-256 ({cfg['files'][FILES[4]]}) and run:",
            shlex.join(
                [
                    cfg["python"],
                    "-IB",
                    str(conf.with_name(FILES[4])),
                    "observe",
                ]
            )
            + " --source-receipt FILE --seconds N",
            END,
        ]
    )


def binding(prompt):
    found = []
    for first, last in ((BEGIN, END), (OLD_BEGIN, OLD_END)):
        if first not in prompt and last not in prompt:
            continue
        need(
            prompt.splitlines().count(first) == prompt.splitlines().count(last) == 1
            and prompt.count(first) == prompt.count(last) == 1,
            "ambiguous FIX Source markers",
        )
        start = prompt.index(first)
        end = prompt.index(last) + len(last)
        need(end > start, "invalid FIX Source block")
        found.append(prompt[start:end])
    need(len(found) <= 1, "multiple initial FIX bindings")
    return found[0] if found else None


def changes_for(prompt, before, after):
    edits = []
    need(binding(prompt) == before, "pending and series Source bindings differ")
    if before is None:
        edits.append(["", "\n\n" + after])
    else:
        edits.append([before, after])
    # Only the ordinary commands are replaced. Keep every following guard and each
    # head-specific owner section intact. Save the exact replaced prose for rollback.
    late = "use the managed ordinary passive Source command above with the original Source receipt and live budget check"
    rules = (
        (
            "After all local checks and an initial successful task-bound Native Source publication",
            r"verify [^\n]+?/wait_cache\.py SHA256 [0-9a-f]{64} and invoke the normal pinned Python [^\n]+?--seconds <1 to 300>",
        ),
        (
            "Use at most one passive observation across these two checkpoints in this attempt.",
            r"Verify [^\n]+?/wait_cache\.py against SHA256 [0-9a-f]{64}\. Run it with the pinned Python [^\n]+?--seconds <1 to 300 within the remaining task, lease and operational deadline>",
        ),
    )
    for prefix, pattern in rules:
        lines = [x for x in prompt.splitlines() if x.startswith(prefix) and "/wait_cache.py" in x]
        if lines:
            need(len(lines) == 1, "ambiguous ordinary passive Source instructions")
            matches = re.findall(pattern, lines[0])
            need(
                len(matches) == 1,
                "unrecognized ordinary passive Source call; review the operator binding",
            )
            edits.append(
                [
                    matches[0],
                    late
                    + (
                        " for the prepublication check"
                        if prefix.startswith("After")
                        else " for the election or handoff check"
                    ),
                ]
            )
    postpush = "[Ordinary OneBase FIX post-push recovery v1]"
    old = "use once 'observe --source-receipt FILE --seconds N' (1..300 within the remaining task and lease budget)"
    new = "use once the dedicated post-push observer in the managed Source block (1..300 within the remaining task and lease budget)"
    postpush_lines = [x for x in prompt.splitlines() if x.startswith(postpush) and old in x]
    if postpush_lines:
        need(len(postpush_lines) == 1, "ambiguous ordinary post-push observation")
        edits.append([old, new])
    return edits


def replace(prompt, edits, undo=False):
    for old, new in reversed(edits) if undo else edits:
        if not old and not undo:
            prompt += new
            continue
        source, dest = (new, old) if undo else (old, new)
        need(
            prompt.count(source) == 1,
            "managed Source instructions changed; review before updating",
        )
        prompt = prompt.replace(source, dest, 1)
    return prompt


def manage(action, data, work, sign, *, cfg=None, conf=None, adopt=None):
    from tools.onebase_fix_lint_source import discover_scope, scoped_bindings, subject_line

    write = action in {"apply", "rollback"}
    with closing(connection(data, write)) as c:
        c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            row = series(c, work)
            sid = row["id"]
            need(cfg is None or cfg["series_id"] == sid, "schedule identity changed")
            key = STATE + str(sid)
            stored = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            state = json.loads(stored[0]) if stored else {"version": 1, "history": [], "exempt": {}}
            need(state.get("version") == 1, "unknown installer state")
            hist = state["history"]
            top = hist[-1] if hist else None
            current = binding(row["prompt"])
            if top:
                need(current == top["block"], "installed scheduler binding changed")
            live = c.execute(
                f"SELECT id,prompt,working_dir,({NEVER_STARTED}) AS editable FROM tasks "
                f"WHERE series_id=? AND {LIVE} ORDER BY id",
                (sid,),
            ).fetchall()
            need(
                all(t["working_dir"] == str(work) for t in live),
                "unexpected active FIX working directory",
            )
            pending = {
                t["id"]: t["prompt"] for t in live if t["editable"] and str(t["id"]) not in state["exempt"]
            }
            if top and top.get("lint_paragraph_sha256"):
                for prompt in (row["prompt"], *pending.values()):
                    line = subject_line(prompt)
                    need(line and sha(line.encode()) == top["lint_paragraph_sha256"],
                         "installed scoped lint instructions changed")
            if action == "rollback":
                if not top or top["sha"] != sign:
                    need(
                        state.get("rolled_back") == sign,
                        "this release is not the active installer binding",
                    )
                    return {"state": "rolled_back", "series_id": sid}
                edits = top["edits"]
                updated = replace(row["prompt"], edits, True)
                updates = {i: replace(p, edits, True) for i, p in pending.items()}
            else:
                target = note(cfg, conf, sign)
                if cfg["version"] >= 2:
                    need(discover_scope(row["prompt"]) == cfg.get("lint_source_scope"),
                         "scoped lint instructions changed since preparation")
                if top and top["sha"] == sign:
                    need(current == target, "release instructions changed")
                    replace(row["prompt"], top["edits"], True)
                    for t in live:
                        actual = binding(t["prompt"])
                        allowed = state["exempt"].get(str(t["id"]))
                        need(
                            actual == target or (str(t["id"]) in state["exempt"] and actual == allowed),
                            f"FIX task {t['id']} did not inherit the current binding",
                        )
                        if actual == target:
                            replace(t["prompt"], top["edits"], True)
                            if cfg["version"] >= 2:
                                need(discover_scope(t["prompt"]) == cfg.get("lint_source_scope"),
                                     "live FIX scoped lint instructions differ from the release")
                    return {"state": "verified", "series_id": sid, "changed": False}
                need(action != "verify", "release is not applied")
                if current and not top:
                    old_sha = sha(current.encode())
                    if action == "preview" and not adopt:
                        return {
                            "state": "adoption_required",
                            "series_id": sid,
                            "replace_block_sha": old_sha,
                        }
                    need(
                        adopt == old_sha,
                        "existing Source binding requires its exact --replace-block-sha",
                    )
                edits = changes_for(row["prompt"], current, target)
                if cfg["version"] >= 2:
                    edits.extend(scoped_bindings(row["prompt"], conf, sign))
                elif top and top.get("lint_paragraph_sha256"):
                    raise ValueError("use rollback to restore the previous scoped lint instructions")
                updated = replace(row["prompt"], edits)
                updates = {}
                for i, p in pending.items():
                    # A pending per-task note may contain unrelated material. Refuse an
                    # independent override inside the managed block and preserve all else.
                    need(
                        binding(p) == current,
                        f"pending FIX {i} has a different Source binding",
                    )
                    if cfg["version"] >= 2:
                        need(discover_scope(p) == cfg.get("lint_source_scope"),
                             f"pending FIX {i} has different scoped lint instructions")
                    updates[i] = replace(p, edits)
                if action == "preview":
                    return {
                        "state": "preview",
                        "series_id": sid,
                        "pending": list(updates),
                        "template_sha256": sha(updated.encode()),
                    }
            # The worker can read a series for recurrence and insert its successor in
            # two transactions. A live occurrence fences the interval between them.
            need(
                live,
                "FIX is between occurrences; retry after the scheduler creates its successor",
            )
            for i, text in updates.items():
                old = pending[i]
                need(
                    c.execute(
                        "UPDATE tasks SET prompt=? WHERE id=? AND series_id=? AND prompt=? AND "
                        + NEVER_STARTED,
                        (text, i, sid, old),
                    ).rowcount
                    == 1,
                    "pending FIX changed during install",
                )
            need(
                c.execute(
                    "UPDATE task_series SET prompt=?,updated_at=? WHERE id=? AND ended_at IS NULL "
                    "AND prompt=?",
                    (
                        updated,
                        datetime.now(timezone.utc).isoformat(),
                        sid,
                        row["prompt"],
                    ),
                ).rowcount
                == 1,
                "FIX template changed during install",
            )
            exempt = {}
            for t in live:
                if t["id"] not in updates:
                    exempt[str(t["id"])] = binding(t["prompt"])
            state["exempt"] = exempt
            if action == "rollback":
                hist.pop()
                state["rolled_back"] = sign
            else:
                entry = {"sha": sign, "block": target, "edits": edits}
                if cfg.get("lint_source_scope"):
                    entry["lint_paragraph_sha256"] = sha(subject_line(updated).encode())
                hist.append(entry)
                state.pop("rolled_back", None)
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(state, ensure_ascii=False)),
            )
            c.commit()
            return {
                "state": "applied" if action == "apply" else "rolled_back",
                "series_id": sid,
                "pending": list(updates),
                "unchanged_active": [int(i) for i in exempt],
                "template_sha256": sha(updated.encode()),
            }
        finally:
            if c.in_transaction:
                c.rollback()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    seed = commands.add_parser(
        "bootstrap", help="create only a blank Mac data directory's paused ordinary FIX"
    )
    seed.add_argument("--checker", type=Path, required=True)
    seed.add_argument("--checker-sha256", required=True)
    seed.add_argument("--review-sha256", required=True)
    seed.add_argument("--fix-sha256", required=True)
    seed.add_argument("--budget", type=Path, required=True, help="reviewed github_budget JSON object")
    seed.add_argument("--recurrence", required=True)
    seed.add_argument("--task-timeout", type=int, default=7200)
    build = commands.add_parser(
        "prepare",
        help="check the configured FIX series and materialize its immutable release",
    )
    undo = commands.add_parser(
        "rollback",
        help="restore the previous FIX binding, even if the new executable is unavailable",
    )
    for op in (seed, build, undo):
        op.add_argument(
            "--working-dir",
            type=Path,
            required=True,
            help="working directory of the recurring OneBase FIX",
        )
        op.add_argument(
            "--data-dir",
            type=Path,
            default=Path(os.environ.get("PP_DATA_DIR", Path.home() / ".promptpilot")),
        )
    build.add_argument("--native", type=Path, required=True, help="active worker executable")
    build.add_argument("--native-sha256", required=True)
    build.add_argument(
        "--cache-native",
        type=Path,
        help="optional separately qualified cache-reading executable",
    )
    build.add_argument("--cache-native-sha256")
    build.add_argument(
        "--legacy-runtime-guard",
        type=Path,
        help="optional existing worker-attestation module",
    )
    build.add_argument("--legacy-runtime-guard-sha256")
    build.add_argument("--worker-plist", type=Path)
    build.add_argument("--budget-policy-sha256", default=PRODUCTION_BUDGET_SHA)
    undo.add_argument("--config-sha", required=True)
    for name in ("preview", "apply", "verify"):
        op = commands.add_parser(name)
        op.add_argument("--config", type=Path, required=True)
        op.add_argument("--config-sha", required=True)
        if name != "verify":
            op.add_argument(
                "--replace-block-sha",
                help="reviewed SHA-256 of a pre-existing local binding to migrate",
            )
    a = parser.parse_args(argv)
    sys.path.insert(0, str(TOOLS.parent))
    try:
        need(not os.environ.get("PP_TASK_ID"), "run from the operator session")
        if a.action == "bootstrap":
            result = bootstrap(a)
        elif a.action == "prepare":
            result = prepare(a)
        elif a.action == "rollback":
            need(
                DIGEST.fullmatch(a.config_sha),
                "expected the release SHA-256 to roll back",
            )
            result = manage(
                a.action,
                a.data_dir.resolve(strict=True),
                a.working_dir.resolve(strict=True),
                a.config_sha,
            )
        else:
            cfg = read_release(a.config, a.config_sha)
            result = manage(
                a.action,
                Path(cfg["data_dir"]),
                Path(cfg["working_dir"]),
                a.config_sha,
                cfg=cfg,
                conf=a.config,
                adopt=getattr(a, "replace_block_sha", None),
            )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(2, f"FIX cache installation: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
