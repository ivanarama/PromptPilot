"""Exercise the opt-in CLI against a publication from an independent sampler."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import time

import pytest

from promptpilot import pipeline_insights as pi, pipeline_source_refresh as source, project_pipeline
from promptpilot.task_identity import task_execution_fingerprint
from tests.source_cost_fixtures import checker_receipt
from tests.test_github_budget import _limits
from tests.test_pipeline_source_refresh import continuation as continuation


@pytest.fixture
def published(continuation, monkeypatch):
    f = continuation
    db, task = f.db, f.task
    db.release_pipeline_github_budget("github-default", token="existing-skill",
                                      task_id=task.id, task_started_at=task.started_at)
    number, head = 42, "a" * 40
    url = "https://github.com/owner/example/pull/42"
    item = dict(kind="pr", number=number, key="pr:42", url=url, labels=["changes-requested"])
    candidate = dict(number=number, head=head, url=url, stage="review")
    monkeypatch.setattr(pi, "_github_search", lambda *_: dict(count=1, items=[item], membership_complete=True))
    monkeypatch.setattr(pi, "_run_profile_health_check", lambda _: checker_receipt(
        {"state": "yellow", "findings": [], "fix_candidates": [candidate]}))
    report = pi.analyze("example", db.list_series(), use_cache=False, refresh_diagnostics=True)
    assert report["cache"]["source"] == "live"
    # The work starts AFTER the separate producer captured the publication.
    started = datetime.now(timezone.utc)
    with db._connect() as c:
        c.execute("UPDATE tasks SET started_at=? WHERE id=?", (started.isoformat(), task.id))
    assert db.reserve_pipeline_github_budget("github-default", token="admitted", task_id=task.id,
        task_started_at=started, profile_id="example", queue_id="fix", route="skill",
        cost=f.profile["github_budget"]["costs"]["skill"], limits=_limits(),
        minimum_remaining={"core": 250, "search": 2, "graphql": 100})["allowed"]
    monkeypatch.setenv("PP_TASK_ID", str(task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", started.isoformat())
    key = pi._cache_key("example", pi._profile_fingerprint(f.profile))
    return f, key, report["cache"]["token"], started


def run_cli(capsys, *, allow_deferred=True):
    args = ["source-refresh", "--profile", "example", "--reuse-fix-cache-only"]
    if allow_deferred:
        args.append("--allow-deferred-insights-read")
    status = project_pipeline.run(args)
    return status, json.loads(capsys.readouterr().out)


def publish_priority_scan_wait(f, **budget):
    decision = {"state": "priority_waiter", "allowed": False, "budget_route": "insights",
                "defer_until": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    value = pi._refresh_blocked_status(decision)
    value["github_budget"].update(budget)
    assert f.db.publish_pipeline_refresh_status("example", f.profile["repository"], value)


@pytest.mark.parametrize("deferred", [False, True])
def test_shared_full_publication_costs_no_scan_and_preserves_executing_lease(published, capsys, monkeypatch, deferred):
    f, _, token, started = published
    reserve_key = f.db._pipeline_github_budget_reservation_key("github-default")
    before = f.db.get_setting(reserve_key)
    assert f.db.acquire_pipeline_scan_lease("github-default", "another-process", 180)["acquired"]
    scan_key = f.db._pipeline_scan_lease_key("github-default")
    scan = f.db.get_setting(scan_key)
    if deferred:
        publish_priority_scan_wait(f)
    event = f.db.get_pipeline_refresh_status("example")
    monkeypatch.setattr(pi, "_github_scan_admission", lambda *a, **k: pytest.fail("cache read requested a scan"))
    if deferred:
        status, legacy = run_cli(capsys, allow_deferred=False)
        assert status == 3 and legacy["cache_miss_reason"] == "priority_waiter"
    status, result = run_cli(capsys)
    assert status == 0 and result["state"] == "reused"
    assert result["source_refreshed"] is False and result["source_reused"] is True
    assert result["cache"]["source"] == "durable" and result["snapshot_token"] == token
    assert token["generated_at"] < started.timestamp() <= result["observed_at"]
    assert "cache_miss_reason" not in result
    assert result["fix_pr_heads"] == {"42": "a" * 40}
    current = f.db.get_task(f.task.id)
    expected = [current.id, started.isoformat(), current.session_id, current.herdr_pane, "admitted",
                task_execution_fingerprint(current), current.series_id, current.priority]
    assert result["attempt_binding_sha256"] == hashlib.sha256(json.dumps(expected, separators=(",", ":")).encode()).hexdigest()
    assert result["mutable_admission_granted"] is False and result["product_mutation_authorized"] is False
    assert f.db.get_setting(reserve_key) == before and len(f.db.list_pipeline_snapshots("example")) == 1
    assert f.db.get_setting(scan_key) == scan
    assert f.db.get_pipeline_refresh_status("example") == event
    assert not result["cache"].get("refresh_blocked") and "refresh_budget" not in result["cache"]
    if deferred:
        assert result["deferred_refresh"] == {"refresh_blocked": "priority_waiter", "refresh_budget": {
            "budget_route": "insights", "state": "priority_waiter", "allowed": False}}
        # Other consumers still observe the denial on this same publication.
        live = pi.read_cached("example", f.db.list_series())
        assert pi._required_snapshot_state(live, f.profile, "fix") == "refresh_blocked=priority_waiter"
    else:
        assert "deferred_refresh" not in result


@pytest.mark.parametrize(("fault", "reason"), [
    ("missing", "snapshot_missing"), ("schema", "snapshot_invalid"),
    ("partial", "queue_incomplete"), ("expired", "snapshot_expired"),
    ("future", "snapshot_from_future"), ("checker-expired", "checker_expired"),
    ("checker-hash", "checker_proof_invalid"), ("checker-failed", "checker_failed"),
    ("unreadable-status", None), ("status-db-error", None), ("profile", "profile_changed"),
    ("epoch", "snapshot_invalidated"), ("global-revision", "publication_changed"),
    ("namespace-revision", "publication_changed"), ("ambiguous-head", "candidate_invalid"),
    ("unknown-head", "candidate_invalid"), ("held", "no_eligible_pr"),
    ("wrong-url", "no_eligible_pr"), ("issue-only", "no_eligible_pr"),
    ("body-race", "publication_changed"), ("cancel-race", None),
    ("status-race", "publication_changed"), ("checker-latch", "checker_failed"),
])
@pytest.mark.parametrize("deferred", [False, True])
def test_unusable_cache_does_not_scan_or_issue_a_receipt(published, monkeypatch, capsys, fault, reason, deferred):
    f, key, token, _ = published
    db = f.db
    value = json.loads(db.get_setting(key))
    report = value["result"]
    if deferred:
        publish_priority_scan_wait(f)
    if fault == "missing":
        with db._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
    elif fault == "schema":
        value["version"] = 0
    elif fault == "checker-failed":
        report["diagnostics"]["checker_failed"] = True
    elif fault == "checker-latch":
        event = db.get_pipeline_refresh_status("example")
        event["source_checker_failure_revisions"] = {pi._profile_fingerprint(f.profile): event["revision"]}
        db.set_setting(db._pipeline_refresh_status_key("example"), json.dumps(event))
    elif fault == "partial":
        report["queues"][0]["membership_complete"] = False
    elif fault in {"expired", "future"}:
        value["generated_at"] = report["generated_at"] = time.time() + (10 if fault == "future" else -300)
    elif fault == "checker-expired":
        report["diagnostics_generated_at"] -= 300
    elif fault == "checker-hash":
        report["diagnostics"]["source_graphql_cost_proof"]["receipt_sha256"] = "0" * 64
    elif fault == "unreadable-status":
        db.set_setting(db._pipeline_refresh_status_key("example"), "malformed")
    elif fault == "status-db-error":
        def unreadable(*args, **kwargs):
            raise sqlite3.OperationalError("database unavailable")
        monkeypatch.setattr(db, "get_pipeline_refresh_status", unreadable)
    elif fault == "profile":
        f.profile["queues"][0]["query"] = "is:issue"
    elif fault == "epoch":
        db.set_setting(pi._CACHE_EPOCH_KEY, str(token["epoch"] + 1))
    elif fault == "global-revision":
        db.set_setting(pi._published_profile_revision_key("example"), str(token["revision"] + 1))
    elif fault == "namespace-revision":
        db.set_setting(pi._published_revision_key("example", token["profile_hash"]), str(token["revision"] + 1))
    elif fault == "ambiguous-head":
        rows = report["diagnostics"]["fix_candidates"]
        rows.append(dict(rows[0], head="b" * 40))
    elif fault == "unknown-head":
        report["diagnostics"]["fix_candidates"][0]["head"] = ""
    elif fault == "held":
        report["queues"][0]["admission_items"][0]["labels"].append("hold")
    elif fault == "wrong-url":
        report["queues"][0]["admission_items"][0]["url"] += "0"
    elif fault == "issue-only":
        report["diagnostics"]["fix_candidates"] = [dict(stage="fix-issue", number=42, eligibility_digest="b" * 40)]
    elif fault in {"body-race", "cancel-race", "status-race"}:
        original = pi._with_persisted_refresh_status
        def race(*args):
            observed = original(*args)
            if fault == "body-race":
                db.set_setting(key, json.dumps(dict(value, extra="replaced")))
            elif fault == "status-race":
                assert db.publish_pipeline_refresh_status("example", f.profile["repository"], None)
            else:
                db.set_setting(f"cancel_task:{f.task.id}", "1")
            return observed
        monkeypatch.setattr(pi, "_with_persisted_refresh_status", race)
    if fault != "missing":
        db.set_setting(key, json.dumps(value))
    monkeypatch.setattr(pi, "_github_scan_admission", lambda *a, **k: pytest.fail("cache miss must not scan"))
    status, result = run_cli(capsys)
    refused = reason is None
    assert status == (2 if refused else 3)
    assert result["state"] == ("refused" if refused else "wait")
    assert not result.get("source_reused") and not result.get("snapshot_token")
    assert result["source_refreshed"] is False and result["mutable_admission_granted"] is False
    if refused:
        assert "cache_miss_reason" not in result
    else:
        assert result["reason"] == "fresh FIX cache unavailable"
        assert result["cache_miss_reason"] == reason


@pytest.mark.parametrize(("blocked", "reason"), [
    ("scan_in_progress", "scan_in_progress"), ("budget_in_flight", "budget_in_flight"),
    ("low", "budget_low"), ("priority_waiter", "priority_waiter"),
    ("fairness_waiter", "fairness_waiter"), ("rate_snapshot_stale", "rate_snapshot_stale"),
    ("arbitrary-private-value", "refresh_blocked"),
])
def test_persisted_refresh_status_returns_only_known_reason_codes(published, capsys, blocked, reason):
    f, _, _, _ = published
    assert f.db.publish_pipeline_refresh_status("example", "owner/example", {
        "refresh_blocked": blocked, "refresh_blocked_reason": "arbitrary-private-description",
        "refresh_deferred_until": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    })
    status, result = run_cli(capsys)
    assert status == 3 and result["state"] == "wait"
    assert result["reason"] == "fresh FIX cache unavailable" and result["cache_miss_reason"] == reason
    assert "arbitrary-private" not in json.dumps(result)


@pytest.mark.parametrize("override", [
    {"allowed": True}, {"allowed": 0}, {"state": "fairness_waiter"}, {"budget_route": "unclassified"},
])
def test_ambiguous_or_contradictory_priority_status_waits(published, capsys, override):
    f, _, _, _ = published
    publish_priority_scan_wait(f, **override)
    status, result = run_cli(capsys)
    assert status == 3 and result["cache_miss_reason"] == "priority_waiter"
    assert not result["source_reused"] and "snapshot_token" not in result


@pytest.mark.parametrize("args", [[], ["--final"]])
def test_deferred_insights_read_cannot_be_an_active_source_request(capsys, args):
    assert project_pipeline.run(["source-refresh", "--profile", "example", "--allow-deferred-insights-read", *args]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "refused" and result["source_refreshed"] is False
    assert "--reuse-fix-cache-only" in result["reason"]
    with pytest.raises(ValueError, match="only an initial"):
        source.refresh("example", budget_only=True, reuse_fix_cache_only=True, allow_deferred_insights_read=True)


@pytest.mark.parametrize("fault", ["replica", "ownership", "existing-target", "wrong-start", "missing-reservation"])
def test_cached_source_refuses_other_worker_modes_and_attempts(published, monkeypatch, capsys, fault):
    f, _, _, started = published
    if fault == "replica":
        f.profile["queues"][0]["replicas"] = 2
    elif fault == "ownership":
        monkeypatch.setenv("PP_PROVIDER_OWNERSHIP_KIND", "headless")
    elif fault == "existing-target":
        with f.db._connect() as conn:
            conn.execute("INSERT INTO pipeline_target_reservations(repository,stage,number,head,task_id,token,reserved_at,expires_at) "
                         "VALUES('owner/example','fix-pr',42,?,?,'target',?,?)",
                         ("a" * 40, f.task.id, started.isoformat(), started.isoformat()))
    elif fault == "wrong-start":
        monkeypatch.setenv("PP_TASK_STARTED_AT", "foreign")
    else:
        f.db.release_pipeline_github_budget("github-default", token="admitted", task_id=f.task.id, task_started_at=started)
    monkeypatch.setattr(pi, "_github_scan_admission", lambda *a, **k: pytest.fail("unexpected scan"))
    status, result = run_cli(capsys)
    assert status == 2 and result["state"] == "refused" and not result.get("snapshot_token")
    assert "cache_miss_reason" not in result


def test_cache_miss_leaves_the_existing_active_refresh_available(published, capsys):
    f, _, initial, _ = published
    pi._discard_cache("example")
    assert run_cli(capsys)[0] == 3
    assert source.run(["--profile", "example"]) == 0
    active = json.loads(capsys.readouterr().out)
    assert active["source_refreshed"] is True and active["cache"]["source"] == "live"
    assert active["snapshot_token"]["revision"] > initial["revision"]
    assert len(f.db.list_pipeline_snapshots("example")) == 2
