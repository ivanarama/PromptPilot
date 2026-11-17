"""Read-only receipt/artifact gate for any configured native external workflow."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sqlite3
import subprocess
import sys

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def default_command():
    """Owner-visible gate command for this installation, with no issue-specific paths."""
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "workflow", "verify-external", "{workflow_id}"]
    else:
        argv = [sys.executable, "-B", str(Path(__file__).resolve()), "{workflow_id}"]
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def relative_path(value, *, prefix=False):
    require(isinstance(value, str) and 0 < len(value) <= 1024, "Invalid artifact path")
    text = value[:-1] if prefix and value.endswith("/") else value
    require(text and not PurePosixPath(text).is_absolute()
            and not any(char in text for char in ("\\", ":", "\0", "\n", "\r"))
            and all(part not in ("", ".", "..") for part in text.split("/")), "Invalid artifact path")
    return value


def _git(repository, *args, input=None, accepted=(0,)):
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                 "-C", str(repository), *args], input=input,
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError("Git verification is unavailable") from exc
    require(result.returncode in accepted, "Patch or repository verification failed")
    require(len(result.stdout) <= MAX_ARTIFACT_BYTES, "Git response exceeds the size limit")
    return result.stdout


def verify_patch(repository, base_sha, patch, allowed_paths):
    require(isinstance(base_sha, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha),
            "Round has no proven Git baseline")
    require(isinstance(allowed_paths, list) and 0 < len(allowed_paths) <= 100,
            "Patch path policy is missing")
    for path in allowed_paths:
        relative_path(path, prefix=True)
    require(0 < len(patch) <= MAX_ARTIFACT_BYTES, "Patch is empty or too large")
    try:
        patch.decode("utf-8")
    except UnicodeError as exc:
        raise VerificationError("Patch is not UTF-8 text") from exc
    for marker in (b"GIT binary patch", b"new file mode ", b"deleted file mode ",
                   b"old mode ", b"new mode ", b"rename from ", b"rename to ",
                   b"copy from ", b"copy to "):
        require(marker not in patch, "File operations are outside the text-patch policy")
    def baseline():
        require(_git(repository, "rev-parse", "--verify", "HEAD").decode().strip() == base_sha,
                "Repository baseline changed")
        _git(repository, "diff", "--no-ext-diff", "--no-textconv", "--quiet", "HEAD", "--")

    baseline()
    paths = []
    for item in _git(repository, "apply", "--numstat", "-z", "-", input=patch).split(b"\0"):
        if not item:
            continue
        fields = item.split(b"\t", 2)
        require(len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit(),
                "Only a regular text diff is accepted")
        try:
            name = relative_path(fields[2].decode("utf-8"))
        except UnicodeError as exc:
            raise VerificationError("Patch path is not UTF-8") from exc
        require(any(name == entry or entry.endswith("/") and name.startswith(entry)
                    for entry in allowed_paths), "Patch changes a path outside the project policy")
        entries = _git(repository, "ls-tree", "-z", base_sha, "--", name).split(b"\0")
        require(len(entries) == 2 and not entries[-1], "Patch target is not a committed regular file")
        metadata, stored_name = entries[0].split(b"\t", 1)
        fields = metadata.split()
        require(len(fields) == 3 and fields[0] in (b"100644", b"100755")
                and fields[1] == b"blob" and stored_name.decode("utf-8") == name,
                "Patch target is not a committed regular file")
        size = _git(repository, "cat-file", "-s", fields[2].decode("ascii")).strip()
        require(size.isdigit() and int(size) <= MAX_ARTIFACT_BYTES,
                "Patch target exceeds the size limit")
        paths.append(name)
    require(paths and len(paths) == len(set(paths)), "Patch has no unique changed paths")
    _git(repository, "apply", "--cached", "--check", "--whitespace=error-all", "-", input=patch)
    baseline()
    return paths


def verify(workflow_id, database):
    require(isinstance(workflow_id, str) and re.fullmatch(r"wf_[0-9a-f]{32}", workflow_id),
            "A native workflow id is required")
    database = Path(database).absolute()
    require(database.is_file(), "Workflow database is unavailable")
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        row = conn.execute("""SELECT w.repository_path,w.config_json,rd.base_sha,
            c.job_id,c.client_id,c.outcome,c.expected_output_sha,c.input_sha256 AS claim_input,
            r.output_json,r.output_sha256,r.input_sha256
            FROM workflows w JOIN workflow_rounds rd
            ON rd.workflow_id=w.id AND rd.round_no=w.current_round
            JOIN workflow_runs r ON r.workflow_id=w.id AND r.round_id=rd.id
            JOIN external_inbox_claims_v1 c ON c.workflow_id=w.id AND c.run_id=r.id
            WHERE w.id=? AND r.role='executor' AND r.status='completed'
            AND c.state='submitted' ORDER BY c.created_at DESC LIMIT 1""", (workflow_id,)).fetchone()
        require(row is not None, "Current round has no confirmed external submission")
        require(row["outcome"] == "completed" and row["input_sha256"] == row["claim_input"],
                "External assignment is incomplete or changed")
        configuration = json.loads(row["config_json"])
        policy = configuration.get("external_result_policy")
        require(isinstance(policy, dict) and not set(policy) - {"artifacts", "text_artifacts", "patch"},
                "Project external result policy is missing or invalid")
        expected = policy.get("artifacts")
        require(isinstance(expected, list) and 0 < len(expected) <= 64
                and all(isinstance(x, str) for x in expected)
                and len(expected) == len(set(expected)), "Expected artifacts are not configured")
        for name in expected:
            relative_path(name)
        require(configuration.get("external_executor", {}).get("client_id") == row["client_id"],
                "External executor binding changed")
        output = json.loads(row["output_json"])
        canonical = json.dumps(output, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode()
        require(hashlib.sha256(canonical).hexdigest() == row["output_sha256"] == row["expected_output_sha"],
                "Native submission proof mismatch")
        require(output.get("external", {}).get("performer") == "external-client:" + row["client_id"],
                "External performer proof mismatch")
        artifacts = list(conn.execute("SELECT path,sha256,content FROM external_inbox_artifacts_v1 WHERE job_id=?",
                                      (row["job_id"],)))
        require(len(artifacts) == len(expected) and {a["path"] for a in artifacts} == set(expected),
                "Submitted artifacts differ from the project policy")
        data = {}
        total = 0
        for item in artifacts:
            raw = bytes(item["content"])
            total += len(raw)
            require(total <= MAX_ARTIFACT_BYTES and raw, "Artifacts are empty or exceed the size limit")
            require(hashlib.sha256(raw).hexdigest() == item["sha256"], "Artifact proof mismatch")
            data[item["path"]] = raw
        text_paths = policy.get("text_artifacts", [])
        require(isinstance(text_paths, list) and all(isinstance(x, str) and x in data for x in text_paths),
                "Invalid text artifact policy")
        for name in text_paths:
            try:
                require("\0" not in data[name].decode("utf-8"), "Text artifact contains binary content")
            except UnicodeError as exc:
                raise VerificationError("Text artifact is not UTF-8") from exc
        changed = []
        if "patch" in policy:
            patch_policy = policy["patch"]
            require(isinstance(patch_policy, dict) and set(patch_policy) == {"artifact", "allowed_paths"}
                    and isinstance(patch_policy["artifact"], str) and patch_policy["artifact"] in data,
                    "Invalid patch policy")
            changed = verify_patch(row["repository_path"], row["base_sha"],
                                   data[patch_policy["artifact"]], patch_policy["allowed_paths"])
        return {"workflow_id": workflow_id, "receipt_integrity": "verified",
                "artifacts": sorted(data), "changed_paths": changed,
                "repository_files_applied": False, "content_acceptance": "requires independent reviewer"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_id")
    args = parser.parse_args(argv)
    data_dir = os.environ.get("PP_DATA_DIR")
    try:
        require(data_dir, "PP_DATA_DIR is required for read-only workflow verification")
        print(json.dumps(verify(args.workflow_id, Path(data_dir) / "promptpilot.db"), ensure_ascii=False))
        return 0
    except (VerificationError, sqlite3.Error, ValueError, TypeError, KeyError, OSError):
        print("External result verification failed; inspect the current receipt and project policy.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
