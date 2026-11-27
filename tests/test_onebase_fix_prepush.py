from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace as NS

import pytest

from tools import onebase_fix_prepush as fix


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / ".promptpilot/fix-runs"
    old = root / "old"
    new = root / "new"
    old.mkdir(parents=True)
    new.mkdir()
    now = 1795770000
    monkeypatch.setattr(fix.time, "time", lambda: now)
    old_start, new_start, third_start = [datetime.fromtimestamp(now - n, timezone.utc).isoformat() for n in (900,60,30)]
    c = sqlite3.connect(root.parent / "promptpilot.db")
    c.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
    c.execute("CREATE TABLE task_series(id INTEGER,title TEXT,working_dir TEXT,paused INTEGER,ended_at TEXT)")
    columns = ("id", "series_id", "working_dir", "started_at", "session_id", "herdr_pane", "machine", "detached",
               "status", "result", "verdict", "task_timeout", "prompt", "provider", "model", "effort", "rights",
               "skip_permissions", "herdr_target", "worktree", "worktree_path", "worktree_branch", "priority")
    c.execute("CREATE TABLE tasks(" + ",".join(columns) + ")")
    directory = str(tmp_path / "Projects/onebase-automation/fix")
    c.execute("INSERT INTO task_series VALUES (2,?,?,0,NULL)", ("OneBase - FIX - исправление одобренного", directory))
    attempts = ((1, old_start), (2, new_start), (3, third_start))
    for task, started in attempts:
        fields = dict(id=task, series_id=2, working_dir=directory, started_at=started, session_id=str(task),
                      status="running", task_timeout=7200)
        c.execute("INSERT INTO tasks VALUES (" + ",".join("?" for _ in columns) + ")", [fields.get(k) for k in columns])
    reservations = [{"task_id": task, "task_started_at": started, "queue_id": "fix", "profile_id": "onebase",
                     "route": "skill", "token": str(task)} for task, started in attempts]
    c.execute("INSERT INTO settings VALUES (?,?)", ("pipeline_github_budget_reservations:v1:"
              + hashlib.sha256(b"github-default").hexdigest(), json.dumps({"version": 1, "reservations": reservations})))
    c.commit()

    checkout = tmp_path / "git"
    checkout.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                        "-c", "commit.gpgsign=false", *args], cwd=checkout).decode().strip()
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "source")
    source = git("rev-parse", "HEAD")
    (checkout / "change").write_text("checked", encoding="utf-8")
    git("add", "change")
    git("commit", "-qm", "candidate")
    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    bundle = old / "candidate.bundle"
    git("bundle", "create", str(bundle), "HEAD")
    pr = 91
    profile = "0" * 64
    proof = dict(review_id=1, review_node="r", claim_id=2, claim_node="c", completion_id=3,
                 completion_node="d", outcome="needs-decision")
    selection = dict(number=pr, head=source, epoch="1" * 64, owner="FIX", decision_id="4",
                     decision_sha256="2" * 64, proof=proof)
    pr_data = dict(number=pr, state="open", merged=False, maintainer_can_modify=True,
                   head=dict(repo=dict(full_name="fork/onebase"), ref="feature", sha=source),
                   base=dict(repo=dict(full_name=fix.REPO), ref="main", sha=source))
    def source_value(task, started, timestamp):
        token = dict(profile_hash=profile, epoch=7, revision=task, generated_at=timestamp)
        return dict(state="refreshed", source_refreshed=True, native_session_bound=True,
                    existing_skill_reservation_reused=True, admitted_route="skill", queue_id="fix",
                    profile_id="onebase", task_id=task, task_started_at=started, product_mutation_authorized=False,
                    scope="Native-source-observation-only", snapshot_token=token,
                    cache=dict(available=True, complete=True, stale=False, invalidated=False, ttl_seconds=300,
                               generated_at=timestamp, token=token, profile_hash=profile, source="live"))
    def ready(task, started):
        return dict(budget_observation_scope="running-budget-policy-v4", proof_kind="running",
                    state="ready", ready=True, profile_hash=profile, task_id=task, started_at=started,
                    profile_id="onebase", queue_id="fix", series_id=2, decision=dict(allowed=True,budget_route="skill"),
                    native_admission_granted=False, mutable_admission_granted=False, receipt_id="r",
                    rate_observed_at=now - 8, generated_at=now - 3)
    sel = write(old / "selected-target.json", selection)
    orig_source = write(old / "source.stdout", source_value(1, old_start, now-650))
    report = write(old / "actual-checks.json", dict(command="test", status=0))
    manifest = write(old / "manifest.json", dict(head=head, source=source, tree=tree,
                     files={str(p):fix.file_hash(p) for p in (sel, orig_source, bundle, report)}))
    write(old / "attempt.json", dict(task_id="1", started_at=old_start, scope="fix-approved", github_mutations=False))
    old_pr = write(old / f"pr-{pr}.json", pr_data)
    stale = write(old / "precheck-budget.json", dict(state="refused", ready=False, reason="rate_snapshot_stale",
                          task_id=1, started_at=old_start, profile_id="onebase", queue_id="fix", profile_hash=profile))
    wait = write(old / "wait.json", dict(state="wait", scope="Native-budget-observation-only", admitted_route="skill",
                 budget=dict(allowed=False, state="scan_in_progress", budget_route="insights"),
                 product_mutation_authorized=False, rate_snapshot_refreshed=False))
    outcome = write(old / "outcome.json", dict(state="availability_wait", stage="fix-approved", reason="scan_in_progress",
                    pushed=False, github_mutations=False, pr=pr, source=source, candidate=head, tree=tree,
                    acceptance_manifest=str(manifest), bundle=str(bundle), budget_observation=str(wait)))
    read_args = NS(pr=pr, source=source, selection=write(new/"new-selected.json", {**selection, "decision_id":4}),
                   source_receipt=write(new/"source.json", source_value(2, new_start, now-25)),
                   budget_receipt=write(new/"budget.json", ready(2, new_start)), pr_read=write(new/"pr.json", pr_data))
    def finish(reason="scan_in_progress", task=1):
        c.execute("UPDATE tasks SET status='completed',verdict='ПУСТО',result=? WHERE id=?",
                  (f"ИТОГ: ПУСТО — нет допуска: {reason}", task))
        c.commit()
    capture_args = NS(before_send=True, manifest=manifest, bundle=bundle, selection=sel, source_receipt=orig_source,
                      pr_read=old_pr, precheck_budget=stale, budget_wait=wait, source_wait=None, source_cache=None,
                      observer_start=None, observer_exit=None)
    yield NS(root=root, pr=pr, source=source, head=head, tree=tree, checkout=checkout, selection=selection,
             manifest=manifest, bundle=bundle, outcome=outcome, old=old, new=new, c=c, finish=finish, now=now,
             old_start=old_start, new_start=new_start, third_start=third_start, ready=ready,
             source_value=source_value, git=git, record_args=NS(outcome=outcome),
             read_args=read_args, capture_args=capture_args,
             claim_args=lambda ticket:NS(ticket=Path(ticket), worktree=checkout, budget_receipt=read_args.budget_receipt))
    c.close()


def test_wait_next_election_and_only_one_claim(case):
    r = case
    assert fix.read(r.root, r.read_args, 2, r.new_start)["state"] == "missing"
    assert fix.capture(r.root, r.capture_args, 1, r.old_start)["state"] == "recorded"
    with pytest.raises(ValueError, match="origin"):
        fix.read(r.root, r.read_args, 2, r.new_start)
    r.finish()
    result = fix.read(r.root, r.read_args, 2, r.new_start)
    assert result["candidate"] == r.head and result["tree"] == r.tree
    assert result["publication_authorized"] is False and result["acceptance_reuse_authorized"] is False
    args = r.claim_args(result["ticket"])
    assert fix.claim(r.root, args, 2, r.new_start)["state"] == "claimed"
    with pytest.raises(ValueError, match="already claimed"):
        fix.claim(r.root, args, 2, r.new_start)
    assert fix.read(r.root, r.read_args, 2, r.new_start)["state"] == "reconcile"


def test_seed_requires_operator_pin_and_clean_origin(case, monkeypatch):
    r = case
    data = fix.evidence(r.root, r.outcome)
    seed = NS(outcome=r.outcome, legacy_sha256="0"*64)
    with pytest.raises(ValueError, match="digest differs"):
        fix.seed(r.root, seed)
    seed.legacy_sha256 = fix.digest(fix.encode(data))
    with pytest.raises(ValueError, match="origin"):
        fix.seed(r.root, seed)
    r.finish()
    monkeypatch.setenv("PP_TASK_ID","2")
    with pytest.raises(ValueError, match="operator"):
        fix.seed(r.root, seed)
    monkeypatch.delenv("PP_TASK_ID")
    assert fix.seed(r.root, seed)["candidate"] == r.head
    assert fix.read(r.root, r.read_args, 2, r.new_start)["candidate"] == r.head


def test_current_task_and_reservation_fences(case, monkeypatch):
    r = case
    monkeypatch.setenv("PP_TASK_ID", "2")
    monkeypatch.delenv("PP_TASK_STARTED_AT", raising=False)
    assert fix.task_env(r.root) == (2, r.new_start)
    fix.record(r.root, r.record_args, 1, r.old_start)
    r.finish()
    ticket = fix.read(r.root, r.read_args, 2, r.new_start)["ticket"]
    r.c.execute("UPDATE tasks SET prompt='rebound' WHERE id=2")
    r.c.commit()
    with pytest.raises(ValueError, match="execution"):
        fix.claim(r.root, r.claim_args(ticket), 2, r.new_start)
    r.c.execute("DELETE FROM settings")
    r.c.commit()
    with pytest.raises(ValueError, match="reservation"):
        fix.read(r.root, r.read_args, 2, r.new_start)


@pytest.mark.parametrize("reason", ["rate_snapshot_stale","cache_invalidated"])
def test_capture_independent_prepublication_stops(case, reason):
    r = case
    a = r.capture_args
    a.budget_wait = None
    if reason == "cache_invalidated":
        a.precheck_budget = write(r.old/"ready-before.json", r.ready(1, r.old_start))
        a.source_wait = write(r.old/"observer.json", dict(fresh_snapshot=None, publication_authorized=False))
        a.observer_start = write(r.old/"start.json", dict(task="1", started_at=r.old_start, seconds=60))
        a.observer_exit = write(r.old/"exit",3)
        a.source_cache = write(r.old/"cache.json", dict(repository=fix.REPO, profile_id="onebase",
                   cache=dict(complete=True, invalidated=True, profile_hash="0"*64), diagnostics=dict(exit_code=0)))
    fix.capture(r.root, a, 1, r.old_start)
    r.finish(reason)
    assert fix.read(r.root, r.read_args, 2, r.new_start)["state"] == "candidate"


@pytest.mark.parametrize("prior", ["interrupted", "damaged", "two_verified_candidates"])
def test_next_verified_capture_remains_recoverable_after_incomplete_origin(case, prior):
    r = case
    first = fix.capture(r.root, r.capture_args, 1, r.old_start)
    if prior == "interrupted":
        r.c.execute("UPDATE tasks SET status='failed' WHERE id=1")
        r.c.commit()
    else:
        r.finish()
    second = r.root / "second"
    second.mkdir()
    a = NS(**vars(r.capture_args))
    for key in ("selection", "pr_read", "budget_wait"):
        old = getattr(a, key)
        setattr(a, key, Path(shutil.copy(old, second / old.name)))
    a.source_receipt = write(second/"source.json", r.source_value(2, r.new_start, r.now-25))
    stale = json.loads(r.capture_args.precheck_budget.read_bytes())
    a.precheck_budget = write(second/"precheck.json", {**stale, "task_id":2, "started_at":r.new_start})
    a.bundle = second / "candidate.bundle"
    if prior == "two_verified_candidates":
        r.git("commit", "-q", "--amend", "-m", "different verified commit")
    head = r.git("rev-parse", "HEAD")
    r.git("bundle", "create", str(a.bundle), "HEAD")
    report = write(second/"test.json", {"command":"test", "status":0})
    a.manifest = write(second/"manifest.json", dict(source=r.source, head=head, tree=r.tree,
                   files={str(path):fix.file_hash(path) for path in (a.bundle, a.selection, a.source_receipt, report)}))
    if prior == "damaged":
        (r.old/"actual-checks.json").write_bytes(b"corrupt")
    added = fix.capture(r.root, a, 2, r.new_start)
    assert Path(first["record"]).is_file() and first["record"] != added["record"]
    r.finish(task=2)
    args = NS(**vars(r.read_args))
    args.source_receipt = write(r.new/"third-source.json", r.source_value(3, r.third_start, r.now-4))
    args.budget_receipt = write(r.new/"third-budget.json", r.ready(3, r.third_start))
    if prior == "two_verified_candidates":
        with pytest.raises(ValueError, match="conflicting verified candidates"):
            fix.read(r.root, args, 3, r.third_start)
    else:
        restored = fix.read(r.root, args, 3, r.third_start)
        assert restored["state"] == "candidate" and restored["bundle"] == str(a.bundle)
    assert not list(r.root.rglob("push-attempt.json"))
