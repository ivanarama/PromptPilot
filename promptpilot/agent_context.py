"""Bounded, project-scoped context from committed Git blobs, never arbitrary files."""
import hashlib
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 128 * 1024
MAX_SLICE = 16384


class ContextError(ValueError):
    """Finite messages safe for the public MCP response."""


def _require(condition, message):
    if not condition:
        raise ContextError(message)


def _relative(value, *, prefix=False):
    _require(isinstance(value, str) and 0 < len(value) <= 1024,
             "Invalid project-relative path")
    text = value[:-1] if prefix and value.endswith("/") else value
    _require(text and not PurePosixPath(text).is_absolute()
             and not any(char in text for char in ("\\", ":", "\0", "\n", "\r"))
             and all(part not in ("", ".", "..") for part in text.split("/")),
             "Invalid project-relative path")
    return value


def _git(repository, *args, max_bytes=200000):
    try:
        result = subprocess.run(["git", "-C", str(repository), *args],
                                capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContextError("Project context is temporarily unavailable") from exc
    _require(result.returncode == 0, "Requested committed file is unavailable")
    _require(len(result.stdout) <= max_bytes, "Project context exceeds the size limit")
    return result.stdout


def read_file(project, path, offset=0, length=MAX_SLICE):
    path = _relative(path)
    _require(type(offset) is int and 0 <= offset <= MAX_FILE_BYTES,
             "Invalid context offset")
    _require(type(length) is int and 1 <= length <= MAX_SLICE,
             "Invalid context length")
    allowed = project.get("readable_paths", ["README.md", "AGENTS.md"])
    _require(isinstance(allowed, list) and len(allowed) <= 100,
             "Project context policy is unavailable")
    for entry in allowed:
        _relative(entry, prefix=True)
    _require(any(path == entry or entry.endswith("/") and path.startswith(entry)
                 for entry in allowed), "File is outside the project's context policy")
    repository = Path(project["repository_path"])
    _require(repository.is_absolute() and repository.is_dir(), "Project is unavailable")
    for parent in (repository, *repository.parents):
        try:
            info = parent.lstat()
        except OSError as exc:
            raise ContextError("Project is unavailable") from exc
        _require(not stat.S_ISLNK(info.st_mode)
                 and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "Linked project directories are not supported")
    head = _git(repository, "rev-parse", "--verify", "HEAD", max_bytes=100).decode("ascii").strip()
    _require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head), "Invalid project revision")
    entries = _git(repository, "ls-tree", "-z", head, "--", path).split(b"\0")
    _require(len(entries) == 2 and not entries[-1], "Requested committed file is unavailable")
    metadata, name = entries[0].split(b"\t", 1)
    fields = metadata.split()
    _require(len(fields) == 3 and fields[0] in (b"100644", b"100755")
             and fields[1] == b"blob" and name.decode("utf-8") == path,
             "Only regular committed text files are available")
    blob = fields[2].decode("ascii")
    size = _git(repository, "cat-file", "-s", blob, max_bytes=40).strip()
    _require(size.isdigit() and int(size) <= MAX_FILE_BYTES,
             "Project context exceeds the size limit")
    raw = _git(repository, "cat-file", "blob", blob, max_bytes=MAX_FILE_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ContextError("Requested file is not UTF-8 text") from exc
    _require("\0" not in text, "Requested file is not UTF-8 text")
    _require(offset <= len(text), "Context offset is past the end of the file")
    chunk = text[offset:offset + length]
    return {"path": path, "head": head, "sha256": hashlib.sha256(raw).hexdigest(),
            "text": chunk, "offset": offset, "length": len(chunk),
            "total_characters": len(text), "next_offset": offset + len(chunk),
            "eof": offset + len(chunk) == len(text), "source": "committed_git_blob"}


def read_project_file(connection_id, project_id, path, offset=0, length=MAX_SLICE):
    from .agent_control import authorized_project
    return read_file(authorized_project(connection_id, project_id), path, offset, length)
