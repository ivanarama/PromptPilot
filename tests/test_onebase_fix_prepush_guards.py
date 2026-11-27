import json
from pathlib import Path
import sqlite3

import pytest

from tools import onebase_fix_prepush as runner

pytest_plugins = ("test_onebase_fix_prepush",)


def load(path):
    return Path(path).read_text(encoding="utf-8")


def update(path, **values):
    Path(path).write_text(json.dumps({**json.loads(load(path)), **values}), encoding="utf-8")


def prepare(case):
    runner.record(case.root, case.record_args, 1, case.old_start)
    case.finish()
    return 2, case.new_start


def test_prior_push_claim_survives_a_new_review_epoch_on_same_remote_head(case):
    attempt = prepare(case)
    candidate = runner.read(case.root, case.read_args, *attempt)
    runner.claim(case.root, case.claim_args(Path(candidate["ticket"])), *attempt)

    prior = json.loads(load(case.read_args.selection))
    epoch = "a" * 64 if prior["epoch"] != "a" * 64 else "b" * 64
    update(case.read_args.selection, epoch=epoch)
    next_action = runner.read(case.root, case.read_args, *attempt)
    assert next_action["state"] == "reconcile"
    assert next_action["publication_authorized"] is False


def test_unequal_owner_decision_never_restores_an_unclaimed_candidate(case):
    attempt = prepare(case)
    original = json.loads(load(case.read_args.selection))
    next_digest = "1" * 64 if original["decision_sha256"] != "1" * 64 else "2" * 64
    update(case.read_args.selection, decision_sha256=next_digest)
    result = runner.read(case.root, case.read_args, *attempt)
    assert result["state"] == "missing"
    assert result["acceptance_reuse_authorized"] is False


def test_base_change_is_reported_without_granting_test_credit(case):
    attempt = prepare(case)
    pr = json.loads(load(case.read_args.pr_read))
    base = pr["base"]["sha"]
    newer = "a" * 40 if base != "a" * 40 else "b" * 40
    update(case.read_args.pr_read, base={**pr["base"], "sha": newer})
    result = runner.read(case.root, case.read_args, *attempt)
    assert result["pr_base_changed"] is True
    assert result["previous_pr_base"] == base
    assert result["current_pr_base"] == newer
    assert result["acceptance_reuse_authorized"] is False
    assert result["publication_authorized"] is False


@pytest.mark.parametrize("receipt,updates", [
    ("source_receipt", {"source_refreshed": False}),
    ("source_receipt", {"queue_id": "merge"}),
    ("budget_receipt", {"ready": False}),
    ("budget_receipt", {"rate_observed_at": 1}),
    ("budget_receipt", {"started_at": "different-worker-attempt"}),
])
def test_refused_changed_or_stale_current_receipt_cannot_issue_a_ticket(case, receipt, updates):
    attempt = prepare(case)
    update(getattr(case.read_args, receipt), **updates)
    with pytest.raises(ValueError):
        runner.read(case.root, case.read_args, *attempt)
    assert not list(case.root.rglob("push-attempt.json"))


def test_changed_remote_branch_or_original_bundle_cannot_resume(case):
    attempt = prepare(case)
    original_pr = load(case.read_args.pr_read)
    pr = json.loads(original_pr)
    update(case.read_args.pr_read, head={**pr["head"], "ref": pr["head"]["ref"] + "-renamed"})
    with pytest.raises(ValueError):
        runner.read(case.root, case.read_args, *attempt)
    Path(case.read_args.pr_read).write_text(original_pr, encoding="utf-8")

    with Path(case.bundle).open("ab") as f:
        f.write(b"changed after the original acceptance")
    with pytest.raises(ValueError):
        runner.read(case.root, case.read_args, *attempt)
    assert not list(case.root.rglob("push-attempt.json"))


@pytest.mark.parametrize("fault", [None, "head", "session", "prompt", "token", "observation", "election",
                                   "cache-replaced", "current-head"])
def test_reused_initial_source_binds_live_election_and_native_attempt(case, fault, monkeypatch):
    from promptpilot.task_identity import task_execution_fingerprint

    task, started = prepare(case)
    case.c.row_factory = sqlite3.Row
    row = case.c.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    binding = [task, started, row["session_id"], None, str(task), task_execution_fingerprint(row),
               row["series_id"], row["priority"]]
    source = case.source_value(task, started, case.now - 100)
    source.update(state="reused", source_refreshed=False, source_reused=True,
                  existing_execution_reservation_reused=True, mutable_admission_granted=False,
                  observed_at=case.now - 25, attempt_binding_sha256=runner.digest(runner.encode(binding)),
                  fix_pr_heads={str(case.pr): case.source})
    source["cache"]["source"] = "durable"
    live = {"profile_id": "onebase", "repository": runner.REPO, "cache": dict(source["cache"]),
            "diagnostics": {"exit_code": 0, "fix_candidates": [dict(stage="review", number=case.pr, head=case.source)]},
            "queues": [dict(id="fix", membership_complete=True, admission_items=[
                dict(kind="pr", number=case.pr, key=f"pr:{case.pr}", labels=["changes-requested"],
                     url=f"https://github.com/{runner.REPO}/pull/{case.pr}")])]}
    monkeypatch.setattr(runner, "read_cached_fix_queue", lambda: live)
    update(case.read_args.selection, task_id=task, task_started_at=started, snapshot_token=source["snapshot_token"])
    if fault == "head":
        source["fix_pr_heads"][str(case.pr)] = "c" * 40
    elif fault in {"session", "prompt"}:
        col = {"session": "session_id", "prompt": "prompt"}[fault]
        case.c.execute(f"UPDATE tasks SET {col}='other' WHERE id=?", (task,))
        case.c.commit()
    elif fault == "token":
        source["attempt_binding_sha256"] = "c" * 64
    elif fault == "observation":
        source["observed_at"] = case.now - 65
    elif fault == "election":
        update(case.read_args.selection, snapshot_token={})
    elif fault == "cache-replaced":
        live["cache"]["token"] = dict(source["snapshot_token"], revision=4)
    elif fault == "current-head":
        live["diagnostics"]["fix_candidates"][0]["head"] = "e" * 40
    update(case.read_args.source_receipt, **source)
    if fault:
        with pytest.raises(ValueError):
            runner.read(case.root, case.read_args, task, started)
    else:
        ticket = runner.read(case.root, case.read_args, task, started)
        assert ticket["state"] == "candidate" and ticket["publication_authorized"] is False
