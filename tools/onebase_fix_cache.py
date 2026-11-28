"""Reuse the initial OneBase FIX snapshot or claim one ordinary refresh per scheduled task."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from types import ModuleType


# The original cache-only command emits the same wait envelope without a classified cause.
MISS_REASONS = frozenset(("snapshot_missing", "snapshot_invalid", "profile_changed", "publication_changed",
                          "snapshot_invalidated", "snapshot_expired", "snapshot_from_future", "queue_incomplete",
                          "refresh_blocked", "scan_in_progress", "budget_in_flight", "priority_waiter",
                          "checker_failed", "checker_expired", "checker_proof_invalid", "candidate_invalid",
                          "no_eligible_pr", "budget_low", "fairness_waiter", "rate_snapshot_stale"))
SLOT_KEY = "pipeline_github_scan_lease:v1:" + hashlib.sha256(b"github-default").hexdigest()


def installed_tools(config, expected_sha):
    if config.parent != Path(__file__).resolve().parent or config.resolve(strict=True) != config:
        raise ValueError("configuration and tools must belong to the same installation")
    raw = config.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("installed configuration changed")
    files = json.loads(raw)["files"]
    path = config.with_name("onebase_fix_cache_guard.py")
    if path.resolve(strict=True) != path:
        raise ValueError("installed receipt guard changed")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != files[path.name]:
        raise ValueError("installed receipt guard changed")
    guard = ModuleType(path.stem)
    guard.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), guard.__dict__)
    guard.pinned_bytes(Path(__file__).resolve(), files["onebase_fix_cache.py"])
    return guard, guard.Installation(config, expected_sha)


def wait_for_slot(database, seconds=180):
    """The sampler owns expiration, acquisition and admission. Observe presence only."""
    deadline = time.monotonic() + seconds
    while True:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            if not conn.execute("SELECT 1 FROM settings WHERE key=?", (SLOT_KEY,)).fetchone():
                return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(1, left))


def private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.resolve(strict=True) != path or path.stat().st_uid != os.geteuid():
        raise ValueError("unexpected private evidence directory")


def child_environment(installed):
    return {**os.environ, "PP_DATA_DIR": str(installed.data_dir),
            "PP_PIPELINE_PROFILES": str(installed.profiles_path)}


def run_initial(guard, installed, args):
    # This entry point runs on the Mac. Keeping fcntl local also allows read-only
    # journal inspection and installer validation on other platforms.
    import fcntl
    identity = installed.identity()
    task, started, native_binding = identity
    installed.verify_worker()
    native = installed.config["native"]
    native_file = Path(native["path"])
    native_stat = guard.pin_binary(native)
    evidence = installed.evidence_dir(identity)
    occurrence = installed.journal / f"occurrence-{task}"
    private_directory(occurrence)
    private_directory(evidence)
    probes = []

    def still_current():
        guard.require(installed.identity() == identity, "ordinary FIX attempt changed")
        installed.verify_worker()
        guard.still_pinned(native_file, native_stat)

    def spent():
        print(json.dumps({"state": "wait", "source_refreshed": False, "mutable_admission_granted": False,
                          "reason": "full Source fallback already claimed for this scheduled occurrence"}))
        return 3

    def probe(phase):
        cache = installed.config["cache_native"]
        executable = Path(cache["path"])
        marker = guard.pin_binary(cache)
        got = subprocess.run([str(executable), "pipelinectl", "source-refresh", "--profile", "onebase",
                              "--reuse-fix-cache-only", "--allow-deferred-insights-read"],
                             stdout=subprocess.PIPE, env=child_environment(installed),
                             timeout=35, check=False)
        guard.still_pinned(executable, marker)
        still_current()
        value = json.loads(got.stdout)
        if (got.returncode == 3 and value.get("state") == "wait" and value.get("source_refreshed") is False
                and value.get("reason") == "fresh FIX cache unavailable"):
            reason = value.get("cache_miss_reason", "legacy_unclassified")
            guard.require((reason == "legacy_unclassified" and "cache_miss_reason" not in value)
                          or reason in MISS_REASONS, "unknown cache decision")
            probes.append({"phase": phase, "reason": reason})
            return None
        if got.returncode == 0:
            guard.require(value.get("state") == "reused" and value.get("source_reused") is True
                          and value.get("source_refreshed") is False
                          and value.get("mutable_admission_granted") is False
                          and value.get("product_mutation_authorized") is False, "unexpected Native success")
            installed.issue(got.stdout, identity)
        sys.stdout.buffer.write(got.stdout)
        return got.returncode if got.returncode in (0, 2, 3) else 2

    with os.fdopen(os.open(occurrence / "initial.lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "rb") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        debit = occurrence / "active.intent.json"
        if args.refresh_after_reused:
            raw = installed.prepush.private(installed.root, args.refresh_after_reused).read_bytes()
            selection = installed.current_file(args.selection, started)
            target = installed.current_file(args.target_read, started)
            reason = installed.fallback_reason(raw, selection, target, identity)
        else:
            guard.require(not (evidence / "source.json").exists(), "initial Source already issued")
            result = probe("before_wait")
            if result is not None:
                return result
        if debit.exists():
            return spent()
        free = wait_for_slot(installed.db)
        still_current()
        if not args.refresh_after_reused:
            result = probe("after_wait")
            if result is not None:
                return result
            reason = probes[-1]["reason"]
        if not free:
            print("Scan slot remained busy. Full Source was not started.", file=sys.stderr)
            return 75
        if args.refresh_after_reused:
            # State and the raw evidence may have changed during the bounded wait.
            reason = installed.fallback_reason(raw, installed.current_file(args.selection, started),
                                               installed.current_file(args.target_read, started), identity)
        still_current()
        claim = {"task": task, "started": started, "native": native_binding, "at": time.time(),
                 "decision": "full_scan_claimed", "reason": reason, "probes": probes}
        if not installed.prepush.unique(debit, claim):
            return spent()
        # Keep stdout, stderr and termination behavior of the native process. The
        # intent is durable even if exec or the worker fails before any response.
        os.execve(native_file, [str(native_file), "pipelinectl", "source-refresh", "--profile", "onebase"],
                  child_environment(installed))


def journal_report(journal):
    """Count scheduled tasks. A cache hit can still need an active scan after election."""
    reused, claims, reasons = set(), set(), Counter()
    unreadable = 0
    for path in journal.glob("*/*.json"):
        dirname, filename = path.parent.name, path.name
        hit = re.fullmatch(r"([1-9][0-9]*)-[0-9a-f]{24}", dirname) if filename == "source.json" else None
        claim = re.fullmatch(r"occurrence-([1-9][0-9]*)", dirname) if filename == "active.intent.json" else None
        if not hit and not claim:
            continue
        try:
            if path.resolve(strict=True) != path:
                raise ValueError("noncanonical record")
            record = json.loads(path.read_bytes())
            if hit:
                if not all(re.fullmatch("[0-9a-f]{64}", record[k]) for k in
                           ("source_sha256", "binding", "value_sha256", "native")):
                    raise ValueError("invalid observation")
                reused.add(int(hit[1]))
            else:
                number = int(claim[1])
                if record["task"] != number or not isinstance(record["started"], str):
                    raise ValueError("invalid claim")
                reason = record.get("reason", "legacy_unclassified")
                if reason not in MISS_REASONS | {"legacy_unclassified", "new_pr_or_head", "issue_requires_live_source"}:
                    raise ValueError("unexpected claim reason")
                claims.add(number)
                reasons[reason] += 1
        except (KeyError, TypeError, ValueError, OSError):
            unreadable += 1
    return {"observed_tasks": len(reused | claims), "initial_cache_hits": len(reused),
            "full_scan_claims": len(claims), "full_scan_after_hit": len(reused & claims),
            "full_scan_reasons": dict(sorted(reasons.items())), "unreadable_records": unreadable}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha", required=True)
    parser.add_argument("--refresh-after-reused", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--target-read", type=Path)
    parser.add_argument("--report", action="store_true", help="summarize existing local decisions")
    args = parser.parse_args(argv)
    fallback_args = (args.refresh_after_reused, args.selection, args.target_read)
    if any(fallback_args) and (not all(fallback_args) or args.report):
        parser.error("fallback requires its original receipt, ordinary election and current REST target")
    guard, installed = installed_tools(args.config, args.config_sha)
    if args.report:
        print(json.dumps(journal_report(installed.journal), sort_keys=True))
        return 0
    return run_initial(guard, installed, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, sqlite3.Error, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
        print(f"Initial FIX Source: {exc}", file=sys.stderr)
        sys.exit(2)
