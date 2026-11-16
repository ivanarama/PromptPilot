"""A trusted explicit request opens one new epoch, never resets review history."""
import copy
import json

import pytest

from promptpilot import project_pipeline as pp
from tests.test_project_pipeline import HEAD, edge, snapshot, trusted_comment


def append_proof(value, first_id):
    info = pp.epoch(value, "owner")
    head = value["headRefOid"]
    for offset, body in enumerate((
        f"Reviewed-SHA: {head}\nOutcome-Label: reviewed\n<!-- pp:review pp:tail=0 -->",
        f"<!-- pp:review-claim {head} review-comment={first_id} epoch-sha256={info['hash']} -->",
        f"<!-- pp:head-reviewed {head} review-comment={first_id} claim={first_id + 1} epoch-sha256={info['hash']} -->",
    )):
        value["edges"].append(trusted_comment(str(first_id + offset), first_id + offset, body))


def history():
    value = snapshot()
    old_head = "d" * 40
    value["headRefOid"] = old_head
    value["edges"][0]["node"]["commit"]["oid"] = old_head
    append_proof(value, 100)
    value["headRefOid"] = HEAD
    value["edges"].append(edge("current", "PullRequestCommit", id="current", commit={"oid": HEAD}))
    append_proof(value, 200)
    value["edges"].append(trusted_comment("override", 220, "pp:review-again"))
    value["labels"] = ["reviewed"]
    return value


def setup_review(tmp_path, monkeypatch, value):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path / "isolated-data"))
    config = {"repository": "owner/repo", "trusted_account": "owner",
              "base_branch": "main", "review_completion_gate": "target-v1"}
    item = {"number": 42, "head": HEAD, "stage": "review", "review_depth": 2}
    health = {"state": "green", "review_candidates": [item],
              "content_review_candidates": [item], "integration_owner": None}
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(pp, "stable_timeline", lambda *_args: copy.deepcopy(value))
    monkeypatch.setattr(pp, "_elect_review_candidate", lambda *_args: (item, None, health))
    monkeypatch.setattr(pp, "fallback_target", lambda *_args, **_kwargs: {"action": "fallback"})
    monkeypatch.setattr(pp, "ensure_identity", lambda *_args: None)
    return config, item


def test_explicit_current_head_review_produces_and_consumes_signed_grant(tmp_path, monkeypatch):
    value = history()
    config, item = setup_review(tmp_path, monkeypatch, value)
    result = pp.next_review(object(), config)
    assert result["action"] == "audit"
    assert result["target"] == item
    lease = pp.decode_signed_lease(result["lease"])
    assert lease["depth"] == 2
    assert lease["explicit_full_current_review"]["anchor"] == "node-220"
    pp.validate_review_lease(lease, config)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"change": "full current audit", "checks": ["actual tests"],
                                  "blocking": [], "tail": []}), encoding="utf-8")
    next_ids = iter((300, 301, 302))

    def post(_gh, _config, _number, body):
        number = next(next_ids)
        value["edges"].append(trusted_comment(str(number), number, body))
        return {"id": number}

    monkeypatch.setattr(pp, "post_comment", post)
    monkeypatch.setattr(pp, "add_label", lambda *_args: None)
    completed = pp.complete_review(object(), config, result["lease"], str(report))
    assert completed["action"] == "completed"
    assert pp.committed_review_depth(value, "owner") == 3
    assert "(круг 3)" in value["edges"][-3]["node"]["body"]
    # The same request cannot produce another audit after its completion.
    assert pp.next_review(object(), config)["action"] != "audit"


@pytest.mark.parametrize("labels", [["needs-decision"], ["changes-requested"]])
def test_exact_explicit_grant_unparks_prior_outcome_only(tmp_path, monkeypatch, labels):
    value = history()
    value["labels"] = labels
    prior = next(e["node"] for e in value["edges"] if e["node"].get("id") == "node-200")
    prior["body"] = prior["body"].replace("Outcome-Label: reviewed", f"Outcome-Label: {labels[0]}")
    config, _ = setup_review(tmp_path, monkeypatch, value)
    assert pp.next_review(object(), config)["action"] == "audit"


@pytest.mark.parametrize("fault", ["missing", "quoted", "fenced", "inline", "foreign", "edited",
                                  "before-proof", "wrong-head", "missing-claim", "missing-review",
                                  "edited-review", "edited-claim", "wrong-epoch", "hold", "manual", "draft"])
def test_explicit_grant_never_uses_invalid_request_or_source(tmp_path, monkeypatch, fault):
    value = history()
    override = value["edges"][-1]["node"]
    if fault == "missing":
        value["edges"].pop()
    elif fault in {"quoted", "fenced", "inline"}:
        override["body"] = {"quoted": "> pp:review-again", "fenced": "```\npp:review-again\n```",
                            "inline": "Please `pp:review-again`."}[fault]
    elif fault == "foreign":
        override["author"]["login"] = "contributor"
    elif fault == "edited":
        override["lastEditedAt"] = "2026-01-02T00:00:00Z"
    elif fault == "before-proof":
        value["edges"].insert(-4, value["edges"].pop())
    elif fault == "wrong-head":
        value["headRefOid"] = "f" * 40
    elif fault in {"missing-claim", "missing-review"}:
        remove = "node-201" if fault == "missing-claim" else "node-200"
        value["edges"] = [e for e in value["edges"] if e["node"].get("id") != remove]
    elif fault in {"edited-review", "edited-claim", "wrong-epoch"}:
        node = next(e["node"] for e in value["edges"] if e["node"].get("id") ==
                    ("node-200" if fault == "edited-review" else "node-201"))
        if fault == "wrong-epoch":
            node["body"] = node["body"].replace("epoch-sha256=", "epoch-sha256=0")
        else:
            node["lastEditedAt"] = "2026-01-02T00:00:00Z"
    elif fault in {"hold", "manual"}:
        value["labels"].append(fault)
    elif fault == "draft":
        value["isDraft"] = True
    config, _ = setup_review(tmp_path, monkeypatch, value)
    try:
        result = pp.next_review(object(), config)
    except pp.PipelineError:
        return
    assert result["action"] != "audit"


@pytest.mark.parametrize("fault", ["anchor", "head", "epoch", "depth", "prior_proof", "unsigned", "missing"])
def test_explicit_grant_cannot_be_rebound_or_unsigned(tmp_path, monkeypatch, fault):
    value = history()
    config, _ = setup_review(tmp_path, monkeypatch, value)
    result = pp.next_review(object(), config)
    assert result["action"] == "audit"
    lease = pp.decode_signed_lease(result["lease"])
    if fault == "unsigned":
        lease["completion_gate"] = "health"
    elif fault == "missing":
        lease.pop("explicit_full_current_review")
    else:
        grant = lease["explicit_full_current_review"]
        grant[fault] = 1 if fault == "depth" else "invalid"
    with pytest.raises(pp.PipelineError):
        pp.validate_review_lease(lease, config)
        pp.content_review_target_gate(value, config, lease)


def test_ordinary_third_review_remains_rejected(tmp_path, monkeypatch):
    value = history()
    value["edges"].pop()
    config, _ = setup_review(tmp_path, monkeypatch, value)
    assert pp.next_review(object(), config)["action"] != "audit"


@pytest.mark.parametrize("fault", ["new-override", "edited-override", "prior-proof-changed",
                                  "new-completion", "head", "hold", "manual", "draft"])
def test_consumer_rechecks_actual_grant_before_any_mutation(tmp_path, monkeypatch, fault):
    value = history()
    config, _ = setup_review(tmp_path, monkeypatch, value)
    result = pp.next_review(object(), config)
    assert result["action"] == "audit"
    if fault == "new-override":
        value["edges"].append(trusted_comment("new", 230, "pp:review-again"))
    elif fault == "edited-override":
        value["edges"][-1]["node"]["lastEditedAt"] = "2026-01-02T00:00:00Z"
    elif fault == "prior-proof-changed":
        value["edges"][-4]["node"]["body"] = "unrelated"
    elif fault == "new-completion":
        append_proof(value, 300)
    elif fault == "head":
        value["headRefOid"] = "f" * 40
    elif fault in {"hold", "manual"}:
        value["labels"].append(fault)
    elif fault == "draft":
        value["isDraft"] = True
    # Rebinding the snapshot alone cannot authorize a changed grant.
    lease = pp.decode_signed_lease(result["lease"])
    lease["snapshot"] = pp.content_review_digest(value)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"change": "full audit", "checks": ["test"],
                                  "blocking": [], "tail": []}), encoding="utf-8")
    monkeypatch.setattr(pp, "post_comment", lambda *_args: pytest.fail("mutation escaped changed grant"))
    with pytest.raises(pp.PipelineError):
        pp.complete_review(object(), config, pp.encode_signed_lease(lease), str(report))


def test_explicit_grant_is_covered_by_lease_signature(tmp_path, monkeypatch):
    config, _ = setup_review(tmp_path, monkeypatch, history())
    result = pp.next_review(object(), config)
    payload, signature = result["lease"].split(".")
    lease = pp.decode_lease(payload)
    lease["explicit_full_current_review"]["anchor"] = "another-anchor"
    with pytest.raises(pp.PipelineError, match="signature"):
        pp.decode_signed_lease(f"{pp.encode_lease(lease)}.{signature}")


def test_explicit_grant_retains_signed_lease_expiry(tmp_path, monkeypatch):
    config, _ = setup_review(tmp_path, monkeypatch, history())
    lease = pp.decode_signed_lease(pp.next_review(object(), config)["lease"])
    monkeypatch.setattr(pp.time, "time", lambda: lease["expires_at"])
    with pytest.raises(pp.PipelineError, match="expired"):
        pp.validate_review_lease(lease, config)
