from datetime import datetime, timedelta, timezone
import copy
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from promptpilot.task_identity import task_execution_fingerprint
from promptpilot import db, pipeline_insights as pi, source_graphql_cost as cost
from tests.source_cost_fixtures import declare_checker
from tools import onebase_fix_cache as launcher
from tools import onebase_fix_cache_guard as guard
from tools import onebase_fix_cache_observe as late
from tools import onebase_fix_postpush as post
from tools import onebase_fix_prepush as prepush


pytest_plugins = ("test_onebase_fix_prepush",)


@pytest.fixture
def runtime(case, tmp_path, monkeypatch):
    series, task, started = 46, 2, case.new_start
    folder = tmp_path / "work"
    folder.mkdir()
    marker = "<!-- pp:onebase-fix-cache config-sha256=" + "a" * 64 + " -->"
    case.c.execute("UPDATE task_series SET id=?,working_dir=?", (series, str(folder)))
    case.c.execute("UPDATE tasks SET series_id=?,working_dir=?,prompt=?", (series, str(folder), marker))
    case.c.execute("CREATE TABLE pipeline_target_reservations(repository,number,head,task_id,task_started_at,expires_at)")
    case.c.commit()
    monkeypatch.setenv("PP_TASK_ID", str(task))
    monkeypatch.delenv("PP_TASK_STARTED_AT", raising=False)
    installed = object.__new__(guard.Installation)
    installed.prepush = prepush
    installed.config_sha = "a" * 64
    installed.config = {"native": {"path": str(tmp_path / "active")},
                        "cache_native": {"path": str(tmp_path / "cache")}}
    installed.series = series
    installed.working_dir = folder
    installed.data_dir, installed.root = case.root.parent, case.root
    installed.profiles_path = installed.data_dir / "profiles.json"
    installed.db = installed.data_dir / "promptpilot.db"
    installed.journal = installed.data_dir / "onebase-fix-initial-observations"
    monkeypatch.setattr(installed, "verify_worker", lambda: None)
    if os.name != "posix":
        monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setitem(sys.modules, "fcntl", NS(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None))
    monkeypatch.setattr(guard, "pin_binary", lambda pin: None)
    monkeypatch.setattr(guard, "still_pinned", lambda *args: None)

    case.c.row_factory = sqlite3.Row
    row = case.c.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    binding = [task, started, row["session_id"], row["herdr_pane"], str(task),
               task_execution_fingerprint(row), series, row["priority"]]
    source = case.source_value(task, started, case.now - 100)
    source.update(state="reused", source_refreshed=False, source_reused=True,
                  existing_execution_reservation_reused=True, mutable_admission_granted=False,
                  observed_at=case.now - 25, attempt_binding_sha256=prepush.digest(prepush.encode(binding)),
                  fix_pr_heads={str(case.pr): case.source})
    source["cache"]["source"] = "durable"
    url = f"https://github.com/{prepush.REPO}/pull/{case.pr}"
    live = {"profile_id": "onebase", "repository": prepush.REPO, "cache": dict(source["cache"]),
            "diagnostics_generated_at": case.now - 15,
            "diagnostics": {"exit_code": 0, "fix_candidates": [dict(stage="review", number=case.pr, head=case.source)]},
            "queues": [dict(id="fix", membership_complete=True, admission_items=[
                dict(kind="pr", number=case.pr, key=f"pr:{case.pr}", labels=["changes-requested"], url=url)])]}
    monkeypatch.setattr(prepush, "read_cached_fix_queue", lambda: live)
    selection = json.loads(case.read_args.selection.read_bytes())
    selection.update(task_id=task, task_started_at=started, snapshot_token=source["snapshot_token"])
    pr = json.loads(case.read_args.pr_read.read_bytes())
    pr.update(labels=[{"name": "changes-requested"}], html_url=url)
    responses = []
    calls = []

    def native(command, **kwargs):
        calls.append(command)
        assert command[-2:] == ["--reuse-fix-cache-only", "--allow-deferred-insights-read"]
        assert kwargs["env"]["PP_DATA_DIR"] == str(installed.data_dir)
        assert kwargs["env"]["PP_PIPELINE_PROFILES"] == str(installed.profiles_path)
        code, response = responses.pop(0)
        return subprocess.CompletedProcess(command, code, stdout=json.dumps(response).encode())

    monkeypatch.setattr(launcher.subprocess, "run", native)
    return NS(inst=installed, source=source, select=selection, pr=pr, live=live, responses=responses, calls=calls,
              args=NS(refresh_after_reused=None, selection=None, target_read=None))


def miss(reason=None):
    value = {"state": "wait", "source_refreshed": False, "reason": "fresh FIX cache unavailable"}
    if reason is not None:
        value["cache_miss_reason"] = reason
    return 3, value


@pytest.mark.parametrize("wait", [False, True])
def test_cache_hit_is_issued_once_and_binds_regular_election(runtime, wait, capsysbinary):
    r = runtime
    if wait:
        r.responses.append(miss("scan_in_progress"))
    r.responses.append((0, r.source))
    assert launcher.run_initial(guard, r.inst, r.args) == 0
    raw = capsysbinary.readouterr().out
    assert raw == json.dumps(r.source).encode()
    r.inst.bind(raw, r.select, r.pr)
    with pytest.raises(ValueError, match="original receipt bytes"):
        r.inst.bind(raw + b" ", r.select, r.pr)
    with pytest.raises(ValueError, match="already issued"):
        launcher.run_initial(guard, r.inst, r.args)
    assert launcher.journal_report(r.inst.journal)["full_scan_claims"] == 0


def test_fallback_after_hit_rechecks_live_election_and_publication(runtime, case, capsysbinary):
    r = runtime
    r.responses.append((0, r.source))
    assert launcher.run_initial(guard, r.inst, r.args) == 0
    raw = capsysbinary.readouterr().out
    ident = r.inst.identity()
    with pytest.raises(ValueError, match="can use the issued"):
        r.inst.fallback_reason(raw, r.select, r.pr, ident)
    with pytest.raises(ValueError, match="live PR"):
        r.inst.fallback_reason(raw, r.select, dict(r.pr, labels=[{"name": "ship"}]), ident)
    r.live["cache"]["stale"] = True
    assert r.inst.fallback_reason(raw, r.select, r.pr, ident) == "publication_changed"
    r.live["cache"]["stale"] = False
    token = dict(r.source["snapshot_token"], revision=1)
    r.live["cache"]["token"] = token
    with pytest.raises(ValueError, match="publication"):
        r.inst.bind(raw, dict(r.select, snapshot_token=token), r.pr)
    case.c.execute("UPDATE tasks SET session_id='replacement' WHERE id=?", (ident[0],))
    case.c.commit()
    with pytest.raises(ValueError, match="another FIX attempt"):
        r.inst.checked_origin(r.source)


def test_deferred_scan_can_bind_and_observe_the_same_verified_publication(runtime, case, tmp_path, capsysbinary,
                                                                        monkeypatch):
    r = runtime
    profile = {"repository": prepush.REPO, "queues": [{"id": "fix"}],
               "health_check": {"cache_seconds": 300}, "github_budget": {}}
    declare_checker(profile, tmp_path)
    profile["priority_control"]["trusted_account"] = "ivanarama"
    fingerprint = pi._profile_fingerprint(profile)
    r.inst.profiles_path.write_text(json.dumps({"profiles": {"onebase": profile}}))
    r.inst.cost = cost
    token = dict(r.source["snapshot_token"], profile_hash=fingerprint, revision=4, epoch=1)
    scan = {"budget_route": "insights", "state": "priority_waiter", "allowed": False}
    until = datetime.fromtimestamp(case.now+180, timezone.utc).isoformat()
    r.source["cache"].update(token=token, profile_hash=fingerprint)
    r.source["deferred_refresh"] = dict(refresh_blocked="priority_waiter", refresh_budget=scan)
    r.live["cache"].update(token=token, profile_hash=fingerprint, refresh_blocked="priority_waiter",
                           refresh_budget=scan, refresh_deferred_until=until)
    r.source["snapshot_token"] = r.select["snapshot_token"] = token
    r.live["generated_at"] = token["generated_at"]
    contract = cost.health_contract(profile)
    checker = {k: contract[k] for k in ("protocol", "repo", "contract_sha256", "trusted_account")}
    checker.update(nonce="0"*32, allowance=10, charged_upper_bound=2, observed_actual_cost=2, physical_attempts=2,
                   preview_attempts=1, data_attempts=1, unproved_attempts=0, uncertain_cost=False, complete=True)
    proof = dict(executable_sha256=contract["executable_sha256"], receipt_sha256=cost.digest(checker))
    r.live["diagnostics"].update(source_graphql_usage=checker, source_graphql_cost_proof=proof)
    r.live["source_search_usage"] = dict(successful_observation_cost_proved=True, uncertain_cost=False,
                                        health_cost_provenance=proof)
    publication = dict(token, version=1, repository=prepush.REPO, profile_id="onebase",
                       result=copy.deepcopy(r.live))
    refresh = dict(version=1, repository=prepush.REPO, revision=3,
                   status=dict(refresh_blocked="priority_waiter", github_budget=scan, refresh_deferred_until=until))
    status_key = db._pipeline_refresh_status_key("onebase")
    cache_key = pi._cache_key("onebase", fingerprint)
    records = {cache_key: json.dumps(publication), status_key: json.dumps(refresh),
               pi._CACHE_EPOCH_KEY: str(token["epoch"]), pi._published_profile_key("onebase"): fingerprint,
               pi._published_profile_revision_key("onebase"): str(token["revision"]),
               pi._published_revision_key("onebase", fingerprint): str(token["revision"])}
    case.c.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", records.items())
    case.c.commit()

    r.responses.append((0, r.source))
    assert launcher.run_initial(guard, r.inst, r.args) == 0
    raw = capsysbinary.readouterr().out
    parsed = json.loads(raw)
    assert not parsed["cache"].get("refresh_blocked") and parsed["deferred_refresh"] == r.source["deferred_refresh"]
    r.inst.bind(raw, r.select, r.pr)
    # Each mutation still checks the unmodified live scan/Source and budget.
    with pytest.raises(ValueError, match="cache is unavailable"):
        prepush.current_fix_token(case.pr, case.source, report=r.live)
    with pytest.raises(ValueError, match="currently refused"):
        r.inst.fallback_reason(raw, r.select, r.pr, r.inst.identity())

    receipt = r.inst.root / "initial.stdout"
    receipt.write_bytes(raw)
    ready = json.loads(case.read_args.budget_receipt.read_bytes())
    ready.update(series_id=r.inst.series, profile_hash=fingerprint)
    def observe(*args, **kwargs):
        return post.observe(*args, **kwargs, read=lambda _: r.live, now=lambda: case.now)
    observed = late.observe_once(r.inst, receipt, 30, budget=lambda: ready, observe=observe)
    assert observed["fresh_snapshot"] == token and observed["refresh_blocked"] == "priority_waiter"
    assert observed["publication_authorized"] is False
    assert len(r.calls) == 1 and launcher.journal_report(r.inst.journal)["full_scan_claims"] == 0

    # Repeat against the same SQLite publication. A selected target does not gain
    # credit for a different publication, route, check or current epoch.
    assert not r.inst.priority_read(r.live, dict(token, revision=2))
    assert not r.inst.priority_read(r.live, token, now=token["generated_at"]+300)
    fake_live = copy.deepcopy(r.live)
    fake_live["queues"][0]["admission_items"][0]["labels"] = ["ship"]
    assert not r.inst.priority_read(fake_live, token)
    # Both the loopback report and durable publication can contain stale or
    # damaged checker evidence. Matching them does not validate that evidence.
    for fault in ("stale_checker", "wrong_checker_proof"):
        raw = copy.deepcopy(publication)
        candidate = raw["result"]
        if fault == "stale_checker":
            candidate["diagnostics_generated_at"] = case.now - 301
        else:
            candidate["diagnostics"]["source_graphql_cost_proof"]["receipt_sha256"] = "f" * 64
        assert not guard.priority_publication(candidate, token, profile, raw, refresh, cost, case.now)
    for blocked, route, allowed in (("fairness_waiter","insights",False), ("priority_waiter","skill",False),
                                    ("priority_waiter","insights",0), ("priority_waiter","insights",True)):
        event = dict(refresh, status=dict(refresh_blocked=blocked, refresh_deferred_until=until,
                     github_budget=dict(state=blocked,budget_route=route,allowed=allowed)))
        case.c.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(event), status_key))
        case.c.commit()
        assert not r.inst.priority_read(r.live, token)
    case.c.execute("UPDATE settings SET value=? WHERE key=?", (records[status_key], status_key))
    case.c.execute("UPDATE settings SET value=? WHERE key=?", ("2", pi._CACHE_EPOCH_KEY))
    case.c.commit()
    assert not r.inst.priority_read(r.live, token)
    case.c.execute("UPDATE settings SET value=? WHERE key=?", (records[pi._CACHE_EPOCH_KEY], pi._CACHE_EPOCH_KEY))
    case.c.commit()
    check = cost.checker_miss
    def invalidate_during_check(*args):
        case.c.execute("UPDATE settings SET value=? WHERE key=?", ("2", pi._CACHE_EPOCH_KEY))
        case.c.commit()
        return check(*args)
    monkeypatch.setattr(cost, "checker_miss", invalidate_during_check)
    assert not r.inst.priority_read(r.live, token)


@pytest.mark.parametrize("number", [2279, 2280])
def test_legacy_scoped_pr_needs_clear_live_source_with_original_receipt(runtime, case, capsysbinary,
                                                                         monkeypatch, number):
    r = runtime
    url = f"https://github.com/{prepush.REPO}/pull/{number}"
    r.source["fix_pr_heads"] = {str(number): case.source}
    r.select.update(number=number)
    r.pr.update(number=number, html_url=url)
    r.live["queues"][0]["admission_items"][0].update(number=number, key=f"pr:{number}", url=url)
    r.live["diagnostics"]["fix_candidates"][0].update(number=number)
    fact = dict(refresh_blocked="priority_waiter", refresh_budget=dict(budget_route="insights",
                                                                       state="priority_waiter", allowed=False))
    r.source["deferred_refresh"] = fact
    r.responses.append((0, r.source))
    assert launcher.run_initial(guard, r.inst, r.args) == 0
    raw = capsysbinary.readouterr().out
    receipt = json.loads(raw)
    # Legacy source_token consumes the original cache availability and allows
    # extensions outside cache. A later live admission still uses its own state.
    assert not receipt["cache"].get("refresh_blocked") and receipt["deferred_refresh"] == fact
    task, started, _ = r.inst.identity()
    assert prepush.source_token(receipt, task, started) == receipt["snapshot_token"]
    for bad_fact in (None, {}, {**fact, "refresh_budget": {**fact["refresh_budget"], "allowed": 0}},
                     {**fact, "unexpected": True}):
        with pytest.raises(ValueError):
            prepush.source_token(dict(receipt, deferred_refresh=bad_fact), task, started)
    monkeypatch.setattr(r.inst, "priority_read", lambda *args: pytest.fail("legacy owner gate changed"))
    r.live["cache"].update(fact)
    with pytest.raises(ValueError, match="cache is unavailable"):
        r.inst.bind(raw, r.select, r.pr)
    r.live["cache"].update(refresh_blocked=None, refresh_budget=None)
    r.inst.bind(raw, r.select, r.pr)
    assert len(r.calls) == 1 and launcher.journal_report(r.inst.journal)["full_scan_claims"] == 0


@pytest.mark.parametrize("fault", [None, "unobserved", "wrong-attempt", "rolled-back", "old-observation",
                                   "changed-head", "failed-observation"])
def test_bind_later_election_only_with_successful_same_task_observation(runtime, case, capsysbinary, monkeypatch, fault):
    r = runtime
    r.responses.append((0, r.source))
    assert launcher.run_initial(guard, r.inst, r.args) == 0
    raw = capsysbinary.readouterr().out
    receipt = r.inst.root / "initial.stdout"
    receipt.write_bytes(raw)
    identity = r.inst.identity()
    original = r.source["snapshot_token"]
    observed = dict(original, revision=3, generated_at=case.now - 10)
    r.live["cache"].update(token=observed, generated_at=observed["generated_at"])

    def read(*args, **kwargs):
        return post.observe(*args, **kwargs, read=lambda seconds: r.live, now=lambda: case.now)

    if fault != "unobserved":
        ready = json.loads(case.read_args.budget_receipt.read_bytes())
        ready["series_id"] = r.inst.series
        late.observe_once(r.inst, receipt, 30, budget=lambda: ready, observe=read)
        records = prepush.passive_records(r.inst.root, *identity[:2])
        witness = records / "observe.result.json"
        data = json.loads(witness.read_bytes())
        if fault == "wrong-attempt":
            data["binding"]["started"] = "another-attempt"
        elif fault == "failed-observation":
            data["fresh_snapshot"] = None
        witness.write_bytes(prepush.encode(data))
    later = dict(original, revision=4, generated_at=case.now - 1)
    if fault == "rolled-back":
        later["epoch"] -= 1
    if fault == "old-observation":
        monkeypatch.setattr(guard.time, "time", lambda: case.now + 301)
        later["generated_at"] = case.now + 300
        r.live["diagnostics_generated_at"] = later["generated_at"]
    r.live["cache"].update(token=later, generated_at=later["generated_at"])
    selection = dict(r.select, snapshot_token=later)
    if fault == "changed-head":
        selection["head"] = "f" * 40
        r.pr["head"]["sha"] = selection["head"]
    if fault:
        with pytest.raises((ValueError, FileNotFoundError)):
            r.inst.bind(raw, selection, r.pr)
    else:
        r.inst.bind(raw, selection, r.pr)
        with pytest.raises(ValueError, match="can use the issued"):
            r.inst.fallback_reason(raw, selection, r.pr, identity)
        assert launcher.journal_report(r.inst.journal)["full_scan_claims"] == 0


def test_full_fallback_claim_survives_exec_failure_and_restart(runtime, case, monkeypatch):
    r = runtime
    r.responses.extend((miss("snapshot_expired"), miss("checker_expired"), miss("snapshot_missing")))
    exec_calls = []

    def fail_exec(path, args, env):
        exec_calls.append((path, args))
        raise OSError("process could not start")

    monkeypatch.setattr(os, "execve", fail_exec)
    with pytest.raises(OSError, match="could not start"):
        launcher.run_initial(guard, r.inst, r.args)
    debit = json.loads((r.inst.journal / "occurrence-2/active.intent.json").read_bytes())
    assert debit["reason"] == "checker_expired"
    assert debit["probes"] == [{"phase": "before_wait", "reason": "snapshot_expired"},
                               {"phase": "after_wait", "reason": "checker_expired"}]
    # The scheduler can restart the same task with a new execution binding.
    task, started, _ = r.inst.identity()
    replacement = (datetime.fromisoformat(started) + timedelta(seconds=10)).isoformat()
    case.c.execute("UPDATE tasks SET started_at=?, session_id='new-session' WHERE id=?", (replacement, task))
    key, blob = case.c.execute("SELECT key,value FROM settings WHERE key LIKE 'pipeline_github_budget_r%'").fetchone()
    ledger = json.loads(blob)
    for reservation in ledger["reservations"]:
        if reservation["task_id"] == task:
            reservation.update(task_started_at=replacement, token="new-token")
    case.c.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(ledger), key))
    case.c.commit()
    assert r.inst.identity()[:2] == (task, replacement)
    assert launcher.run_initial(guard, r.inst, r.args) == 3
    assert len(exec_calls) == 1 and len(r.calls) == 3
    assert launcher.journal_report(r.inst.journal)["full_scan_reasons"] == {"checker_expired": 1}


@pytest.mark.parametrize("response,code", [((2, {"state": "refused"}), 2),
                                           ((3, {"state": "wait", "reason": "not cache unavailable"}), 3),
                                           (miss("arbitrary value"), None)])
def test_terminal_or_unknown_response_cannot_claim_a_scan(runtime, response, code):
    runtime.responses.append(response)
    if code is None:
        with pytest.raises(ValueError, match="unknown cache decision"):
            launcher.run_initial(guard, runtime.inst, runtime.args)
    else:
        assert launcher.run_initial(guard, runtime.inst, runtime.args) == code
    assert len(runtime.calls) == 1
    assert launcher.journal_report(runtime.inst.journal)["observed_tasks"] == 0


def test_expired_wait_preserves_active_fallback(runtime, monkeypatch):
    runtime.responses.extend((miss(), miss()))
    monkeypatch.setattr(launcher, "wait_for_slot", lambda db: False)
    assert launcher.run_initial(guard, runtime.inst, runtime.args) == 75
    assert not list(runtime.inst.journal.glob("*/active.intent.json"))


def test_reports_legacy_claims_without_inventing_a_cause(tmp_path):
    witness = dict.fromkeys(("source_sha256", "binding", "value_sha256", "native"), "f" * 64)
    for item in (1, 2):
        assert prepush.unique(tmp_path / (str(item) + "-" + "a" * 24) / "source.json", witness)
    for item in (2, 3):
        assert prepush.unique(tmp_path / f"occurrence-{item}/active.intent.json", {"task": item, "started": "old"})
    assert prepush.unique(tmp_path / "occurrence-4/active.intent.json", {})
    assert launcher.journal_report(tmp_path) == {
        "observed_tasks": 3, "initial_cache_hits": 2, "full_scan_claims": 2, "full_scan_after_hit": 1,
        "full_scan_reasons": {"legacy_unclassified": 2}, "unreadable_records": 1}
