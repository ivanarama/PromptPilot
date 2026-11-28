"""Validate a cached OneBase FIX observation and bind its PR to the ordinary election."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import subprocess
import sys
import time
from types import ModuleType


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def pinned_bytes(path, expected):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path and path.is_file(),
            f"expected a canonical file: {path.name}")
    with path.open("rb") as stream:
        raw = stream.read()
    require(sha256(raw) == expected, f"file digest changed: {path.name}")
    return raw


def pinned_module(path, sha):
    module = ModuleType(path.stem)
    module.__file__ = str(path)
    exec(compile(pinned_bytes(path, sha), str(path), "exec"), module.__dict__)
    return module


def pin_binary(pin):
    path = Path(pin["path"])
    require(path.resolve(strict=True) == path and os.access(path, os.X_OK)
            and path.stat().st_uid == os.geteuid(),
            "expected a canonical executable")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "expected a regular executable")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    require(digest.hexdigest() == pin["sha256"], "executable digest changed")
    return before


def still_pinned(path, before):
    after = path.stat()
    require((before.st_dev, before.st_ino, before.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_ctime_ns), "executable changed during observation")


class Installation:
    def __init__(self, config_path, config_sha):
        require(config_path.parent == Path(__file__).resolve().parent,
                "configuration and tools must belong to the same installation")
        self.config = json.loads(pinned_bytes(config_path, config_sha))
        self.config_sha = config_sha
        files = self.config["files"]
        pinned_bytes(Path(__file__).resolve(), files["onebase_fix_cache_guard.py"])
        self.prepush = pinned_module(config_path.with_name("onebase_fix_prepush.py"),
                                     files["onebase_fix_prepush.py"])
        self.data_dir = Path(self.config["data_dir"])
        self.series = self.config["series_id"]
        self.working_dir = Path(self.config["working_dir"])
        require(self.data_dir.resolve(strict=True) == self.data_dir
                and self.working_dir.resolve(strict=True) == self.working_dir
                and type(self.series) is int and self.series > 0, "invalid installation identity")
        if "PP_DATA_DIR" in os.environ:
            require(Path(os.environ["PP_DATA_DIR"]).resolve() == self.data_dir,
                    "worker data directory differs from the installation")
        self.profiles_path = Path(self.config["profiles_path"])
        require(self.profiles_path.resolve(strict=True) == self.profiles_path and self.profiles_path.is_file(),
                "invalid pipeline profiles path")
        if "PP_PIPELINE_PROFILES" in os.environ:
            require(Path(os.environ["PP_PIPELINE_PROFILES"]).resolve() == self.profiles_path,
                    "worker profiles path differs from the installation")
        self.root = self.data_dir / "fix-runs"
        self.db = self.data_dir / "promptpilot.db"
        self.journal = self.data_dir / "onebase-fix-initial-observations"

    def identity(self, source_binding=None):
        task, started = self.prepush.task_env(self.root, series_id=self.series)
        binding = self.prepush.native(self.root, task, started, source_binding=source_binding,
                                      series_id=self.series, working_dir=self.working_dir)
        marker = f"<!-- pp:onebase-fix-cache config-sha256={self.config_sha} -->"
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT prompt FROM tasks WHERE id=? AND started_at=? AND status='running'",
                               (task, started)).fetchone()
        require(row and row[0].splitlines().count(marker) == 1,
                "running FIX does not own this installation")
        return task, started, binding

    def verify_worker(self):
        native = self.config["native"]
        legacy = self.config.get("legacy_runtime_guard")
        if legacy:
            guard = pinned_module(Path(legacy["path"]), legacy["sha256"])
            binding = guard.check_fix_task()
            require(str(binding.path) == native["path"] and binding.sha == native["sha256"],
                    "task's active Native binding differs from installed binary")
            guard.check_runtime(binding)
            return
        plist = Path(self.config.get("worker_plist", Path.home() / "Library/LaunchAgents/com.promptpilot.worker.plist"))
        require(plist.is_absolute() and plist.resolve(strict=True) == plist, "worker plist changed")
        service = plistlib.loads(plist.read_bytes())
        args = service["ProgramArguments"]
        require(args[-2:] == [native["path"], "worker"] and service["Label"] == "com.promptpilot.worker",
                "running worker must use the installed Native binary")
        registered = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/com.promptpilot.worker"],
                                    capture_output=True, text=True, check=True, timeout=5)
        lines = registered.stdout.splitlines()
        start = lines.index("\targuments = {") + 1
        end = lines.index("\t}", start)
        require("\tstate = running" in lines and [val.strip() for val in lines[start:end]] == args,
                "registered worker differs from the configured worker")

    def evidence_dir(self, identity):
        task, started, _ = identity
        return self.journal / f"{task}-{sha256(started.encode())[:24]}"

    def issue(self, raw, identity):
        task, started, guard = identity
        value = json.loads(raw)
        token = self.prepush.source_token(value, task, started)
        bound = value["attempt_binding_sha256"]
        require(value["state"] == "reused" and self.identity(bound) == identity
                and 0 <= time.time() - token["generated_at"] < 300,
                "cached Source is stale or bound to another FIX")
        witness = {"native": guard, "source_sha256": sha256(raw), "binding": bound,
                   "value_sha256": sha256(self.prepush.encode(value)), "decision": "reused",
                   "at": time.time()}
        require(self.prepush.unique(self.evidence_dir(identity) / "source.json", witness),
                "initial Source already issued for this attempt")

    def checked_origin(self, prior, identity=None):
        current = self.identity(prior["attempt_binding_sha256"])
        require(identity is None or identity == current, "initial FIX attempt changed")
        self.prepush.source_token(prior, *current[:2])
        witness = self.prepush.read_json(self.data_dir, self.evidence_dir(current) / "source.json")
        require(prior["state"] == "reused" and witness["native"] == current[2]
                and witness["binding"] == prior["attempt_binding_sha256"]
                and witness["value_sha256"] == sha256(self.prepush.encode(prior)),
                "cached Source differs from the original observation")
        return witness

    def current_file(self, path, started):
        p = self.prepush.private(self.root, path)
        modified = p.stat().st_mtime
        require(datetime.fromisoformat(started).timestamp() <= modified and 0 <= time.time() - modified < 120,
                "ordinary election and raw REST target must be current")
        return json.loads(p.read_bytes())

    def pr_identity(self, raw, selection):
        number, head = raw["number"], raw["head"]["sha"]
        self.prepush.refs(raw, number, head)
        self.prepush.selected(selection, number, head)
        labels = live_labels(raw)
        require("changes-requested" in labels and not labels & {"hold", "ship", "manual"}
                and raw["html_url"] == f"https://github.com/{self.prepush.REPO}/pull/{number}",
                "live PR is no longer admitted to FIX")
        return number, head

    def check_target(self, identity, number, head):
        task, started, _ = identity
        excluded = json.loads(os.environ.get("PP_PIPELINE_EXCLUDED_NUMBERS", "[]"))
        require(isinstance(excluded, list) and all(type(n) is int and n > 0 for n in excluded)
                and number not in excluded, "target excluded by the ordinary election")
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("BEGIN")
            def setting(key):
                record = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                return json.loads(record[0]) if record else {}
            hold = setting(f"pipeline_item_holds:v1:{self.series}").get(str(number))
            if isinstance(hold, dict) and type(hold.get("at")) in (int, float) and 0 <= time.time() - hold["at"] < 86400:
                baseline = setting(f"pipeline_item_baseline:v1:{self.series}")
                same_attempt = baseline.get("task_id") == task and baseline.get("attempt_started_at") == started
                if "fingerprint" in hold:
                    require(same_attempt, "no current baseline for the FIX hold")
                    held = hold["fingerprint"] == baseline["states"].get(str(number))
                else:
                    held = bool(head) and hold.get("head") == head
                raw = json.dumps(hold, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                released = same_attempt and baseline.get("progress_releases", {}).get(str(number)) == sha256(raw)
                require(not held or released, "selected target has an active hold")
            rows = conn.execute(
                """SELECT number,head,task_id,task_started_at FROM pipeline_target_reservations AS r
                   WHERE repository=? AND (number=? OR task_id=?) AND (expires_at>? OR EXISTS (
                     SELECT 1 FROM tasks AS t WHERE t.id=r.task_id AND t.status='running'
                     AND (r.task_started_at='' OR t.started_at=r.task_started_at)))""",
                (self.prepush.REPO, number, task, datetime.now(timezone.utc).isoformat())).fetchall()
            require(all(r == (number, head, task, started) for r in rows),
                    "target or FIX task has a conflicting reservation")

    def current_report(self):
        report = self.prepush.read_cached_fix_queue()
        diagnostics = report["diagnostics"]
        require(report["repository"] == self.prepush.REPO and report["profile_id"] == "onebase"
                and type(diagnostics.get("exit_code")) is int and diagnostics["exit_code"] == 0
                and not diagnostics.get("errors") and not diagnostics.get("checker_failed")
                and not report.get("errors"), "wrong profile or failed Source checker")
        return report

    def bind(self, source_bytes, selection, prdata):
        source = json.loads(source_bytes)
        identity = self.identity()
        witness = self.checked_origin(source, identity)
        require(witness["source_sha256"] == sha256(source_bytes), "original receipt bytes changed")
        number, head = self.pr_identity(prdata, selection)
        self.check_target(identity, number, head)
        original = source["snapshot_token"]
        later = selection["snapshot_token"] != original
        self.prepush.check_source_election(source, selection, *identity[:2], passive=later)
        report = self.current_report()
        token = self.prepush.current_fix_token(number, head, report=report)
        stamp = report["diagnostics_generated_at"]
        require(type(stamp) in (int, float) and 0 <= time.time() - stamp < 300
                and selection["snapshot_token"] == token
                and continues_publication(original, token),
                "current FIX publication does not confirm the ordinary election")
        if later:
            observed = self.prepush.passive_result(self.root, source_bytes, original, *identity)
            require(isinstance(observed, dict) and continues_publication(original, observed)
                    and continues_publication(observed, token)
                    and 0 <= time.time() - observed["generated_at"] < 300,
                    "a fresh passive result for this FIX must confirm the later publication")
        require(self.identity() == identity, "FIX admission changed while binding the target")

    def fallback_reason(self, source_bytes, selection, raw, identity):
        prior = json.loads(source_bytes)
        witness = self.checked_origin(prior, identity)
        require(witness["source_sha256"] == sha256(source_bytes)
                and selection.get("task_id") == identity[0] and selection.get("task_started_at") == identity[1],
                "fallback requires the issued receipt and the original ordinary election")
        report = self.current_report()
        cache = report["cache"]
        stored = prior["snapshot_token"]
        require(cache["profile_hash"] == stored["profile_hash"]
                and cache.get("refresh_blocked") in (None, "", "scan_in_progress", "cache_stale", "cache_invalidated"),
                "active Source is currently refused or its profile changed")
        if "head" in raw:
            number, head = self.pr_identity(raw, selection)
            self.check_target(identity, number, head)
            if prior["fix_pr_heads"].get(str(number)) != head:
                return "new_pr_or_head"
            current = cache.get("token")
            records = self.prepush.passive_records(self.root, *identity[:2])
            if (current != stored and selection.get("snapshot_token") == current
                    and cache.get("stale") is False and cache.get("invalidated") is False
                    and (records / "observe.intent.json").exists()):
                # Reconcile the same ordinary election against the existing passive claim.
                self.bind(source_bytes, selection, raw)
                raise ValueError("selected PR can use the issued Source and its passive observation")
            if (cache.get("token") != stored or cache.get("stale") is True
                    or cache.get("invalidated") is True or time.time() - stored["generated_at"] >= 300):
                return "publication_changed"
            raise ValueError("selected PR can use the issued Source")
        number = raw.get("number")
        labels = live_labels(raw)
        url = f"https://github.com/{self.prepush.REPO}/issues/{number}"
        require(type(number) is int and number > 0 and raw.get("state") == "open" and "pull_request" not in raw
                and raw.get("html_url") == url and bool(labels & {"ready-fix", "approved"})
                and not labels & {"hold", "manual", "ship"}
                and selection.get("kind") == "issue" and selection.get("number") == number,
                "an active refresh requires the elected open FIX issue")
        queue, = (q for q in report["queues"] if q["id"] == "fix" and q["membership_complete"] is True)
        item, = (i for i in queue["admission_items"] if i["kind"] == "issue" and i["number"] == number)
        require(item["key"] == f"issue:{number}" and item["url"] == url, "issue absent from FIX queue")
        self.check_target(identity, number, selection.get("eligibility_digest"))
        return "issue_requires_live_source"


def continues_publication(earlier, later):
    return (later["profile_hash"] == earlier["profile_hash"]
            and all(type(later[k]) is int and later[k] >= earlier[k] for k in ("epoch", "revision"))
            and type(later["generated_at"]) in (int, float) and later["generated_at"] >= earlier["generated_at"]
            and (later["revision"] > earlier["revision"] or later == earlier))


def live_labels(raw):
    labels = raw.get("labels")
    require(isinstance(labels, list) and all(isinstance(x, dict) and isinstance(x.get("name"), str)
                                           for x in labels), "unfiltered current REST labels required")
    return {x["name"] for x in labels}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha", required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--pr-read", type=Path, required=True)
    args = parser.parse_args()
    installed = Installation(args.config, args.config_sha)
    _, started, _ = installed.identity()
    installed.bind(installed.prepush.private(installed.root, args.source_receipt).read_bytes(),
                   installed.current_file(args.selection, started), installed.current_file(args.pr_read, started))
    print(json.dumps({"initial_fix_source_bound": True, "publication_authorized": False}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Initial FIX Source: {exc}", file=sys.stderr)
        sys.exit(2)
