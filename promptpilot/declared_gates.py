"""Prepare declared argv gates without shell parsing or process execution.

Owner-approved legacy shell strings retain their exact old path. Declared
inputs bind a new gate's scope; they are not a proof of arbitrary script
idempotence or a sandbox. The existing durable gate journal owns each attempt.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from .models import WorkflowGateCommand

MAX_INPUT_BYTES = 16*1024*1024


def display(command):
    if isinstance(command, str):
        return command
    if isinstance(command, WorkflowGateCommand):
        command = command.model_dump(mode="json")
    return json.dumps(command, ensure_ascii=False, separators=(",", ":"))


def unique(commands):
    result, seen = [], set()
    for command in commands:
        key = ("shell", command) if isinstance(command, str) else ("argv", json.dumps(command, sort_keys=True))
        if key not in seen:
            seen.add(key)
            result.append(command)
    return result


def _identity(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def _input_path(root, name):
    # Python 3.10 has no Path.is_junction(). lstat's reparse attribute also
    # covers NTFS junctions, which are not reported by is_symlink(). Check
    # every component again during context validation, including after hashing.
    if root.resolve(strict=True) != root:
        raise ValueError("declared input repository path changed")
    path = root
    for part in [None, *name.split("/")]:
        if part is not None:
            path = path/part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("declared input cannot traverse a link or reparse point: "+name)
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError("declared input is outside the repository: "+name)
    return path


def _file(root, name, budget):
    path = _input_path(root, name)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("declared input is not a regular file: "+name)
    if before.st_size>budget:
        raise ValueError("declared input byte budget exceeded: "+name)
    descriptor = os.open(path, os.O_RDONLY|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ValueError("declared input changed during open: "+name)
        digest = hashlib.sha256()
        size = 0
        while True:
            part = os.read(descriptor, 65536)
            if not part: break
            size += len(part)
            if size>budget: raise ValueError("declared input byte budget exceeded: "+name)
            digest.update(part)
        current = _input_path(root, name)
        if _identity(os.fstat(descriptor)) != _identity(before) or _identity(current.stat()) != _identity(before):
            raise ValueError("declared input changed while hashing: "+name)
        return dict(sha256=digest.hexdigest(),identity=_identity(before)),size
    finally:
        os.close(descriptor)


def _program(root, value, inputs):
    candidate = Path(value)
    if not candidate.is_absolute() and ("/" in value or "\\" in value):
        candidate = root/candidate
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("relative gate program is outside the repository")
    else:
        found = shutil.which(value)
        if not found:
            raise ValueError("gate program is unavailable: "+value)
        resolved = Path(found).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("gate program is not executable: "+value)
    if os.name == "nt" and resolved.suffix.lower() in {".bat", ".cmd"}:
        raise ValueError("declare an explicit interpreter instead of an implicit batch shell")
    if resolved.is_relative_to(root) and resolved.relative_to(root).as_posix() not in inputs:
        raise ValueError("repository gate program must be a declared input")
    return resolved


def prepare(commands, cwd):
    """Return complete preflight evidence. Any error prevents every factory."""
    root = Path(cwd).resolve()
    arguments, programs, files, errors = [], [], {}, []
    budget = MAX_INPUT_BYTES
    for index, command in enumerate(commands, start=1):
        if isinstance(command, str):
            arguments.append(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
                             if os.name=="nt" else ["/bin/sh", "-lc", command])
            programs.append(None)
            continue
        try:
            spec = WorkflowGateCommand.model_validate(command)
            for name in spec.inputs:
                if name not in files:
                    files[name],used = _file(root,name,budget)
                    budget -= used
            executable = _program(root,spec.argv[0],spec.inputs)
            argv = [str(executable),*spec.argv[1:]]
            if os.name=="nt" and len(subprocess.list2cmdline(argv).encode("utf-16-le"))//2>=32767:
                raise ValueError("declared gate command exceeds the Windows argument limit")
            arguments.append(argv)
            programs.append(dict(path=str(executable),identity=_identity(executable.stat())))
        except (OSError,ValueError) as exc:
            errors.append(f"Gate #{index}: {exc}")
            arguments.append([])
            programs.append(None)
    return dict(argv=arguments,programs=programs,inputs=files,validation_errors=errors)
