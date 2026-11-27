import hashlib
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError

import pytest

from promptpilot import db
from promptpilot.models import TaskCreate
from promptpilot.task_identity import task_execution_fingerprint
from tools import install_onebase_fix_postpush as installer
from tools import onebase_fix_postpush as runner


SOURCE, HEAD, TREE, EPOCH = "a" * 40, "b" * 40, "c" * 40, "d" * 64
BASE = ["--head", HEAD, "--pr", "17"]


def test_receipt_from_publish_to_next_attempt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".promptpilot" / "fix-runs" / "original-attempt"
    root.mkdir(parents=True)
    report = root / "report.txt"
    marker = f"<!-- pp:fix-pushed from={SOURCE} head={HEAD} review-comment=1 claim=2 epoch-sha256={EPOCH} -->"
    report.write_text("Проверены все тесты.\n" + marker + "\n", encoding="utf-8")
    log = root / "test.log"
    log.write_bytes(b"PASS")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"source": SOURCE, "head": HEAD, "tree": TREE,
                                   "files": {str(log): hashlib.sha256(log.read_bytes()).hexdigest()}}))
    record = ["record", *BASE, "--report", str(report), "--manifest", str(manifest)]
    assert runner.main(record) == 0
    assert runner.main(record) == 0
    assert runner.main(["read", *BASE]) == 0
    restored = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert restored["report"] == str(report) and restored["tree"] == TREE
    assert not restored["publication_authorized"] and not restored["report_post_claimed"]
    assert runner.main(["read", "--head", SOURCE, "--pr", "17"]) == 2
    assert runner.main(["read", "--head", HEAD, "--pr", "18"]) == 2
    comments = root / "comments.json"
    comments.write_text("[[]]")
    claim = ["claim-report", *BASE, "--comments", str(comments)]
    assert runner.main(claim) == 0
    assert runner.main(claim) == 2
    published = dict(id=123, node_id="IC_fixture", user={"login": "ivanarama"}, body=report.read_bytes().decode(),
                     created_at="created", updated_at="created")
    comments.write_text(json.dumps([[published]]))
    read = ["read", *BASE, "--comments", str(comments)]
    assert runner.main(read) == 2
    graph_path = root / "report-node.json"
    graph = dict(__typename="IssueComment", id=published["node_id"], fullDatabaseId="123",
                 lastEditedAt=None, author={"login": "ivanarama"}, body=published["body"],
                 pullRequest={"number": 17, "repository": {"nameWithOwner": "ivanarama/onebase"}})
    read += ["--report-node", str(graph_path)]
    for bad in ({"lastEditedAt": published["created_at"]}, {"id": "IC_other"},
                {"fullDatabaseId": "124"}, {"__typename": "PullRequestReviewComment"},
                {"author": {"login": "other"}}, {"body": "different"},
                {"pullRequest": {**graph["pullRequest"], "number": 18}},
                {"pullRequest": {"number": 17, "repository": {"nameWithOwner": "other/onebase"}}}):
        graph_path.write_text(json.dumps({"data": {"node": {**graph, **bad}}}))
        assert runner.main(read) == 2
    missing_edit_field = {k: v for k, v in graph.items() if k != "lastEditedAt"}
    for response in ({"data": {"node": missing_edit_field}},
                     {"data": {"node": graph}, "errors": [{"message": "partial result"}]}):
        graph_path.write_text(json.dumps(response))
        assert runner.main(read) == 2
    graph_path.write_text(json.dumps({"data": {"node": graph}}))
    assert runner.main(read) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verified_report_id"] == 123
    assert runner.main(claim) == 2
    for bad in ([published, published], [{**published, "updated_at": "edited"}],
                [{**published, "body": "altered " + published["body"]}]):
        comments.write_text(json.dumps([bad]))
        assert runner.main(read) == 2
    comments.write_text(json.dumps([[{**published, "user": {"login": "untrusted"}}]]))
    assert runner.main(read) == 2
    assert runner.main(["read", *BASE, "--comments", str(comments)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verified_report_id"] is None
    assert runner.main(["read", *BASE]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["report_post_claimed"]
    log.write_bytes(b"altered")
    assert runner.main(["read", *BASE]) == 2
    assert runner.main(record) == 2
    log.write_bytes(b"PASS")
    report.write_text("replacement\n" + marker)
    assert runner.main(["read", *BASE]) == 2
    assert runner.main(record) == 2


def sources():
    original = dict(profile_hash=EPOCH, generated_at=90, revision=10, epoch=1)
    prior = dict(state="refreshed", source_refreshed=True, task_id=42, task_started_at="start",
                 profile_id="onebase", queue_id="fix", native_session_bound=True, snapshot_token=original)
    cache = dict(available=True, complete=True, stale=False, invalidated=False, refresh_blocked=None,
                 source="durable", token={**original, "generated_at": 100, "revision": 11}, profile_hash=EPOCH,
                 ttl_seconds=300, generated_at=100)
    return prior, dict(profile_id="onebase", repository="ivanarama/onebase", cache=cache, diagnostics={"exit_code": 0})


def run_observer(prior, values, seconds=200):
    elapsed = 0
    requests = []
    def advance(interval):
        nonlocal elapsed
        elapsed += interval
    def read(timeout):
        requests.append(elapsed)
        value = values[min(len(requests)-1, len(values)-1)]
        if isinstance(value, Exception):
            advance(timeout)
            raise value
        return value
    result = runner.observe(prior, 42, "start", seconds, read=read, sleep=advance,
                            clock=lambda: elapsed, now=lambda: 180)
    return result, requests


def test_passive_recovery_from_timeout_and_contention():
    prior, good = sources()
    blocked = {**good, "cache": {**good["cache"], "refresh_blocked": "priority_waiter"}}
    result, requests = run_observer(prior, [TimeoutError(), blocked, good])
    assert requests == [0, 50, 80]
    assert result["fresh_snapshot"] == good["cache"]["token"] and not result["publication_authorized"]


@pytest.fixture
def reused_fix_source(tmp_path, monkeypatch):
    directory = tmp_path / ".promptpilot"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(db, "DB_DIR", directory)
    monkeypatch.setattr(db, "DB_PATH", directory / "promptpilot.db")
    db.init_db()
    db.create_task(TaskCreate(prompt="other series", recurrence="1h"))
    task = db.create_task(TaskCreate(prompt="Обычный FIX", recurrence="1h", priority=1, task_timeout=7200))
    now = time.time()
    started = datetime.fromtimestamp(now - 500, timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE tasks SET started_at=?,status='running',session_id='session' WHERE id=?", (started, task.id))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
    cost = {"core": 1200, "search": 12, "graphql": 1000}
    reservation = {"task_id": task.id, "task_started_at": started, "profile_id": "onebase", "queue_id": "fix",
                   "route": "skill", "token": "original reservation", "cost": cost}
    db.set_setting(db._pipeline_github_budget_reservation_key("github-default"),
                   json.dumps({"version": 1, "reservations": [reservation]}))
    binding = [task.id, started, row["session_id"], row["herdr_pane"], reservation["token"],
               task_execution_fingerprint(row), row["series_id"], row["priority"]]
    digest = hashlib.sha256(json.dumps(binding, separators=(",", ":")).encode()).hexdigest()
    prior, good = sources()
    old = dict(prior["snapshot_token"], generated_at=now - 490)
    good["cache"]["generated_at"] = good["cache"]["token"]["generated_at"] = now - 5
    cache = dict(good["cache"], token=old, generated_at=old["generated_at"])
    prior.update(state="reused", source_reused=True, source_refreshed=False, task_id=task.id, task_started_at=started,
                 scope="Native-source-observation-only", mutable_admission_granted=False, product_mutation_authorized=False,
                 admitted_route="skill", existing_execution_reservation_reused=True, existing_skill_reservation_reused=True,
                 cache=cache, snapshot_token=old, observed_at=now - 450, fix_pr_heads={"17": SOURCE},
                 attempt_binding_sha256=digest)
    return prior, good


def test_reused_original_can_observe_after_expiry_with_only_worker_task_id(reused_fix_source, tmp_path, monkeypatch, capsys):
    prior, good = reused_fix_source
    path = tmp_path / "source.json"
    path.write_text(json.dumps(prior), encoding="utf-8")
    monkeypatch.setenv("PP_TASK_ID", str(prior["task_id"]))
    monkeypatch.delenv("PP_TASK_STARTED_AT", raising=False)
    class LocalCache:
        def open(self, url, timeout):
            assert url == runner.CACHE_URL and url.endswith("?refresh=false")
            return BytesIO(json.dumps(good).encode())
    monkeypatch.setattr(runner, "build_opener", lambda *_args: LocalCache())
    assert runner.main(["observe", "--source-receipt", str(path), "--seconds", "30"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fresh_snapshot"] == good["cache"]["token"] and result["publication_authorized"] is False
    monkeypatch.setenv("PP_TASK_STARTED_AT", "another attempt")
    assert runner.main(["observe", "--source-receipt", str(path), "--seconds", "30"]) == 2


@pytest.mark.parametrize("fault", ["at-expiry", "future", "other-binding", "wrong-route", "partial", "session-change",
                                   "changed-prompt", "reservation-released"])
def test_cached_observer_requires_original_freshness_and_current_attempt(reused_fix_source, fault):
    prior, good = reused_fix_source
    deferred = fault in {"session-change", "changed-prompt", "reservation-released"}
    if fault == "at-expiry":
        prior["observed_at"] = prior["snapshot_token"]["generated_at"] + 300
    elif fault == "future":
        prior["snapshot_token"]["generated_at"] = prior["cache"]["generated_at"] = time.time() + 5
        prior["observed_at"] = time.time() + 10
    elif fault == "other-binding":
        prior["attempt_binding_sha256"] = "c" * 64
    elif fault == "wrong-route":
        prior["admitted_route"] = "fallback_targeted"
    elif fault == "partial":
        prior["cache"]["complete"] = False
    def read(timeout):
        assert deferred, "invalid receipt reached the local observer"
        with db._connect() as conn:
            if fault == "session-change":
                conn.execute("UPDATE tasks SET session_id='next' WHERE id=?", (prior["task_id"],))
            elif fault == "changed-prompt":
                conn.execute("UPDATE tasks SET prompt='different' WHERE id=?", (prior["task_id"],))
            elif fault == "reservation-released":
                conn.execute("DELETE FROM settings WHERE key=?", (db._pipeline_github_budget_reservation_key("github-default"),))
        return good
    with pytest.raises(ValueError):
        runner.observe(prior, prior["task_id"], prior["task_started_at"], 30, read=read)


def test_timeout_of_whole_window_then_new_native_attempt():
    prior, good = sources()
    result, requests = run_observer(prior, [TimeoutError()], seconds=60)
    assert requests == [0, 50] and not result["fresh_snapshot"]
    with pytest.raises(ValueError):
        runner.observe(prior, 43, "new", 60, read=lambda timeout: pytest.fail())
    next_receipt = {**prior, "task_id": 43, "task_started_at": "new"}
    result = runner.observe(next_receipt, 43, "new", 60, read=lambda _: good, now=lambda: 180)
    assert result["fresh_snapshot"] == good["cache"]["token"]


def test_refusals_and_changed_profile_never_become_authority():
    prior, good = sources()
    for bad in ({**prior, "state": "wait"}, {**prior, "queue_id": "merge"}):
        with pytest.raises(ValueError):
            run_observer(bad, [good])
    with pytest.raises(ValueError):
        run_observer(prior, [{**good, "repository": "other/project"}])
    with pytest.raises(ValueError):
        run_observer(prior, [{**good, "diagnostics": {"checker_failed": True}}])
    with pytest.raises(HTTPError):
        run_observer(prior, [HTTPError(runner.CACHE_URL, 401, "rejected", {}, None)])
    for changed in ({"revision": 9}, {"epoch": 0}, {"revision": 10, "epoch": 2}):
        token = {**prior["snapshot_token"], **changed}
        snapshot = {**good, "cache": {**good["cache"], "token": token, "generated_at": token["generated_at"]}}
        assert not run_observer(prior, [snapshot], seconds=1)[0]["fresh_snapshot"]
    with pytest.raises(ValueError, match="Source refused"):
        run_observer(prior, [{**good, "cache": {**good["cache"], "refresh_blocked": "low"}}])
    future = {**prior, "snapshot_token": {**prior["snapshot_token"], "generated_at": 101}}
    assert not run_observer(future, [good], seconds=1)[0]["fresh_snapshot"]


def test_installer_targets_only_future_fix_and_preserves_ordinary_source(monkeypatch):
    anchor = installer.ANCHOR + " fixture"
    monkeypatch.setattr(installer, "ANCHOR_SHA", installer.sha(anchor.encode()))
    binding = installer.note("/release/runner.py", EPOCH)
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE task_series(id,title,working_dir,prompt,ended_at,updated_at);
        CREATE TABLE tasks(id,series_id,working_dir,prompt,status,started_at,session_id,retry_count);
    """)
    db.execute("INSERT INTO task_series VALUES(2,?,?,?,NULL,NULL)", (installer.TITLE, "FIX", anchor))
    for n, series, status in [(11,2,"pending"), (12,2,"running"), (13,4,"pending")]:
        db.execute("INSERT INTO tasks VALUES(?,?,'FIX',?,?,NULL,NULL,0)",(n,series,anchor,status))
    db.commit()
    def bind(action):
        return installer.bind(db,"FIX",binding,action,series=2,title=installer.TITLE,transform_prompt=installer.transform)
    assert bind("preview")["pending_changed"] == [11]
    assert bind("apply")["pending_changed"] == [11]
    assert not bind("verify")["series_changed"] and not bind("apply")["pending_changed"]
    assert db.execute("SELECT prompt FROM tasks WHERE id=11").fetchone()[0] == anchor + "\n\n" + binding
    assert db.execute("SELECT distinct prompt FROM tasks WHERE id<>11").fetchall() == [(anchor,)]
    assert bind("rollback")["pending_changed"] == [11]
    with pytest.raises(ValueError):
        installer.transform(anchor + "\n" + anchor, binding)
    db.close()
