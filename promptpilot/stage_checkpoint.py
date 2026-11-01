"""Publish an immutable reviewed tree, with a recoverable index/ref intent.

Claim/admission callbacks fence SQLite versions; only publication and the
lifecycle commit take its writer lock. Git locks are never silently removed
after a process crash: an operator must inspect the recorded intent first.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import tempfile
import threading

from .process_tree import OwnedProcess


class CheckpointError(RuntimeError):
    pass


class CheckpointBusyError(CheckpointError):
    pass


class _GitError(CheckpointError):
    def __init__(self, result):
        self.returncode = result.returncode
        self.stderr = result.stderr
        message = result.stderr.decode("utf-8", "replace").strip()
        if not message:
            command = " ".join(str(arg) for arg in result.args[3:])[:120]
            message = f"Git {command} failed (exit {result.returncode})"
        super().__init__(message[:400])


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _git(repo, *args, data=None, index=None, english=False):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    if english:
        env["LC_ALL"] = "C"
    if index:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(["git", "-C", str(repo), *args], input=data,
                            capture_output=True, timeout=30, env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise _GitError(result)
    return result.stdout


def _text(repo, *args, **kwargs):
    return _git(repo, *args, **kwargs).decode("utf-8").strip()


def _direct_head(repo):
    """Read one symbolic hop, including on older POSIX Git (Apple Git 2.37).

    Recursive symbolic-ref alone hides branch aliases. The legacy fallback
    reads only the regular HEAD file in Git's worktree-specific directory;
    publication still checks this identity under the prepared ref transaction.
    """
    try:
        return _text(repo, "symbolic-ref", "--no-recurse", "HEAD", english=True)
    except _GitError as exc:
        unsupported = any(line.startswith(b"error: unknown option ")
                          and b"`no-recurse'" in line for line in exc.stderr.splitlines())
        if exc.returncode != 129 or not unsupported:
            raise
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise CheckpointError("Git lacks --no-recurse and safe HEAD reading is unavailable")
    gitdir = Path(_text(repo, "rev-parse", "--absolute-git-dir")).resolve()
    path = Path(_text(repo, "rev-parse", "--path-format=absolute", "--git-path", "HEAD"))
    if path.name != "HEAD" or path.parent.resolve() != gitdir:
        raise CheckpointError("unexpected checkpoint HEAD location")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
            raise CheckpointError("checkpoint HEAD must be a bounded regular file")
        data = os.read(descriptor, 4097)
        if len(data) != before.st_size:
            raise CheckpointError("short read of checkpoint HEAD")
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        def identity(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise CheckpointError("checkpoint HEAD changed while reading")
    finally:
        os.close(descriptor)
    if (not data.startswith(b"ref: refs/heads/") or not data.endswith(b"\n")
            or data.count(b"\n") != 1 or b"\0" in data or len(data) > 4096):
        raise CheckpointError("checkpoint requires a direct local branch HEAD")
    try:
        branch = data[5:-1].decode("utf-8")
    except UnicodeDecodeError:
        raise CheckpointError("checkpoint HEAD is not UTF-8") from None
    _git(repo, "check-ref-format", branch)
    if _text(repo, "symbolic-ref", "HEAD") != branch:
        raise CheckpointError("symbolic branch aliases are unsupported")
    return branch


def _atomic(path, data):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def reviewed(repo, paths, signature):
    """Capture acceptance evidence before the pending checkpoint is persisted."""
    repo = Path(repo).resolve()
    branch = _direct_head(repo)
    head = _text(repo, "rev-parse", "HEAD")
    if _text(repo, "for-each-ref", "--format=%(symref)", branch):
        raise CheckpointError("symbolic branch aliases are unsupported")
    signatures = {}
    modes = {}
    base = _entries(repo, tree=head)
    filemode = _text(repo, "config", "--bool", "core.filemode") == "true"
    for path in paths:
        file = repo / path
        signatures[path] = signature(str(file)) if file.exists() or file.is_symlink() else "D"
        if signatures[path] == "unreadable":
            raise CheckpointError("unreadable reviewed path")
        modes[path] = _mode(file, base.get(path), filemode)
    return {"repo": str(repo), "head": head, "branch": branch, "signatures": signatures,
            "modes": modes, "filemode": filemode}


def _mode(path, base, filemode):
    if path.is_symlink():
        return "120000"
    if not path.exists():
        return None
    if not path.is_file():
        raise CheckpointError("non-regular checkpoint path")
    if filemode:
        return "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
    return base[0] if base else "100644"


def _entries(repo, *, tree=None, index=None):
    args = ("ls-tree", "-r", "-z", tree) if tree else ("ls-files", "--stage", "-z")
    entries = {}
    for record in _git(repo, *args, index=index).split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, _, oid = meta.split() if tree else (None, None, None)
        if not tree:
            mode, oid, stage = meta.split()
            if stage != b"0":
                raise CheckpointError("unmerged index cannot be checkpointed")
        path = name.decode("utf-8")
        entries[path] = (mode.decode(), oid.decode())
    return entries


def _replace_entries(repo, index, entries):
    records = []
    for path, entry in entries.items():
        mode, oid = entry or ("0", "0" * 40)
        records.append(f"{mode} {oid}\t{path}".encode("utf-8") + b"\0")
    _git(repo, "update-index", "-z", "--index-info", data=b"".join(records), index=index)


class _RefTransaction:
    """Bounded Git prepared transaction; keeps symbolic HEAD and ref fenced."""
    def __init__(self, repo, branch, old, new, empty_hooks):
        self.repo, self.branch, self.old, self.new = repo, branch, old, new
        self.empty_hooks = empty_hooks
        self.process = None
        self.output = queue.Queue()

    def _read(self):
        for line in self.process.stdout:
            self.output.put(line.strip())
        self.output.put(None)

    def _expect(self, expected):
        try:
            line = self.output.get(timeout=15)
        except queue.Empty:
            raise CheckpointError("Git ref transaction timed out") from None
        if line != expected:
            if line is None:
                self.process.wait(timeout=5)
                detail = self.process.stderr.read().decode("utf-8", "replace")[:400]
            else:
                detail = repr(line)
            raise CheckpointError(f"Git ref transaction refused: {detail}")

    def __enter__(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.owned = OwnedProcess.start([
            "git", "-C", str(self.repo), "-c", f"core.hooksPath={self.empty_hooks}",
            "update-ref", "--stdin"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=flags)
        self.process = self.owned.process
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            operation = (f"update HEAD {self.new} {self.old}" if self.new
                         else f"verify HEAD {self.old}")
            # Dereferencing HEAD makes Git fence both HEAD and its branch.
            # Check branch identity AFTER prepare and before any publication.
            commands = f"start\n{operation}\nprepare\n".encode()
            self.process.stdin.write(commands)
            self.process.stdin.flush()
            self._expect(b"start: ok")
            self._expect(b"prepare: ok")
            if _direct_head(self.repo) != self.branch:
                raise CheckpointError("checkpoint branch identity changed")
            if _text(self.repo, "for-each-ref", "--format=%(symref)", self.branch):
                raise CheckpointError("symbolic branch aliases are unsupported")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def commit(self):
        self.process.stdin.write(b"commit\n")
        self.process.stdin.flush()
        self._expect(b"commit: ok")

    def __exit__(self, *exc):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(b"abort\n")
                    self.process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.owned.terminate()
                    self.process.wait(timeout=5)
        finally:
            # A descendant can retain a pipe after Git itself exits. Close the
            # owned process boundary first, then let its reader observe EOF.
            # Closing a buffered pipe concurrently with iteration can corrupt
            # the reader (observed on macOS) or block on its internal lock.
            self.owned.close()
            self.reader.join(timeout=5)
            if self.reader.is_alive():
                raise CheckpointError("Git ref transaction output reader did not stop")
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass


def _fault(point):
    """Injection seam for process-crash recovery tests; production is a no-op."""


def confirm(pending, commit, signature):
    """Freshly confirm a cleaned-up publication receipt before projecting it.

    The immutable commit/index intent was created under the original admission.
    This read-only check follows the caller's new SQLite writer/version gate;
    it grants no new ref write or permission to accept changed working bytes.
    """
    repo = Path(pending["check_dir"]).resolve()
    evidence = pending.get("reviewed")
    if not evidence or evidence["repo"] != str(repo):
        raise CheckpointError("checkpoint receipt lost its reviewed repository evidence")
    if commit is not None and (not isinstance(commit, str) or len(commit) != 40
                               or any(char not in "0123456789abcdef" for char in commit)):
        raise CheckpointError("checkpoint receipt commit is not an exact Git object ID")
    expected_head = commit or evidence["head"]

    def identity():
        if (_direct_head(repo) != evidence["branch"]
                or _text(repo, "rev-parse", "HEAD") != expected_head
                or _text(repo, "for-each-ref", "--format=%(symref)", evidence["branch"])):
            raise CheckpointError("checkpoint receipt HEAD or branch changed before acceptance")

    identity()
    if not pending.get("checkpoint_paths"):
        return
    gitdir = Path(_text(repo, "rev-parse", "--absolute-git-dir"))
    index = Path(_text(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    operation = _digest((pending["workflow_id"] + ":" + pending["stage_id"]
                         + ":" + pending["run_id"]).encode())
    folder = gitdir / "promptpilot-checkpoints" / operation
    if commit:
        try:
            intent = json.loads((folder / "intent.json").read_text())
        except (ValueError, UnicodeError) as exc:
            raise CheckpointError("checkpoint receipt intent is unreadable") from exc
        if (not isinstance(intent, dict) or set(intent) != {
                "evidence", "index", "head", "branch", "tree", "commit", "before", "after"}):
            raise CheckpointError("checkpoint receipt intent shape changed")
        if (intent["commit"] != commit or intent["head"] != evidence["head"]
                or intent["evidence"] != evidence or intent["branch"] != evidence["branch"]
                or intent["index"] != str(index)):
            raise CheckpointError("checkpoint receipt intent identity changed")
        expected_index = intent["after"]
        if (_text(repo, "rev-parse", commit + "^{tree}") != intent["tree"]
                or _text(repo, "rev-parse", commit + "^") != intent["head"]):
            raise CheckpointError("checkpoint receipt commit tree or parent changed")
    else:
        expected_index = _digest((folder / "index.before").read_bytes())
    for name in pending.get("checkpoint_paths", []):
        path = repo / name
        actual = signature(str(path)) if path.exists() or path.is_symlink() else "D"
        if actual != evidence["signatures"][name]:
            raise CheckpointError("checkpoint receipt reviewed bytes changed before acceptance")
        if evidence["filemode"] and _mode(path, None, True) != evidence["modes"][name]:
            raise CheckpointError("checkpoint receipt reviewed mode changed before acceptance")
    if _digest(index.read_bytes()) != expected_index:
        raise CheckpointError("checkpoint receipt index changed before acceptance")
    identity()


@contextmanager
def restart(pending, observed_head, admit):
    """Revoke acceptance; never infer a successful checkpoint from a legacy ref."""
    repo = Path(pending["check_dir"]).resolve()
    gitdir = Path(_text(repo, "rev-parse", "--absolute-git-dir"))
    index = Path(_text(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    operation = _digest((pending["workflow_id"] + ":" + pending["stage_id"]
                         + ":" + pending["run_id"]).encode())
    folder = gitdir / "promptpilot-checkpoints" / operation
    hooks = folder / "empty-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    lock = Path(str(index) + ".lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise CheckpointBusyError("checkpoint index is locked; restart cannot steal an active operation") from None
    identity = os.fstat(descriptor)
    marker = json.dumps({"checkpoint_restart": operation, "pid": os.getpid()}).encode()
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        branch = _direct_head(repo)
        evidence = pending.get("reviewed")
        if evidence and (evidence["repo"] != str(repo) or evidence["branch"] != branch):
            raise CheckpointError("restart repository/branch differs from reviewed identity")
        current_index = index.read_bytes()
        digest = _digest(current_index)
        intent_path = folder / "intent.json"
        intent = json.loads(intent_path.read_text()) if intent_path.exists() else None
        rollback = None
        if intent:
            if (intent["evidence"] != evidence or intent["index"] != str(index)
                    or intent["branch"] != branch):
                raise CheckpointError("restart intent identity mismatch")
            if observed_head == intent["commit"]:
                raise CheckpointError("checkpoint was published; complete normal resume recovery first")
            if observed_head != intent["head"] or digest not in {intent["before"], intent["after"]}:
                raise CheckpointError("unknown checkpoint state; operator inspection required")
            if digest == intent["after"]:
                rollback = (folder / "index.before").read_bytes()
                if _digest(rollback) != intent["before"]:
                    raise CheckpointError("restart index backup damaged")
        elif evidence and observed_head != evidence["head"]:
            raise CheckpointError("unknown HEAD drift; inspect before restarting checkpoint")
        with ExitStack() as fences:
            with admit():
                fences.enter_context(_RefTransaction(repo, branch, observed_head, None, hooks))
                if index.read_bytes() != current_index:
                    raise CheckpointError("index changed while acquiring restart admission")
                if rollback is not None:
                    _atomic(index, rollback)
                yield {"legacy_ambiguous": not bool(evidence), "observed_head": observed_head,
                       "branch": branch, "index_sha256": digest,
                       "index_rollback": rollback is not None}
    finally:
        try:
            current = lock.stat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) and lock.read_bytes() == marker:
                lock.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def publish(pending, signature, claim, admit):
    evidence = pending.get("reviewed")
    if not evidence:
        if pending.get("reviewed_error"):
            raise CheckpointError("checkpoint evidence capture failed: " + pending["reviewed_error"])
        raise CheckpointError("legacy checkpoint lacks reviewed content; renewed review required")
    repo = Path(evidence["repo"]).resolve()
    if repo != Path(pending["check_dir"]).resolve():
        raise CheckpointError("checkpoint repository identity changed")
    paths = pending["checkpoint_paths"]
    if not paths:
        claim()
        with admit():
            yield None
        return
    for name in paths:
        path = Path(name)
        if path.is_absolute() or any(part.lower() in {"..", ".git"} for part in path.parts):
            raise CheckpointError("unsafe checkpoint path")
        if any(parent.is_symlink() for parent in (repo / name).parents if parent != repo):
            raise CheckpointError("symlink directory checkpoint path is unsupported")
    gitdir = Path(_text(repo, "rev-parse", "--absolute-git-dir"))
    index = Path(_text(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    operation = _digest((pending["workflow_id"] + ":" + pending["stage_id"]
                         + ":" + pending["run_id"]).encode())
    folder = gitdir / "promptpilot-checkpoints" / operation
    folder.mkdir(parents=True, exist_ok=True)
    hooks = folder / "empty-hooks"
    hooks.mkdir(exist_ok=True)
    lock = Path(str(index) + ".lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise CheckpointBusyError(f"index is locked; inspect {folder} before operator cleanup of stale locks") from None
    marker = json.dumps({"checkpoint": operation, "pid": os.getpid()}).encode()
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
        # The exact pending checkpoint/version is claimed before creating Git
        # objects. Preparation does not hold SQLite's global writer lock.
        claim()
        if _direct_head(repo) != evidence["branch"]:
            raise CheckpointError("checkout branch changed after review")
        intent_path = folder / "intent.json"
        current_index = index.read_bytes()
        if intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if intent["evidence"] != evidence or intent["index"] != str(index):
                raise CheckpointError("checkpoint intent identity mismatch")
        else:
            if _text(repo, "rev-parse", "HEAD") != evidence["head"]:
                raise CheckpointError("HEAD changed after review; renewed review required")
            base = _entries(repo, tree=evidence["head"])
            staged = _entries(repo)
            flags = _git(repo, "ls-files", "-v", "-z").split(b"\0")
            if any(flag and (flag[:1] == b"S" or flag[:1].islower()) for flag in flags):
                raise CheckpointError("sparse/assume-unchanged index requires a separate adapter")
            frozen = {}
            for name in paths:
                path = repo / name
                sig = signature(str(path)) if path.exists() or path.is_symlink() else "D"
                if sig != evidence["signatures"][name]:
                    raise CheckpointError("reviewed bytes changed; renewed gate/review required")
                if sig == "D":
                    entry = None
                else:
                    # Custom clean filters/encoding/gitlinks need separate
                    # reviewed adapters; standard Git CRLF normalization works.
                    attrs = _git(repo, "check-attr", "filter", "working-tree-encoding", "--", name)
                    if any(not line.endswith(b": unspecified") and not line.endswith(b": unset")
                           for line in attrs.splitlines()):
                        raise CheckpointError("custom Git filter/encoding is unsupported")
                    mode = _mode(path, base.get(name), evidence["filemode"])
                    if mode != evidence["modes"][name]:
                        raise CheckpointError("reviewed file mode changed")
                    if path.is_symlink():
                        mode = "120000"
                        data = os.fsencode(os.readlink(path))
                        frozen_sig = "sha256:" + _digest(data) + ":link"
                    else:
                        if not path.is_file() or mode not in {"100644", "100755"}:
                            raise CheckpointError("non-regular checkpoint entry")
                        data = path.read_bytes()
                        frozen_sig = f"sha256:{_digest(data)}:{len(data)}"
                    # The proof must describe the exact immutable buffer fed
                    # to Git, not two separate filesystem reads around it.
                    if frozen_sig != evidence["signatures"][name]:
                        raise CheckpointError("frozen bytes differ from reviewed content")
                    args = ("--stdin",) if mode == "120000" else ("--path=" + name, "--stdin")
                    blob = _text(repo, "hash-object", "-w", *args, data=data)
                    if signature(str(path)) != sig:
                        raise CheckpointError("file changed while freezing reviewed content")
                    entry = (mode, blob)
                if staged.get(name) not in (base.get(name), entry):
                    raise CheckpointError("divergent staged content on checkpoint path")
                frozen[name] = entry
            before = folder / "index.before"
            after = folder / "index.after"
            _atomic(before, current_index)
            _atomic(after, current_index)
            temp_index = folder / "commit.index"
            temp_index.unlink(missing_ok=True)
            _git(repo, "read-tree", evidence["head"], index=temp_index)
            _replace_entries(repo, temp_index, frozen)
            tree = _text(repo, "write-tree", index=temp_index)
            changed = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                               evidence["head"] + "^{tree}", tree).decode().split("\0")) - {""}
            if not changed <= set(paths):
                raise CheckpointError("checkpoint includes paths outside reviewed scope")
            if tree == _text(repo, "rev-parse", evidence["head"] + "^{tree}"):
                with ExitStack() as fences:
                    with admit():
                        fences.enter_context(_RefTransaction(
                            repo, evidence["branch"], evidence["head"], None, hooks))
                        yield None
                return
            message = f"stage {pending['stage_code']}: reviewed checkpoint\n\nGenerated-with: PromptPilot\n"
            commit = _text(repo, "commit-tree", tree, "-p", evidence["head"], data=message.encode())
            _replace_entries(repo, after, frozen)
            intent = {"evidence": evidence, "index": str(index), "head": evidence["head"],
                      "branch": evidence["branch"], "tree": tree, "commit": commit,
                      "before": _digest(current_index), "after": _digest(after.read_bytes())}
            _atomic(intent_path, json.dumps(intent, sort_keys=True).encode())
        before, after = folder / "index.before", folder / "index.after"
        if _digest(before.read_bytes()) != intent["before"] or _digest(after.read_bytes()) != intent["after"]:
            raise CheckpointError("checkpoint intent index backup damaged")
        if _text(repo, "rev-parse", intent["commit"] + "^{tree}") != intent["tree"]:
            raise CheckpointError("checkpoint commit tree mismatch")
        if _text(repo, "rev-parse", intent["commit"] + "^") != intent["head"]:
            raise CheckpointError("checkpoint commit parent mismatch")
        _fault("after_preparation_before_admission")
        with ExitStack() as fences:
            with admit():
                head = _text(repo, "rev-parse", "HEAD")
                digest = _digest(current_index)
                if head == intent["head"] and digest in {intent["before"], intent["after"]}:
                    # Before publication, even recovery must not reuse a PASS after
                    # working bytes change. No git-add reread is used for the commit.
                    for name in paths:
                        path = repo / name
                        sig = signature(str(path)) if path.exists() or path.is_symlink() else "D"
                        if sig != evidence["signatures"][name]:
                            raise CheckpointError("reviewed bytes changed; renewed gate/review required")
                        if _mode(path, None, evidence["filemode"]) != evidence["modes"][name] and evidence["filemode"]:
                            raise CheckpointError("reviewed file mode changed")
                    with _RefTransaction(repo, intent["branch"], head, intent["commit"], hooks) as tx:
                        _atomic(index, after.read_bytes())
                        _fault("after_index_before_ref")
                        tx.commit()
                    _fault("after_ref_before_database")
                elif head != intent["commit"] or digest != intent["after"]:
                    raise CheckpointError("unknown checkpoint ref/index state; operator inspection required")
                # Capture the receipt under both ref and SQLite admission. The
                # caller applies it only after this fence's cleanup succeeds,
                # under a new version gate and fresh confirm() readback.
                fences.enter_context(_RefTransaction(
                    repo, intent["branch"], intent["commit"], None, hooks))
                yield intent["commit"]

    finally:
        # Normal exceptions release only our own lock. Abrupt process death
        # leaves the marker for explicit, evidence-based operator inspection.
        try:
            current = lock.stat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) and lock.read_bytes() == marker:
                lock.unlink()
        except FileNotFoundError:
            pass
