"""Exercise the public CLI without importing a live scheduler database."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from promptpilot import project_pipeline as pp
from promptpilot.models import TaskCreate


HEAD = "a" * 40
ISSUE = {"stage": "fix-issue", "number": 7, "eligibility_digest": "b" * 40}
PR = {"stage": "review", "number": 12, "head": HEAD}


@pytest.fixture
def input_files(tmp_path):
    config = tmp_path / "pipelinectl.json"
    health = tmp_path / "health.json"
    values = {
        "repository": "owner/repo",
        "trusted_account": "owner",
        "health_command": ["a-checker-that-must-never-run"],
        "sync_base_before_health": True,
    }
    snapshot = {"state": "yellow", "findings": [], "fix_candidates": [ISSUE, PR]}
    health.write_text(json.dumps(snapshot), encoding="utf-8")
    config.write_text(json.dumps(values), encoding="utf-8")
    return config, health, values, snapshot


def observe(input_files, capsys, *, db_path=None):
    config, health, _, _ = input_files
    argv = ["--config", str(config), "observe-fix", "--health-file", str(health)]
    if db_path is not None:
        argv.extend(["--reservations-db", str(db_path)])
    exit_code = pp.run(argv)
    return exit_code, json.loads(capsys.readouterr().out)


def test_observation_in_legacy_install_does_not_bootstrap_scheduler(input_files, tmp_path):
    config, health, _, _ = input_files
    data = tmp_path / "unused-data"
    result = subprocess.run(
        [sys.executable, "-m", "promptpilot", "pipelinectl", "--config",
         str(config), "observe-fix", "--health-file", str(health)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PP_DATA_DIR": str(data)},
        capture_output=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["route"] == "legacy"
    assert report["mutation_authorized"] is False
    assert {row["reason"] for row in report["candidates"]} == {"legacy_skill_selects"}
    assert not (data / "promptpilot.db").exists()


def test_parallel_keeps_real_candidate_order_and_observes_active_reserves(
        input_files, isolated_db, capsys):
    config, _, values, _ = input_files
    config.write_text(json.dumps({**values, "parallel_fix_enabled": True}), encoding="utf-8")
    isolated_db.create_task(TaskCreate(prompt="FIX"))
    active = isolated_db.get_next_runnable()
    assert isolated_db.reserve_pipeline_target(
        "owner/repo", "fix-pr", 12, HEAD, active.id, 300,
        task_started_at=active.started_at.isoformat(),
        now=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    before = hashlib.sha256(isolated_db.DB_PATH.read_bytes()).hexdigest()
    code, result = observe(input_files, capsys, db_path=isolated_db.DB_PATH)
    assert code == 0
    assert [(row["number"], row["reason"]) for row in result["candidates"]] == [
        (12, "locally_reserved"), (7, "no_local_reservation_seen")]
    assert result["local_reservations_checked_at"]
    assert hashlib.sha256(isolated_db.DB_PATH.read_bytes()).hexdigest() == before
    assert "token" not in json.dumps(result)


def test_source_bound_route_takes_precedence_and_marks_ordinary_issue(input_files, capsys):
    config, health, values, snapshot = input_files
    config.write_text(json.dumps({**values, "parallel_fix_enabled": True,
        "technical_handoff": {"enabled": True, "protocol": "review-routing-v1",
                              "max_extra_attempts": 2}}), encoding="utf-8")
    technical = {"stage": "fix-pr", "number": 12, "head": HEAD, "source": {
        "publication_id": "12", "claim_id": "13", "completion_id": "14", "head": HEAD,
        "identity_sha256": "b" * 64, "epoch": "c" * 64, "report_sha256": "d" * 64,
    }}
    health.write_text(json.dumps({**snapshot, "technical_fix_candidates": [technical]}),
                      encoding="utf-8")
    code, result = observe(input_files, capsys)
    assert code == 0 and result["route"] == "source_bound"
    assert [row["reason"] for row in result["candidates"]] == [
        "local_reservations_not_checked", "outside_source_bound_route"]


@pytest.mark.parametrize("change", [{"state": "red"}, {"repository": "other/repo"}])
def test_incomplete_or_other_repository_snapshot_cannot_look_empty(input_files, capsys, change):
    _, health, _, snapshot = input_files
    health.write_text(json.dumps({**snapshot, **change}), encoding="utf-8")
    code, result = observe(input_files, capsys)
    assert code == 2 and result["action"] == "error"
    assert "candidates" not in result
