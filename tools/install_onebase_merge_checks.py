"""Bind an operator-verified local checks runner to future native OneBase MERGE tasks."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SERIES = 4
TITLE = "OneBase - MERGE - безопасное слияние"
ANCHOR_PREFIX = "In future native OneBase REVIEW and MERGE occurrences,"
ANCHOR_SHA = "1a9f61c74561baeeece1af66a667a5d827b4dfad1d842be252580e3282bcdd27"
MARKER = "[OneBase MERGE local checks v1]"
PENDING = "series_id=? AND status='pending' AND started_at IS NULL AND session_id IS NULL AND retry_count=0"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def note(path, digest):
    return (
        f"{MARKER} For ordinary native base-sync that requires go build ./... or go vet ./..., use "
        f"{path} (SHA256 {digest}) under the operator-pinned Python with -IB. Verify the file first. "
        "From the prepared worktree run each matching command with --number N --expect-source SHA "
        "--expect-base SHA --expect-tree SHA --expect-go goVERSION --expect-node vVERSION "
        "--check build (or --check vet) --artifacts NEW_EXTERNAL_DIR. Supply the actual task's tool versions. "
        "Use the existing full test runners for UI, package tests and additional checks. Inspect the raw "
        "logs for executed and reused results. Only use a prior completed check if the runner "
        "accepts it on a retry after a pre-publication GitHub Source wait and the usual full GitHub "
        "readback confirms that no intent or branch update exists for this transition. If history is "
        "unclear, run the check afresh. Run every additional "
        "required check. On fresh_required (exit 3), unavailable runner, or unverified evidence "
        "execute the original full check. A test failure fails the check. Explicit fresh-run and "
        "target-specific owner instructions take precedence. Obtain fresh native Source, CI, "
        "review, ship, budget and publication authority as usual before each write."
    )


def transform(prompt, binding, remove=False):
    anchors = [line for line in prompt.splitlines() if line.startswith(ANCHOR_PREFIX)]
    if len(anchors) != 1 or sha(anchors[0].encode()) != ANCHOR_SHA:
        raise ValueError("unrecognized ordinary MERGE template anchor")
    anchor = anchors[0]
    installed = anchor + "\n\n" + binding
    if MARKER in prompt:
        if prompt.count(MARKER) != 1 or installed not in prompt or binding not in prompt.splitlines():
            raise ValueError("existing MERGE local checks binding differs")
        return prompt.replace(installed, anchor, 1) if remove else prompt
    return prompt if remove else prompt.replace(anchor, installed, 1)


def runner_path(home, digest):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("expected a full SHA-256")
    return home / "PromptPilotBuild" / ("onebase-merge-checks-" + digest[:16]) / "onebase_merge_checks.py"


def qualify(path, source, digest, install):
    contents = source.read_bytes()
    if not contents or sha(contents) != digest:
        raise ValueError("release helper differs from expected SHA-256")
    if install:
        if path.parent.parent.resolve() != path.parent.parent:
            raise ValueError("unexpected build root")
        path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.resolve() != path.parent:
        raise ValueError("unexpected install directory")
    if install and not path.exists():
        fd, temp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(contents)
                out.flush()
                os.fsync(out.fileno())
            if os.name == "posix":
                os.chmod(temp, 0o400)
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temp)
    if path.is_symlink() or (path.exists() and (
        not stat.S_ISREG(path.lstat().st_mode) or sha(path.read_bytes()) != digest
    )):
        raise ValueError("conflicting installed runner")
    return path.is_file()


def bind(db, working_dir, binding, action, *, series=SERIES, title=TITLE, transform_prompt=transform):
    write = action in {"apply", "rollback"}
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        row = db.execute(
            "SELECT prompt FROM task_series WHERE id=? AND title=? AND working_dir=? AND ended_at IS NULL",
            (series, title, str(working_dir)),
        ).fetchone()
        if not row:
            raise ValueError("expected active native OneBase series missing")
        target = transform_prompt(row[0], binding, action == "rollback")
        changes = []
        for task, prompt, directory in db.execute(
            "SELECT id,prompt,working_dir FROM tasks WHERE " + PENDING + " ORDER BY id", (series,)
        ).fetchall():
            if directory != str(working_dir):
                raise ValueError(f"unexpected working directory for pending task {task}")
            updated = transform_prompt(prompt, binding, action == "rollback")
            if updated != prompt:
                changes.append(task)
                if write and db.execute(
                    "UPDATE tasks SET prompt=? WHERE id=? AND prompt=? AND " + PENDING,
                    (updated, task, prompt, series),
                ).rowcount != 1:
                    raise ValueError(f"pending task {task} changed")
        series_changed = target != row[0]
        if action == "verify" and (series_changed or changes):
            raise ValueError("series binding is incomplete")
        if write and series_changed:
            if db.execute(
                "UPDATE task_series SET prompt=?,updated_at=? WHERE id=? AND prompt=? AND ended_at IS NULL",
                (target, datetime.now(timezone.utc).isoformat(), series, row[0]),
            ).rowcount != 1:
                raise ValueError("series changed")
        if write:
            db.commit()
        else:
            db.rollback()
        return {"action": action, "series_changed": series_changed, "pending_changed": changes,
                "template_sha256": sha(target.encode())}
    except BaseException:
        db.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply", "verify", "rollback"))
    parser.add_argument("release_script", type=Path)
    parser.add_argument("sha256")
    args = parser.parse_args()
    if os.environ.get("PP_TASK_ID"):
        parser.error("run from the release operator session")
    home = Path.home().resolve()
    path = runner_path(home, args.sha256)
    available = qualify(path, args.release_script, args.sha256, args.action == "apply")
    if args.action == "verify" and not available:
        raise ValueError("installed runner missing")
    mode = "rw" if args.action in {"apply", "rollback"} else "ro"
    database = (home / ".promptpilot/promptpilot.db").as_uri() + "?mode=" + mode
    with sqlite3.connect(database, uri=True, timeout=15) as db:
        result = bind(db, home / "Projects/onebase-automation/merge", note(path, args.sha256), args.action)
    print(json.dumps({**result, "runner": str(path), "sha256": args.sha256}, sort_keys=True))


if __name__ == "__main__":
    main()
