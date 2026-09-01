#!/usr/bin/env python3
"""Read-only health check for the generic GitHub maintenance route."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys


REVIEW = re.compile(r"(?m)^<!-- pp:review head=([0-9a-f]{40}) -->$")


def gh_json(*args: str):
    executable = os.environ.get("GH_EXE") or shutil.which("gh") or shutil.which("gh.exe")
    if not executable:
        raise RuntimeError("GitHub CLI not found")
    run = subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if run.returncode:
        raise RuntimeError((run.stderr or run.stdout).strip())
    return json.loads(run.stdout)


def gh_pages(path: str) -> list[dict]:
    pages = gh_json("api", "--paginate", "--slurp", path)
    return [item for page in pages for item in page]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--trusted-login", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    pulls = gh_pages(f"repos/{args.repo}/pulls?state=open&per_page=100")
    findings = []
    checked = 0

    for pull in pulls:
        checked += 1
        number = pull["number"]
        head = pull["head"]["sha"]
        labels = {item["name"] for item in pull.get("labels", [])}
        if "ship" in labels and ({"changes-requested", "needs-decision", "hold"} & labels):
            findings.append({
                "severity": "red",
                "code": "ship_with_blocker",
                "pr": number,
                "message": "ship conflicts with a blocking route label",
            })
        if "reviewed" in labels and "changes-requested" in labels:
            findings.append({
                "severity": "red",
                "code": "conflicting_review_route",
                "pr": number,
                "message": "reviewed and changes-requested are both present",
            })

        if "reviewed" in labels or "ship" in labels:
            comments = gh_pages(f"repos/{args.repo}/issues/{number}/comments?per_page=100")
            trusted_heads = {
                match.group(1)
                for comment in comments
                if comment.get("user", {}).get("login") == args.trusted_login
                and comment.get("created_at") == comment.get("updated_at")
                for match in REVIEW.finditer(comment.get("body") or "")
            }
            if head not in trusted_heads:
                findings.append({
                    "severity": "red",
                    "code": "route_without_current_review",
                    "pr": number,
                    "message": "reviewed/ship has no trusted review marker for current HEAD",
                })

    state = "red" if any(item["severity"] == "red" for item in findings) else "green"
    report = {
        "state": state,
        "summary": f"open PR: {checked}; invariant findings: {len(findings)}",
        "findings": findings,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"{state}: {report['summary']}")
        for item in findings:
            print(f"- PR #{item['pr']}: {item['message']}")
    return 1 if state == "red" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        failure = {
            "state": "red",
            "summary": f"health-check failed: {exc}",
            "findings": [{"severity": "red", "code": "health_check_failed", "message": str(exc)}],
        }
        print(json.dumps(failure, ensure_ascii=False))
        raise SystemExit(2)
