"""Running commands on another machine over ssh.

Machines answer in one of two dialects:

  * ``posix``      — the payload goes through ``bash -lc '<quoted>'``;
  * ``powershell`` — the payload goes through
    ``powershell -EncodedCommand <base64/UTF-16LE>``.

The encoded form matters: a Windows OpenSSH server hands the command line to
``cmd.exe`` first, and nothing survives its quoting rules intact. Base64 has no
characters cmd treats specially, so the script arrives byte for byte.
"""

import base64
import shlex
import subprocess
from typing import NamedTuple

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]

# Safety cap for a remote call: the commands we issue are bounded on the
# far side, so anything longer means a hung ssh connection.
REMOTE_CALL_TIMEOUT = 180

POSIX = "posix"
POWERSHELL = "powershell"

# PowerShell decodes a native program's output with [Console]::OutputEncoding,
# so without this a UTF-8 answer (herdr's JSON, any Cyrillic) comes back mangled.
PS_PRELUDE = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"


class Remote(NamedTuple):
    """Where a command runs: ssh target + the shell that answers there."""
    host: str
    shell: str = POSIX


def as_remote(target):
    """Accept a Remote, a bare host string, or None (= this machine)."""
    if not target:
        return None
    return target if isinstance(target, Remote) else Remote(str(target))


def ps_quote(value) -> str:
    """Single-quoted PowerShell literal ('' escapes a quote)."""
    return "'" + str(value).replace("'", "''") + "'"


def _ps_encode(script: str) -> str:
    return base64.b64encode((PS_PRELUDE + script).encode("utf-16-le")).decode("ascii")


def ssh_script(target, script: str) -> list:
    """argv running a snippet written in the target's own shell dialect."""
    r = as_remote(target)
    # '--' ends ssh option parsing so a host that begins with '-' can't smuggle
    # an option like -oProxyCommand=... (which would run a local command). The
    # registry also validates host on the way in; this is defence in depth.
    if r.shell == POWERSHELL:
        return ["ssh", *SSH_OPTS, "--", r.host, "powershell", "-NoProfile",
                "-NonInteractive", "-EncodedCommand", _ps_encode(script)]
    # ssh flattens argv into one remote command line — quote the -lc payload
    return ["ssh", *SSH_OPTS, "--", r.host, "bash", "-lc", shlex.quote(script)]


def ssh_command(target, argv, env: dict = None) -> list:
    """argv running a program with arguments (and optional env) remotely.

    The program is named by basename: what its path is on this machine says
    nothing about the other one — it is resolved by the remote PATH.
    """
    r = as_remote(target)
    argv = [str(a) for a in argv]
    env = {k: v for k, v in (env or {}).items() if v}
    if r.shell == POWERSHELL:
        prefix = "".join(f"$env:{k}={ps_quote(v)};" for k, v in env.items())
        return ssh_script(r, prefix + "& " + " ".join(ps_quote(a) for a in argv))
    prefixed = [f"{k}={v}" for k, v in env.items()]
    inner = (["env", *prefixed] if prefixed else []) + argv
    return ssh_script(r, " ".join(shlex.quote(x) for x in inner))


_SHELL_PROBES = (
    (POSIX, "echo __pp_posix__", "__pp_posix__"),
    (POWERSHELL, "Write-Output '__pp_powershell__'", "__pp_powershell__"),
)


def detect_shell(host: str) -> str:
    """Which dialect `host` speaks: "posix", "powershell", or "" if it could
    not be reached at all.

    A Windows box with Git Bash wired into sshd answers the posix probe and is
    driven as posix — that is correct, not a misdetection.
    """
    for shell, script, marker in _SHELL_PROBES:
        try:
            proc = subprocess.run(ssh_script(Remote(host, shell), script),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=30,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode == 0 and marker in (proc.stdout or ""):
            return shell
    return ""
