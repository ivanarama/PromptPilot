"""Bind the ordinary FIX post-push recovery helper to future native tasks."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

if __package__:
    from .install_onebase_merge_checks import bind, qualify, sha
else:
    # Isolated operator Python omits the script directory from sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from install_onebase_merge_checks import bind, qualify, sha


ANCHOR = "After all local checks and a successful task-bound Native Source refresh earlier in THIS invocation,"
ANCHOR_SHA = "f80992eb8b3b4a1bcbe1f9ddd48dd4059dd3fbe0bddf851222c59b73590d5a2b"
MARKER = "[Ordinary OneBase FIX post-push recovery v1]"
TITLE = "OneBase - FIX - исправление одобренного"


def note(path, digest):
    return (
        f"{MARKER} For ordinary FIX rework only, verify {path} SHA256 {digest} and use the pinned "
        "Python -IB with this helper. After creating the checked local commit and before CAS push, keep its UTF-8 report and acceptance "
        "manifest under the private ~/.promptpilot/fix-runs directory outside the temporary worktree. "
        "The manifest contains source, head, tree and files mapping absolute artifact paths to SHA256. "
        "Record these with 'record --pr N --head H --report FILE --manifest FILE'. On every new "
        "ordinary FIX, prioritize the current canonical unfinished remote PP-Fix-Transition among "
        "admitted targets. Run 'read --pr N --head H' for that exact PR and confirmed published HEAD. "
        "A legacy transition with no record may be imported with 'record' only after verifying the "
        "original acceptance, report, complete prior send history and current remote transition. "
        "Never repeat the push or content tests after a confirmed push. After a successful Source "
        "refresh bound to THIS attempt, when later cache-only observation times out or reports stale, "
        "invalidated or a competing scan as the sole remaining Source obstacle, use once 'observe "
        "--source-receipt FILE --seconds N' (1..300 within the remaining task and lease budget) "
        "instead of the old passive observer, only for this post-push phase. A token is a hint. "
        "If the window expires keep the artifacts and routing label for the next scheduled ordinary "
        "FIX. A typed Source refusal, budget denial or failed checker keeps its original stop. "
        "Before each mutation repeat the normal task, Source, budget, PR, remote ref, permission, "
        "canonical review/decision, trailer, comments, labels and ownership checks. Reconcile trusted "
        "unedited exact report bytes and marker before sending: pass --comments FILE from the current complete gh api --paginate --slurp REST comment read to read and claim-report. For an existing report pass --report-node FILE containing the unfiltered result of "
        "gh api graphql -f id=<REST node_id> -f 'query=query($id:ID!){node(id:$id){__typename ... on "
        "IssueComment{id fullDatabaseId lastEditedAt author{login} body pullRequest{number repository{nameWithOwner}}}}}'. "
        "Verify the matching unedited node before label removal. "
        "Only if none exists and any earlier "
        "send is excluded, use 'claim-report --pr N --head H --comments FILE' once immediately before posting. If a "
        "POST was claimed earlier, reconcile by readback only. Ambiguous or conflicting evidence "
        "requires a named human verdict for this PR with its HEAD and the uncertain send, without a second POST. After verified report readback repeat all normal guards, remove only the "
        "existing changes-requested and verify the result. Preserve all scoped/pilot rules."
    )


def transform(prompt, binding, remove=False):
    anchors = [line for line in prompt.splitlines() if line.startswith(ANCHOR)]
    if len(anchors) != 1 or sha(anchors[0].encode()) != ANCHOR_SHA:
        raise ValueError("unrecognized ordinary FIX passive observer")
    anchor = anchors[0]
    installed = anchor + "\n\n" + binding
    if MARKER in prompt:
        if prompt.count(MARKER) != 1 or installed not in prompt or binding not in prompt.splitlines():
            raise ValueError("existing FIX binding differs")
        return prompt.replace(installed, anchor, 1) if remove else prompt
    return prompt if remove else prompt.replace(anchor, installed, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply", "verify", "rollback"))
    parser.add_argument("release_script", type=Path)
    parser.add_argument("sha256")
    args = parser.parse_args()
    if os.environ.get("PP_TASK_ID") or not re.fullmatch("[0-9a-f]{64}", args.sha256):
        parser.error("requires an operator session and a full release SHA-256")
    home = Path.home().resolve()
    path = home / "PromptPilotBuild" / ("fix-postpush-" + args.sha256[:16]) / "onebase_fix_postpush.py"
    available = qualify(path, args.release_script, args.sha256, args.action == "apply")
    if args.action == "verify" and not available:
        raise ValueError("installed FIX helper missing")
    mode = "rw" if args.action in {"apply", "rollback"} else "ro"
    database = (home / ".promptpilot/promptpilot.db").as_uri() + "?mode=" + mode
    with sqlite3.connect(database, uri=True, timeout=15) as db:
        result = bind(db, home / "Projects/onebase-automation/fix", note(path, args.sha256),
                      args.action, series=2, title=TITLE, transform_prompt=transform)
    print(json.dumps({**result, "helper": str(path), "sha256": args.sha256}, sort_keys=True))


if __name__ == "__main__":
    main()
