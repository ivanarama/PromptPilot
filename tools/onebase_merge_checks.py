"""Run a fixed offline OneBase Mac MERGE check and retain its original evidence.

Exit 0 is local evidence only. Exit 1 is a failed check. Exit 3 requires the ordinary fresh check.
No command in this tool requests GitHub access or write admission.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack

CHECKS = {"build": 600, "packages": 1200, "ui": 3600, "vet": 600,
          "trade": 300, "modules": 180, "diff": 60}
PACKAGES = {"./internal/metadata", "./internal/configcheck"}
WAIT_REASONS = {"scan_in_progress", "budget_in_flight", "fairness_waiter"}
MAX_LOG = 16 * 1024 * 1024
LIFETIME = 24 * 3600
GIT = "/usr/bin/git"
SANDBOX = "/usr/bin/sandbox-exec"
VAULT = Path.home() / ".promptpilot" / "merge-checks-v1"
DATABASE = Path.home() / ".promptpilot" / "promptpilot.db"


class FreshRequired(Exception):
    """The normal procedure must run the check afresh."""


class CheckFailed(Exception):
    """An executed check reported a failure; the original logs are retained."""


def require(condition, reason):
    if not condition:
        raise FreshRequired(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def command(args):
    if args.package:
        require(args.check == "packages" and set(args.package) <= PACKAGES and
                len(set(args.package)) == len(args.package), "unsupported package plan")
    if args.race:
        require(args.check == "packages", "unsupported race plan")
    if args.check == "packages":
        require(args.package, "explicit packages required")
        flags = ["-race"] if args.race else []
        return ["go", "test", *flags, "-count=1", "-timeout=10m", "-p=2", "-json", *args.package]
    require(args.check in ("build", "vet", "diff"), "use the original ordinary check")
    return {
        "build": ["go", "build", "./..."], "vet": ["go", "vet", "./..."],
        "diff": [GIT, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                 "diff", "--no-ext-diff", "--no-textconv", "--cached", "--check"],
    }[args.check]


def safe_env():
    # Only explicit, fixed values reach repository code. An active feature or
    # external service calls for the original environment and a fresh check.
    blocked = ("ONEBASE_", "TEST_", "PG", "POSTGRES", "DATABASE_", "CGO_", "GODEBUG",
               "GOWORK", "GOFLAGS", "GOEXPERIMENT", "NODE_OPTIONS", "DYLD_", "LD_PRELOAD",
               "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET",
               "GOAMD64", "GOARM", "GOARCH", "GOOS", "GOTMPDIR", "GOCACHEPROG")
    for name, value in os.environ.items():
        if name == "CGO_ENABLED" and value in ("", "1"):
            continue
        if (name.startswith(blocked) or name in {"CC", "CXX", "AR", "PKG_CONFIG", "GCCGO"}) and value:
            raise FreshRequired("non-default external service, feature or tool environment")
    return {"LANG": "C", "LC_ALL": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "GOMAXPROCS": "2", "CGO_ENABLED": "1", "GOTOOLCHAIN": "local", "GOENV": "off",
            "GOWORK": "off", "GOPROXY": "off", "GOSUMDB": "off", "GOTELEMETRY": "off",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1"}


def read(args, env=None, cwd=None):
    return subprocess.check_output(args, env=env, cwd=cwd, stderr=subprocess.DEVNULL, timeout=120)


def git(*args, env, cwd=None):
    return read([GIT, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                 "-c", "core.hooksPath=/dev/null", *args], env, cwd)


def identity(args, env, checkout, canonical):
    def g(*values):
        return git(*values, env=env, cwd=checkout).strip()
    require(checkout.samefile(os.fsdecode(g("rev-parse", "--show-toplevel"))), "not a checkout root")
    common = Path(os.fsdecode(g("rev-parse", "--path-format=absolute", "--git-common-dir"))).resolve()
    require(common.samefile(canonical / ".git") and not checkout.samefile(canonical),
            "a separate worktree of the native MERGE repository is required")
    gd = Path(os.fsdecode(g("rev-parse", "--absolute-git-dir"))).resolve()
    require(gd.parent == common / "worktrees", "a linked MERGE worktree is required")
    require(g("config", "--get", "remote.origin.url") in
            {b"https://github.com/ivanarama/onebase.git", b"git@github.com:ivanarama/onebase.git"},
            "unexpected repository")
    require(g("rev-parse", "HEAD").decode() == args.expect_source and
            (gd / "MERGE_HEAD").read_text().splitlines() == [args.expect_base] and
            g("write-tree").decode() == args.expect_tree, "source, main or staged tree changed")
    require(not g("ls-files", "--others", "-z"), "checkout contains extra or ignored files")
    # The index can hide modified files through stat caching, assume-unchanged
    # or skip-worktree flags. Compare real bytes and executable mode to each blob.
    for record in git("ls-files", "--stage", "-z", env=env, cwd=checkout).split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        require(stage == b"0" and mode in (b"100644", b"100755"), "unsupported staged entry")
        path = checkout / os.fsdecode(name)
        st = path.lstat()
        require(stat.S_ISREG(st.st_mode) and path.resolve() == path and
                bool(st.st_mode & 0o100) == (mode == b"100755"), "checkout entry differs from index")
        blob = hashlib.sha1(b"blob " + str(st.st_size).encode() + b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                blob.update(chunk)
        require(blob.hexdigest().encode() == oid, "checkout bytes differ from staged tree")
    return common, gd


def git_metadata(args, checkout, common, gitdir, dest, env):
    for path in (common / "info/attributes", gitdir / "info/attributes",
                 Path("/etc/gitattributes"), Path.home() / ".config/git/attributes"):
        require(not path.exists() or not path.stat().st_size, "local Git attributes require fresh checks")
    custom = subprocess.run([GIT, "config", "--local", "--name-only", "--get-regexp",
                             r"^(diff\.|filter\.|extensions\.|core\.(attributesfile|whitespace|eol|autocrlf)$)"],
                            env=env, cwd=checkout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    require(custom.returncode == 1, "non-default Git diff or checkout settings")
    require(not git("rev-parse", "--shared-index-path", env=env, cwd=checkout).strip(),
            "split Git index unsupported")
    tags = git("for-each-ref", "--format=%(objectname) %(refname)", "refs/tags",
               env=env, cwd=checkout)
    (dest / "refs").mkdir()
    (dest / "objects").mkdir()
    (dest / "HEAD").write_text(args.expect_source + "\n")
    (dest / "MERGE_HEAD").write_text(args.expect_base + "\n")
    (dest / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n")
    (dest / "packed-refs").write_bytes(tags)
    (dest / "index").write_bytes((gitdir / "index").read_bytes())
    isolated = {**env, "GIT_DIR": str(dest), "GIT_WORK_TREE": str(checkout),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
    require(git("write-tree", env=isolated, cwd=checkout).strip().decode() == args.expect_tree,
            "isolated index differs from candidate")
    return isolated, digest(tags)


def origin_eligible(task, current_id, started_at):
    if not task or task["id"] == current_id or task["series_id"] != 4 or task["status"] != "completed":
        return False
    if task["started_at"] != started_at or task["verdict"] != "ПУСТО":
        return False
    body = (task["result"] or "").rsplit("\n--- Meta ---", 1)[0].rstrip().splitlines()
    return bool(body) and body[-1].strip() in {
        "ИТОГ: ПУСТО — нет допуска: " + reason for reason in WAIT_REASONS}


class NativeAttempt:
    def __init__(self):
        home = Path.home()
        self.plist = home / "Library/LaunchAgents/com.promptpilot.worker.plist"
        self.worker_config = self.plist.read_bytes()
        argv = plistlib.loads(self.worker_config)["ProgramArguments"]
        require(len(argv) >= 2 and argv[-1] == "worker", "unexpected native worker")
        binary = Path(argv[-2]).resolve(strict=True)
        self.build = binary.parent.parent
        require(self.build.parent == home / "PromptPilotBuild" and binary == self.build / "dist/pp",
                "unexpected native build path")
        sys.path.insert(0, str(self.build / "source"))
        from promptpilot import db, pipeline_insights
        profiles = pipeline_insights._profiles()
        self.policy = pipeline_insights._with_shared_budget_floor(
            pipeline_insights._github_budget_policy(profiles["onebase"]))
        self.db = db
        self.task_id = int(os.environ.get("PP_TASK_ID", "0"))
        require(self.task_id > 0, "native MERGE task required")
        self.started_at = os.environ.get("PP_TASK_STARTED_AT")
        self.binding = self.guard()
        with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as db:
            self.canonical = Path(db.execute("SELECT working_dir FROM tasks WHERE id=?",
                                           (self.task_id,)).fetchone()[0]).resolve(strict=True)

    def guard(self):
        require(self.plist.read_bytes() == self.worker_config, "native worker build changed")
        s = self.db.pipeline_source_refresh_state(self.policy["lease_scope"], self.task_id,
                                                 "onebase", "merge", self.policy["costs"])
        g = s["guard"]
        require(g["series_id"] == 4 and g["started_at"] == self.started_at
                and g["route"] in ("skill", "fallback_targeted"), "ordinary native MERGE required")
        current = {k: g[k] for k in ("started_at", "task_id", "series_id", "execution_sha256",
                                      "route", "cost", "session_id", "herdr_pane", "token")}
        require(not hasattr(self, "binding") or current == self.binding, "native attempt changed")
        return current

    def eligible(self, origin_id, origin_start):
        with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            task = db.execute("SELECT id,series_id,status,started_at,verdict,result FROM tasks WHERE id=?",
                              (origin_id,)).fetchone()
        return origin_eligible(task, self.task_id, origin_start)


def sandbox_profile(readable, scratch, compiler_cache=None):
    def subpath(p):
        return "(subpath " + json.dumps(str(p)) + ")"
    roots = ["/System", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/libexec", "/usr/share",
             "/bin", "/sbin", "/Library/Developer", "/private/etc/localtime",
             "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", *readable, scratch]
    writable = [scratch]
    if compiler_cache is not None:
        roots.append(compiler_cache)
        writable.append(compiler_cache)
    # stat()/realpath() must traverse the ancestors, without listing their
    # contents or opening any sibling file (secrets and receipts live there).
    parents = sorted({str(parent) for root in roots for parent in Path(root).parents})
    ancestor_rules = " ".join("(literal " + json.dumps(p) + ")" for p in parents)
    return "\n".join(["(version 1)", "(allow default)", "(deny file-read*)",
                      "(allow file-read-metadata " + ancestor_rules + ")",
                      "(allow file-read* " + " ".join(map(subpath, roots)) + ")",
                      "(deny file-write*)", "(allow file-write* " + " ".join(map(subpath, writable)) +
                      ' (literal "/dev/null"))', "(deny network*)", "(deny appleevent-send)",
                      "(deny signal)", "(deny mach-lookup)", "(deny process-info*)", "(deny iokit-open)"])


def tools_for(args, env):
    h = Path.home()
    arch = {"x86_64": "amd64", "arm64": "arm64"}[platform.machine()]
    cache = h / "go/pkg/mod"
    roots = (cache / ("golang.org/toolchain@v0.0.1-" + args.expect_go + ".darwin-" + arch),
             h / (".local/opt/go-" + args.expect_go.removeprefix("go")))
    root = next((p.resolve() for p in roots if (p / "bin/go").is_file()), None)
    require(root, "expected Go toolchain unavailable")
    node = (h / ".nvm/versions/node" / args.expect_node / "bin/node").resolve(strict=True)
    go = root / "bin/go"
    require(read([str(go), "env", "GOVERSION"], env).decode().strip() == args.expect_go and
            read([str(node), "--version"], env).decode().strip() == args.expect_node,
            "Go or Node version differs")
    compiler = Path(read(["/usr/bin/xcrun", "--find", "clang"], env).decode().strip()).resolve()
    sdk = Path(read(["/usr/bin/xcrun", "--show-sdk-path"], env).decode().strip()).resolve()
    developer = Path(read(["/usr/bin/xcode-select", "-p"], env).decode().strip()).resolve()
    real_git = Path(read(["/usr/bin/xcrun", "--find", "git"], env).decode().strip()).resolve()
    toolfiles = [go, node, real_git, Path(SANDBOX), compiler, sdk / "SDKSettings.json",
                 *(p for p in sorted((root / "pkg/tool" / ("darwin_" + arch)).iterdir()) if p.is_file())]
    stamp = {str(p): file_digest(p) for p in toolfiles}
    stamp["os_build"] = read(["/usr/bin/sw_vers", "-buildVersion"], env).decode().strip()
    stamp["developer"] = str(developer)
    stamp["architecture"] = arch
    # APFS changes ctime on any file or metadata write, including a restored
    # mtime. This host-local identity also covers headers, archives, stdlib and
    # SDK sources without reading their many gigabytes on every attempt.
    if args.check != "diff":
        h = hashlib.sha256()
        for directory in (root, developer):
            for base, dirs, files in os.walk(directory):
                dirs.sort()
                for name in sorted([*dirs, *files]):
                    path = Path(base, name)
                    st = path.lstat()
                    h.update(encoded((str(path), st.st_dev, st.st_ino, st.st_mode, st.st_size,
                                      st.st_mtime_ns, st.st_ctime_ns)))
        stamp["installed_tree"] = h.hexdigest()
    return root, node, cache, real_git, stamp


def module_inputs(checkout, cache):
    """Only expose modules named by the staged go.sum and pin cache presence.

    Go verifies module bytes against its local .ziphash. Checking .ziphash against
    the checkout's independent go.sum also catches a replaced local hash.
    """
    readable = set()
    materialized = []
    go_sum = checkout / "go.sum"
    for line in go_sum.read_text().splitlines() if go_sum.exists() else ():
        name, version, checksum = line.split()
        is_mod = version.endswith("/go.mod")
        version = version.removesuffix("/go.mod")
        require(all(re.fullmatch(r"[\w.~-]+", s, re.ASCII) and s not in (".", "..")
                    for s in name.split("/")) and re.fullmatch(r"v[\w.+~-]+", version, re.ASCII),
                "unrecognized module path")
        def escape(s):
            return re.sub("[A-Z]", lambda m: "!" + m[0].lower(), s)
        name, version = escape(name), escape(version)
        prefix = cache / "cache/download" / name / "@v"
        directory = cache / (name + "@" + version)
        if is_mod:
            path = prefix / (version + ".mod")
            if path.exists():
                contents = digest(path.read_bytes()) + "  go.mod\n"
                computed = base64.b64encode(hashlib.sha256(contents.encode()).digest()).decode()
                require(checksum == "h1:" + computed, "cached go.mod differs from go.sum")
                readable.add(path)
                materialized.append(str(path))
        else:
            zhash = prefix / (version + ".ziphash")
            if directory.exists() or zhash.exists():
                require(zhash.read_text().strip() == checksum, "cached module differs from go.sum")
                for p in (directory, zhash, prefix / (version + ".zip"), prefix / (version + ".info")):
                    if p.exists():
                        require(p.resolve() == p, "unsupported module-cache symlink")
                        readable.add(p)
                        materialized.append(str(p))
    return sorted(readable), digest(encoded(sorted(materialized)))


def signature(key, identity_key, task, started, timestamp, stdout, stderr):
    return hmac.new(key, encoded([identity_key, task, started, timestamp,
                                  digest(stdout), digest(stderr)]), hashlib.sha256).hexdigest()


class Vault:
    def __init__(self, path):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = path.lstat()
        require(stat.S_ISDIR(st.st_mode) and not (st.st_mode & 0o077) and st.st_uid == os.getuid(),
                "evidence directory must be private")
        self.path = path
        key = path / "key"
        try:
            with os.fdopen(os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
                f.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        require(not key.is_symlink() and not (key.stat().st_mode & 0o077), "unsafe evidence key")
        self.key = key.read_bytes()
        require(len(self.key) == 32, "incomplete evidence key")
        self.db = sqlite3.connect(path / "evidence.sqlite", timeout=5)
        self.db.execute("CREATE TABLE IF NOT EXISTS evidence (k TEXT, task INTEGER, start TEXT,"
                        " ts INTEGER, stdout BLOB, stderr BLOB, sig TEXT, PRIMARY KEY(k,task,start))")

    def load(self, k, attempt):
        for row in self.db.execute("SELECT task,start,ts,stdout,stderr,sig FROM evidence "
                                   "WHERE k=? AND ts BETWEEN ? AND ? ORDER BY ts DESC",
                                   (k, int(time.time()) - LIFETIME, int(time.time()))):
            task, start, ts, out, err, sig = row
            if (hmac.compare_digest(sig, signature(self.key, k, task, start, ts, out, err))
                    and attempt.eligible(task, start)):
                return task, out, err
            break
        return None

    def discard(self, k):
        with self.db:
            self.db.execute("DELETE FROM evidence WHERE k=?", (k,))

    def save(self, k, attempt, out, err):
        now = int(time.time())
        task, start = attempt.task_id, attempt.started_at
        sig = signature(self.key, k, task, start, now, out, err)
        with self.db:
            self.db.execute("DELETE FROM evidence WHERE ts < ?", (now - LIFETIME,))
            self.db.execute("DELETE FROM evidence WHERE k=?", (k,))
            self.db.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?,?)",
                            (k, task, start, now, out, err, sig))
            # Bound the storage even if the MERGE queue never goes idle.
            self.db.execute("DELETE FROM evidence WHERE rowid NOT IN "
                            "(SELECT rowid FROM evidence ORDER BY ts DESC,rowid DESC LIMIT 64)")


def run_child(args, cwd, env, output, prefix, guard, limit):
    with (output / (prefix + ".stdout")).open("xb") as out, (output / (prefix + ".stderr")).open("xb") as err:
        p = subprocess.Popen(args, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True)
        deadline = time.monotonic() + limit
        try:
            while True:
                try:
                    p.wait(timeout=min(20, max(0.1, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
                guard()
                if time.monotonic() > deadline:
                    raise CheckFailed("ordinary command deadline reached")
                if p.returncode is not None:
                    return p.returncode
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()


def check_exit(rc, artifacts, prefix):
    if rc == 0:
        return
    # Ordinary assertion, compilation, vet or integrity failures are final.
    # An explicitly identified sandbox/offline infrastructure incompatibility
    # requires the ordinary check in its original environment.
    restrictions = (b"operation not permitted", b"module lookup disabled by goproxy=off",
                    b"network is unreachable", b"permission denied")
    restricted = False
    for name in (prefix + ".stdout", prefix + ".stderr"):
        with (artifacts / name).open("rb") as stream:
            for line in stream:
                if re.search(rb"^.{1,500}\.(?:go|s|c|h):\d+(?::\d+)?:", line):
                    raise CheckFailed("compiler or vet reported a diagnostic")
                if line.startswith(b"{"):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        event = None
                    if isinstance(event, dict) and event.get("Action") == "fail" and event.get("Test"):
                        raise CheckFailed("test failed; inspect retained original logs")
                restricted |= any(marker in line.lower() for marker in restrictions)
    if restricted:
        raise FreshRequired("offline sandbox restriction; inspect retained original logs")
    raise CheckFailed("check failed; inspect retained original logs")


def verify_packages(out, packages):
    expected = {"github.com/ivantit66/onebase/" + p[2:] for p in packages}
    passed = []
    tested = set()
    for line in out.splitlines():
        e = json.loads(line)
        require(e.get("Package") in expected and e["Action"] not in ("skip", "fail", "build-fail"),
                "skipped, failed or unexpected tests require a fresh classification")
        if e["Action"] == "pass":
            if e.get("Test"):
                tested.add(e["Package"])
            else:
                passed.append(e["Package"])
    require(set(passed) == tested == expected and len(passed) == len(expected),
            "missing package test terminal")


def execute(args):
    require(platform.system() == "Darwin" and sys.flags.isolated and not sys.flags.optimize,
            "isolated Mac Python required")
    require(args.number > 0 and all(re.fullmatch("[0-9a-f]{40}", s) for s in
            (args.expect_source, args.expect_base, args.expect_tree)), "invalid candidate identity")
    require(re.fullmatch(r"go\d+\.\d+\.\d+", args.expect_go) and
            re.fullmatch(r"v\d+\.\d+\.\d+", args.expect_node), "invalid tool versions")
    cmd = command(args)
    env = safe_env()
    attempt = NativeAttempt()
    cwd = Path.cwd().resolve()
    common, gitdir = identity(args, env, cwd, attempt.canonical)
    artifacts = args.artifacts.resolve()
    require(args.artifacts.is_absolute() and not artifacts.is_relative_to(cwd) and
            not artifacts.is_relative_to(VAULT) and not artifacts.exists(), "new external artifacts required")
    artifacts.mkdir(mode=0o700)
    go_root, node, modcache, real_git, tools = tools_for(args, env)
    module_paths, modules = ([], None) if args.check == "diff" else module_inputs(cwd, modcache)
    inputs = {"number": args.number, "source": args.expect_source, "main": args.expect_base,
              "tree": args.expect_tree, "command": cmd, "env": env, "tools": tools,
              "runner": file_digest(Path(__file__)), "build": str(attempt.build), "modules": modules,
              "task_definition": attempt.binding["execution_sha256"]}
    vault = Vault(VAULT)
    k = None
    try:
        with ExitStack() as stack:
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pp-merge-check-"))).resolve()
            metadata = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pp-merge-git-"))).resolve()
            isolated, tags = git_metadata(args, cwd, common, gitdir, metadata, env)
            k = digest(encoded({**inputs, "tags": tags}))
            # Go build/vet execute the installed compiler, linker and vet only.
            # Never expose their writable cache to a candidate test or go run.
            compiler_cache = None
            if args.check in ("build", "vet"):
                compiler_cache = VAULT / "compiler-cache"
                compiler_cache.mkdir(mode=0o700, exist_ok=True)
            local = {**isolated, "HOME": str(scratch), "TMPDIR": str(scratch), "GOPATH": str(scratch / "go"),
                     "GOROOT": str(go_root), "GOMODCACHE": str(modcache),
                     "GOCACHE": str(compiler_cache or scratch / "gocache"),
                     "DEVELOPER_DIR": tools["developer"],
                     "PATH": os.pathsep.join((str(go_root / "bin"), str(node.parent),
                                             str(real_git.parent), env["PATH"]))}
            # Permit repository objects and refs, never the user's home, credentials,
            # scheduler, receipt store, host network, or outside output directory.
            readable = [cwd, common / "objects", metadata, go_root, *module_paths, node]
            policy_path = artifacts / "check.sb"
            policy_path.write_text(sandbox_profile(readable, scratch, compiler_cache))
            sandbox_argv = [SANDBOX, "-f", str(policy_path)]
            # Verify module contents on the consumer as well. The sandbox prevents
            # project code from changing the host's Go modules or toolchain.
            hit = vault.load(k, attempt)
            if args.check != "diff":
                # Go's builtin integrity verifier probes even absent transitive
                # modules. It executes no project code and can read the host's
                # readonly module cache so those paths return ENOENT normally.
                verifier = artifacts / "verify.sb"
                verifier.write_text(sandbox_profile([*readable, modcache], scratch))
                verify_env = {**local, "GOCACHE": str(scratch / "gocache")}
                rc = run_child([SANDBOX, "-f", str(verifier), str(go_root / "bin/go"), "mod", "verify"],
                               cwd, verify_env, artifacts, "dependencies", attempt.guard, 180)
                check_exit(rc, artifacts, "dependencies")
            if hit:
                task, out, err = hit
                (artifacts / "check.stdout").write_bytes(out)
                (artifacts / "check.stderr").write_bytes(err)
                state = "reused"
            else:
                executable = go_root / "bin/go" if cmd[0] == "go" else real_git
                invoke = [str(executable), *cmd[1:]]
                rc = run_child([*sandbox_argv, *invoke], cwd, local, artifacts, "check",
                               attempt.guard, CHECKS[args.check])
                check_exit(rc, artifacts, "check")
                outpath, errpath = artifacts / "check.stdout", artifacts / "check.stderr"
                require(outpath.stat().st_size + errpath.stat().st_size <= MAX_LOG,
                        "evidence exceeds retention limit")
                out, err = outpath.read_bytes(), errpath.read_bytes()
                task, state = attempt.task_id, "executed"
            if args.check == "packages":
                verify_packages(out, args.package)
            identity(args, env, cwd, attempt.canonical)
            attempt.guard()
            if not hit:
                vault.save(k, attempt, out, err)
            result = {"state": state, "check": args.check, "origin_task": task, "current_task": attempt.task_id,
                      "source": args.expect_source, "base": args.expect_base, "tree": args.expect_tree,
                      "evidence_key": k, "stdout_sha256": digest(out), "stderr_sha256": digest(err)}
            (artifacts / "result.json").write_bytes(encoded(result))
            return result
    except CheckFailed:
        if k is not None:
            vault.discard(k)
        raise
    finally:
        vault.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", type=int, required=True)
    for name in ("source", "base", "tree", "go", "node"):
        parser.add_argument("--expect-" + name, required=True)
    parser.add_argument("--check", choices=CHECKS, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument("--race", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = execute(args)
    except CheckFailed as exc:
        print(json.dumps({"state": "failed", "reason": str(exc)}))
        return 1
    except (FreshRequired, OSError, ValueError, KeyError, TypeError, ImportError,
            sqlite3.Error, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, FreshRequired) else type(exc).__name__
        print(json.dumps({"state": "fresh_required", "reason": reason}))
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
