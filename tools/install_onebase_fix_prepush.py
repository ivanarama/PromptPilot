"""Install the checked-candidate handoff for scheduled ordinary OneBase FIX tasks."""

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

if __package__:
    from .install_onebase_fix_postpush import TITLE
    from .install_onebase_merge_checks import bind, qualify, sha
else:
    # The release operator uses the pinned interpreter in isolated mode.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from install_onebase_fix_postpush import TITLE
    from install_onebase_merge_checks import bind, qualify, sha


ANCHOR = "Use the already elected PR, exact completion and candidate SHAs,"
ANCHOR_SHA = "56e46ca827e2b1a6d27ea4edc156a29ee771b7d37444212cfe9017040db07dbf"
MARKER = "[Ordinary OneBase FIX pre-push recovery v1]"


def note(path, digest):
    return (
        f"{MARKER} For ordinary elected FIX rework use the pinned Python -IB and {path} "
        f"after verifying its SHA256 {digest}. Keep the existing task schedule, election and all "
        "target-specific owner requirements. In each new ordinary occurrence, immediately after "
        "fresh Source, election and ready running-budget checkpoint, obtain the usual current "
        "raw REST PR. Before creating a new change run 'read --pr N --source SHA --selection FILE "
        "--source-receipt FILE --budget-receipt FILE --pr-read FILE' with this attempt's private "
        "files. For the stable election JSON see 'capture --help'; extract it from the canonical "
        "review and owner decision in this election. A miss follows the normal fresh development "
        "and acceptance path. On candidate, "
        "check all intervening send history, remote branch and REST head, owner decision, review "
        "and epoch. Obtain current protected refs/heads/main; compare the actual base used for "
        "acceptance rather than relying on the PR object's base SHA. Restore the verified bundle's "
        "exact candidate in this task's own isolated "
        "worktree. Prove its single source parent, tree and transition trailer before using it. "
        "Apply fix-approved section 6 to each check: reusable credit needs independent trusted "
        "evidence for the current base, candidate, command, environment, inputs, complete results "
        "and permitted skips. Rerun each check without that proof and every check required fresh. "
        "On ambiguous send, prior claim or a third remote head, reconcile by canonical readback. "
        "If only local evidence is missing, changed, or unsupported and readback excludes a prior "
        "send, proceed with the ordinary fresh path. A refusal by a current Native or repository "
        "gate keeps its original stop. A definite refusal before the push leaves the candidate "
        "available. Keep the exact git bundle, selection and successful task-bound Source receipt "
        "in the external private run directory; include their absolute paths and SHA256 in the "
        "acceptance manifest. On a supported definite pre-send wait, before cleanup run "
        "'capture --before-send --manifest FILE --bundle FILE --selection FILE --source-receipt FILE "
        "--pr-read FILE --precheck-budget FILE'. For a stale budget checkpoint alone, supply that "
        "checkpoint. If budget-refresh then returned scan_in_progress, also pass --budget-wait FILE. "
        "If the existing passive Source observer expired on cache_invalidated with ready budget, "
        "also pass --source-wait FILE --source-cache FILE --observer-start FILE --observer-exit FILE. "
        "Use the raw evidence from this attempt and end on its original wait. After a successful "
        "capture use the terminal line 'ИТОГ: ПУСТО — нет допуска: REASON' with the exact reason "
        "rate_snapshot_stale, scan_in_progress or cache_invalidated. Never capture a possible or "
        "completed send. "
        "For a restored candidate invoke the normal pinned push_and_confirm exactly once with "
        "fresh_pre_push_checks. Last inside that callback, after all ordinary guards pass, run "
        "'claim-push --ticket FILE --worktree DIR --budget-receipt FILE' with the returned ticket "
        "and this invocation's current ready checkpoint. Raise on refusal; return the fresh raw "
        "REST PR to the original guarded CAS. Reconcile an existing claim by readback only. "
        "After confirmed publication continue the established post-push report and CI procedure."
    )


def transform(prompt, binding, remove=False):
    anchors = [line for line in prompt.splitlines() if line.startswith(ANCHOR)]
    if len(anchors) != 1 or sha(anchors[0].encode()) != ANCHOR_SHA:
        raise ValueError("unrecognized ordinary FIX guarded push anchor")
    anchor = anchors[0]
    installed = anchor + "\n\n" + binding
    if MARKER in prompt:
        if prompt.count(MARKER) != 1 or installed not in prompt or binding not in prompt.splitlines():
            raise ValueError("existing FIX pre-push binding differs")
        return prompt.replace(installed, anchor, 1) if remove else prompt
    return prompt if remove else prompt.replace(anchor, installed, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply", "verify", "rollback"))
    parser.add_argument("release_script", type=Path)
    parser.add_argument("sha256")
    args = parser.parse_args()
    if os.environ.get("PP_TASK_ID") or not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
        parser.error("requires an operator session and a full release SHA-256")

    home = Path.home().resolve()
    path = home / "PromptPilotBuild" / ("fix-prepush-" + args.sha256[:16]) / "onebase_fix_prepush.py"
    available = qualify(path, args.release_script, args.sha256, args.action == "apply")
    if args.action == "verify" and not available:
        raise ValueError("installed FIX pre-push helper missing")
    mode = "rw" if args.action in {"apply", "rollback"} else "ro"
    database = (home / ".promptpilot/promptpilot.db").as_uri() + "?mode=" + mode
    with sqlite3.connect(database, uri=True, timeout=15) as db:
        result = bind(db, home / "Projects/onebase-automation/fix", note(path, args.sha256),
                      args.action, series=2, title=TITLE, transform_prompt=transform)
    print(json.dumps({**result, "helper": str(path), "sha256": args.sha256}, sort_keys=True))


if __name__ == "__main__":
    main()
