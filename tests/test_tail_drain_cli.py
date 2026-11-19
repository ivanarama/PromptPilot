"""Exercise the public durable TAIL commands against GitHub REST/GraphQL wire fixtures."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import sys
import threading

import pytest

from promptpilot import config, project_pipeline as pipeline
from promptpilot.pipeline_errors import PipelineError
from tail_admission_fixture import add_comment, add_edge, graphql_response, source_fixture
from test_tail_discovery_cli import CLI, GitHubPages, REPO, gh_bridge, node, utc  # noqa: F401


class Peer:
    """Model the repository seen by both workers. Record each accepted external write."""

    def __init__(self, *sources):
        self.sources = {source["pull"]["number"]: source for source in sources}
        self.cutover = sources[0]["cutover"]
        self.pages = GitHubPages()
        for number, source in self.sources.items():
            found = node(number, source["pull"]["merged_at"])
            found["headRefOid"] = source["candidate"]["head"]
            self.pages.nodes.append(found)
        self.writes = []
        self.refs = {}
        self.issues = []
        self.lock = threading.Lock()
        self.after = None
        self.select_together = None
        self.pause_claim = False
        self.claim_visible = threading.Event()
        self.resume = threading.Event()

    def reply(self, args, payload):
        # Hold each selector's first remote read after both loaded their SQLite row.
        barrier = self.select_together
        if barrier is not None and args == ("api", f"repos/{REPO}/pulls/42"):
            barrier.wait(60)
        with self.lock:
            before = len(self.writes)
            output = self._request(args, payload)
            write = self.writes[-1] if len(self.writes) > before else None
            observed = write
            if args == ("api", "user"):
                observed = "authenticated"
            elif args == ("api", f"repos/{REPO}/git/ref/heads/main"):
                observed = "base-read"
            elif (self.writes[-1:] == ["intent"]
                  and f"repos/{REPO}/issues?state=all&per_page=100" in args):
                observed = "post-intent-dedupe-read"
            if self.after is not None and self.after[0] == observed:
                change_source(self.sources[42], self.after[1])
                self.after = None
        if write == "claim" and self.pause_claim:
            self.claim_visible.set()
            assert self.resume.wait(90), "test did not release claim response"
        return output

    def _request(self, args, payload):
        assert args[0] == "api", args
        if args[1:3] == ("graphql", "--input"):
            return json.dumps(self.pages.response(payload["query"], payload["variables"]))
        if args[1] == "graphql":
            number = next(int(arg.split("=")[1]) for arg in args if arg.startswith("number="))
            return json.dumps(graphql_response(self.sources[number]))
        if args[1:] == ("user",):
            return json.dumps({"login": "owner"})
        prefix = f"repos/{REPO}/"
        path = next(arg.removeprefix(prefix) for arg in args if arg.startswith(prefix))
        if path == "pulls/1261":
            return json.dumps(self.cutover)
        if path.startswith("pulls/"):
            return json.dumps(self.sources[int(path.split("/")[1])]["pull"])
        if path.startswith("issues?state=all&"):
            assert "--paginate" in args and "--jq" in args
            return "\n".join(map(json.dumps, self.issues))
        if path.startswith("issues/"):
            source = self.sources[int(path.split("/")[1])]
            if path.endswith("/comments?per_page=100"):
                assert "--paginate" in args and "--jq" in args
                return "\n".join(map(json.dumps, source["comments"]))
            assert path.endswith("/comments") and "POST" in args
            body = payload["body"]
            kind = next(name for token, name in (
                ("pp:tail-claim ", "claim"), ("pp:tail-lease ", "lease"),
                ("pp:tail-create-intent ", "intent"), ("pp:tail-item-done ", "item-done"),
            ) if body.startswith("<!-- " + token))
            self.writes.append(kind)
            return json.dumps(add_comment(source, body, created=utc(datetime.now(timezone.utc))))
        if path == "git/ref/heads/main":
            return json.dumps({"object": {"sha": "d" * 40}})
        if path == "git/refs":
            assert "--include" in args and "POST" in args
            ref, sha = payload["ref"], payload["sha"]
            assert ref.startswith("refs/heads/pp-tail-dedupe/") and sha == "d" * 40
            if ref in self.refs:
                return 'HTTP/2 422 Unprocessable Entity\n\n{"message":"already exists"}'
            self.writes.append("ref")
            self.refs[ref] = sha
            return "HTTP/2 201 Created\n\n" + json.dumps({"ref": ref, "object": {"sha": sha}})
        assert path == "issues" and "POST" in args, args
        self.writes.append("issue")
        result = {**payload, "number": 2000 + len(self.issues), "user": {"login": "owner"}}
        self.issues.append(result)
        return json.dumps(result)


def change_source(source, event):
    now = utc(datetime.now(timezone.utc))
    if event in ("hold", "no-tail"):
        source["pull"]["labels"].append({"name": event})
        source["snapshot"]["labels"].append(event)
    elif event == "override":
        add_comment(source, "pp:review-again", created=now)
    else:
        review = next(row for row in source["comments"] if row["id"] == 101)
        edges = source["snapshot"]["edges"]
        edge = next(edge for edge in edges if edge["node"].get("id") == review["node_id"])
        if event == "edit":
            review["body"] += "\nEdited by the reviewer"
            review["updated_at"] = now
            edge["node"].update(body=review["body"], lastEditedAt=now)
        else:
            assert event == "delete"
            source["comments"].remove(review)
            edges.remove(edge)
            add_edge(source, "CommentDeletedEvent", id="deletion", createdAt=now)


def enable(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "tail_durable_drain": True}), encoding="utf-8")


def selected(run, result=None):
    ready = (run("tail-next") if result is None else result)["ready"]
    assert len(ready) == 1 and ready[0]["number"] == 42
    item, = ready[0]["items"]
    assert item["index"] == 1 and item["kind"] == "issue"
    assert item["title"] == "Сохранение записи"
    target = ("--number", "42", "--version", ready[0]["version"])
    assert run("tail-gate", *target)["items"] == ready[0]["items"]
    return ("tail-create", *target, "--item", "1")


@pytest.fixture
def local_cli(tmp_path, isolated_db, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", isolated_db.DB_PATH)
    source = source_fixture()
    peer = Peer(source)

    class LocalGitHub(pipeline.GitHub):
        def __init__(self, **kwargs):
            super().__init__(executable="in-process test transport", **kwargs)

        def run(self, *args, input_value=None, **_):
            return peer.reply(args, input_value)

    monkeypatch.setattr(pipeline, "GitHub", LocalGitHub)
    cli = CLI(tmp_path, "", ("unused", "unused"))
    enable(cli.config)

    def run(*args, code=0):
        result = pipeline.run(["--config", str(cli.config), *args])
        output = capsys.readouterr()
        assert result == code, (args, output)
        return json.loads(output.out)

    run("tail-discover", "--max-pages", "5")
    return run, peer


def test_tail_create_publishes_verified_context_without_changing_identity(local_cli, tmp_path):
    from hashlib import sha256

    run, peer = local_cli
    selection = run("tail-next")
    command = selected(run, selection)
    candidate, = selection["ready"]
    item, = candidate["items"]
    path = tmp_path / "проверка main.md"
    for malformed in (b"\xff", b"\x00", b"<!-- pp:tail-source pr=20 -->", b"pp:tail-drop 1",
                      "Каноничная суть: подмена".encode(), b"x" * 65_536):
        path.write_bytes(malformed)
        assert run(*command, "--context-file", str(path), code=2)["action"] == "error"
        assert peer.writes == []

    peer.sources[42]["pull"]["title"] = "Форма записи"
    context = "В src/form.py:17 повторное сохранение очищает поле.\nИзменение теряется после открытия формы."
    path.write_bytes((context.replace("\n", "\r\n") + "\r\n").encode("utf-8-sig"))
    assert run(*command, "--context-file", str(path))["state"] == "item-done"
    issue, = peer.issues
    assert issue["title"] == item["title"]
    title_hash = sha256(item["title"].encode()).hexdigest()
    task_hash = sha256(item["text"].encode()).hexdigest()
    dedupe = sha256(f"pp-tail-task-v1\ntitle-sha256={title_hash}\ntask-sha256={task_hash}\n".encode()).hexdigest()
    assert issue["body"] == (
        "Найдено при ревью PR #42 (Форма записи), в хвост попало как отдельная работа.\n\n"
        f"Каноничная суть: {item['text']}\n\n{context}\n\n"
        f"Источник: https://github.com/{REPO}/pull/42#issuecomment-101\n"
        f"<!-- pp:tail-source pr=42 review-comment=101 review-updated={candidate['review_updated_at']} "
        f"item=1 item-sha256={item['item_sha256']} dedupe-sha256={dedupe} -->\n"
        f"<!-- pp:tail-task-v1 title-sha256={title_hash} task-sha256={task_hash} dedupe-sha256={dedupe} -->\n"
        f"<!-- pp:tail-dedupe sha256={dedupe} -->"
    )
    assert list(peer.refs) == [f"refs/heads/pp-tail-dedupe/{dedupe}"]


def test_tail_next_preserves_quoted_ui_name_from_review_formatter(local_cli):
    run, peer = local_cli
    source = peer.sources[42]
    title = "Исправить кнопку «Отмена»"
    body, _ = pipeline.format_review(
        {"depth": 0, "head": source["candidate"]["head"]},
        {"change": "Форма", "checks": ["Открытие формы"], "blocking": [],
         "tail": [{"kind": "issue", "text": "Сохранить изменения", "title": title}]},
    )
    source["comments"][0]["body"] = source["snapshot"]["edges"][1]["node"]["body"] = body

    candidate, = run("tail-next")["ready"]
    item, = candidate["items"]
    assert item["title"] == title
    assert peer.writes == []


@pytest.mark.parametrize("event", ["edit", "delete", "override", "hold", "no-tail"])
@pytest.mark.parametrize("boundary", ["claim", "ref", "intent", "issue"])
def test_new_source_or_policy_stops_every_pre_create_write(local_cli, event, boundary):
    run, peer = local_cli
    command = selected(run)
    sequence = ["claim", "ref", "intent", "issue"]
    previous = sequence[:sequence.index(boundary)]
    last_read_or_write = {"claim": "authenticated", "ref": "base-read", "intent": "ref",
                          "issue": "post-intent-dedupe-read"}
    peer.after = (last_read_or_write[boundary], event)
    assert run(*command, code=2)["action"] == "error"
    assert peer.after is None
    assert peer.writes == previous
    assert peer.issues == []


BRIDGE = """
import json, os, sys, urllib.request
args = sys.argv[1:]
body = json.load(sys.stdin) if "--input" in args else None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request(os.environ["TAIL_TEST_HTTP"],
                                 data=json.dumps([args, body]).encode())
with opener.open(request, timeout=100) as response:
    sys.stdout.buffer.write(response.read())
"""


@pytest.fixture(scope="module")
def wire_bridge(tmp_path_factory, request):
    script = tmp_path_factory.mktemp("drain-gh") / "gh.py"
    script.write_text("#!" + sys.executable + "\n" + BRIDGE, encoding="utf-8")
    if os.name == "nt":
        return request.getfixturevalue("gh_bridge")[0], script
    script.chmod(0o700)
    return script, script


@pytest.fixture
def http_cli(tmp_path, wire_bridge):
    servers = []

    def start(*sources):
        peer = Peer(*sources)
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    args, data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    result = peer.reply(tuple(args), data).encode("utf-8")
                    self.send_response(200)
                except Exception as exc:
                    errors.append(repr(exc))
                    result = repr(exc).encode()
                    self.send_response(500)
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        worker.start()
        servers.append((server, worker, peer, errors))
        cli = CLI(tmp_path, f"http://127.0.0.1:{server.server_port}", wire_bridge)
        cli.env["PP_CODEX_APP_SERVER"] = "0"
        enable(cli.config)
        cli.finish()
        return cli, peer

    yield start
    for server, worker, peer, errors in servers:
        peer.resume.set()
        server.shutdown()
        worker.join()
        server.server_close()
        assert not errors, errors


def test_two_cli_processes_share_one_item_lease_and_issue(http_cli):
    merged = utc(datetime.now(timezone.utc) - timedelta(days=35))
    cli, peer = http_cli(source_fixture(merged_at=merged))
    peer.select_together = threading.Barrier(2)
    with cli.start("tail-next") as first:
        second = cli.run("tail-next")
        out, err = first.communicate(timeout=90)
        assert first.returncode == 0, (out, err)
    peer.select_together = None
    first_selection = json.loads(out)
    assert first_selection["ready"] == second["ready"]
    command = selected(cli.run, first_selection)
    second_command = selected(cli.run, second)
    peer.pause_claim = True
    with cli.start(*command) as child:
        try:
            assert peer.claim_visible.wait(60), "first worker did not publish its claim"
            contender = cli.run(*second_command)
            assert contender["state"] == "active-elsewhere"
        finally:
            peer.resume.set()
        out, err = child.communicate(timeout=180)
        assert child.returncode == 0, (out, err)
    assert json.loads(out)["issue"] == cli.run(*command)["issue"] == 2000
    assert peer.writes == ["claim", "ref", "intent", "issue", "item-done"]
    assert len(peer.issues) == len(peer.refs) == 1
    source = peer.sources[42]
    claim, intent, done = source["comments"][-3:]
    owner = re.search(r" owner=([0-9a-f-]+) -->$", claim["body"])[1]
    assert f" lease={claim['id']} owner={owner} -->" in intent["body"]
    assert " issue=2000 -->" in done["body"]
    assert peer.issues[0]["title"] == "Сохранение записи"
    dedupe = next(iter(peer.refs)).rsplit("/", 1)[1]
    assert f"<!-- pp:tail-dedupe sha256={dedupe} -->" in peer.issues[0]["body"].splitlines()
    # The canonical worker posts overall completion after disposing every item.
    review = source["comments"][0]
    add_comment(source, f"<!-- pp:tail-done review-comment={review['id']} review-updated={review['updated_at']} -->",
                created=utc(datetime.now(timezone.utc)))
    assert cli.run("tail-gate", *command[1:-2])["completion_kind"] == "versioned"
    assert cli.backlog()["completions"] == {"versioned": 1}


def test_cli_hold_after_ref_preserves_intent_boundary(http_cli):
    cli, peer = http_cli(source_fixture())
    command = selected(cli.run)
    peer.after = ("ref", "hold")
    assert cli.run(*command, code=2)["action"] == "error"
    assert peer.writes == ["claim", "ref"]
    assert peer.issues == []


def test_empty_and_legacy_done_survive_restart_without_external_writes(http_cli):
    legacy = source_fixture(protocol="legacy", number=43, merged_at="2026-02-01T00:00:00Z")
    add_comment(legacy, "<!-- pp:tail-done -->")
    cli, peer = http_cli(source_fixture(items=[]), legacy)
    result = cli.run("tail-next")
    assert result["ready"] == []
    assert {(r["number"], r["completion_kind"]) for r in result["observations"]} == {
        (42, "empty"), (43, "legacy"),
    }
    assert cli.run("tail-next")["ready"] == []
    backlog = cli.backlog()
    assert backlog["completions"] == {"empty": 1, "legacy": 1}
    assert backlog["counts"] == {} and backlog["candidates"] == []
    assert peer.writes == []


def test_timeout_after_issue_creation_recovers_the_existing_number(local_cli, monkeypatch):
    run, peer = local_cli
    command = selected(run)
    request = peer._request

    def lose_issue_response(args, payload):
        result = request(args, payload)
        if args[1:3] == ("repos/owner/project/issues", "--method"):
            raise PipelineError("timeout after the server accepted the issue")
        return result

    monkeypatch.setattr(peer, "_request", lose_issue_response)
    first = run(*command)
    assert first["state"] == "ambiguous-create"
    assert peer.writes == ["claim", "ref", "intent", "issue"]
    monkeypatch.setattr(peer, "_request", request)
    recovered = run(*command)
    assert recovered["state"] == "existing-issue" and recovered["issue"] == 2000
    assert peer.writes == ["claim", "ref", "intent", "issue"]


def test_late_hold_reports_the_created_issue_for_exact_source_recovery(local_cli):
    run, peer = local_cli
    command = selected(run)
    peer.after = ("issue", "hold")
    outcome = run(*command)
    assert outcome["state"] == "existing-issue" and outcome["issue"] == 2000
    assert peer.writes == ["claim", "ref", "intent", "issue"]
    assert run("tail-backlog")["counts"] == {"waiting-policy": 1}


def test_returned_intent_must_still_be_present_before_create(local_cli, monkeypatch):
    run, peer = local_cli
    command = selected(run)
    request = peer._request

    def hide_intent(args, payload):
        result = request(args, payload)
        if payload and (payload.get("body") or "").startswith("<!-- pp:tail-create-intent "):
            ident = json.loads(result)["id"]
            source = peer.sources[42]
            source["comments"][:] = [c for c in source["comments"] if c["id"] != ident]
            source["snapshot"]["edges"][:] = [
                edge for edge in source["snapshot"]["edges"]
                if str(edge["node"].get("fullDatabaseId")) != str(ident)]
        return result

    monkeypatch.setattr(peer, "_request", hide_intent)
    outcome = run(*command)
    assert outcome["state"] == "ambiguous-create"
    assert peer.writes == ["claim", "ref", "intent"]


def test_global_task_on_another_pr_reuses_the_created_issue(local_cli):
    run, peer = local_cli
    created = run(*selected(run))
    assert created["state"] == "item-done"
    first_issue, = peer.issues
    assert first_issue["number"] == created["issue"]
    assert "<!-- pp:tail-source pr=42 " in first_issue["body"]
    prior_writes = len(peer.writes)

    # The following discovery pass finds another merged PR within its overlap.
    merged = utc(datetime.now(timezone.utc) - timedelta(hours=2))
    second = source_fixture(number=43, merged_at=merged)
    peer.sources[43] = second
    peer.pages.nodes.append({**node(43, merged), "headRefOid": second["candidate"]["head"]})
    run("tail-discover", "--max-pages", "5")
    candidate, = [row for row in run("tail-next")["ready"] if row["number"] == 43]
    target = ("--number", "43", "--version", candidate["version"])
    assert run("tail-gate", *target)["state"] == "ready"
    outcome = run("tail-create", *target, "--item", "1")
    assert outcome["state"] == "item-done" and outcome["issue"] == first_issue["number"]
    assert peer.writes[prior_writes:] == ["claim", "item-done"]
    assert len(peer.issues) == peer.writes.count("issue") == 1
    assert f" issue={first_issue['number']} -->" in second["comments"][-1]["body"]


def test_maintainer_drops_exact_item_between_selection_and_creation(local_cli):
    run, peer = local_cli
    result = run("tail-next")
    command = selected(run, result)
    candidate, = result["ready"]
    item, = candidate["items"]
    drop = (f"pp:tail-drop review-comment={candidate['review_id']} review-updated={candidate['review_updated_at']} "
            f"item={item['index']} item-sha256={item['item_sha256']}")
    add_comment(peer.sources[42], drop, login="owner", created=utc(datetime.now(timezone.utc)))
    outcome = run(*command)
    assert outcome["state"] == "waiting-policy" and outcome["issue"] is None
    assert peer.writes == [] and peer.issues == []
