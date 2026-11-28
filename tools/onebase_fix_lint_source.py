"""Bind a previously enrolled OneBase lint supervisor to its verified, local Source read."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from types import ModuleType


BEGIN = " [Managed pre-lint Source v1] "
END = " [/Managed pre-lint Source]"
# Reviewed ABIs of the private operator bindings. All owner decisions, evidence
# and command paths still come from the operator's existing enrollment.
ABI = {
    "claim": ("claim-2277.py", "de2b6ecf43c5c4e55f8b246a304f663a49a76c5ac6bbdf18bd76a307453f0fb7"),
    "window": ("source_window.py", "432cc987a31e0f27a33aed5831f7cb9294c4268ed20660012541ecc955a710de"),
    "reader": ("onebase_fix_prepush.py", "d200a2c9ca352649c869db5d1a7bdd5850485d6ca27eaf7a602934fac638d817"),
}


def require(test, message):
    if not test:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def subject_line(prompt):
    lines = [line for line in prompt.splitlines() if line.startswith("#2277 at ")]
    require(len(lines) <= 1, "ambiguous scoped lint enrollment")
    return lines[0] if lines else None


def discover_scope(prompt):
    line = subject_line(prompt)
    if not line:
        require(BEGIN not in prompt and END not in prompt, "orphaned scoped lint instructions")
        return None
    if BEGIN in line or END in line:
        require(line.count(BEGIN) == line.count(END) == 1 and line.endswith(END),
                "incomplete scoped lint instructions")
        line = line.split(BEGIN)[0]
    found = re.match(r"#2277 at ([0-9a-f]{40}) uses OWNER ([1-9][0-9]*), ", line)
    require(found, "unsupported scoped lint subject")
    pins = {}
    for name, prefix in (("scope", "exact scope "), ("claim", "ordinary FIX guard "),
                         ("window", "single standard passive wait through "),
                         ("reader", "For this PR use the accepted canonical pre-push reader ")):
        matches = re.findall(re.escape(prefix) + r"(.+?) SHA256 ([0-9a-f]{64})(?=[., ]|$)", line)
        require(len(matches) == 1, f"ambiguous or missing scoped lint {name}")
        path, sha = matches[0]
        file = Path(path)
        require(file.is_absolute() and file.resolve(strict=True) == file and file.is_file()
                and digest(file.read_bytes()) == sha, f"scoped lint {name} binding changed")
        if name in ABI:
            require((file.name, sha) == ABI[name], f"unrecognized scoped lint {name} ABI")
        pins[name] = {"path": path, "sha256": sha}
    require(len({Path(p["path"]).parent for p in pins.values()}) == 1, "scoped operators differ")
    scope = json.loads(Path(pins["scope"]["path"]).read_bytes())
    require(scope["pr"] == 2277 and scope["source_head"] == found[1]
            and str(scope["owner_comment"]["id"]) == found[2], "scoped owner or head changed")
    return {"pr": 2277, "head": found[1], "owner_id": found[2], "pins": pins,
            "paragraph_sha256": digest(line.encode())}


def scoped_bindings(prompt, config_path, config_sha):
    original = subject_line(prompt)
    if original is None:
        return []
    base = original.split(BEGIN)[0]
    raw = config_path.read_bytes()
    require(digest(raw) == config_sha, "scoped lint config changed")
    config = json.loads(raw)
    require(discover_scope(prompt) == config["lint_source_scope"], "scoped lint enrollment changed")
    def command(script):
        return shlex.join([config["python"], "-IB", str(config_path.with_name(script)),
                           "--config", str(config_path), "--config-sha", config_sha])
    adapter = command("onebase_fix_lint_source.py")
    observer = command("onebase_fix_cache_observe.py")
    text = (f"The entry point for the enrolled read, run and recovery read above is {adapter}. "
            "Use 'read' for original claim readback, 'run' with the original arguments to invoke the same "
            "sealed supervisor, and 'recover' with the original pre-push read arguments for the pre-lint "
            "recovery check. Before deciding local lint is blocked by Source, run "
            "'check --selection FILE --source-receipt FILE' with the same entry point. "
            "It checks the live Source for the elected PR and HEAD. A deferred background scan may leave "
            "the existing verified publication readable. Save its decision. If passive observation is "
            f"needed, use {observer} --source-receipt FILE --seconds N for the same single window. "
            "Read back that same result and repeat the ordinary election where required. "
            "For any later push use all original strict publication and claim checks.")
    updated = base + BEGIN + text + END
    return [[original, updated]] if updated != original else []


def pinned_harness(config, expected):
    require(config.is_absolute() and config.resolve(strict=True) == config
            and config.parent == Path(__file__).resolve().parent, "use the installed scoped adapter")
    raw = config.read_bytes()
    require(digest(raw) == expected, "scoped lint config changed")
    files = json.loads(raw)["files"]
    path = config.with_name("onebase_fix_cache_guard.py")
    require(path.resolve(strict=True) == path, "invalid guard path")
    raw = path.read_bytes()
    require(digest(raw) == files[path.name], "installed guard changed")
    guard = ModuleType(path.stem)
    guard.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), guard.__dict__)
    guard.pinned_bytes(Path(__file__), files["onebase_fix_lint_source.py"])
    installation = guard.Installation(config, expected)
    scope = installation.config["lint_source_scope"]
    modules = {}
    for name, info in scope["pins"].items():
        path = Path(info["path"])
        require(path.stat().st_uid == os.geteuid(), "scoped operator ownership changed")
        if name == "scope":
            grant = json.loads(guard.pinned_bytes(path, info["sha256"]))
            require(grant["pr"] == scope["pr"] == 2277 and grant["source_head"] == scope["head"]
                    and str(grant["owner_comment"]["id"]) == scope["owner_id"], "owner grant changed")
        else:
            require((path.name, info["sha256"]) == ABI[name], "unexpected scoped operator version")
            modules[name] = guard.pinned_module(path, info["sha256"])
    claim = modules["claim"]
    require(installation.series == 2 and installation.root == claim.RUNS
            and installation.working_dir == claim.FIX and claim.HEAD == scope["head"]
            and claim.BASE == Path(scope["pins"]["window"]["path"]).parent,
            "operator enrollment and installation differ")
    return installation, modules



def verify_enrollment(inst, config, sha):
    task, started, _ = inst.identity()
    from contextlib import closing
    with closing(sqlite3.connect(inst.db.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT prompt FROM tasks WHERE id=? AND started_at=? AND status='running'",
                         (task, started)).fetchone()
    require(row and discover_scope(row[0]) == inst.config["lint_source_scope"]
            and scoped_bindings(row[0], config, sha) == [], "running task does not own this scoped lint binding")


class LintCheckpoint:
    def __init__(self, installation, modules, args):
        self.inst = installation
        self.pre = installation.prepush
        self.claim = modules["claim"]
        self.window = modules["window"]
        self.reader = modules["reader"]
        self.identity = installation.identity()
        self.task, self.started, _ = self.identity
        self.inst.verify_worker()
        self.source_path = self.pre.private(installation.root, args.source_receipt)
        self.source_bytes = self.source_path.read_bytes()
        self.prior = json.loads(self.source_bytes)
        self.original = self.pre.source_token(self.prior, self.task, self.started)
        self.used_deferred = "deferred_refresh" in self.prior
        if self.prior["state"] == "reused":
            witness = installation.checked_origin(self.prior, self.identity)
            require(witness["source_sha256"] == digest(self.source_bytes)
                    and self.prior["fix_pr_heads"].get("2277") == self.claim.HEAD,
                    "original FIX did not witness this scoped head")
        self.election = self.pre.read_json(installation.root, args.selection)
        scope = installation.config["lint_source_scope"]
        selected = self.reader.selected(self.election, 2277, self.claim.HEAD)
        require(selected["decision_id"] == scope["owner_id"] and selected["decision_sha256"] == self.claim.BODY
                and selected["proof"] == self.claim.PROOF and selected["epoch"] == self.claim.EPOCH,
                "election and pinned lint decision differ")
        # This is a private, individually loaded instance of the original read-only
        # recovery module. Its source binding and ticket identity remain byte-exact.
        self.reader.source_token = self.pre.source_token
        self.reader.current_fix_token = self.current_fix_token

    def current_fix_token(self, pr, head=None):
        require(pr == 2277 and head in (None, self.claim.HEAD), "wrong scoped read target")
        report = self.inst.current_report()
        def priority_read(live):
            accepted = self.inst.priority_read(live, self.original)
            if accepted:
                self.used_deferred = True
            return accepted
        return self.pre.current_fix_token(pr, self.claim.HEAD, report=report, priority_read=priority_read)

    def check(self, h, source, selection, pr, head, task, started):
        require((task, started) == self.identity[:2] and pr == 2277 and head == self.claim.HEAD
                and source == self.source_path and self.pre.private(self.inst.root, source).read_bytes() == self.source_bytes
                and selection == self.election and self.inst.identity() == self.identity,
                "scoped Source/election/attempt changed")
        folder = self.pre.passive_records(self.inst.root, task, started)
        passive = (folder / "observe.intent.json").exists() or not 0 <= time.time() - self.original["generated_at"] < 300
        self.pre.check_source_election(self.prior, selection, task, started, passive=True)
        if passive:
            token = self.reader.passive_source_token(self.inst.root, source, self.original, task, started,
                                                     self.identity[2], selection, pr)
        else:
            token = self.current_fix_token(pr, head)
            if token == self.original and self.prior["state"] == "reused":
                require(selection.get("snapshot_token") == token, "original scoped election changed")
            if token != self.original:
                require(token["profile_hash"] == self.original["profile_hash"]
                        and all(token[k] >= self.original[k] for k in ("epoch", "revision", "generated_at"))
                        and token["revision"] > self.original["revision"] and selection.get("task_id") == task
                        and selection.get("task_started_at") == started and selection.get("snapshot_token") == token,
                        "repeat the scoped election on the new Source")
        self.window.budget(h, task, started, token["profile_hash"])
        self.window.active_target(pr, head, task, started)
        self.inst.check_target(self.identity, pr, head)
        require(self.pre.private(self.inst.root, source).read_bytes() == self.source_bytes
                and self.inst.identity() == self.identity and 0 <= time.time() - token["generated_at"] < 300,
                "scoped Source expired or FIX changed during verification")
        return token

    def recovery(self, args):
        require(args.pr == 2277 and args.source == self.claim.HEAD, "wrong scoped recovery target")
        issue = self.reader.same_or_new
        def strict_ticket(path, receipt):
            self.current_fix_token(args.pr, args.source)
            require(not self.used_deferred, "a recovery ticket for publication requires the ordinary unblocked Source")
            return issue(path, receipt)
        self.reader.same_or_new = strict_ticket
        return self.reader.read(self.inst.root, args, self.task, self.started)

    def run(self, args):
        # Only the sealed claim's pinned Source window is adapted. The original
        # operator still owns selection, live OWNER, resource checks, flock,
        # one-shot receipt and process invocation, including its closing reads.
        original_load = self.claim.load
        def load_operator(path, sha):
            if path == self.claim.BASE / "source_window.py" and sha == self.claim.SOURCE_WINDOW_SHA:
                require(sha == ABI["window"][1], "changed lint Source window binding")
                return {**self.window.__dict__, "check": self.check}
            return original_load(path, sha)
        self.claim.load = load_operator
        return self.claim.invoke(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("read")
    check = sub.add_parser("check")
    run = sub.add_parser("run")
    recover = sub.add_parser("recover")
    for op in (check, run, recover):
        for arg in ("selection", "source-receipt"):
            op.add_argument("--" + arg, type=Path, required=True)
    run.add_argument("--worktree", type=Path, required=True)
    run.add_argument("--artifacts", type=Path, required=True)
    recover.add_argument("--pr", type=int, required=True)
    recover.add_argument("--source", required=True)
    recover.add_argument("--budget-receipt", type=Path, required=True)
    recover.add_argument("--pr-read", type=Path, required=True)
    recover.add_argument("--passive-source", action="store_true")
    args = parser.parse_args(argv)
    try:
        inst, modules = pinned_harness(args.config, args.config_sha)
        if args.action == "read":
            result = modules["claim"].readback()
        else:
            verify_enrollment(inst, args.config, args.config_sha)
            checkpoint = LintCheckpoint(inst, modules, args)
            if args.action == "run":
                return checkpoint.run(args)
            if args.action == "recover":
                result = checkpoint.recovery(args)
            else:
                token = checkpoint.check(checkpoint.reader.__dict__, checkpoint.source_path,
                                         checkpoint.election, 2277, checkpoint.claim.HEAD, *checkpoint.identity[:2])
                result = {"state": "ready", "snapshot_token": token,
                          "publication_authorized": False, "lint_claimed": False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "refused", "reason": str(exc), "publication_authorized": False}),
              file=sys.stderr)
        return 75 if args.action == "run" else 2


if __name__ == "__main__":
    sys.exit(main())
