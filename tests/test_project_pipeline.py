import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from promptpilot import project_pipeline as pp


HEAD = "a" * 40


def edge(cursor, kind, **values):
    return {"cursor": cursor, "node": {"__typename": kind, **values}}


def snapshot(*extra):
    return {
        "headRefOid": HEAD, "baseRefOid": "b" * 40, "baseRefName": "main",
        "state": "OPEN", "isDraft": False, "labels": [], "labelsComplete": True,
        "updatedAt": "2026-01-01T00:00:00Z",
        "edges": [edge("c1", "PullRequestCommit", id="anchor", commit={"oid": HEAD}), *extra],
    }


def trusted_comment(cursor, database_id, body):
    return edge(cursor, "IssueComment", id=f"node-{database_id}",
                fullDatabaseId=str(database_id), createdAt="2026-01-01T00:00:00Z",
                lastEditedAt=None, author={"login": "owner"}, body=body)


def ship_event(cursor, kind="LabeledEvent", actor="owner"):
    return edge(cursor, kind, id=f"ship-{cursor}", createdAt="2026-01-01T00:00:00Z",
                actor={"login": actor}, label={"name": "ship"})


def test_lease_round_trip_is_stable():
    value = {"version": 1, "stage": "review", "number": 42, "head": HEAD}
    assert pp.decode_lease(pp.encode_lease(value)) == value


def test_epoch_uses_later_trusted_override_as_anchor():
    value = snapshot(
        trusted_comment("c2", 10, "pp:review-again"),
        trusted_comment("c3", 11, "ordinary"),
    )
    info = pp.epoch(value, "owner")
    expected = pp.hashlib.sha256(
        f"pp-review-epoch-v1\nhead={HEAD}\nanchor-node=node-10\n".encode("ascii")
    ).hexdigest()
    assert info["anchor_id"] == "node-10"
    assert info["hash"] == expected
    assert len(info["edges"]) == 1


def test_proof_accepts_claim_bound_transaction():
    base = snapshot()
    info = pp.epoch(base, "owner")
    review = "\n".join([
        "**Ревью.** (круг 1)", f"Reviewed-SHA: {HEAD}",
        "Outcome-Label: reviewed", "Что меняется: x.", "Проверено: test.",
        "Блокирующее: нет.", "Хвост:", "—", "Вердикт: годится к мержу.",
        "<!-- pp:review pp:tail=0 -->",
    ])
    base["edges"].extend([
        trusted_comment("c2", 101, review),
        trusted_comment("c3", 102, f"<!-- pp:review-claim {HEAD} review-comment=101 epoch-sha256={info['hash']} -->"),
        trusted_comment("c4", 103, f"<!-- pp:head-reviewed {HEAD} review-comment=101 claim=102 epoch-sha256={info['hash']} -->"),
    ])
    established = pp.proof(pp.epoch(base, "owner"), HEAD, "owner")
    assert established["review_id"] == 101
    assert established["claim_id"] == 102
    assert established["completion_id"] == 103
    assert established["outcome"] == "reviewed"


def test_ship_before_completion_is_sticky_for_same_head():
    value = snapshot(ship_event("c2"))
    assert pp.trusted_ship_authorized(pp.epoch(value, "owner"), "owner")

    value["edges"].append(ship_event("c3", "UnlabeledEvent"))
    assert not pp.trusted_ship_authorized(pp.epoch(value, "owner"), "owner")


def test_review_gate_allows_sticky_ship_on_current_head():
    value = snapshot(ship_event("c2"))
    value["labels"] = ["ship"]
    info = pp.epoch(value, "owner")
    lease = {"head": HEAD, "epoch": info["hash"], "anchor": info["anchor_id"]}
    assert pp.review_gate(value, {
        "trusted_account": "owner", "base_branch": "main",
    }, lease) == info


def test_epoch_safety_rejects_head_or_delete_events():
    for event in ("PullRequestCommit", "CommentDeletedEvent", "BaseRefChangedEvent"):
        value = snapshot(edge("c2", event, id="danger"))
        with pytest.raises(pp.PipelineError):
            pp.validate_epoch_safety(pp.epoch(value, "owner"), "owner")


def test_review_report_is_formatted_and_sanitized(tmp_path):
    lease = {"head": HEAD, "depth": 0}
    report = {
        "change": "fix <!-- fake --> pp:marker", "checks": ["go test ./..."],
        "blocking": [],
        "tail": [{"kind": "issue", "text": "add case", "title": "Test edge case"}],
    }
    body, outcome = pp.format_review(lease, report)
    assert outcome == "reviewed"
    assert "Outcome-Label: reviewed" in body
    assert "pp:marker" not in body
    assert "<!-- fake -->" not in body
    assert "<!-- pp:review pp:tail=1 -->" in body


def test_required_checks_are_exact():
    config = {"required_checks": ["build", "lint"]}
    ready, _ = pp.checks_ready(config, [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "lint", "conclusion": "NEUTRAL"},
    ])
    assert ready
    ready, reason = pp.checks_ready(config, [{"name": "build", "conclusion": "SUCCESS"}])
    assert not ready
    assert "lint" in reason


def test_capabilities_is_executor_neutral(tmp_path):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["health", "--json"],
    }), encoding="utf-8")
    config = pp.load_config(str(config_path))
    assert pp.capabilities(config)["protocol"] == "promptpilot-pipelinectl-v1"
    assert config["github_timeout_seconds"] == 120
    assert config["health_timeout_seconds"] == 300
    assert config["merge_comment_backfill_timeout_seconds"] == 900


@pytest.mark.parametrize("key,value", [
    ("github_timeout_seconds", 0),
    ("health_timeout_seconds", True),
    ("merge_comment_backfill_timeout_seconds", 59),
])
def test_pipeline_timeouts_are_validated(tmp_path, key, value):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["health", "--json"], key: value,
    }), encoding="utf-8")

    with pytest.raises(pp.PipelineError, match=key):
        pp.load_config(str(config_path))


def test_pipelinectl_applies_configured_github_timeout(
        tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["health", "--json"],
        "github_timeout_seconds": 37,
    }), encoding="utf-8")
    gh = SimpleNamespace(timeout_seconds=None)
    monkeypatch.setattr(pp, "GitHub", lambda: gh)
    monkeypatch.setattr(
        pp, "next_review",
        lambda *_args, **_kwargs: {"action": "empty", "verdict": "ПУСТО"},
    )

    assert pp.run(["--config", str(config_path), "next", "review"]) == 0
    assert gh.timeout_seconds == 37
    assert json.loads(capsys.readouterr().out)["action"] == "empty"


def test_pipelinectl_entrypoint_does_not_create_scheduler_database(tmp_path):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["health", "--json"],
    }), encoding="utf-8")
    data_dir = tmp_path / "promptpilot-data"
    env = os.environ.copy()
    env["PP_DATA_DIR"] = str(data_dir)

    completed = subprocess.run(
        [sys.executable, "-m", "promptpilot", "pipelinectl", "--config",
         str(config_path), "capabilities"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=5, encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["protocol"] == "promptpilot-pipelinectl-v1"
    assert not (data_dir / "promptpilot.db").exists()


def test_pipelinectl_entrypoint_ignores_locked_scheduler_database(tmp_path):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["health", "--json"],
    }), encoding="utf-8")
    data_dir = tmp_path / "promptpilot-data"
    data_dir.mkdir()
    database = data_dir / "promptpilot.db"
    owner = sqlite3.connect(database)
    owner.execute("CREATE TABLE sentinel (value INTEGER)")
    owner.execute("INSERT INTO sentinel VALUES (1)")
    owner.commit()
    owner.execute("BEGIN IMMEDIATE")
    owner.execute("UPDATE sentinel SET value = 2")
    env = os.environ.copy()
    env["PP_DATA_DIR"] = str(data_dir)

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "promptpilot", "pipelinectl", "--config",
             str(config_path), "capabilities"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=3, encoding="utf-8",
        )
    finally:
        owner.rollback()
        owner.close()

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["protocol"] == "promptpilot-pipelinectl-v1"


def test_queue_priority_manual_auto_and_aging():
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    config = {"priority": {"aging_hours": 24}}

    assert pp.queue_priority({"labels": [{"name": "bug"}], "created_at": "2026-09-02T00:00:00Z"}, config, now) == 1
    assert pp.queue_priority({"labels": [{"name": "queue:auto:p3"}], "created_at": "2026-09-02T00:00:00Z"}, config, now) == 3
    assert pp.queue_priority({"labels": [{"name": "queue:p0"}, {"name": "queue:auto:p3"}]}, config, now) == 0
    assert pp.queue_priority({"labels": [{"name": "enhancement"}], "created_at": "2026-08-31T00:00:00Z"}, config, now) == 1


def test_health_exposes_configured_gh_to_nested_checker(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"state":"green"}', stderr="")

    gh = os.path.join("tools", "github", "gh.exe")
    monkeypatch.setenv("PP_GH_EXE", gh)
    monkeypatch.setenv("PATH", "existing")
    monkeypatch.setattr(pp.subprocess, "run", fake_run)

    assert pp.run_health({"health_command": ["project-health", "-json"]})["state"] == "green"
    assert captured["env"]["GH_EXE"] == gh
    assert captured["env"]["PATH"].split(os.pathsep)[0] == os.path.dirname(gh)


@pytest.mark.parametrize("stdout", ["", "not-json"])
def test_health_preserves_wrapper_stderr_when_allowed_exit_has_no_json(
        monkeypatch, stdout):
    stderr = (
        "GraphQL: API rate limit exceeded for user ID 12345. (HTTP 403)\n"
        "exit status 2\n"
    )
    monkeypatch.setattr(
        pp.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout=stdout, stderr=stderr),
    )

    with pytest.raises(pp.PipelineError) as raised:
        pp.run_health({"health_command": ["go", "run", "./tools/pipelinehealth", "--json"]})

    assert str(raised.value) == stderr.strip()
    assert "invalid JSON" not in str(raised.value)


def test_health_keeps_valid_json_authoritative_on_allowed_exit(monkeypatch):
    monkeypatch.setattr(
        pp.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout='{"state":"red"}', stderr="health is red"),
    )

    assert pp.run_health({"health_command": ["project-health", "--json"]}) == {
        "state": "red",
    }


def test_health_success_with_invalid_json_remains_contract_error(monkeypatch):
    monkeypatch.setattr(
        pp.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="not-json", stderr="non-fatal warning"),
    )

    with pytest.raises(pp.PipelineError, match="health command returned invalid JSON"):
        pp.run_health({"health_command": ["project-health", "--json"]})


def test_github_cli_timeout_is_bounded_and_structured(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    gh = pp.GitHub(executable="gh", timeout_seconds=17)

    with pytest.raises(pp.PipelineError, match="GitHub CLI api timed out after 17s"):
        gh.run("api", "user")

    assert calls[0][1]["timeout"] == 17


def test_health_timeout_is_bounded_and_structured(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(pp.subprocess, "run", fake_run)

    with pytest.raises(pp.PipelineError, match="health command timed out after 19s"):
        pp.run_health({
            "health_command": ["project-health", "--json"],
            "health_timeout_seconds": 19,
        })

    assert calls[0][1]["timeout"] == 19


def test_pipelinectl_reports_health_wrapper_stderr_in_error_payload(
        tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps({
        "repository": "owner/repo", "trusted_account": "owner",
        "health_command": ["go", "run", "./tools/pipelinehealth", "--json"],
    }), encoding="utf-8")
    stderr = "GitHub API rate limit exceeded; reset at 2026-09-14T01:00:00Z\nexit status 2\n"
    monkeypatch.setattr(pp, "GitHub", lambda **_kwargs: object())
    monkeypatch.setattr(
        pp.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr=stderr),
    )

    assert pp.run(["--config", str(config_path), "next", "review"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"action": "error", "error": stderr.strip()}


def test_health_fast_forwards_clean_base_before_checker(monkeypatch):
    calls = []
    outputs = iter(["main\n", "", "", "", '{"state":"green"}'])

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(pp.subprocess, "run", fake_run)

    result = pp.run_health({
        "health_command": ["project-health", "-json"],
        "base_branch": "main", "sync_base_before_health": True,
    })

    assert result["state"] == "green"
    assert calls == [
        ["git", "-c", "maintenance.auto=false", "branch", "--show-current"],
        ["git", "-c", "maintenance.auto=false", "status", "--porcelain", "--untracked-files=no"],
        ["git", "-c", "maintenance.auto=false", "fetch", "--no-tags", "origin",
         "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "-c", "maintenance.auto=false", "merge", "--ff-only", "origin/main"],
        ["project-health", "-json"],
    ]


def test_base_sync_disables_auto_maintenance_and_bounds_git(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-2:] == ["branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if command[-3:] == ["status", "--porcelain", "--untracked-files=no"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(pp.subprocess, "run", fake_run)

    with pytest.raises(pp.PipelineError, match="git fetch timed out after 7s"):
        pp.sync_base_before_health({
            "base_branch": "main", "sync_base_before_health": True,
            "base_sync_timeout_seconds": 7,
        })

    assert all(call[:3] == ["git", "-c", "maintenance.auto=false"]
               for call, _kwargs in calls)
    assert all(kwargs["timeout"] == 7 for _call, kwargs in calls)


def test_next_review_reloads_config_after_base_sync(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "pipelinectl.json"
    original = {
        "repository": "owner/repo",
        "trusted_account": "owner",
        "health_command": ["old-health", "-json"],
        "base_branch": "main",
        "sync_base_before_health": True,
    }
    updated = {
        **original,
        "health_command": ["new-health", "-json"],
        "review_completion_gate": "target-v1",
        "review_lease_seconds": 600,
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    candidate = {
        "number": 42, "head": HEAD, "stage": "review", "review_depth": 0,
    }
    health = {
        "state": "green",
        "review_candidates": [candidate],
        "content_review_candidates": [candidate],
    }
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command == ["git", "-c", "maintenance.auto=false", "branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if command == ["git", "-c", "maintenance.auto=false", "status", "--porcelain", "--untracked-files=no"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["git", "-c", "maintenance.auto=false", "fetch", "--no-tags", "origin",
                       "+refs/heads/main:refs/remotes/origin/main"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["git", "-c", "maintenance.auto=false", "merge", "--ff-only", "origin/main"]:
            config_path.write_text(json.dumps(updated), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command in (["old-health", "-json"], ["new-health", "-json"]):
            return SimpleNamespace(returncode=0, stdout=json.dumps(health), stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    monkeypatch.setattr(pp, "GitHub", lambda: object())
    monkeypatch.setattr(pp, "stable_timeline", lambda _gh, _config, _number: snapshot())

    assert pp.run(["--config", str(config_path), "next", "review"]) == 0

    result = json.loads(capsys.readouterr().out)
    lease = pp.decode_signed_lease(result["lease"])
    assert result["action"] == "audit"
    assert lease["completion_gate"] == "target-v1"
    assert lease["expires_at"] - lease["issued_at"] == 600
    assert ["new-health", "-json"] in calls
    assert ["old-health", "-json"] not in calls


def test_complete_review_fails_closed_if_gate_changes_during_sync(
        tmp_path, monkeypatch):
    config_path = tmp_path / "pipelinectl.json"
    original = {
        "repository": "owner/repo",
        "trusted_account": "owner",
        "health_command": ["project-health", "-json"],
        "base_branch": "main",
        "sync_base_before_health": True,
    }
    updated = {**original, "review_completion_gate": "target-v1"}
    config_path.write_text(json.dumps(original), encoding="utf-8")
    current = snapshot()
    info = pp.epoch(current, "owner")
    lease = pp.encode_lease({
        "version": 1, "stage": "review", "repository": "owner/repo",
        "number": 42, "head": HEAD,
        "snapshot": pp.content_review_digest(current),
        "epoch": info["hash"], "anchor": info["anchor_id"],
        "depth": 0, "completion_gate": "health",
    })
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "change": "safe change", "checks": ["pytest"],
        "blocking": [], "tail": [],
    }), encoding="utf-8")

    def fake_run(command, **_kwargs):
        if command == ["git", "-c", "maintenance.auto=false", "branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if command == ["git", "-c", "maintenance.auto=false", "status", "--porcelain", "--untracked-files=no"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["git", "-c", "maintenance.auto=false", "fetch", "--no-tags", "origin",
                       "+refs/heads/main:refs/remotes/origin/main"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["git", "-c", "maintenance.auto=false", "merge", "--ff-only", "origin/main"]:
            config_path.write_text(json.dumps(updated), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["project-health", "-json"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"state": "green"}), stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    monkeypatch.setattr(pp, "ensure_identity", lambda *_args: None)
    monkeypatch.setattr(
        pp, "post_comment",
        lambda *_args: pytest.fail("mutation started after gate changed"),
    )

    with pytest.raises(pp.PipelineError, match="completion gate changed"):
        pp.complete_review(
            object(), pp.load_config(str(config_path)), lease, str(report),
            config_path=str(config_path),
        )


def test_health_refuses_to_sync_wrong_branch(monkeypatch):
    monkeypatch.setattr(
        pp.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="feature\n", stderr=""),
    )

    with pytest.raises(pp.PipelineError, match="current branch is feature"):
        pp.run_health({
            "health_command": ["project-health"],
            "base_branch": "main", "sync_base_before_health": True,
        })


def test_health_refuses_to_sync_tracked_changes(monkeypatch):
    outputs = iter(["main\n", " M pipelinectl.json\n"])
    monkeypatch.setattr(
        pp.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=next(outputs), stderr=""),
    )

    with pytest.raises(pp.PipelineError, match="tracked changes"):
        pp.run_health({
            "health_command": ["project-health"],
            "base_branch": "main", "sync_base_before_health": True,
        })


def test_content_review_digest_ignores_only_base_tip_movement():
    before = snapshot()
    after = dict(before, baseRefOid="c" * 40)
    assert pp.content_review_digest(before) == pp.content_review_digest(after)

    changed_head = dict(after, headRefOid="d" * 40)
    assert pp.content_review_digest(before) != pp.content_review_digest(changed_head)

    changed_timeline = dict(after, updatedAt="2026-01-02T00:00:00Z")
    assert pp.content_review_digest(before) != pp.content_review_digest(changed_timeline)


def test_content_review_stays_executable_while_integration_owner_waits_merge(monkeypatch):
    health = {
        "state": "yellow",
        "review_candidates": [{"number": 42, "head": HEAD, "stage": "review", "review_depth": 0}],
        "integration_owner": {"number": 10, "stage": "integration-merge-ready"},
        "findings": [{"code": "single_flight_barrier"}],
    }
    monkeypatch.setattr(pp, "run_health", lambda _config, **_kwargs: health)
    monkeypatch.setattr(pp, "stable_timeline", lambda _gh, _config, _number: snapshot())

    result = pp.next_review(object(), {
        "repository": "owner/repo", "trusted_account": "owner", "base_branch": "main",
    })
    assert result["action"] == "audit"
    assert result["target"]["number"] == 42


def test_content_review_completion_ignores_unrelated_integration_owner():
    health = {
        "review_candidates": [
            {"number": 10, "stage": "integration-review"},
        ],
        "content_review_candidates": [
            {"number": 42, "stage": "review"},
            {"number": 43, "stage": "review"},
        ],
    }

    assert pp.content_review_allowed(health, 42)
    assert not pp.content_review_allowed(health, 10)
    assert not pp.content_review_allowed(health, 99)


def test_content_review_completion_supports_older_health_contract():
    assert pp.content_review_allowed({
        "review_candidates": [{"number": 42, "stage": "review"}],
    }, 42)


def target_review_lease(value, *, depth=0):
    info = pp.epoch(value, "owner")
    issued_at = int(pp.time.time())
    return {
        "version": 1, "stage": "review", "repository": "owner/repo",
        "number": 42, "head": HEAD,
        "snapshot": pp.content_review_digest(value),
        "epoch": info["hash"], "anchor": info["anchor_id"],
        "depth": depth, "completion_gate": "target-v1",
        "issued_at": issued_at, "expires_at": issued_at + 7200,
        "nonce": "1" * 32,
    }


def test_content_review_target_gate_rejects_draft_and_live_depth_change():
    config = {"trusted_account": "owner", "base_branch": "main"}
    value = snapshot()
    lease = target_review_lease(value)

    draft = dict(value, isDraft=True)
    with pytest.raises(pp.PipelineError, match="draft"):
        pp.content_review_target_gate(draft, config, lease)

    old_head = "d" * 40
    prior_completion = (
        f"<!-- pp:head-reviewed {old_head} review-comment=91 claim=92 "
        f"epoch-sha256={'e' * 64} -->"
    )
    changed = copy.deepcopy(value)
    changed["edges"].insert(0, trusted_comment("c0", 93, prior_completion))
    with pytest.raises(pp.PipelineError, match="review depth changed"):
        pp.content_review_target_gate(changed, config, lease)


def test_complete_review_target_gate_does_not_run_global_health(tmp_path, monkeypatch):
    config = {
        "repository": "owner/repo", "trusted_account": "owner",
        "base_branch": "main", "review_completion_gate": "target-v1",
    }
    current = snapshot()
    lease = target_review_lease(current)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "change": "safe change", "checks": ["pytest"],
        "blocking": [], "tail": [],
    }), encoding="utf-8")
    next_id = iter((101, 102, 103))

    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(pp, "run_health", lambda _config: pytest.fail("global health was called"))
    monkeypatch.setattr(pp, "ensure_identity", lambda _gh, _config: None)
    monkeypatch.setattr(
        pp, "stable_timeline",
        lambda _gh, _config, _number: copy.deepcopy(current),
    )

    def fake_post(_gh, _config, _number, body):
        database_id = next(next_id)
        current["edges"].append(trusted_comment(f"c{database_id}", database_id, body))
        return {"id": database_id, "body": body, "user": {"login": "owner"}}

    def fake_add(_gh, _config, _number, label):
        current["labels"] = sorted(set(current["labels"]) | {label})

    monkeypatch.setattr(pp, "post_comment", fake_post)
    monkeypatch.setattr(pp, "add_label", fake_add)

    result = pp.complete_review(object(), config, pp.encode_signed_lease(lease), str(report))

    assert result["action"] == "completed"
    assert result["outcome"] == "reviewed"
    assert result["completion"] == 103


def test_signed_review_lease_rejects_payload_tampering(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path / "data"))
    lease = target_review_lease(snapshot())
    encoded = pp.encode_signed_lease(lease)
    payload, signature = encoded.split(".")
    decoded = pp.decode_lease(payload)
    decoded["number"] = 99
    tampered = f"{pp.encode_lease(decoded)}.{signature}"

    with pytest.raises(pp.PipelineError, match="signature"):
        pp.decode_signed_lease(tampered)


def test_pipeline_lease_key_concurrent_first_use_is_atomic(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PP_DATA_DIR", str(data_dir))

    with ThreadPoolExecutor(max_workers=12) as pool:
        keys = list(pool.map(lambda _index: pp.pipeline_lease_key(create=True), range(24)))

    assert len(set(keys)) == 1
    assert (data_dir / "pipeline-lease.key").read_bytes() == keys[0]
    assert list(data_dir.iterdir()) == [data_dir / "pipeline-lease.key"]


def test_complete_review_rechecks_expiry_before_first_mutation(tmp_path, monkeypatch):
    config = {
        "repository": "owner/repo", "trusted_account": "owner",
        "base_branch": "main", "review_completion_gate": "target-v1",
    }
    current = snapshot()
    lease = target_review_lease(current)
    lease.update({"issued_at": 900, "expires_at": 1001})
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "change": "safe change", "checks": ["pytest"],
        "blocking": [], "tail": [],
    }), encoding="utf-8")
    clock = iter((1000, 1002))

    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(pp.time, "time", lambda: next(clock))
    monkeypatch.setattr(pp, "ensure_identity", lambda _gh, _config: None)
    monkeypatch.setattr(
        pp, "stable_timeline",
        lambda _gh, _config, _number: copy.deepcopy(current),
    )
    monkeypatch.setattr(pp, "post_comment", lambda *_args: pytest.fail("mutation started"))

    with pytest.raises(pp.PipelineError, match="expired"):
        pp.complete_review(object(), config, pp.encode_signed_lease(lease), str(report))


@pytest.mark.parametrize("label", ["hold", "needs-decision", "changes-requested"])
def test_content_review_target_gate_rejects_routing_labels(label):
    value = snapshot()
    lease = target_review_lease(value)
    value["labels"] = [label]

    with pytest.raises(pp.PipelineError, match="routing|FIX"):
        pp.content_review_target_gate(
            value, {"trusted_account": "owner", "base_branch": "main"}, lease,
        )


def test_content_review_target_gate_rejects_protocol_state():
    value = snapshot()
    lease = target_review_lease(value)
    value["edges"].append(trusted_comment(
        "c2", 99,
        f"<!-- pp:review-claim {HEAD} review-comment=98 epoch-sha256={lease['epoch']} -->",
    ))

    with pytest.raises(pp.PipelineError, match="protocol state"):
        pp.content_review_target_gate(
            value, {"trusted_account": "owner", "base_branch": "main"}, lease,
        )


def test_target_review_election_requires_exact_content_candidate():
    selected = {"number": 42, "head": HEAD, "stage": "review", "review_depth": 1}
    assert pp.content_review_elected({"content_review_candidates": [selected]}, selected)
    assert not pp.content_review_elected({"review_candidates": [selected]}, selected)
    assert not pp.content_review_elected({"content_review_candidates": [
        dict(selected, head="d" * 40),
    ]}, selected)
    assert not pp.content_review_elected({"content_review_candidates": [
        dict(selected, review_depth=0),
    ]}, selected)


def test_empty_review_reason_explains_waiting_state():
    reason = pp.review_empty_reason({
        "integration_owner": {"number": 10, "stage": "integration-merge-ready"},
    })
    assert "#10" in reason
    assert "integration-merge-ready" in reason

    reason = pp.review_empty_reason({"reviewed_waiting_ship": [{"number": 11}]})
    assert "ship" in reason
    assert "#11" in reason


def test_plan_handoff_returns_approved_issue_to_fix():
    class FakeGitHub:
        def __init__(self):
            self.comments = []
            self.added = []
            self.removed = []

        def json(self, *args, input_value=None):
            path = args[1]
            if path.endswith("/issues/1274"):
                return {"state": "open", "labels": [
                    {"name": "approved"}, {"name": "plan-in-review"},
                    {"name": "needs-decision"},
                ]}
            if path.endswith("/issues/1274/comments"):
                self.comments.append(input_value["body"])
                return {"body": input_value["body"], "user": {"login": "owner"}}
            if path.endswith("/issues/1274/labels"):
                self.added.extend(input_value["labels"])
                return [{"name": value} for value in input_value["labels"]]
            raise AssertionError(path)

        def run(self, *args, input_value=None, allow=(0,), **_kwargs):
            if "--paginate" in args:
                return ""
            self.removed.append(args[-1].rsplit("/", 1)[-1])
            return ""

    gh = FakeGitHub()
    result = pp.finish_plan_handoff(gh, {
        "repository": "owner/repo", "trusted_account": "owner",
    }, 1400, "Summary\nPlan-Issue: #1274\nPlan-Path: Plans/159-undefined-values.md")

    assert result == {"issue": 1274, "path": "Plans/159-undefined-values.md"}
    assert gh.added == ["ready-fix"]
    assert gh.removed == ["plan-in-review", "needs-decision"]
    assert "pp:plan-ready issue=1274 pr=1400" in gh.comments[0]


def test_plan_handoff_accepts_repository_native_unicode_filename():
    class FakeGitHub:
        def json(self, *args, input_value=None):
            path = args[1]
            if path.endswith("/issues/7"):
                return {"state": "open", "labels": [
                    {"name": "approved"}, {"name": "plan-in-review"},
                ]}
            if path.endswith("/issues/7/comments"):
                return {"body": input_value["body"], "user": {"login": "owner"}}
            if path.endswith("/issues/7/labels"):
                return [{"name": value} for value in input_value["labels"]]
            raise AssertionError(path)

        def run(self, *args, input_value=None, allow=(0,)):
            return ""

    result = pp.finish_plan_handoff(FakeGitHub(), {
        "repository": "owner/repo", "trusted_account": "owner",
    }, 1401, "Plan-Issue: #7\nPlan-Path: Plans/7-план-исправления.md")

    assert result == {"issue": 7, "path": "Plans/7-план-исправления.md"}


def test_same_repo_closing_issues_preserve_repository_identity():
    body = (
        "Fixes #9\n"
        "closed: OWNER/REPO#9\n"
        "Resolves owner/repo#17\n"
        "Fixes other/project#42\n"
        "fixed #3"
    )
    assert pp.same_repo_closing_issues(body, "owner/repo") == [3, 9, 17]


def test_pending_merge_intents_skip_completed_and_untrusted_comments(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    intent_1 = (
        f"<!-- pp:merge-cleanup-intent head={HEAD} proof-sha256={'b' * 64} "
        f"body-sha256={'c' * 64} issues=3,9 -->"
    )
    intent_2 = (
        f"<!-- pp:merge-cleanup-intent head={'d' * 40} proof-sha256={'e' * 64} "
        f"body-sha256={'f' * 64} issues=none -->"
    )
    done = f"<!-- pp:merge-cleanup-done intent=101 head={HEAD} merge={'1' * 40} -->"
    comments = [
        {"id": 101, "body": intent_1, "user": {"login": "owner"},
         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
         "issue_url": "https://api.github.com/repos/owner/repo/issues/41"},
        {"id": 102, "body": done, "user": {"login": "owner"},
         "created_at": "2026-01-01T00:01:00Z", "updated_at": "2026-01-01T00:01:00Z",
         "issue_url": "https://api.github.com/repos/owner/repo/issues/41"},
        {"id": 103, "body": intent_2, "user": {"login": "owner"},
         "created_at": "2026-01-01T00:02:00Z", "updated_at": "2026-01-01T00:02:00Z",
         "issue_url": "https://api.github.com/repos/owner/repo/issues/42"},
        {"id": 104, "body": intent_1, "user": {"login": "attacker"},
         "created_at": "2026-01-01T00:03:00Z", "updated_at": "2026-01-01T00:03:00Z",
         "issue_url": "https://api.github.com/repos/owner/repo/issues/43"},
    ]

    class FakeGitHub:
        def run(self, *args, **kwargs):
            return "\n".join(json.dumps(item) for item in comments)

    pending = pp.pending_merge_intents(FakeGitHub(), {
        "repository": "owner/repo", "trusted_account": "owner",
    })
    assert [(item["id"], item["number"], item["issues"]) for item in pending] == [
        (103, 42, []),
    ]


def _repository_comment(comment_id, body, *, number=42,
                        created="2026-01-01T00:00:00Z", updated=None,
                        login="owner"):
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login},
        "created_at": created,
        "updated_at": updated or created,
        "issue_url": f"https://api.github.com/repos/owner/repo/issues/{number}",
    }


def test_merge_comment_index_backfills_once_then_scans_only_updated_delta(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    clock = iter([
        datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc),
    ])
    monkeypatch.setattr(pp, "_utc_now", lambda: next(clock))
    config = {
        "repository": "owner/repo", "trusted_account": "owner",
        "merge_comment_backfill_timeout_seconds": 777,
    }
    intent_body = (
        f"<!-- pp:merge-cleanup-intent head={HEAD} proof-sha256={'b' * 64} "
        f"body-sha256={'c' * 64} issues=none -->"
    )
    baseline = [
        _repository_comment(10, "ordinary", created="2026-01-01T00:00:00Z"),
        # Seen near the end of a long initial pagination.  It must not move the
        # durable watermark past an unseen marker created during that scan.
        _repository_comment(11, "late page", created="2026-01-01T00:19:00Z"),
    ]
    intent = _repository_comment(
        20, intent_body, created="2026-01-01T00:11:00Z")

    class FakeGitHub:
        def __init__(self):
            self.queries = []
            self.timeouts = []

        def run(self, *args, **_kwargs):
            query = next(item for item in args if item.startswith("repos/"))
            self.queries.append(query)
            self.timeouts.append(_kwargs.get("timeout_seconds"))
            values = baseline if "&since=" not in query else [intent]
            return "\n".join(json.dumps(item) for item in values)

    gh = FakeGitHub()
    assert pp.pending_merge_intents(gh, config) == []
    assert [item["id"] for item in pp.pending_merge_intents(gh, config)] == [20]

    assert len(gh.queries) == 2
    assert "sort=created&direction=asc" in gh.queries[0]
    assert "&since=" not in gh.queries[0]
    assert "&since=2026-01-01T00:05:00Z" in gh.queries[1]
    assert "sort=updated&direction=asc" in gh.queries[1]
    assert gh.timeouts == [777, None]
    assert not (tmp_path / "promptpilot.db").exists()


def test_long_backfill_watermark_catches_concurrent_marker_edit(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    clock = iter([
        datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
    ])
    monkeypatch.setattr(pp, "_utc_now", lambda: next(clock))
    config = {"repository": "owner/repo", "trusted_account": "owner"}
    body = (
        f"<!-- pp:merge-cleanup-intent head={HEAD} proof-sha256={'b' * 64} "
        f"body-sha256={'c' * 64} issues=none -->"
    )
    original = _repository_comment(30, body)
    edited = _repository_comment(
        30, body + " edited", created="2026-01-01T00:00:00Z",
        updated="2026-01-01T00:11:00Z")
    late_page = _repository_comment(
        31, "late page", created="2026-01-01T00:20:00Z")

    class FakeGitHub:
        def __init__(self):
            self.calls = 0
            self.queries = []

        def run(self, *args, **_kwargs):
            self.calls += 1
            self.queries.append(next(
                item for item in args if item.startswith("repos/")))
            values = [original, late_page] if self.calls == 1 else [edited]
            return "\n".join(json.dumps(value) for value in values)

    gh = FakeGitHub()
    assert [item["id"] for item in pp.pending_merge_intents(gh, config)] == [30]
    assert pp.pending_merge_intents(gh, config) == []
    assert "&since=2026-01-01T00:05:00Z" in gh.queries[1]


def test_comment_scan_tolerates_duplicate_id_at_same_timestamp():
    config = {"repository": "owner/repo", "trusted_account": "owner"}
    body = pp.intent_body(HEAD, {"review_id": 17}, "", [])
    marker = _repository_comment(35, body)
    edited = _repository_comment(35, body + " edited")
    state = pp._new_merge_comment_index(config)

    pp._apply_merge_comments(state, [marker, edited], config)

    assert pp._pending_from_merge_comment_index(state) == []


def test_empty_repository_backfill_persists_scan_start_for_next_delta(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    clock = iter([
        datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc),
    ])
    monkeypatch.setattr(pp, "_utc_now", lambda: next(clock))
    config = {"repository": "owner/repo", "trusted_account": "owner"}
    body = pp.intent_body(HEAD, {"review_id": 17}, "", [])
    intent = _repository_comment(
        40, body, created="2026-01-01T00:11:00Z")

    class FakeGitHub:
        def __init__(self):
            self.queries = []

        def run(self, *args, **_kwargs):
            query = next(item for item in args if item.startswith("repos/"))
            self.queries.append(query)
            return "" if len(self.queries) == 1 else json.dumps(intent)

    gh = FakeGitHub()
    assert pp.pending_merge_intents(gh, config) == []
    state = json.loads(
        pp._merge_comment_index_path(config).read_text(encoding="utf-8"))
    assert state["initialized"] is True
    assert state["checkpoint"] == "2026-01-01T00:10:00Z"

    assert [item["id"] for item in pp.pending_merge_intents(gh, config)] == [40]
    assert "&since=2026-01-01T00:05:00Z" in gh.queries[1]


def test_corrupt_merge_comment_index_fails_closed_without_github_scan(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    config = {"repository": "owner/repo", "trusted_account": "owner"}
    path = pp._merge_comment_index_path(config)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")

    class FakeGitHub:
        def run(self, *_args, **_kwargs):
            pytest.fail("a corrupt durable index must not be silently replaced")

    with pytest.raises(pp.PipelineError, match="invalid identity or schema"):
        pp.pending_merge_intents(FakeGitHub(), config)


def test_direct_intent_does_not_advance_listing_checkpoint_or_hide_a_racer(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    clock = iter([
        datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 11, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 12, tzinfo=timezone.utc),
    ])
    monkeypatch.setattr(pp, "_utc_now", lambda: next(clock))
    config = {"repository": "owner/repo", "trusted_account": "owner"}
    baseline = _repository_comment(10, "ordinary")
    own_body = pp.intent_body(HEAD, {"review_id": 17}, "", [])
    own = _repository_comment(
        100, own_body, created="2026-01-01T00:30:00Z")
    racer_body = pp.intent_body("d" * 40, {"review_id": 18}, "", [])
    racer = _repository_comment(
        99, racer_body, number=43, created="2026-01-01T00:10:30Z")

    class FakeGitHub:
        def __init__(self):
            self.scans = 0
            self.expose_racer = False

        def run(self, *args, **_kwargs):
            if args[:2] != ("api", "--paginate"):
                raise AssertionError(args)
            self.scans += 1
            values = [baseline] if self.scans == 1 else []
            if self.expose_racer:
                values.append(racer)
            return "\n".join(json.dumps(item) for item in values)

        def json(self, *args, input_value=None):
            assert args[-2:] == ("--input", "-")
            assert input_value == {"body": own_body}
            return own

    gh = FakeGitHub()
    reserved, pending = pp.reserve_merge_intent(gh, config, 42, own_body)
    assert reserved["id"] == 100
    assert [item["id"] for item in pending] == [100]
    state = json.loads(pp._merge_comment_index_path(config).read_text(encoding="utf-8"))
    assert state["checkpoint"] == "2026-01-01T00:11:00Z"

    gh.expose_racer = True
    assert [item["id"] for item in pp.pending_merge_intents(gh, config)] == [99, 100]


def test_merge_intent_publication_is_single_flight_across_local_processors(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    config = {"repository": "owner/repo", "trusted_account": "owner"}
    baseline = _repository_comment(10, "ordinary")
    posted = []

    class FakeGitHub:
        def run(self, *args, **_kwargs):
            assert args[:2] == ("api", "--paginate")
            return json.dumps(baseline)

        def json(self, *args, input_value=None):
            number = int(args[1].split("/issues/", 1)[1].split("/", 1)[0])
            comment = _repository_comment(
                100 + len(posted), input_value["body"], number=number,
                created="2026-01-01T00:01:00Z")
            posted.append(comment)
            return comment

    gh = FakeGitHub()
    first = pp.intent_body(HEAD, {"review_id": 1}, "", [])
    second = pp.intent_body("d" * 40, {"review_id": 2}, "", [])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda item: pp.reserve_merge_intent(gh, config, *item),
            [(42, first), (43, second)],
        ))

    assert len(posted) == 1
    assert sum(result[0] is not None for result in results) == 1
    assert all([item["id"] for item in result[1]] == [100] for result in results)


@pytest.mark.parametrize("marker", ["missing", "edited"])
def test_cached_intent_must_still_be_exact_in_open_pr_timeline(
        monkeypatch, marker):
    config = {
        "repository": "owner/repo", "trusted_account": "owner",
        "base_branch": "main", "allow_no_checks": True,
    }
    body = pp.intent_body(HEAD, {"review_id": 17}, "", [])
    intent = pp.parse_merge_intent(_repository_comment(70, body), config)
    value = snapshot()
    if marker == "edited":
        value["edges"].append(trusted_comment("c2", 70, body + " edited"))

    class FakeGitHub:
        def json(self, *_args, **_kwargs):
            return {
                "state": "open", "merged": False,
                "head": {"sha": HEAD}, "base": {"ref": "main"},
            }

    monkeypatch.setattr(pp, "stable_timeline", lambda *_: value)
    monkeypatch.setattr(
        pp, "pr_checks",
        lambda *_: pytest.fail("an absent exact marker must stop before checks"),
    )

    result = pp.pending_merge_action(FakeGitHub(), config, intent)

    assert result == {
        "action": "fallback",
        "reason": "merge cleanup intent is missing or edited in GraphQL timeline",
    }


def test_complete_merge_revalidates_exact_intent_before_mutation(monkeypatch):
    config = {
        "repository": "owner/repo", "trusted_account": "owner",
        "base_branch": "main", "merge_method": "merge",
        "allow_no_checks": True,
    }
    established = {"review_id": 17}
    body = pp.intent_body(HEAD, established, "", [])
    intent = pp.parse_merge_intent(_repository_comment(70, body), config)
    value = snapshot(ship_event("c2"))
    value["labels"] = ["ship"]
    lease = {
        "version": 1, "stage": "merge", "repository": "owner/repo",
        "number": 42, "head": HEAD, "snapshot": pp.digest(value),
        "proof": established, "intent": intent,
    }

    class FakeGitHub:
        def json(self, *args, **_kwargs):
            pytest.fail(f"merge mutation must not run: {args}")

    monkeypatch.setattr(pp, "ensure_identity", lambda *_: None)
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: {"state": "green"})
    monkeypatch.setattr(pp, "stable_timeline", lambda *_: value)
    monkeypatch.setattr(pp, "epoch", lambda *_: {})
    monkeypatch.setattr(pp, "validate_epoch_safety", lambda *_: None)
    monkeypatch.setattr(pp, "proof", lambda *_: established)
    monkeypatch.setattr(
        pp, "pr_checks",
        lambda *_: ({"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
                     "body": ""}, []),
    )

    with pytest.raises(pp.PipelineError, match="missing or edited"):
        pp.complete_merge(FakeGitHub(), config, pp.encode_lease(lease))


def test_recover_merge_cleanup_finishes_labels_before_done(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_DATA_DIR", str(tmp_path))
    comments = []

    class FakeGitHub:
        def __init__(self):
            self.labels = {9: {"in-work"}, 42: {"ship"}}

        def json(self, *args, input_value=None):
            path = args[1]
            if path == "user":
                return {"login": "owner"}
            if path.endswith("/comments"):
                comments.append(input_value["body"])
                return {
                    "id": 900, "body": input_value["body"],
                    "user": {"login": "owner"},
                    "created_at": "2026-01-01T00:03:00Z",
                    "updated_at": "2026-01-01T00:03:00Z",
                    "issue_url": "https://api.github.com/repos/owner/repo/issues/42",
                }
            number = int(path.rsplit("/", 1)[-1])
            return {"state": "closed", "labels": [{"name": value} for value in sorted(self.labels[number])]}

        def run(self, *args, input_value=None, allow=(0,), **_kwargs):
            if "--paginate" in args:
                return ""
            path = args[-1]
            number = int(path.split("/issues/", 1)[1].split("/", 1)[0])
            label = path.rsplit("/", 1)[-1]
            self.labels[number].discard(label)
            return ""

    intent = {"id": 700, "number": 42, "head": HEAD, "issues": [9]}
    monkeypatch.setattr(
        pp, "validate_merged_intent",
        lambda gh, config, value: ({"body": "Fixes #9"}, "f" * 40),
    )
    gh = FakeGitHub()
    result = pp.recover_merge_cleanup(gh, {
        "repository": "owner/repo", "trusted_account": "owner",
    }, intent)

    assert result["action"] == "completed"
    assert result["in_work_removed"] == [9]
    assert gh.labels == {9: set(), 42: set()}
    assert comments == [
        f"{pp.MERGE_DONE_MESSAGE}\n"
        f"<!-- pp:merge-cleanup-done intent=700 head={HEAD} merge={'f' * 40} -->",
    ]


def test_visible_service_markers_preserve_legacy_protocol_parsing():
    claim = (
        f"<!-- pp:review-claim {HEAD} review-comment=17 epoch-sha256={'a' * 64} -->"
    )
    completion = (
        f"<!-- pp:head-reviewed {HEAD} review-comment=17 claim=18 "
        f"epoch-sha256={'a' * 64} -->"
    )
    assert pp.CLAIM.fullmatch(claim)
    assert pp.CLAIM.fullmatch(f"{pp.CLAIM_MESSAGE}\n{claim}")
    assert pp.COMPLETE.fullmatch(completion)
    assert pp.COMPLETE.fullmatch(f"{pp.COMPLETE_MESSAGE}\n{completion}")

    intent = pp.intent_body(HEAD, {"review_id": 17}, "Fixes #9", [9])
    assert intent.startswith(pp.MERGE_INTENT_MESSAGE + "\n")
    assert pp.MERGE_CLEANUP_INTENT.fullmatch(intent)
