"""Register an operator-verified OneBase resolution for a later base-sync confirmation."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


REPO = "ivanarama/onebase"


def publish(path, content):
    """Publish complete bytes once, allowing an identical concurrent install."""
    fd, name = tempfile.mkstemp(dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        if not stat.S_ISREG(path.lstat().st_mode) or path.read_bytes() != content:
            raise ValueError(f"conflicting resolution record at {path}")
    finally:
        tmp.unlink()


def read_comment(comment_id):
    return json.loads(subprocess.check_output(
        ["gh", "api", f"repos/{REPO}/issues/comments/{comment_id}"], timeout=30))


def install(state, raw, expected_sha, approvals, *, read=read_comment):
    if len(raw) > 1048576:
        raise ValueError("manifest exceeds the verifier's 1 MiB limit")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha:
        raise ValueError("manifest differs from the approved SHA-256")
    data = json.loads(raw)
    number = data["pull_request"]
    pins = [data[k] for k in ("source", "main", "tree")]
    if (data["repository"] != REPO or type(number) is not int or number <= 0 or
            not all(isinstance(pin, str) and re.fullmatch("[0-9a-f]{40}", pin) for pin in pins)):
        raise ValueError("invalid manifest identity")
    conflicts = data.get("conflicts")
    if not isinstance(conflicts, dict) or not conflicts or not all(
        name and isinstance(entries, dict) and set(entries) == {"source", "main", "resolved"}
        and all(isinstance(v, str) and re.fullmatch(r"100644 blob [a-f0-9]{40}", v)
                for v in entries.values()) for name, entries in conflicts.items()
    ):
        raise ValueError("invalid conflict manifest")
    if not 1 <= len(approvals) <= 16:
        raise ValueError("expected one to sixteen owner approvals")
    records = []
    seen = set()
    last = None
    for i, (number_text, body_sha) in enumerate(approvals):
        comment_id = int(number_text)
        if not 0 < comment_id < 2**63 or comment_id in seen:
            raise ValueError("invalid or duplicate approval")
        seen.add(comment_id)
        comment = read(comment_id)
        body = comment["body"]
        stamp = (comment["created_at"], comment_id)
        if last is not None and stamp <= last:
            raise ValueError("approvals must follow timeline order")
        last = stamp
        hashes = set(re.findall(r"(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])", body))
        if not (comment["id"] == comment_id and comment["user"]["login"] == "ivanarama"
                and comment["author_association"] == "OWNER"
                and comment["issue_url"] == f"https://api.github.com/repos/{REPO}/issues/{number}"
                and comment["created_at"] == comment["updated_at"]
                and hashlib.sha256(body.encode()).hexdigest() == body_sha
                and hashes.issuperset(pins) and (i > 0 or digest in hashes)):
            raise ValueError(f"approval {comment_id} differs from the operator's evidence")
        records.append({"id": comment_id, "body_sha256": body_sha})

    if not state.is_absolute() or state.resolve(strict=True) != state or not state.is_dir():
        raise ValueError("use the worker's existing private pipeline state directory")
    directory = state
    for part in ("merge-resolutions", *REPO.split("/"), str(number)):
        directory = directory / part
        directory.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError(f"unexpected resolution directory {directory}")
    pointer = {"version": 1, "resolution_sha256": digest, "approvals": records}
    encoded = (json.dumps(pointer, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > 1048576:
        raise ValueError("approval record exceeds the verifier's 1 MiB limit")
    publish(directory / (digest + ".json"), raw)
    path = directory / (pins[-1] + ".json")
    publish(path, encoded)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--sha256", required=True, help="digest in the owner's decision")
    parser.add_argument("--approval", action="append", nargs=2, required=True,
                        metavar=("COMMENT_ID", "BODY_SHA256"), help="repeat in timeline order")
    args = parser.parse_args()
    if os.environ.get("PP_TASK_ID"):
        parser.error("run from the operator account")
    data_dir = Path(os.environ.get("PP_DATA_DIR", Path.home() / ".promptpilot"))
    state = Path(os.environ.get("PP_PIPELINE_STATE_DIR", data_dir / "pipelinectl-state"))
    print(install(state, args.manifest.read_bytes(), args.sha256, args.approval))
