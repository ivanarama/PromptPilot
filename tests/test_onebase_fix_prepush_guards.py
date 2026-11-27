import json
from pathlib import Path

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
