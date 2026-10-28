"""Bounded, read-only pipeline watch; no agent launches or GitHub mutations."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import urlopen


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def read_api(base, path):
    with urlopen(base.rstrip("/") + path, timeout=20) as response:
        return json.load(response)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def summarize(tasks, series, started_at, now):
    completed = [t for t in tasks if t.get("completed_at")
                 and timestamp(started_at) <= timestamp(t["completed_at"]) <= timestamp(now)]
    usage = {"input": 0, "cached_input": 0, "uncached_input": 0, "output": 0}
    model_runs = 0
    for task in completed:
        result = task.get("result") or ""
        tokens = re.search(r"Tokens: (\d+) in / (\d+) out", result)
        cached = re.search(r"Cached input: (\d+)", result)
        if tokens:
            model_runs += 1
            usage["input"] += int(tokens[1])
            usage["output"] += int(tokens[2])
        if cached:
            usage["cached_input"] += int(cached[1])
    usage["uncached_input"] = usage["input"] - usage["cached_input"]
    alerts = []
    running = []
    now_dt = timestamp(now)
    for task in tasks:
        if task.get("status") != "running" or not task.get("started_at"):
            continue
        age = (now_dt - timestamp(task["started_at"])).total_seconds()
        running.append({"id": task["id"], "series_id": task.get("series_id"),
                        "age_minutes": round(age / 60, 1)})
        timeout = task.get("task_timeout") or 3600
        if age > timeout:
            alerts.append(f"task #{task['id']} exceeded timeout")
    for item in series:
        if item.get("paused") and item.get("auto_pause_reason"):
            alerts.append(f"series #{item['id']} auto-paused: {item['auto_pause_reason']}")
    return {
        "completed_runs": len(completed),
        "productive_runs": sum(t.get("verdict") == "ГОТОВО" for t in completed),
        "reported_done_runs": sum(t.get("verdict") == "ГОТОВО" for t in completed),
        "model_runs_with_usage": model_runs,
        "runs_without_usage": len(completed) - model_runs,
        "human_handoffs": sum(t.get("verdict") == "НУЖЕН ЧЕЛОВЕК" for t in completed),
        "empty_runs": sum(t.get("verdict") == "ПУСТО" for t in completed),
        "completed_run_tokens": usage, "running": running, "alerts": alerts,
    }


def read_merges(gh, repository, started):
    """Read every closed-PR page updated in the observation window.

    A full first page is not evidence of complete coverage. Stop only after a
    short page or crossing the window boundary in GitHub's updated ordering.
    """
    merges = {}
    page = 1
    while True:
        result = subprocess.run([
            gh, "api", f"repos/{repository}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}",
        ], capture_output=True, text=True, encoding="utf-8", timeout=45)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:400])
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise ValueError("GitHub pull response is not a list")
        for pr in rows:
            if pr.get("merged_at") and timestamp(pr["merged_at"]) >= timestamp(started):
                merges[pr["number"]] = {
                    "number": pr["number"], "merged_at": pr["merged_at"],
                    "title": pr.get("title"), "url": pr.get("html_url"),
                    "labels": [label["name"] for label in pr.get("labels", [])],
                }
        if (len(rows) < 100 or any(pr.get("updated_at")
                and timestamp(pr["updated_at"]) < timestamp(started) for pr in rows)):
            return merges
        page += 1
        if page > 100:
            raise RuntimeError("GitHub observation exceeds 100 pages; coverage incomplete")


def delivery_metrics(report, merges, github_current):
    """Ratios describe the observed window, not causality, money or quota."""
    count = len(merges)
    usage = report.get("completed_run_tokens") or {}
    return {
        "confirmed_merges": count,
        "github_current": github_current,
        "reported_done_is_delivery": False,
        "completed_runs_per_merge": (
            round(report.get("completed_runs", 0) / count, 2)
            if count and github_current else None),
        "observed_uncached_input_per_merge": (
            round(usage.get("uncached_input", 0) / count)
            if count and github_current else None),
        "observed_output_per_merge": (
            round(usage.get("output", 0) / count)
            if count and github_current else None),
        "note": "Usage covers completed runs with token logs only; not a price, quota or per-PR attribution.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8420")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--gh", default=os.environ.get("PP_GH_EXE", "gh"))
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)
            or not 1 <= args.duration <= 86400 or not 10 <= args.interval <= 60):
        parser.error("invalid repository, duration or interval")
    started = utcnow()
    project_prefix = args.repository.split("/", 1)[1].lower()
    deadline = time.monotonic() + args.duration
    seen = {}
    merges = {}
    next_github_read = 0
    github_current = False
    report = {"started_at": started, "repository": args.repository,
              "planned_seconds": args.duration, "read_only": True}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    while True:
        now = utcnow()
        errors = []
        try:
            tasks = read_api(args.url, "/api/tasks?limit=400")
            # Avoid reading the worker's real DB or executing project health.
            tasks = [t for t in tasks if (t.get("series_title") or "").lower().startswith(project_prefix)]
            seen.update({t["id"]: t for t in tasks})
            series = read_api(args.url, "/api/schedule")
            series = [s for s in series if str(s.get("title") or "").lower().startswith(project_prefix)]
            report.update(summarize(list(seen.values()), series, started, now))
            report["worker"] = read_api(args.url, "/api/worker/status")
        except Exception as exc:
            errors.append(f"API read failed: {exc}")
        if time.monotonic() >= next_github_read or time.monotonic() >= deadline:
            try:
                merges.update(read_merges(args.gh, args.repository, started))
                github_current = True
                report["github_checked_at"] = now
            except Exception as exc:
                github_current = False
                errors.append(f"GitHub read failed: {exc}")
            next_github_read = time.monotonic() + 600
        finished = time.monotonic() >= deadline
        report.update(last_updated_at=now, finished=finished,
                      confirmed_merges=list(merges.values()), read_errors=errors,
                      delivery=delivery_metrics(report, merges, github_current))
        temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.report)
        print(json.dumps({"at": now, "merges": list(merges),
                          "alerts": report.get("alerts", []), "errors": errors}), flush=True)
        if finished:
            return 0
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
