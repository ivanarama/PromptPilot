"""Git worktrees for tasks — the agent edits its own checkout, not yours.

A task with ``worktree=True`` runs in a linked checkout of its ``working_dir``
repository on branch ``pp/t<id>``, so:

  * the user's work tree keeps its own uncommitted state while the agent works;
  * the result is a branch — reviewable as a diff, mergeable, discardable;
  * a retry resumes the same branch instead of piling onto a half-changed tree;
  * two tasks in the same repository can run at the same time.

The checkout lands in ``<parent of repo root>/.pp-worktrees/<repo>-t<id>`` —
beside the repository, on the same filesystem, and computed from a path that was
resolved on the machine that will actually run the task. ``PP_WORKTREES_ROOT``
overrides the location with ``<root>/<repo>/t<id>``.

The herdr executor does not use this module: herdr creates worktree-backed
workspaces itself (``herdr worktree create``), which also keeps them visible and
removable in its UI. Both paths agree on the branch name.

Everything here works over ssh too: pass ``host`` and each git call is issued on
that machine, so the paths that come back belong to it.
"""

import os
import shutil
import subprocess
from pathlib import PurePosixPath, PureWindowsPath

from .config import (WORKTREE_BRANCH_PREFIX, WORKTREE_COPY, WORKTREE_DIRNAME,
                     WORKTREES_ROOT)
from .remote import REMOTE_CALL_TIMEOUT, as_remote, ssh_command

GIT_TIMEOUT = 120


class WorktreeError(Exception):
    """A worktree could not be prepared — the task cannot run as requested."""


def branch_for(task_id: int) -> str:
    """Branch a task's work lands on. Stable across retries by design."""
    return f"{WORKTREE_BRANCH_PREFIX}t{task_id}"


def _git(args, host=None):
    """Run a git command here or on `host`. Returns (rc, stdout, stderr)."""
    argv = ["git", *[str(a) for a in args]]
    timeout = GIT_TIMEOUT
    if as_remote(host):
        argv = ssh_command(host, argv)
        timeout = REMOTE_CALL_TIMEOUT
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise WorktreeError("ssh не найден" if host else "git не найден в PATH")
    except subprocess.TimeoutExpired:
        raise WorktreeError(f"git {args[0] if args else ''} не ответил за {timeout}s")
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _path_tools(path: str):
    """Path helpers matching the flavour of `path` — a Windows machine may be
    driven from this Linux one, so os.path is the wrong ruler for remote paths."""
    return PureWindowsPath if ("\\" in path and "/" not in path) or (len(path) > 1 and path[1] == ":") else PurePosixPath


def repo_root(path: str, host=None):
    """Top level of the git work tree holding `path`, or None if it is not one."""
    if not path:
        return None
    try:
        rc, out, _ = _git(["-C", path, "rev-parse", "--show-toplevel"], host)
    except WorktreeError:
        return None
    return out.splitlines()[0].strip() if rc == 0 and out else None


def is_git_repo(path: str, host=None) -> bool:
    return repo_root(path, host) is not None


def checkout_path(root: str, task_id: int) -> str:
    """Where task `task_id`'s checkout of repository `root` belongs."""
    p = _path_tools(root)
    root_p = p(root)
    name = root_p.name or "repo"
    if WORKTREES_ROOT:
        return str(p(WORKTREES_ROOT) / name / f"t{task_id}")
    return str(root_p.parent / WORKTREE_DIRNAME / f"{name}-t{task_id}")


def _branch_exists(root: str, branch: str, host=None) -> bool:
    rc, _, _ = _git(["-C", root, "show-ref", "--verify", "--quiet",
                     f"refs/heads/{branch}"], host)
    return rc == 0


def _registered(root: str, path: str, host=None) -> bool:
    """Is `path` already a live worktree of `root`?"""
    rc, out, _ = _git(["-C", root, "worktree", "list", "--porcelain"], host)
    if rc != 0:
        return False
    wanted = path.replace("\\", "/").rstrip("/").lower()
    return any(line[len("worktree "):].replace("\\", "/").rstrip("/").lower() == wanted
               for line in out.splitlines() if line.startswith("worktree "))


def copy_extras(root: str, path: str, host=None) -> list:
    """Carry gitignored essentials (.env & co) into the fresh checkout.

    Only files git itself refuses to carry: anything not ignored is either
    already in the checkout or would show up as a change the agent did not make.
    Skipped for remote machines — the files live there, not here. Best-effort:
    a task must not fail because a nice-to-have file could not be copied.
    """
    if host or not WORKTREE_COPY:
        return []
    copied = []
    for name in WORKTREE_COPY:
        src, dst = os.path.join(root, name), os.path.join(path, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        rc, _, _ = _git(["-C", root, "check-ignore", "-q", name])
        if rc != 0:
            continue
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
            copied.append(name)
        except OSError:
            pass
    return copied


def prepare(working_dir: str, task_id: int, host=None) -> dict:
    """Create (or reuse) the worktree for a task. Raises WorktreeError.

    Returns {"path", "branch", "root", "reused", "copied"}.
    """
    root = repo_root(working_dir, host)
    if not root:
        where = f" на {as_remote(host).host}" if host else ""
        raise WorktreeError(
            f"«{working_dir or '(не задана)'}»{where} — не git-репозиторий, "
            f"worktree создать не из чего")

    branch = branch_for(task_id)
    path = checkout_path(root, task_id)

    # Drop registrations whose directory a human already deleted; without this
    # `worktree add` refuses the path it still believes is taken.
    _git(["-C", root, "worktree", "prune"], host)

    if _registered(root, path, host):
        # A previous attempt of THIS task left its checkout behind: reuse it,
        # the work in progress there is the retry's best starting point.
        return {"path": path, "branch": branch, "root": root, "reused": True, "copied": []}

    add = ["-C", root, "worktree", "add"]
    add += [path, branch] if _branch_exists(root, branch, host) else ["-b", branch, path]
    rc, _, err = _git(add, host)
    if rc != 0:
        raise WorktreeError(f"git worktree add: {err or 'не удалось создать worktree'}")

    return {"path": path, "branch": branch, "root": root, "reused": False,
            "copied": copy_extras(root, path, host)}


def remove(root: str, path: str, host=None, force: bool = False) -> bool:
    """Drop a checkout. The branch survives — that is where the work is."""
    rc, _, _ = _git(["-C", root, "worktree", "remove", *(["--force"] if force else []), path], host)
    return rc == 0


def summary(path: str, branch: str, copied=(), attach: str = "") -> str:
    """The line a finished task shows so the user can go look at the result."""
    out = f"Worktree: {path}\nВетка: {branch}"
    if copied:
        out += f"\nСкопировано в worktree: {', '.join(copied)}"
    if attach:
        out += f"\n{attach}"
    return out


NO_CHANGES = "изменений нет"


def is_untouched(root: str, path: str, host=None) -> bool:
    """True only when the checkout provably holds nothing worth keeping.

    Anything unclear (git error, unreadable path) counts as touched — deleting
    an agent's only copy of its work is the one mistake worth being paranoid about.
    """
    return status(root, path, host) == NO_CHANGES


def status(root: str, path: str, host=None) -> str:
    """Short 'is there anything to review' line: commits ahead + dirty files.

    Ahead of the source work tree's HEAD, so it answers the question the user
    actually has — what does this branch carry that mine does not.

    Returns "" when the checkout could not be read at all: callers must never
    confuse "asked git, found nothing" with "never got an answer" — the second
    one is what a deleted checkout looks like from here.
    """
    try:
        rc, base, _ = _git(["-C", root, "rev-parse", "HEAD"], host)
        if rc != 0 or not base:
            return ""
        rc, ahead, _ = _git(["-C", path, "rev-list", "--count", "HEAD", f"^{base}"], host)
        if rc != 0:
            return ""
        rc, porcelain, _ = _git(["-C", path, "status", "--porcelain"], host)
        if rc != 0:
            return ""
    except WorktreeError:
        return ""

    bits = []
    if ahead.isdigit() and int(ahead):
        bits.append(f"коммитов: {ahead}")
    dirty = len([l for l in porcelain.splitlines() if l.strip()])
    if dirty:
        bits.append(f"незакоммиченных файлов: {dirty}")
    return ", ".join(bits) or NO_CHANGES
