"""Observe the local OneBase queue once during the original ordinary FIX attempt."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import ModuleType
from urllib.error import URLError


def installed_reader(config, expected):
    if config.resolve(strict=True) != config or config.parent != Path(__file__).resolve().parent:
        raise ValueError("expected config in the installed release")
    raw = config.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("installed configuration changed")
    files = json.loads(raw)["files"]
    path = config.with_name("onebase_fix_cache_guard.py")
    if path.resolve(strict=True) != path:
        raise ValueError("unexpected installed guard path")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != files[path.name]:
        raise ValueError("installed guard changed")
    guard = ModuleType(path.stem)
    guard.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), guard.__dict__)
    guard.pinned_bytes(Path(__file__).resolve(), files["onebase_fix_cache_observe.py"])
    reader = config.with_name("onebase_fix_postpush.py")
    post = guard.pinned_module(reader, files[reader.name])
    return guard, guard.Installation(config, expected), post


def current_budget(guard, installation):
    native = installation.config["cache_native"]
    executable = Path(native["path"])
    marker = guard.pin_binary(native)
    read = subprocess.run(
        [str(executable), "pipelinectl", "budget-status", "--profile", "onebase",
         "--series-id", str(installation.series), "--policy-sha256", installation.config["budget_policy_sha256"]],
        stdout=subprocess.PIPE, timeout=30, check=False,
        env={**os.environ, "PP_DATA_DIR": str(installation.data_dir),
             "PP_PIPELINE_PROFILES": str(installation.profiles_path)},
    )
    guard.still_pinned(executable, marker)
    value = json.loads(read.stdout)
    if read.returncode != 0:
        raise ValueError("running FIX budget is unavailable: " + str(value.get("reason", value.get("state"))))
    return value


def observe_once(installation, source, seconds, *, budget, observe, budget_receipt=None):
    if type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError("expected an observation window of 1 to 300 seconds")
    pre = installation.prepush
    identity = installation.identity()
    task, started, native = identity
    installation.verify_worker()
    path = pre.private(installation.root, source)
    raw = path.read_bytes()
    prior = json.loads(raw)
    original = pre.source_token(prior, task, started)
    digest = hashlib.sha256(raw).hexdigest()
    if prior["state"] == "reused" and installation.checked_origin(prior, identity)["source_sha256"] != digest:
        raise ValueError("original reused Source bytes changed")

    def check_budget(value):
        pre.budget_now(value, task, started, original["profile_hash"], time.time(), series_id=installation.series)

    if budget_receipt is not None:
        check_budget(pre.read_json(installation.root, budget_receipt))
    check_budget(budget())
    if installation.identity() != identity:
        raise ValueError("running FIX changed during the budget check")
    ticket = dict(task=task, started=started, native=native, source_sha256=digest, original=original)
    folder = installation.data_dir / "onebase-fix-passive-observations" / f"{task}-{hashlib.sha256(started.encode()).hexdigest()[:24]}"
    intent, result = folder / "observe.intent.json", folder / "observe.result.json"
    if not pre.unique(intent, ticket):
        if pre.read_json(installation.data_dir, intent) != ticket or not result.is_file():
            raise ValueError("this FIX already opened its passive observation")
        previous = pre.read_json(installation.data_dir, result)
        if previous["binding"] != ticket or previous["publication_authorized"] is not False:
            raise ValueError("passive result does not belong to this observation")
        return previous
    read = observe(prior, task, started, seconds, database=installation.db,
                   series_id=installation.series, working_dir=installation.working_dir)
    installation.verify_worker()
    if installation.identity() != identity or pre.private(installation.root, source).read_bytes() != raw:
        raise ValueError("running FIX or original Source changed during the observation")
    if read["publication_authorized"] is not False:
        raise ValueError("unexpected publication authority in passive observation")
    value = dict(binding=ticket, fresh_snapshot=read["fresh_snapshot"], publication_authorized=False)
    if not pre.unique(result, value):
        raise ValueError("duplicate observation result")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha", required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--budget-receipt", type=Path)
    parser.add_argument("--seconds", required=True, type=int)
    args = parser.parse_args()
    try:
        guard, installation, post = installed_reader(args.config, args.config_sha)
        receipt = observe_once(installation, args.source_receipt, args.seconds,
                               budget=lambda: current_budget(guard, installation), observe=post.observe,
                               budget_receipt=args.budget_receipt)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, URLError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "refused", "reason": str(exc)}))
        return 2
    print(json.dumps(receipt))
    return 0 if receipt["fresh_snapshot"] is not None else 3


if __name__ == "__main__":
    sys.exit(main())
