"""Keep private evidence and observe an existing Source snapshot after a OneBase FIX push.

These operations cannot authorize or perform a GitHub write.
"""
import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


CACHE_URL = "http://127.0.0.1:8420/api/pipeline-insights/onebase?refresh=false"
SHA = r"[a-f0-9]{40}"
DIGEST = r"[a-f0-9]{64}"
MARKER = re.compile(rf"<!-- pp:fix-pushed from=({SHA}) head=({SHA}) review-comment=([1-9][0-9]*) "
                    rf"claim=([1-9][0-9]*) epoch-sha256=({DIGEST}) -->")


def artifact(root, name):
    path = Path(name)
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or not path.is_relative_to(root) or not path.is_file()):
        raise ValueError("expected a private regular FIX artifact outside the temporary worktree")
    return path


def evidence(root, head, manifest_path, report_path):
    manifest_path = artifact(root, manifest_path)
    report_path = artifact(root, report_path)
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    body = report_path.read_bytes()
    markers = [line for line in body.decode("utf-8").splitlines() if "pp:fix-pushed" in line]
    match = MARKER.fullmatch(markers[0]) if len(markers) == 1 else None
    if (not match or match[2] != head or match[1] != manifest["source"] or match[1] == head
            or manifest["head"] != head or not re.fullmatch(SHA, manifest["tree"])):
        raise ValueError("manifest and report must identify the same transition")
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("missing acceptance artifacts")
    for name, expected in files.items():
        with artifact(root, name).open("rb") as fd:
            checksum = hashlib.sha256()
            for chunk in iter(lambda: fd.read(1024 * 1024), b""):
                checksum.update(chunk)
        if not re.fullmatch(DIGEST, expected) or checksum.hexdigest() != expected:
            raise ValueError("acceptance artifact changed")
    return {"source": match[1], "head": head, "tree": manifest["tree"], "marker": match[0],
            "report": str(report_path), "report_sha256": hashlib.sha256(body).hexdigest(),
            "manifest": str(manifest_path), "manifest_sha256": hashlib.sha256(raw).hexdigest()}


def put_once(path, data):
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True
    finally:
        os.unlink(temp)


def report_state(root, record, comments, pr, graph):
    # The normal FIX gate obtains and validates the complete paginated REST read.
    pages = json.loads(artifact(root, comments).read_bytes())
    if not isinstance(pages, list) or not pages or not all(isinstance(page, list) for page in pages):
        raise ValueError("expected pages from the complete REST comment read")
    matches = []
    for page in pages:
        for comment in page:
            if comment["user"]["login"] != "ivanarama":
                continue
            body = comment["body"]
            lines = [line for line in body.splitlines()
                     if "<!-- pp:fix-pushed " in line and "head=" + record["head"] in line]
            if not lines:
                continue
            if (lines != [record["marker"]] or not comment.get("created_at")
                    or comment["created_at"] != comment.get("updated_at")
                    or hashlib.sha256(body.encode("utf-8")).hexdigest() != record["report_sha256"]
                    or type(comment.get("id")) is not int or comment["id"] <= 0):
                raise ValueError("conflicting or edited report for this HEAD; human readback required")
            matches.append(comment)
    if len(matches) > 1:
        raise ValueError("duplicate reports for this HEAD; human readback required")
    if not matches:
        if graph is not None:
            raise ValueError("GraphQL report has no matching REST comment")
        return None
    comment = matches[0]
    if graph is None:
        raise ValueError("REST report requires --report-node from a current GraphQL node read")
    response = json.loads(artifact(root, graph).read_bytes())
    node = response["data"]["node"]
    if (response.get("errors") or not isinstance(node, dict) or node.get("__typename") != "IssueComment"
            or not isinstance(comment.get("node_id"), str) or not comment["node_id"]
            or node["id"] != comment["node_id"] or str(node["fullDatabaseId"]) != str(comment["id"])
            or node["lastEditedAt"] is not None or node["author"]["login"] != comment["user"]["login"]
            or node["body"] != comment["body"]
            or type(node["pullRequest"]["number"]) is not int or node["pullRequest"]["number"] != pr
            or node["pullRequest"]["repository"]["nameWithOwner"] != "ivanarama/onebase"):
        raise ValueError("GraphQL report differs, was edited, or belongs to another PR")
    return comment["id"]


def receipt(root, args):
    if args.pr < 1 or not re.fullmatch(SHA, args.head):
        raise ValueError("expected a PR number and a full commit SHA")
    folder = root / "post-push-v1" / str(args.pr)
    path = folder / (args.head + ".json")
    intent = path.with_suffix(".report-intent")
    if root.resolve() != root or folder.resolve() != folder or path.is_symlink() or intent.is_symlink():
        raise ValueError("unexpected receipt path")
    if args.action == "record":
        record = evidence(root, args.head, args.manifest, args.report)
        data = json.dumps(record, sort_keys=True).encode()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not put_once(path, data) and path.read_bytes() != data:
            raise ValueError("conflicting evidence for this transition")
    else:
        record = json.loads(path.read_bytes())
        if record != evidence(root, args.head, record["manifest"], record["report"]):
            raise ValueError("original report or manifest changed")
    report_id = None
    if getattr(args, "comments", None):
        report_id = report_state(root, record, args.comments, args.pr, args.report_node)
    elif getattr(args, "report_node", None):
        raise ValueError("--report-node requires the matching REST --comments read")
    if args.action == "claim-report":
        if report_id is not None or not put_once(intent, record["report_sha256"].encode()):
            raise ValueError("report already exists or POST was claimed; reconcile by reading GitHub")
    return {"pr": args.pr, **record, "verified_report_id": report_id, "report_post_claimed": intent.exists(),
            "publication_authorized": False}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def read_cache(timeout):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(CACHE_URL, timeout=timeout) as response:
        return json.load(response)


def valid_stamp(value):
    return type(value) in (int, float) and math.isfinite(value)


def observe(prior, task, started, seconds, read=read_cache,
            clock=time.monotonic, now=time.time, sleep=time.sleep):
    if not (prior.get("state") == "refreshed" and prior.get("source_refreshed") is True
            and type(prior.get("task_id")) is int and prior["task_id"] == task
            and task > 0 and started and prior.get("task_started_at") == started
            and prior.get("native_session_bound") is True and prior.get("profile_id") == "onebase"
            and prior.get("queue_id") == "fix" and 1 <= seconds <= 300):
        raise ValueError("a successful Source receipt from this FIX attempt is required")
    original = prior["snapshot_token"]
    expected = original["profile_hash"]
    if (not re.fullmatch(DIGEST, expected) or not valid_stamp(original["generated_at"])
            or any(type(original.get(k)) is not int or original[k] < 0 for k in ("epoch", "revision"))):
        raise ValueError("invalid source receipt")
    deadline = clock() + seconds
    attempts = 0
    while clock() < deadline:
        attempts += 1
        try:
            report = read(min(20, deadline - clock()))
        except HTTPError:
            raise
        except (TimeoutError, ConnectionError, http.client.IncompleteRead):
            report = None
        except URLError as exc:
            if not isinstance(exc.reason, (TimeoutError, ConnectionError)):
                raise
            report = None
        if report is not None:
            cache = report["cache"]
            if (report.get("repository") != "ivanarama/onebase" or report.get("profile_id") != "onebase"
                    or cache.get("profile_hash") != expected):
                raise ValueError("Native cache identity changed")
            if report["diagnostics"].get("checker_failed") or report["diagnostics"].get("exit_code") != 0:
                raise ValueError("Native checker failed")
            blocked = cache.get("refresh_blocked")
            if blocked not in (None, "", "scan_in_progress", "priority_waiter"):
                raise ValueError("Source refused: " + str(blocked))
            token = cache.get("token") or {}
            stamp = token.get("generated_at")
            if (cache.get("available") is True and cache.get("complete") is True
                    and cache.get("stale") is False and cache.get("invalidated") is False
                    and not cache.get("refresh_blocked") and cache.get("source") in ("durable", "memory")
                    and token.get("profile_hash") == expected and type(token.get("epoch")) is int
                    and type(token.get("revision")) is int and valid_stamp(stamp)
                    and cache.get("generated_at") == stamp and cache.get("ttl_seconds") == 300
                    and token["epoch"] >= original["epoch"] and token["revision"] >= original["revision"]
                    and (token["revision"] > original["revision"] or token == original)
                    and stamp >= original["generated_at"] and 0 <= now() - stamp < 300
                    and clock() < deadline):
                return {"fresh_snapshot": token, "attempts": attempts, "publication_authorized": False}
        if deadline - clock() <= 30:
            break
        sleep(30)
    return {"fresh_snapshot": None, "attempts": attempts, "publication_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("record", "read", "claim-report"):
        command = sub.add_parser(name)
        command.add_argument("--pr", required=True, type=int)
        command.add_argument("--head", required=True)
        if name in {"read", "claim-report"}:
            command.add_argument("--comments", type=Path, required=name == "claim-report")
            command.add_argument("--report-node", type=Path,
                                 help="GraphQL node(id: REST node_id) response for an existing report")
        if name == "record":
            command.add_argument("--report", required=True, type=Path)
            command.add_argument("--manifest", required=True, type=Path)
    command = sub.add_parser("observe")
    command.add_argument("--source-receipt", required=True, type=Path)
    command.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "observe":
            value = observe(json.loads(args.source_receipt.read_bytes()), int(os.environ["PP_TASK_ID"]),
                            os.environ["PP_TASK_STARTED_AT"], args.seconds)
            print(json.dumps(value))
            return 0 if value["fresh_snapshot"] else 3
        result = receipt(Path.home() / ".promptpilot" / "fix-runs", args)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
