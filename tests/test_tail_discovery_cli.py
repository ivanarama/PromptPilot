"""Run the installed pipelinectl module with fresh processes and a local GitHub peer."""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/project"
GH_BRIDGE = """
import json, os, sys, urllib.request
args = sys.argv[1:]
graphql = args == ["api", "graphql", "--input", "-"]
url = os.environ["TAIL_TEST_HTTP"] + ("/graphql" if graphql else "/unexpected")
data = sys.stdin.buffer.read() if graphql else repr(args).encode()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(urllib.request.Request(url, data=data), timeout=15) as response:
    raw = response.read()
    sys.stdout.buffer.write(raw)
    if json.loads(raw).get("errors"):
        print("gh: GraphQL query failed", file=sys.stderr)
        sys.exit(1)
"""


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def node(number, merged_at, base="main"):
    return {
        "__typename": "PullRequest", "number": number, "state": "MERGED", "repository": {"nameWithOwner": REPO},
        "createdAt": "2010-01-01T00:00:00Z", "mergedAt": merged_at,
        "headRefOid": f"{number:040x}", "baseRefName": base,
    }


@pytest.fixture(scope="module")
def gh_bridge(tmp_path_factory):
    folder = tmp_path_factory.mktemp("tail-http-gh")
    script = folder / "gh.py"
    script.write_text("#!" + sys.executable + "\n" + GH_BRIDGE, encoding="utf-8")
    if os.name != "nt":
        script.chmod(0o700)
        return script, script
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    exe = folder / "gh.exe"
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(exe),
                    str(Path(__file__).with_name("fake_gh_launcher.cs"))],
                   check=True, capture_output=True, timeout=120)
    return exe, script


class GitHubPages:
    """The server enforces the 100-result page and 1,000-result search limits."""

    def __init__(self):
        self.nodes = []
        self.calls = []
        self.unexpected = []
        self.change_response = None

    def response(self, document, variables):
        if re.search(r"\bmutation\b", document):
            raise AssertionError("discovery attempted a GitHub mutation")
        for field in ("number", "mergedAt", "headRefOid", "baseRefName", "nameWithOwner",
                      "remaining", "cost", "resetAt"):
            assert re.search(rf"\b{field}\b", document), f"required field not requested: {field}"
        search = "TailSearch" in document
        if search:
            cursor = variables["after"]
            assert "hasNextPage" in document and "endCursor" in document
            query = variables["query"]
            assert "issueCount" in document
            assert f"{variables['owner']}/{variables['name']}" == REPO
            assert re.search(r"repository\(owner: \$owner, name: \$name\)\s*\{\s*nameWithOwner"
                             r"\s+pullRequests\(first: 1\)\s*\{\s*totalCount", document)
            assert f"repo:{REPO}" in query and "is:pr" in query and "is:merged" in query
            assert "created:" not in query
            base = re.search(r'\bbase:"?([^"\s]+)', query).group(1)
            bounds = re.search(r"merged:(\S+)\.\.(\S+)", query)
            lo, hi = map(timestamp, bounds.groups())
            rows = [row for row in self.nodes if row["baseRefName"] == base and row["state"] == "MERGED"
                    and lo <= timestamp(row["mergedAt"]) <= hi]
            count = len(rows)
            rows = rows[:1000]
            connection = {"issueCount": count}
            data = {"repository": {"nameWithOwner": REPO, "pullRequests": {"totalCount": len(self.nodes)}},
                    "search": connection}
            start = int(cursor.removeprefix("cursor:")) if cursor else 0
            end = min(start + 100, len(rows))
            info = {"hasNextPage": end < len(rows), "endCursor": f"cursor:{end}" if end else None}
        else:
            cursor = variables["before"]
            assert "TailEnumerate" in document
            assert "totalCount" in document and "createdAt state" in document
            assert "last: 100, before: $before" in document
            assert "hasPreviousPage" in document and "startCursor" in document
            assert "states:" not in document and "baseRefName:" not in document
            assert f"{variables['owner']}/{variables['name']}" == REPO
            rows = sorted(self.nodes, key=lambda row: timestamp(row["createdAt"]))
            connection = {"totalCount": len(rows)}
            data = {"repository": {"nameWithOwner": REPO, "pullRequests": connection}}
            end = int(cursor.removeprefix("cursor:")) if cursor else len(rows)
            start = max(0, end - 100)
            info = {"hasPreviousPage": start > 0, "startCursor": f"cursor:{start}" if end else None}

        connection.update(nodes=rows[start:end], pageInfo=info)
        self.calls.append(("search" if search else "enumerate", variables.copy()))
        reset = datetime.now(timezone.utc) + timedelta(hours=1)
        data["rateLimit"] = {"remaining": 4999, "cost": 1, "resetAt": utc(reset)}
        response = {"data": data}
        if self.change_response is not None:
            self.change_response(response, connection, cursor)
        return response


class CLI:
    def __init__(self, folder, endpoint, bridge):
        self.config = folder / "pipelinectl.json"
        self.database = folder / "data/promptpilot.db"
        self.configure("main")
        exe, script = bridge
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GH_", "GITHUB_", "PP_"))}
        self.env.update(PP_GH_EXE=str(exe), ROUTING_FAKE_SCRIPT=str(script),
                        ROUTING_FAKE_PYTHON=sys.executable, TAIL_TEST_HTTP=endpoint,
                        PP_DATA_DIR=str(self.database.parent), PYTHONUTF8="1",
                        HOME=str(folder), USERPROFILE=str(folder))

    def configure(self, base):
        self.config.write_text(json.dumps({
            "repository": REPO, "trusted_account": "owner", "health_command": ["unused"],
            "base_branch": base,
        }), encoding="utf-8")

    def start(self, *args):
        return subprocess.Popen([sys.executable, "-X", "utf8", "-m", "promptpilot.project_pipeline",
                                 "--config", str(self.config), *args], cwd=ROOT, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8")

    def run(self, *args, code=0):
        with self.start(*args) as child:
            try:
                out, err = child.communicate(timeout=200)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise
            assert child.returncode == code, (args, out, err)
            return json.loads(out)

    def discover(self, pages, code=0):
        return self.run("tail-discover", "--max-pages", str(pages), code=code)

    def backlog(self, *args):
        return self.run("tail-backlog", "--json", *args)

    def finish(self):
        for _ in range(12):
            result = self.discover(100)
            if result["state"] == "complete":
                return result
            assert result["state"] == "incomplete", result
        pytest.fail("stable finite fixture did not complete")


@pytest.fixture
def tail(tmp_path, gh_bridge):
    github = GitHubPages()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                assert self.path == "/graphql", (self.path, body)
                payload = json.loads(body)
                result = github.response(payload["query"], payload["variables"])
            except Exception as exc:
                github.unexpected.append(repr(exc))
                result = {"errors": [{"message": repr(exc)}]}
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A crash test can stop the CLI before the response arrives.

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        worker.start()
        try:
            yield CLI(tmp_path, f"http://127.0.0.1:{server.server_port}", gh_bridge), github
        finally:
            server.shutdown()
            worker.join()
        assert not github.unexpected, github.unexpected


@pytest.mark.parametrize("count", [300, 1000, 1001])
def test_all_merged_prs_and_search_ceiling(tail, count):
    cli, github = tail
    # 1,001 merges in a single second cannot be separated by a finer qualifier.
    same_second = "2020-05-01T16:30:00Z"
    github.nodes = [node(n, same_second if count == 1001 or n % 2 else "2012-05-01T16:30:00Z")
                    for n in range(1, count + 1)]
    github.nodes += [node(count + 1, same_second, "other")]
    if count == 1001:
        # A repository-wide listing can see merges newer than the frozen upper bound.
        github.nodes.append(node(count + 2, utc(datetime.now(timezone.utc) + timedelta(hours=1))))
    first = cli.discover(1)
    assert first["state"] == "incomplete"
    assert len(github.calls) == 1
    done = cli.finish()
    assert done["discovery_complete_through"]
    if count >= 1000:
        assert len({v["query"] for mode, v in github.calls if mode == "search"}) > 1
    if count == 1001:
        assert sum(mode == "enumerate" for mode, _ in github.calls) >= 22
    total_calls = len(github.calls)
    seen = []
    after = 0
    while True:
        page = cli.backlog("--limit", "250", "--after", str(after))
        numbers = [row["pr_number"] for row in page["candidates"]]
        assert numbers == sorted(numbers) and all(n > after for n in numbers)
        seen.extend(numbers)
        if not page["next_after"]:
            break
        after = page["next_after"]
    assert seen == list(range(1, count + 1))
    assert len(github.calls) == total_calls


@pytest.mark.parametrize("committed", [False, True], ids=["before-page-commit", "after-page-commit"])
def test_killed_cli_resumes_from_last_committed_page(tail, committed):
    cli, github = tail
    github.nodes = [node(n, "2020-01-01T00:00:00Z") for n in range(1, 104)]
    waiting = threading.Event()
    release = threading.Event()

    def stop_at_page(_result, _connection, after):
        if bool(after) == committed:
            waiting.set()
            release.wait(timeout=20)

    github.change_response = stop_at_page
    with cli.start("tail-discover", "--max-pages", "5") as child:
        try:
            assert waiting.wait(timeout=20), "CLI did not reach expected HTTP page"
            child.kill()
        finally:
            release.set()
            child.communicate(timeout=20)
    github.change_response = None
    backlog = cli.backlog("--limit", "100")
    assert [r["pr_number"] for r in backlog["candidates"]] == (list(range(1, 101)) if committed else [])
    assert backlog["discovery"]["state"] == "incomplete"
    before = len(github.calls)
    cli.finish()
    assert github.calls[before][1].get("after") == ("cursor:100" if committed else None)
    assert len(cli.backlog("--limit", "100", "--after", "100")["candidates"]) == 3


def test_rollback_updates_neither_candidates_nor_page_cursor(tail):
    cli, github = tail
    github.nodes = [node(n, "2020-01-01T00:00:00Z") for n in range(1, 111)]
    assert cli.discover(1)["state"] == "incomplete"
    with sqlite3.connect(cli.database) as conn:
        conn.execute("""CREATE TRIGGER fail_mid_page BEFORE INSERT ON tail_candidates
                     WHEN NEW.pr_number = 105 BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END""")
    cli.discover(1, code=2)
    assert not cli.backlog("--limit", "10", "--after", "100")["candidates"]
    with sqlite3.connect(cli.database) as conn:
        conn.execute("DROP TRIGGER fail_mid_page")
    before = len(github.calls)
    cli.finish()
    assert github.calls[before][1]["after"] == "cursor:100"
    assert len(cli.backlog("--limit", "11", "--after", "100")["candidates"]) == 10


@pytest.mark.parametrize("response", ["same", "changed", "unavailable"])
def test_overlapping_discoveries_verify_the_page_only_one_committed(tail, response):
    cli, github = tail
    github.nodes = [node(n, "2020-01-01T00:00:00Z") for n in range(1, 202)]
    cli.discover(1)
    slow_request = threading.Event()
    release = threading.Event()

    def delay_first(result, connection, _after):
        github.change_response = None
        if response == "changed":
            # Identical count and continuation, different PR observation.
            connection["nodes"][0] = dict(connection["nodes"][0], headRefOid="f" * 40)
        elif response == "unavailable":
            result["errors"] = [{"message": "fixture read failed"}]
        slow_request.set()
        release.wait(timeout=20)

    github.change_response = delay_first
    with cli.start("tail-discover", "--max-pages", "1") as child:
        try:
            assert slow_request.wait(timeout=20), "first process did not reach the peer"
            assert cli.discover(1)["state"] == "incomplete"
        finally:
            release.set()
            out, err = child.communicate(timeout=20)
        assert child.returncode == (0 if response == "same" else 2), (out, err)
        observed = json.loads(out)
        if response == "same":
            assert observed["state"] == "incomplete" and observed["pages"] == 1
        elif response == "changed":
            assert "discovery changed" in observed["error"]
        else:
            assert "fixture read failed" in observed["error"]
    saved = cli.backlog("--after", "100", "--limit", "1")
    assert saved["candidates"][0]["head"] == github.nodes[100]["headRefOid"]
    assert saved["discovery"]["revision"] == 3 and saved["discovery"]["scanned"] == 200
    before = len(github.calls)
    cli.finish()
    assert github.calls[before][1]["after"] == "cursor:200"
    assert cli.backlog()["counts"] == {"pending-proof": 201}


@pytest.mark.parametrize(("slow_quota", "fast_quota"), [(0, 17), (17, 0), (6, 17)])
def test_matching_terminal_pages_keep_the_lowest_budget_across_restart(tail, slow_quota, fast_quota):
    cli, github = tail
    github.nodes = [node(1, "2020-01-01T00:00:00Z")]
    assert cli.discover(1)["state"] == "incomplete"
    ready, release = threading.Event(), threading.Event()
    reset = utc(datetime.now(timezone.utc) + timedelta(days=1))

    def charge(result, _connection, _cursor):
        rate = result["data"]["rateLimit"]
        rate.update(resetAt=reset, remaining=fast_quota if ready.is_set() else slow_quota)
        if not ready.is_set():
            ready.set()
            assert release.wait(timeout=20)

    github.change_response = charge
    with cli.start("tail-discover", "--max-pages", "1") as slow:
        try:
            assert ready.wait(timeout=20)
            assert cli.discover(1)["state"] == "complete"
        finally:
            release.set()
            out, err = slow.communicate(timeout=30)
        assert slow.returncode == 0 and json.loads(out)["state"] == "complete", (out, err)
    github.change_response = None
    checkpoint = cli.backlog()["discovery"]
    assert checkpoint["budget_remaining"] == min(slow_quota, fast_quota)
    assert checkpoint["budget_reset_at"] == reset
    if checkpoint["budget_remaining"] == 0:
        before = len(github.calls)
        assert "budget exhausted" in cli.discover(1, code=2)["error"]
        assert len(github.calls) == before


def test_graphql_error_preserves_partial_backlog_and_checkpoint(tail):
    cli, github = tail
    github.nodes = [node(n, "2020-01-01T00:00:00Z") for n in range(1, 103)]
    incomplete = cli.discover(1)
    assert incomplete["state"] == "incomplete"

    def partial(result, _connection, _after):
        result["errors"] = [{"type": "INTERNAL", "message": "interrupted page"}]

    github.change_response = partial
    cli.discover(1, code=2)
    assert not cli.backlog("--limit", "5", "--after", "100")["candidates"]
    github.change_response = None
    before = len(github.calls)
    assert cli.finish()["generation"] == incomplete["generation"]
    assert github.calls[before][1]["after"] == "cursor:100"


def test_new_query_starts_without_cursor_from_old_base(tail):
    cli, github = tail
    merged = utc(datetime.now(timezone.utc) - timedelta(hours=1))
    github.nodes = [node(n, merged) for n in range(2, 103)]
    github.nodes.extend([node(1, merged, "stable"), node(103, merged, "stable")])
    cli.finish()
    old = cli.discover(1)
    assert cli.backlog()["discovery"]["scanned"] == 100
    cli.configure("stable")
    before = len(github.calls)
    unseen = cli.backlog()
    assert unseen["base"] == "stable"
    assert unseen["discovery"] is None and unseen["age_seconds"] is None
    assert unseen["counts"] == {} and unseen["candidates"] == []
    assert len(github.calls) == before

    new = cli.finish()
    assert new["generation"] != old["generation"]
    assert github.calls[before][1].get("after") is None
    assert "stable" in github.calls[before][1]["query"]
    stable = cli.backlog("--limit", "1")
    assert stable["discovery"]["state"] == "complete" and stable["age_seconds"] is not None
    assert stable["counts"] == {"pending-proof": 2}
    assert [r["pr_number"] for r in stable["candidates"]] == [1]
    assert stable["next_after"] == 1
    rest = cli.backlog("--limit", "1", "--after", "1")
    assert [r["pr_number"] for r in rest["candidates"]] == [103]
    assert rest["next_after"] is None

    cli.configure("main")
    main = cli.backlog("--limit", "150")
    assert main["discovery"] is None and main["age_seconds"] is None
    assert main["counts"] == {"pending-proof": 101}
    assert [r["pr_number"] for r in main["candidates"]] == list(range(2, 103))


@pytest.mark.parametrize("fault", ["repeated-page", "missing-last-row", "expired-cursor"])
def test_damaged_pagination_never_certifies_an_interval(tail, fault):
    cli, github = tail
    github.nodes = [node(n, "2020-02-01T00:00:00Z") for n in range(1, 102)]
    cli.discover(1)

    def break_final_page(result, connection, after):
        assert after == "cursor:100"
        if fault == "repeated-page":
            # A different end cursor cannot turn an already saved row into the missing PR.
            connection["nodes"] = [github.nodes[0]]
        elif fault == "missing-last-row":
            connection["nodes"] = []
        else:
            result["errors"] = [{"type": "INVALID_CURSOR", "message": "invalid cursor"}]

    github.change_response = break_final_page
    cli.discover(1, code=2)
    data = cli.backlog()
    assert not data["discovery"].get("discovery_complete_through")
    assert data["discovery"]["state"] == "incomplete"
    github.change_response = None
    before = len(github.calls)
    cli.finish()
    if fault == "expired-cursor":
        assert github.calls[before][1].get("after") is None
    assert [r["pr_number"] for r in cli.backlog("--after", "99")["candidates"]] == [100, 101]


def test_two_matching_passes_required_even_if_the_count_stays_the_same(tail):
    cli, github = tail
    github.nodes = [node(1, "2020-09-01T00:00:00Z")]
    first = cli.discover(1)
    assert first["state"] == "incomplete"
    github.nodes[0]["headRefOid"] = "a" * 40
    second = cli.discover(1)
    assert second["state"] == "incomplete"
    assert cli.finish()["generation"] == first["generation"]
    rows = cli.backlog()["candidates"]
    assert len(rows) == 1 and rows[0]["head"] == "a" * 40


def test_enumeration_finishes_its_frozen_interval_while_the_repository_grows(tail):
    cli, github = tail
    base = "release+stable"
    cli.configure(base)
    future = utc(datetime.now(timezone.utc) + timedelta(hours=1))
    github.nodes = [node(n, "2020-01-01T00:00:00Z", base) for n in range(1, 202)]
    # Older open PRs merge during the scan. Both their original creation dates
    # and their base branches are deliberately intermixed with the target PRs.
    pending = [dict(node(n, None, base), state="OPEN") for n in range(202, 222)]
    github.nodes[50:50] = pending
    github.nodes.append(dict(node(222, None, base), state="CLOSED"))
    github.nodes.append(node(223, "2020-01-01T00:00:00Z", "another-base"))

    def add_activity(_response, _connection, _cursor):
        if pending:
            pending.pop().update(state="MERGED", mergedAt=future)
        # More new PRs arrive than can fit on the next page. Each pass must
        # nevertheless finish walking the PRs that existed on its first page.
        first = len(github.nodes) + 1
        github.nodes.extend(dict(node(n, None, base), state="OPEN", createdAt=future)
                            for n in range(first, first + 110))

    github.change_response = add_activity
    first = cli.discover(1)
    assert first["state"] == "incomplete" and first["pages"] == 1
    for _ in range(6):
        result = cli.discover(2)
        assert result["pages"] <= 2 and result["generation"] == first["generation"]
        if result["state"] == "complete":
            break
    assert result["state"] == "complete"
    assert sum(variables["before"] is None for _, variables in github.calls) == 2
    candidates = cli.backlog("--limit", "250")["candidates"]
    assert [row["pr_number"] for row in candidates] == list(range(1, 202))


def test_enumeration_rejects_a_missing_row_while_the_repository_grows(tail):
    cli, github = tail
    base = "release+stable"
    cli.configure(base)
    github.nodes = [node(n, "2020-01-01T00:00:00Z", base) for n in range(1, 202)]
    future = utc(datetime.now(timezone.utc) + timedelta(hours=1))

    def drop_one_row(_response, connection, cursor):
        if cursor is None:
            github.nodes.append(dict(node(202, None, base), state="OPEN", createdAt=future))
        elif cursor == "cursor:101":
            connection["nodes"].pop()

    github.change_response = drop_one_row
    cli.discover(5, code=2)
    assert cli.backlog()["discovery"]["discovery_complete_through"] is None
    github.change_response = None
    cli.finish()
    assert cli.backlog()["counts"] == {"pending-proof": 201}


def test_enumeration_rejects_replayed_rows_even_outside_the_interval(tail):
    cli, github = tail
    # The '+' branch name forces the exact repository enumeration route.
    base = "release+stable"
    cli.configure(base)
    future = utc(datetime.now(timezone.utc) + timedelta(hours=1))
    github.nodes = [node(1, "2020-01-01T00:00:00Z", base)]
    github.nodes.extend(node(n, future, base) for n in range(2, 202))

    def repeat_filtered_page(_response, connection, after):
        if after == "cursor:101":
            connection["nodes"] = github.nodes[-100:]

    github.change_response = repeat_filtered_page
    cli.discover(5, code=2)
    backlog = cli.backlog()
    assert backlog["discovery"]["state"] == "incomplete"
    assert not backlog["discovery"]["discovery_complete_through"]
    github.change_response = None
    cli.finish()
    assert [row["pr_number"] for row in cli.backlog()["candidates"]] == [1]


def test_api_budget_stops_before_another_request(tail):
    cli, github = tail
    github.nodes = [node(n, "2020-01-01T00:00:00Z") for n in range(1, 110)]

    def depleted(result, _connection, _after):
        result["data"]["rateLimit"]["remaining"] = 0

    github.change_response = depleted
    cli.discover(5, code=2)
    assert len(github.calls) == 1
    assert cli.backlog()["discovery"]["state"] == "incomplete"
    github.change_response = None
    cli.discover(5, code=2)
    assert len(github.calls) == 1
    with sqlite3.connect(cli.database) as conn:
        row = conn.execute("SELECT checkpoint_json FROM tail_discovery").fetchone()
        checkpoint = json.loads(row[0])
        assert timestamp(checkpoint["budget_reset_at"]) > datetime.now(timezone.utc)
        checkpoint["budget_reset_at"] = utc(datetime.now(timezone.utc) - timedelta(seconds=1))
        conn.execute("UPDATE tail_discovery SET checkpoint_json = ?", (json.dumps(checkpoint),))
    cli.finish()
    assert cli.backlog()["counts"] == {"pending-proof": 109}


@pytest.mark.parametrize("repository", [
    None,
    {"nameWithOwner": "owner/other", "pullRequests": {"totalCount": 0}},
    {"nameWithOwner": REPO, "pullRequests": None},
], ids=["inaccessible", "different-repository", "pull-requests-inaccessible"])
def test_search_requires_access_to_the_configured_repository_before_certifying_empty(tail, repository):
    cli, github = tail

    def unavailable(result, _connection, _after):
        result["data"]["repository"] = repository

    github.change_response = unavailable
    cli.discover(2, code=2)
    checkpoint = cli.backlog()["discovery"]
    assert checkpoint["state"] == "incomplete"
    assert checkpoint["discovery_complete_through"] is None
    assert len(github.calls) == 1
    github.change_response = None
    assert cli.finish()["state"] == "complete"
    assert cli.backlog()["candidates"] == []


@pytest.mark.parametrize(("fault", "mode"), [
    ("outside-filter", "enumerate"), ("query-error", "enumerate"), ("source-error", "search"),
])
def test_search_errors_respect_exhausted_budget_across_restart(tail, fault, mode):
    cli, github = tail
    github.nodes = [node(1, "2020-01-01T00:00:00Z")]
    reset_at = utc(datetime.now(timezone.utc) + timedelta(hours=1))

    def unsupported(result, connection, _after):
        result["data"]["rateLimit"].update(remaining=0, resetAt=reset_at)
        if fault == "outside-filter":
            connection["nodes"] = [node(1, "2020-01-01T00:00:00Z", "other")]
        elif fault == "query-error":
            result["errors"] = [{"message": "unsupported search qualifier"}]
        else:
            result["errors"] = [{"type": "FORBIDDEN", "message": "cannot read repository"}]

    github.change_response = unsupported
    cli.discover(5, code=2)
    github.change_response = None
    cli.discover(5, code=2)
    assert [operation for operation, _ in github.calls] == ["search"]
    result = cli.backlog()
    assert result["candidates"] == []
    assert result["discovery"]["state"] == "incomplete"
    assert result["discovery"]["mode"] == mode
    assert result["discovery"]["budget_remaining"] == 0
    assert result["discovery"]["budget_reset_at"] == reset_at


def test_local_search_fallback_still_uses_the_page_budget_for_enumeration(tail):
    cli, github = tail
    cli.configure("release+stable")
    assert cli.discover(1)["state"] == "incomplete"
    assert [mode for mode, _ in github.calls] == ["enumerate"]


def test_backlog_before_discovery_makes_no_database_or_http_request(tail):
    cli, github = tail
    result = cli.backlog()
    assert result["discovery"] is None and result["candidates"] == []
    assert not cli.database.exists()
    assert not github.calls


def test_catch_up_from_thirty_day_old_checkpoint(tail):
    cli, github = tail
    now = datetime.now(timezone.utc).replace(microsecond=0)
    last_complete = now - timedelta(days=30)
    github.nodes = [node(1, utc(last_complete - timedelta(days=1)))]
    result = cli.finish()
    # Simulate the saved database of a CLI which last ran a month ago. All
    # candidate discovery and subsequent reads still use the public commands.
    with sqlite3.connect(cli.database) as conn:
        raw, = conn.execute("SELECT checkpoint_json FROM tail_discovery").fetchone()
        state = json.loads(raw)
        old_upper = result["discovery_complete_through"]
        checkpoint = json.dumps(state).replace(old_upper, utc(last_complete))
        assert checkpoint != json.dumps(state)
        conn.execute("UPDATE tail_discovery SET checkpoint_json = ?", (checkpoint,))
    github.nodes.append(node(2, utc(last_complete + timedelta(days=1))))
    before = len(github.calls)
    new = cli.finish()
    assert new["generation"] != result["generation"]
    assert timestamp(new["discovery_complete_through"]) >= now
    first_query = github.calls[before][1]["query"]
    low = re.search(r"merged:(\S+)\.\.", first_query).group(1)
    assert timestamp(low) == last_complete - timedelta(hours=48)
    assert [r["pr_number"] for r in cli.backlog()["candidates"]] == [1, 2]


def test_old_search_watermark_rechecks_history_and_keeps_known_candidates(tail):
    cli, github = tail
    github.nodes = [node(1, "2020-01-01T00:00:00Z")]
    old = cli.finish()
    # Prior builds could mark an inaccessible repository complete after two
    # empty search responses. Simulate that version's on-disk checkpoint.
    old_query = json.dumps(["merged-tail-v1", REPO, "main"]).encode("ascii")
    with sqlite3.connect(cli.database) as conn:
        raw, = conn.execute("SELECT checkpoint_json FROM tail_discovery").fetchone()
        saved = json.loads(raw)
        saved["fingerprint"] = hashlib.sha256(old_query).hexdigest()
        conn.execute("UPDATE tail_discovery SET checkpoint_json = ?", (json.dumps(saved),))

    backlog = cli.backlog()
    assert backlog["discovery"] is None and backlog["age_seconds"] is None
    assert [row["pr_number"] for row in backlog["candidates"]] == [1]
    github.nodes.append(node(2, "2019-01-01T00:00:00Z"))
    assert cli.finish()["generation"] == old["generation"] + 1
    assert [row["pr_number"] for row in cli.backlog()["candidates"]] == [1, 2]
