"""Keep orchestration tokens and test databases outside the agent's OS identity."""

import os
import re
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def owner_sid():
    output = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], timeout=15)
    match = re.search(rb"S-1-5-(?:\d+-)*\d+", output)
    if not match:
        raise RuntimeError("Cannot identify the worker account")
    return match.group().decode("ascii")


def protect_directory(directory):
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("Private directories cannot be symbolic links")
    if os.name == "nt":
        subprocess.run(
            [
                "icacls",
                str(directory),
                "/inheritance:r",
                "/grant:r",
                f"*{owner_sid()}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
                "/Q",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    else:
        directory.chmod(0o700)
