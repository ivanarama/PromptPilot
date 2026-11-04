"""Explicit, task-scoped result files for Telegram delivery.

An agent copies deliverables into its attempt's output directory and emits one
PP-FILE line per file. Ordinary source links never authorize an upload.
"""
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .remote import POWERSHELL, as_remote, ps_quote, ssh_script

MAX_BYTES = 50_000_000
MAX_FILES = 5
MARKER = "PP-FILE:"


class RejectedFile(ValueError):
    """Permanent error safe to show without the source path or file contents."""


class MissingFile(RejectedFile):
    """A reply without file deliverables need not create a manifest."""


def _flavour(path):
    return PureWindowsPath if re.match(r"^[A-Za-z]:[\\/]", str(path)) else PurePosixPath


def output_root(task):
    base = getattr(task, "worktree_path", None) or getattr(task, "working_dir", None)
    started = getattr(task, "started_at", None)
    if not base or not started or not getattr(task, "id", None):
        return None
    if isinstance(started, datetime):
        started = started.astimezone(timezone.utc).isoformat()
    path = _flavour(base)(base)
    if (not path.is_absolute() or ".." in path.parts or str(base).startswith("\\\\")
            or any(ord(c) < 32 for c in str(base))):
        return None
    attempt = hashlib.sha256(str(started).encode()).hexdigest()[:12]
    return str(path / ".promptpilot-results" / f"{task.id}-{attempt}")


def prompt_instruction(task):
    root = output_root(task)
    if not getattr(task, "tg_chat_id", None) or not root:
        return ""
    return (
        "\n\n<telegram-result-files>\n"
        "If the user requests files as deliverables, copy only those finished files "
        f"to this output directory: {root}\n"
        "Keep this directory out of source commits. Do not copy credentials or private keys. "
        f"At most {MAX_FILES} files, each at most {MAX_BYTES} bytes; use an archive if needed. "
        "In the final answer, outside code blocks and before any closing verdict, "
        "add one line per deliverable: PP-FILE: <absolute path to the copied file>. "
        "Use the actual path, without angle brackets. PromptPilot will attach these files "
        "to the Telegram result. Ordinary file links do not attach files. "
        "If there are no requested file deliverables, add no PP-FILE lines.\n"
        "</telegram-result-files>"
    )


def visible_result(text):
    result, fenced = [], False
    for line in text.splitlines():
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
        if fenced or not line.strip().startswith(MARKER):
            result.append(line)
    return "\n".join(result)


def specs(task):
    root = output_root(task)
    if not root or not getattr(task, "tg_chat_id", None):
        return []
    flavour = _flavour(root)
    parent = flavour(root)
    result, seen, fenced = [], set(), False
    for line in (getattr(task, "result", "") or "").split("\n--- Meta ---", 1)[0].splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not line.startswith(MARKER):
            continue
        value = line[len(MARKER):].strip().strip('`"')
        path = flavour(value)
        if (not path.is_absolute() or ".." in path.parts or len(value) > 2048
                or any(ord(c) < 32 for c in value) or value.startswith("\\\\")):
            raise RejectedFile("Invalid result-file path")
        try:
            relative = path.relative_to(parent)
        except ValueError:
            raise RejectedFile("Result file is outside this task's output directory") from None
        if not relative.parts:
            raise RejectedFile("Result file must be a regular file")
        name = path.name
        if (name.lower() == ".env" or name.lower().startswith(".env.")
                or path.suffix.lower() in {".key", ".pem", ".pfx", ".p12"}
                or name.lower() in {"id_rsa", "id_ed25519", "id_ecdsa", "credentials.json"}
                or any(part.lower() == ".ssh" for part in relative.parts)):
            raise RejectedFile("Credentials and private keys are not result attachments")
        if flavour is PureWindowsPath and ':' in str(path)[2:]:
            raise RejectedFile("Alternate data streams are not result attachments")
        canonical = str(path)
        if path in seen:
            continue
        seen.add(path)
        if len(result) >= MAX_FILES:
            raise RejectedFile("Too many result files; use an archive")
        result.append({"path": canonical, "root": root, "name": name})
    return result


def _local_bytes(spec, limit):
    path, root = Path(spec["path"]), Path(spec["root"])
    try:
        if (path.is_symlink() or root.is_symlink() or root.parent.is_symlink()
                or not root.resolve().is_relative_to(root.parent.parent.resolve())
                or not path.resolve().is_relative_to(root.resolve())):
            raise RejectedFile("Result-file links outside the output directory are not allowed")
        if not path.exists():
            raise MissingFile("Result file is missing")
        if not path.is_file():
            raise RejectedFile("Result file is missing or not a regular file")
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise RejectedFile("Result file is not regular or exceeds the Telegram size limit")
            data = stream.read(limit + 1)
    except FileNotFoundError:
        raise MissingFile("Result file is missing") from None
    except (IsADirectoryError, PermissionError):
        raise RejectedFile("Result file is missing or unreadable") from None
    if len(data) > limit:
        raise RejectedFile("Result file exceeds the Telegram size limit")
    return data


def export_script(spec, shell, limit=MAX_BYTES):
    if shell == POWERSHELL:
        return (
            "$ErrorActionPreference='Stop';"
            f"$p=[IO.Path]::GetFullPath({ps_quote(spec['path'])});"
            f"$r=[IO.Path]::GetFullPath({ps_quote(spec['root'])}).TrimEnd([char]92)+[char]92;"
            "if(-not $p.StartsWith($r,[StringComparison]::OrdinalIgnoreCase)){exit 23};"
            "if(-not(Test-Path -LiteralPath $p)){exit 24};"
            "$i=Get-Item -LiteralPath $p;"
            "if($i.PSIsContainer -or ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)){exit 23};"
            "$d=$i.Directory;while($d){if($d.Attributes -band [IO.FileAttributes]::ReparsePoint){exit 23};$d=$d.Parent};"
            "$s=[IO.File]::OpenRead($p);try{"
            f"if($s.Length -gt {limit}){{exit 23}};"
            "$b=New-Object byte[] $s.Length;$n=0;"
            "while($n -lt $b.Length){$k=$s.Read($b,$n,$b.Length-$n);if($k -eq 0){throw 'short read'};$n+=$k};"
            "[Console]::WriteLine([Convert]::ToBase64String($b))"
            "}finally{$s.Dispose()}"
        )
    import shlex
    payload = base64.b64encode(json.dumps({**spec, "limit": limit}).encode()).decode()
    code = f"""import base64,json,os,pathlib,stat,sys
v=json.loads(base64.b64decode('{payload}'))
p=pathlib.Path(v['path']);r=pathlib.Path(v['root'])
if p.is_symlink() or r.is_symlink() or r.parent.is_symlink(): raise ValueError('link')
if not r.resolve().is_relative_to(r.parent.parent.resolve()) or not p.resolve().is_relative_to(r.resolve()): raise ValueError('scope')
if not p.exists(): sys.exit(24)
s=p.stat()
if not stat.S_ISREG(s.st_mode) or s.st_size>v['limit']: raise ValueError('size or type')
with p.open('rb') as f:
 s=os.fstat(f.fileno())
 if not stat.S_ISREG(s.st_mode) or s.st_size>v['limit']: raise ValueError('size or type')
 b=f.read(v['limit']+1)
if len(b)>v['limit']: raise ValueError('size')
print(base64.b64encode(b).decode())
"""
    return "python3 -c " + shlex.quote(code)


def read_bytes(spec, remote=None, limit=MAX_BYTES):
    if not remote:
        return _local_bytes(spec, limit)
    target = as_remote(remote)
    process = subprocess.run(
        ssh_script(target, export_script(spec, target.shell, limit)),
        capture_output=True, stdin=subprocess.DEVNULL, timeout=90,
    )
    if process.returncode:
        if process.returncode == 24:
            raise MissingFile("Result file is missing")
        if process.returncode == 255:
            raise OSError("Result-file machine is temporarily unavailable")
        raise RejectedFile("Remote result file is missing, unreadable, outside its directory, or too large")
    try:
        data = base64.b64decode(process.stdout.strip(), validate=True)
    except ValueError:
        raise RejectedFile("Invalid result-file response") from None
    if len(data) > limit:
        raise RejectedFile("Result file exceeds the Telegram size limit")
    return data


def native_context(agent, machine, endpoint, chat_id):
    sequence = agent.get('completion_seq')
    if sequence is None:
        sequence = agent.get('state_change_seq')
    if type(sequence) is not int or sequence < 0:
        return None
    nonce = uuid.uuid4().hex
    context = {
        'id': 'reply-' + nonce, 'started_at': nonce,
        'working_dir': agent.get('cwd') or agent.get('foreground_cwd'),
        'worktree_path': None, 'tg_chat_id': chat_id, 'machine': machine,
        'endpoint': list(endpoint) if endpoint else None,
        'pane': agent.get('pane_id'), 'terminal_id': agent.get('terminal_id'),
        'agent_session': agent.get('agent_session'), 'baseline_seq': sequence,
        'expires_at': time.time() + 86400, 'queued': False,
    }
    if not context['pane'] or not (context['terminal_id'] or context['agent_session']):
        return None
    from types import SimpleNamespace
    return context if output_root(SimpleNamespace(**context)) else None


def native_instruction(context):
    from types import SimpleNamespace
    root = output_root(SimpleNamespace(**context))
    return (
        '<telegram-result-files>\n'
        'For requested file deliverables in this reply, copy only the finished files '
        f'into this delivery directory: {root}\n'
        'This directory supersedes previous delivery directories for this reply. '
        'Do not copy credentials or private keys, and keep this directory out of commits. '
        f'At most {MAX_FILES} files, each at most {MAX_BYTES} bytes. '
        'After all file copies finish, write .pp-files.json in that directory with this schema: '
        '{"files":[{"name":"the actual copied filename","sha256":"its SHA-256 hex digest"}]}. '
        'Use filenames only in the manifest, no directories. Do not write the manifest '
        'unless there are finished requested file deliverables. Do not emit PP-FILE '
        'lines for this reply; PromptPilot reads the manifest automatically. '
        'The final human-readable answer can contain ordinary file links.\n'
        '</telegram-result-files>\n\n'
    )


def manifest_specs(context, remote):
    from types import SimpleNamespace
    task = SimpleNamespace(**context)
    root = output_root(task)
    if not root:
        raise RejectedFile('Invalid result directory')
    flavour = _flavour(root)
    path = str(flavour(root) / '.pp-files.json')
    try:
        raw = read_bytes({'path': path, 'root': root}, remote, limit=32768)
    except MissingFile:
        return []
    try:
        manifest = json.loads(raw.decode('utf-8-sig'))
        files = manifest['files']
        if not isinstance(files, list) or len(files) > MAX_FILES:
            raise ValueError()
        result, seen = [], set()
        for file in files:
            name, digest = file['name'], file['sha256']
            if (not isinstance(name, str) or not name or name in {'.', '..', '.pp-files.json'}
                    or flavour(name).name != name or len(flavour(name).parts) != 1
                    or not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest)):
                raise ValueError()
            task.result = MARKER + ' ' + str(flavour(root) / name)
            spec = specs(task)[0]
            if flavour(spec['path']) in seen:
                continue
            seen.add(flavour(spec['path']))
            result.append({**spec, 'expected_sha256': digest.lower()})
        return result
    except (ValueError, KeyError, TypeError, IndexError):
        raise RejectedFile('Invalid result-file manifest') from None
