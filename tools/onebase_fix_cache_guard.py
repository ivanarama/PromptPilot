"""Validate a cached OneBase FIX observation and bind its PR to the ordinary election."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
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
        self.cost = pinned_module(config_path.with_name("source_graphql_cost.py"), files["source_graphql_cost.py"])
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
        if "deferred_refresh" in value:
            witness["refresh_blocked"] = value["deferred_refresh"]["refresh_blocked"]
        require(self.prepush.unique(self.evidence_dir(identity) / "source.json", witness),
                "initial Source already issued for this attempt")

    def checked_origin(self, prior, identity=None):
        current = self.identity(prior["attempt_binding_sha256"])
        require(identity is None or identity == current, "initial FIX attempt changed")
        self.prepush.source_token(prior, *current[:2])
        witness = self.prepush.read_json(self.data_dir, self.evidence_dir(current) / "source.json")
        require(prior["state"] == "reused" and witness["native"] == current[2]
                and witness["binding"] == prior["attempt_binding_sha256"]
                and witness.get("refresh_blocked") == prior.get("deferred_refresh", {}).get("refresh_blocked")
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

    def priority_read(self, report, original_token, *, now=None):
        """Observe this same publication while the independent insights scan yields."""
        cache = report["cache"]
        if not self.prepush.priority_scan_deferred(cache) or cache.get("token") != original_token:
            return False
        profile_hash = original_token["profile_hash"]
        config_raw = self.profiles_path.read_bytes()
        profiles = json.loads(config_raw)
        profile = profiles.get("profiles", profiles)["onebase"]
        key = "pipeline_insights_cache:v1:onebase:" + profile_hash
        status = "pipeline_github_refresh_status:v1:" + sha256(b"onebase")
        expected = {
            "pipeline_insights_cache_epoch:v1": str(original_token["epoch"]),
            "pipeline_insights_published_revision:v1:onebase:" + profile_hash: str(original_token["revision"]),
            "pipeline_insights_published_profile:v1:onebase": profile_hash,
            "pipeline_insights_published_profile_revision:v1:onebase": str(original_token["revision"]),
        }
        keys = [*expected, key, status]
        query = "SELECT key,value FROM settings WHERE key IN (?,?,?,?,?,?)"
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            stored = dict(conn.execute(query, keys))
            if (any(stored.get(k, "0" if k == "pipeline_insights_cache_epoch:v1" else None) != v
                    for k, v in expected.items()) or key not in stored or status not in stored):
                return False
            event = json.loads(stored[status])
            publication = json.loads(stored[key])
            observed = time.time() if now is None else now
            if not priority_publication(report, original_token, profile, publication, event, self.cost, observed):
                return False
            # Recheck the event and publication after reading the pinned checker artifacts.
            finish = time.time() if now is None else now
            return (0 <= finish - original_token["generated_at"] < 300
                    and 0 <= finish - report["diagnostics_generated_at"] < checker_ttl(profile)
                    and self.profiles_path.read_bytes() == config_raw
                    and dict(conn.execute(query, keys)) == stored)

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
        # These two legacy, owner-scoped rework operators require a clear
        # current Source before any claim. They read the same original receipt.
        priority = None if number in (2279, 2280) else lambda r: self.priority_read(r, original)
        token = self.prepush.current_fix_token(number, head, report=report, priority_read=priority)
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


def checker_ttl(profile):
    return min(300, int(profile["health_check"].get("cache_seconds", 1800)))


def priority_publication(report, token, profile, publication, event, cost, now):
    """Validate the immutable Source and its authoritative deferred-scan decision."""
    repository = "ivanarama/onebase"
    cache = report["cache"]
    profile_hash = token["profile_hash"]
    namespace = {k: v for k, v in profile.items() if k != "github_budget"}
    protocol = profile["github_budget"].get("source_cost_protocol")
    if (protocol != cost.PROTOCOL or profile.get("repository") != repository
            or profile["priority_control"].get("trusted_account") != "ivanarama"
            or cost.digest({"profile": namespace, "source_cost_protocol": protocol}) != profile_hash):
        return False
    stamp = token["generated_at"]
    if (type(stamp) not in (int, float) or not 0 <= now - stamp < 300
            or type(token["epoch"]) is not int or token["epoch"] < 0
            or type(token["revision"]) is not int or token["revision"] < 1
            or report.get("profile_id") != "onebase" or report.get("repository") != repository
            or report.get("errors") or report["diagnostics"].get("errors") or report["diagnostics"].get("checker_failed")
            or cache.get("source") not in ("durable", "memory") or cache.get("available") is not True
            or cache.get("complete") is not True or cache.get("invalidated") is not False
            or cache.get("stale") is not False or cache.get("ttl_seconds") != 300
            or cache.get("profile_hash") != profile_hash or cache.get("generated_at") != stamp
            or cache.get("token") != token or cache.get("refresh_blocked") != "priority_waiter"):
        return False
    if (publication.get("version") != 1 or publication.get("profile_id") != "onebase"
            or publication.get("repository") != repository or any(publication.get(k) != v for k, v in token.items())):
        return False
    result = publication["result"]
    if (result.get("errors") or any(result[k] != report[k] for k in
            ("profile_id", "repository", "diagnostics", "diagnostics_generated_at", "source_search_usage"))
            or result.get("generated_at") != stamp or report.get("generated_at") != stamp):
        return False
    # Queue runtime fields are recalculated by the loopback endpoint. Compare
    # the original membership and its completeness for every configured queue.
    def memberships(queues):
        return {q["id"]: q["admission_items"] for q in queues if q["membership_complete"] is True}
    members = memberships(result["queues"])
    if (members.keys() != {q["id"] for q in profile["queues"]} or "fix" not in members
            or len(members) != len(result["queues"]) or len(members) != len(report["queues"])
            or members != memberships(report["queues"])):
        return False
    revision = event["revision"]
    if (event.get("version") != 1 or type(revision) is not int or revision <= 0
            or event.get("repository") != repository or not isinstance(event.get("status"), dict)):
        return False
    status = event["status"]
    if (status.get("refresh_blocked") != "priority_waiter" or any(
            not isinstance(budget, dict) or budget.get("budget_route") != "insights"
            or budget.get("state") != "priority_waiter" or budget.get("allowed") is not False
            for budget in (cache.get("refresh_budget"), status.get("github_budget")))):
        return False
    until = status.get("refresh_deferred_until")
    if not isinstance(until, str) or cache.get("refresh_deferred_until") != until:
        return False
    deadline = datetime.fromisoformat(until.replace("Z", "+00:00"))
    if deadline.tzinfo is None or now >= deadline.timestamp():
        return False
    failures = event.get("source_checker_failure_revisions", {})
    if failures is None:
        failures = {}
    verified = result.get("source_checker_verified_failure_revision", 0)
    if (not isinstance(failures, dict) or type(verified) is not int or not 0 <= verified <= revision
            or any(not re.fullmatch("[0-9a-f]{64}", k) or type(v) is not int or not 0 < v <= revision
                   for k, v in failures.items()) or failures.get(profile_hash, 0) > verified):
        return False
    return cost.checker_miss(profile, result, now, checker_ttl(profile)) is None


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
