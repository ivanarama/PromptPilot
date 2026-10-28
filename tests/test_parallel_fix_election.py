"""Opt-in exact FIX election, delivery and task ownership fences."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
import json
from threading import Barrier

import pytest

from promptpilot import fix_handoff
from promptpilot import project_pipeline as pipelinectl
from promptpilot.models import TaskCreate


HEAD_A = "a" * 40
HEAD_B = "b" * 40
DIGEST = "c" * 40
CONFIG = {
    "repository": "owner/repo", "parallel_fix_enabled": True,
    "target_reservation_ttl_seconds": 300,
}


def _attempt(db, monkeypatch, name):
    db.create_task(TaskCreate(prompt=name))
    task = db.get_next_runnable()
    monkeypatch.setenv("PP_TASK_ID", str(task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", task.started_at.astimezone(
        timezone.utc).isoformat())
    monkeypatch.setenv("PP_PROVIDER_OWNERSHIP_KIND", "headless")
    return task


def test_fix_election_requires_opt_in_and_versioned_issue():
    issue = {"stage": "fix-issue", "number": 7, "updated_at": "2026-09-29"}
    with pytest.raises(pipelinectl.PipelineError, match="disabled"):
        pipelinectl._elect_fix_candidate({"repository": "owner/repo"}, [issue])
    with pytest.raises(pipelinectl.PipelineError, match="unversioned"):
        pipelinectl._ordered_fix_candidates([issue])
    assert pipelinectl._fix_candidate_key({
        **issue, "eligibility_digest": DIGEST,
    }) == ("fix-issue", 7, DIGEST)


def test_fix_election_prioritizes_pr_rework_and_preserves_issue_order(
        isolated_db, monkeypatch):
    candidates = [
        {"stage": "fix-issue", "number": 3, "eligibility_digest": DIGEST},
        {"stage": "review", "number": 10, "head": HEAD_A},
        {"stage": "review", "number": 12, "head": HEAD_B},
    ]
    _attempt(isolated_db, monkeypatch, "first FIX")
    first, reservation = pipelinectl._elect_fix_candidate(CONFIG, candidates)
    assert first["number"] == 10
    assert (reservation["stage"], reservation["number"], reservation["head"]) == (
        "fix-pr", 10, HEAD_A)

    _attempt(isolated_db, monkeypatch, "second FIX")
    second, reservation = pipelinectl._elect_fix_candidate(CONFIG, candidates)
    assert second["number"] == 12
    assert reservation["stage"] == "fix-pr"

    _attempt(isolated_db, monkeypatch, "third FIX")
    third, reservation = pipelinectl._elect_fix_candidate(CONFIG, candidates)
    assert third["number"] == 3
    assert reservation["stage"] == "fix-issue"


def test_fix_election_same_attempt_never_retargets_after_revision_change(
        isolated_db, monkeypatch):
    _attempt(isolated_db, monkeypatch, "FIX")
    first = {"stage": "review", "number": 10, "head": HEAD_A}
    _, reservation = pipelinectl._elect_fix_candidate(CONFIG, [first])
    repeated, renewed = pipelinectl._elect_fix_candidate(CONFIG, [first])
    assert repeated == first
    assert renewed["token"] == reservation["token"]
    with pytest.raises(pipelinectl.PipelineError, match="no longer eligible"):
        pipelinectl._elect_fix_candidate(CONFIG, [
            {"stage": "review", "number": 10, "head": HEAD_B},
            {"stage": "fix-issue", "number": 11,
             "eligibility_digest": DIGEST},
        ])


def test_fix_election_concurrent_attempts_do_not_duplicate_target(
        isolated_db, monkeypatch):
    tasks = [_attempt(isolated_db, monkeypatch, f"FIX {index}")
             for index in range(2)]
    barrier = Barrier(2)
    candidate = {"stage": "review", "number": 10, "head": HEAD_A}

    def elect(task):
        # Environment variables are process-global; exercise the concurrent
        # reservation boundary directly with each exact task attempt.
        barrier.wait()
        return isolated_db.reserve_pipeline_target(
            "owner/repo", "fix-pr", 10, HEAD_A, task.id, 300,
            task_started_at=task.started_at.astimezone(timezone.utc).isoformat(),
            ownership_kind="headless",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(elect, tasks))
    assert len([result for result in results if result is not None]) == 1
    assert pipelinectl._fix_candidate_key(candidate) == ("fix-pr", 10, HEAD_A)


def _health(*candidates):
    return {"state": "green", "findings": [],
            "fix_candidates": list(candidates)}


def _setup_handoff(isolated_db, monkeypatch, tmp_path, candidates):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "2")
    monkeypatch.chdir(tmp_path)
    consumer = tmp_path / ".claude/skills/fix-approved/SKILL.md"
    consumer.parent.mkdir(parents=True)
    consumer.write_text(fix_handoff.CONSUMER_MARKER + "\n", encoding="utf-8")
    _attempt(isolated_db, monkeypatch, "parallel FIX")
    health = _health(*candidates)
    monkeypatch.setattr(pipelinectl, "run_health", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(pipelinectl, "ensure_identity", lambda *_args: None)
    return health


def test_fix_handoff_is_signed_and_gate_keeps_exact_target(
        isolated_db, monkeypatch, tmp_path):
    candidate = {"stage": "review", "number": 10, "head": HEAD_A}
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [candidate])
    preflight = pipelinectl.next_fix(None, dict(CONFIG))
    assert preflight["action"] == "fallback"
    assert preflight["target"] == {
        "stage": "fix-pr", "number": 10, "head": HEAD_A,
    }
    assert fix_handoff.validate(preflight)["target"] == preflight["target"]
    gated = fix_handoff.gate(None, dict(CONFIG), preflight["handoff"]["lease"])
    assert gated["action"] == "validated"
    assert gated["mutation_authorized"] is False


def test_fix_handoff_rejects_tampering_and_changed_health(
        isolated_db, monkeypatch, tmp_path):
    candidate = {"stage": "review", "number": 10, "head": HEAD_A}
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [candidate])
    preflight = pipelinectl.next_fix(None, dict(CONFIG))
    lease = preflight["handoff"]["lease"]
    with pytest.raises(pipelinectl.PipelineError, match="signature"):
        fix_handoff.gate(None, dict(CONFIG), lease[:-1] + ("A" if lease[-1] != "A" else "B"))
    monkeypatch.setattr(pipelinectl, "run_health", lambda *_args, **_kwargs: _health(
        {**candidate, "head": HEAD_B}))
    with pytest.raises(pipelinectl.PipelineError, match="no longer in the executable queue"):
        fix_handoff.gate(None, dict(CONFIG), lease)


def test_fix_handoff_rejects_new_unreserved_priority_and_config_change(
        isolated_db, monkeypatch, tmp_path):
    candidate = {"stage": "review", "number": 10, "head": HEAD_A}
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [candidate])
    preflight = pipelinectl.next_fix(None, dict(CONFIG))
    lease = preflight["handoff"]["lease"]
    with pytest.raises(pipelinectl.PipelineError, match="configuration changed"):
        fix_handoff.gate(None, {**CONFIG, "parallel_fix_enabled": False}, lease)
    monkeypatch.setattr(pipelinectl, "run_health", lambda *_args, **_kwargs: _health(
        {"stage": "review", "number": 9, "head": HEAD_B}, candidate))
    with pytest.raises(pipelinectl.PipelineError, match="higher-priority"):
        fix_handoff.gate(None, dict(CONFIG), lease)


def test_fix_next_requires_multiple_replicas_before_reserving(
        isolated_db, monkeypatch, tmp_path):
    candidate = {"stage": "review", "number": 10, "head": HEAD_A}
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [candidate])
    monkeypatch.setenv("PP_PIPELINE_REPLICAS", "1")
    with pytest.raises(pipelinectl.PipelineError, match="at least two replicas"):
        pipelinectl.next_fix(None, dict(CONFIG))
    assert isolated_db.list_pipeline_target_reservations(repository="owner/repo") == []


def _cli(monkeypatch, capsys, tmp_path, *args):
    config = tmp_path / "pipelinectl.json"
    config.write_text(json.dumps({
        **CONFIG, "trusted_account": "owner", "health_command": ["unused"],
    }), encoding="utf-8")
    code = pipelinectl.run(["--config", str(config), *args])
    output = capsys.readouterr()
    return code, json.loads(output.out)


def test_public_issue_handoff_admission_and_own_delivery_finalization(
        isolated_db, monkeypatch, tmp_path, capsys):
    """Creating the PR removes the issue from FIX; cleanup must retain ownership."""
    candidate = {"stage": "fix-issue", "number": 7, "eligibility_digest": DIGEST}
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [candidate])
    code, preflight = _cli(monkeypatch, capsys, tmp_path, "next", "fix")
    assert code == 0
    assert preflight["target"] == {"stage": "fix-issue", "number": 7, "head": DIGEST}
    lease_file = tmp_path / "election.lease"
    lease_file.write_text(preflight["handoff"]["lease"], encoding="ascii")
    code, admitted = _cli(monkeypatch, capsys, tmp_path, "gate-fallback", "fix",
                           "--lease-file", str(lease_file))
    assert code == 0 and admitted["phase"] == "admitted"
    assert admitted["mutation_authorized"] is False

    # The project procedure alone authorizes the PR and its subsequent labels.
    # Here the fake checker only models its resulting queue membership.
    monkeypatch.setattr(pipelinectl, "run_health", lambda *_a, **_k: _health())
    progress_file = tmp_path / "progress.lease"
    progress_file.write_text(admitted["ownership_lease"], encoding="ascii")
    code, progress = _cli(monkeypatch, capsys, tmp_path, "gate-fix-owner",
                           "--lease-file", str(progress_file))
    assert code == 0 and progress["phase"] == "ownership"
    assert progress["target"] == preflight["target"]
    assert progress["mutation_authorized"] is False

    # Neither direction can confuse scheduling admission with continuation.
    for args in (("gate-fix-owner", "--lease-file", str(lease_file)),
                 ("gate-fallback", "fix", "--lease-file", str(progress_file))):
        code, refused = _cli(monkeypatch, capsys, tmp_path, *args)
        assert code == 2 and refused["action"] == "error"

    _attempt(isolated_db, monkeypatch, "different attempt")
    code, refused = _cli(monkeypatch, capsys, tmp_path, "gate-fix-owner",
                          "--lease-file", str(progress_file))
    assert code == 2 and "another task" in refused["error"]


def test_public_fix_requires_paired_consumer_before_reservation(
        isolated_db, monkeypatch, tmp_path, capsys):
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [
        {"stage": "review", "number": 10, "head": HEAD_A}])
    (tmp_path / ".claude/skills/fix-approved/SKILL.md").write_text(
        "Legacy unqualified FIX election\n", encoding="utf-8")
    code, value = _cli(monkeypatch, capsys, tmp_path, "next", "fix")
    assert code == 2 and "consumer protocol" in value["error"]
    assert isolated_db.list_pipeline_target_reservations(repository="owner/repo") == []


@pytest.mark.parametrize("change", ["consumer", "red", "lost", "attempt", "replicas", "single"])
def test_public_progress_gate_fails_closed_after_live_change(
        isolated_db, monkeypatch, tmp_path, capsys, change):
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [
        {"stage": "review", "number": 10, "head": HEAD_A}])
    _, preflight = _cli(monkeypatch, capsys, tmp_path, "next", "fix")
    _, admitted = _cli(monkeypatch, capsys, tmp_path, "gate-fallback", "fix",
                         "--lease", preflight["handoff"]["lease"])
    if change == "consumer":
        with (tmp_path / ".claude/skills/fix-approved/SKILL.md").open(
                "a", encoding="utf-8") as handle:
            handle.write("changed contract\n")
    elif change == "red":
        monkeypatch.setattr(pipelinectl, "run_health", lambda *_a, **_k: {
            **_health(), "state": "red"})
    elif change == "lost":
        with isolated_db._connect() as connection:
            connection.execute("DELETE FROM pipeline_target_reservations")
    elif change == "attempt":
        monkeypatch.setenv("PP_TASK_STARTED_AT", "2026-01-01T00:00:00+00:00")
    else:
        monkeypatch.setenv("PP_PIPELINE_REPLICAS", "1" if change == "single" else "3")
    code, value = _cli(monkeypatch, capsys, tmp_path, "gate-fix-owner",
                       "--lease", admitted["ownership_lease"])
    assert code == 2 and value["action"] == "error"


def test_health_checkout_is_pinned_across_worktree_changes(
        isolated_db, monkeypatch, tmp_path):
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [
        {"stage": "review", "number": 10, "head": HEAD_A}])
    preflight = pipelinectl.next_fix(None, dict(CONFIG))
    selected = fix_handoff.validate(preflight)
    other = tmp_path / "target-worktree"
    other.mkdir()
    monkeypatch.chdir(other)
    calls = []

    def health(_config, **kwargs):
        calls.append(kwargs["working_dir"])
        return _health({"stage": "review", "number": 10, "head": HEAD_A})

    monkeypatch.setattr(pipelinectl, "run_health", health)
    assert fix_handoff.gate(None, dict(CONFIG), preflight["handoff"]["lease"])[
        "action"] == "validated"
    assert calls == [selected["health_checkout"]] == [str(tmp_path.resolve())]


def test_fix_reuses_negative_admission_exclusions(
        isolated_db, monkeypatch, tmp_path):
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [
        {"stage": "review", "number": 9, "head": HEAD_B},
        {"stage": "review", "number": 10, "head": HEAD_A}])
    monkeypatch.setenv("PP_PIPELINE_EXCLUDED_NUMBERS", "[9]")
    preflight = pipelinectl.next_fix(None, dict(CONFIG))
    assert preflight["target"]["number"] == 10
    monkeypatch.delenv("PP_PIPELINE_EXCLUDED_NUMBERS")
    assert fix_handoff.gate(None, dict(CONFIG), preflight["handoff"]["lease"])[
        "action"] == "validated"


@pytest.mark.parametrize("purpose", ["admission", "ownership"])
@pytest.mark.parametrize("slow_operation", ["actor", "renewal"])
def test_public_gate_rechecks_expiry_after_slow_operations(
        isolated_db, monkeypatch, tmp_path, capsys, purpose, slow_operation):
    original_identity = pipelinectl.ensure_identity
    _setup_handoff(isolated_db, monkeypatch, tmp_path, [
        {"stage": "review", "number": 10, "head": HEAD_A}])
    _, preflight = _cli(monkeypatch, capsys, tmp_path, "next", "fix")
    _, admitted = _cli(monkeypatch, capsys, tmp_path, "gate-fallback", "fix",
                         "--lease", preflight["handoff"]["lease"])
    token = (preflight["handoff"]["lease"] if purpose == "admission"
             else admitted["ownership_lease"])
    clock = [pipelinectl.decode_signed_lease(token)["expires_at"] - 1]
    monkeypatch.setattr(fix_handoff.time, "time", lambda: clock[0])
    if slow_operation == "actor":
        class SlowGitHub:
            def json(self, *args):
                assert args == ("api", "user")
                clock[0] += 2
                return {"login": "owner"}

        monkeypatch.setattr(pipelinectl, "GitHub", SlowGitHub)
        monkeypatch.setattr(pipelinectl, "ensure_identity", original_identity)
    else:
        renew = pipelinectl.renew_lease_target_reservation

        def slow_renew(*args):
            result = renew(*args)
            clock[0] += 2
            return result

        monkeypatch.setattr(pipelinectl, "renew_lease_target_reservation", slow_renew)
    command = (["gate-fallback", "fix"] if purpose == "admission"
               else ["gate-fix-owner"])
    code, value = _cli(monkeypatch, capsys, tmp_path, *command, "--lease", token)
    assert code == 2 and value["action"] == "error"
    assert "expired" in value["error"]
    assert "ownership_lease" not in value
