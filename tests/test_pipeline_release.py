import copy
from pathlib import PurePosixPath
import hashlib
import json
import os
import plistlib
import sqlite3
import sys

import pytest

from tools.install_onebase_release import configure, rewrite_prompt, validate_manifest
from tools import install_onebase_release as release


def finite_integration_decision(number):
    source = hashlib.sha256(str(number).encode()).hexdigest()[:40]
    scope_sha = hashlib.sha256(f"scope-{number}".encode()).hexdigest()
    path = (f"/Users/operator/.promptpilot/onebase-owner-control/scopes/"
            f"integration-{number}-20261029-{source[:12]}.json")
    return (f"\n\nFINITE OWNER INTEGRATION DECISION #{number}, 29 OCTOBER: "
            "authority is the explicit current user approval and existing technical delegation. "
            f"Fully read {path}, SHA256 {scope_sha}. Applies ONLY to source {source} "
            "and descendant main of minimum_base, with the exact documented conflict resolutions. "
            "All ordinary election/source proof/trusted ship/CI8/hold/ownership/intent/done/"
            "parents/CAS/readback rules remain. New integration HEAD MUST receive independent "
            "integration REVIEW and CI8.\n")


def test_prompt_upgrade_preserves_existing_finite_merge_decisions_verbatim():
    decisions = "".join(finite_integration_decision(n) for n in [2276, 1482, 2175, 2190, 2236, 2264])
    original = ("Read CLAUDE.md canonical .claude/skills/merge-shepherd/SKILL.md "
                "adapter .agents/skills/merge-shepherd/SKILL.md."
                "\n\nОператорский релиз: previous frozen policy." + decisions)
    first = rewrite_prompt(original, "merge-shepherd", PurePosixPath("/release"), "v1")
    second = rewrite_prompt(first, "merge-shepherd", PurePosixPath("/next"), "v2")
    for result in [first, second]:
        assert result.endswith(decisions)
        for n in [2276, 1482, 2175, 2190, 2236, 2264]:
            assert result.count(finite_integration_decision(n).strip()) == 1
        assert "previous frozen policy" not in result
    assert "Read /next/CLAUDE.md" in second
    assert "/next/.claude/skills/merge-shepherd/SKILL.md" in second
    assert ".agents/skills/merge-shepherd/SKILL.md" in second
    assert "/release/" not in second


@pytest.mark.parametrize("corrupt", [
    lambda value: value.replace("SHA256 ", "SHA256 short-"),
    lambda value: value.replace("DECISION #1482", "DECISION #2175"),
    lambda value: value.replace("integration-1482-", "integration-9999-"),
    lambda value: value + value,
    lambda value: value + "\n\nUnrecognized extra operator instruction.",
    lambda value: value.replace("FINITE OWNER", " FINITE OWNER"),
    lambda value: value.lstrip("\n"),
    lambda value: value.replace("FINITE OWNER", "finite owner"),
    lambda value: value.rstrip("\n") + " FINITE OWNER INTEGRATION DECISION #2175\n",
])
def test_prompt_upgrade_rejects_ambiguous_or_malformed_finite_decisions(corrupt):
    original = ("Read CLAUDE.md canonical .claude/skills/merge-shepherd/SKILL.md"
                "\n\nОператорский релиз: previous." + corrupt(finite_integration_decision(1482)))
    with pytest.raises(ValueError, match="finite integration"):
        rewrite_prompt(original, "merge-shepherd", PurePosixPath("/release"), "v1")


@pytest.mark.parametrize("change", [
    lambda value: value.rstrip("\n") + " Existing inline operator note.\n",
    lambda value: value.replace("Fully read ", "Existing operator context. Fully read "),
    lambda value: value.split("with the exact documented")[0].rstrip() + "\n",
])
def test_existing_local_body_is_preserved_opaque_not_semantically_validated(change):
    record = change(finite_integration_decision(1482))
    original = ("Read CLAUDE.md canonical .claude/skills/merge-shepherd/SKILL.md"
                "\n\nОператорский релиз: previous." + record)
    result = rewrite_prompt(original, "merge-shepherd", PurePosixPath("/release"), "v1")
    assert result.endswith(record)


def test_finite_merge_decisions_cannot_move_to_another_stage():
    original = ("Read CLAUDE.md canonical .claude/skills/fix-approved/SKILL.md"
                "\n\nОператорский релиз: previous." + finite_integration_decision(1482))
    with pytest.raises(ValueError, match="finite integration"):
        rewrite_prompt(original, "fix-approved", PurePosixPath("/release"), "v1")


def test_finite_decision_preservation_is_bounded():
    base = ("Read CLAUDE.md canonical .claude/skills/merge-shepherd/SKILL.md"
            "\n\nОператорский релиз: previous.")
    for decisions in ["".join(finite_integration_decision(n) for n in range(1, 34)),
                      finite_integration_decision(1482) + "x" * (128 * 1024)]:
        with pytest.raises(ValueError, match="finite integration"):
            rewrite_prompt(base + decisions, "merge-shepherd", PurePosixPath("/release"), "v1")


def test_prompt_upgrade_replaces_old_hotfix_and_preserves_adapter():
    original = ("Read CLAUDE.md canonical /old/review-queue/SKILL.md "
                "adapter .agents/skills/review-queue/SKILL.md."
                "\n\nОператорский runtime hotfix: old clause")
    result = rewrite_prompt(original, "review-queue", PurePosixPath("/release"), "v1")
    assert "Read /release/CLAUDE.md" in result
    assert "/release/.claude/skills/review-queue/SKILL.md" in result
    assert ".agents/skills/review-queue/SKILL.md" in result
    assert "old clause" not in result
    again = rewrite_prompt(result, "review-queue", PurePosixPath("/next"), "v2")
    assert "/release" not in again
    assert "Read /next/CLAUDE.md" in again


def test_unknown_prompt_is_not_rewritten_broadly():
    with pytest.raises(ValueError):
        rewrite_prompt("Do arbitrary work", "fix-approved", PurePosixPath("/release"), "v1")


def test_configuration_preserves_other_profiles_gates_and_budget():
    config = {"health_command": ["old", "-json"], "required_checks": ["build", "test-windows"],
              "trusted_account": "owner", "review_completion_gate": "target-v1"}
    profiles = {"profiles": {"onebase": {"health_check": {"command": ["old", "-json"]},
                 "github_budget": {"minimum_remaining": {"core": 250}},
                 "queues": [{"id": "fix"}, {"id": "plan"},
                     {"id": "merge", "execution": {"direct_complete": True,
                      "command": ["{python}", "-m", "promptpilot.project_pipeline", "next", "merge"],
                      "probe_command": ["{python}", "-m", "promptpilot.project_pipeline", "capabilities"]}}]},
                "other": {"untouched": True}}}
    old = copy.deepcopy(profiles)
    old_config = copy.deepcopy(config)
    result, cfg = configure(profiles, config, PurePosixPath("/r/pp"), PurePosixPath("/r/health"),
                            PurePosixPath("/r/procedures"), "r")
    assert result["profiles"]["other"] == old["profiles"]["other"]
    assert result["profiles"]["onebase"]["github_budget"] == old["profiles"]["onebase"]["github_budget"]
    for key in ["required_checks", "trusted_account", "review_completion_gate"]:
        assert cfg[key] == old_config[key]
    assert result["profiles"]["onebase"]["queues"][2]["execution"]["direct_complete"] is True
    assert result["profiles"]["onebase"]["queues"][0]["item_blockers"] is True
    assert result["profiles"]["onebase"]["queues"][0]["item_error_holds"] is True
    from promptpilot.pipeline_insights import _adaptive_cadence_policy
    policy = _adaptive_cadence_policy(result["profiles"]["onebase"]["queues"][2])
    assert policy is not None
    assert result["profiles"]["onebase"]["queues"][2]["execution"]["command"] == ["/r/pp", "pipelinectl", "next", "merge"]
    updated, _ = configure(result, cfg, PurePosixPath("/next/pp"), PurePosixPath("/next/health"),
                           PurePosixPath("/next/procedures"), "next")
    assert updated["profiles"]["onebase"]["queues"][2]["execution"]["command"][0] == "/next/pp"


def test_manifest_detects_changed_artifact_before_install(tmp_path):
    path = tmp_path / "skill"
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        validate_manifest({"artifacts": {str(path): "0" * 64}})


def test_release_enables_snapshot_recovery_without_changing_reserves():
    budget = {"costs": {"insights": {"search": 10}},
              "minimum_remaining": {"core": 250, "search": 2, "graphql": 100},
              "priority_one_headroom": {"core": 1200, "search": 12, "graphql": 1000}}
    original = copy.deepcopy(budget)
    profiles = {"profiles": {"onebase": {"github_budget": budget,
                "health_check": {"command": ["old"]}, "queues": []}}}
    configure(profiles, {"health_command": ["old"]}, PurePosixPath("/pp"),
              PurePosixPath("/health"), PurePosixPath("/procedures"), "test")
    assert budget == {**original, "essential_snapshot_headroom": True}


def test_operator_classifications_are_repository_bound_and_preserve_overrides():
    profile = {"repository": "ivanarama/onebase", "queues": [],
               "health_check": {"command": ["old"]},
               "delivery_classifications": {"1700": {"category": "unclassified", "evidence": "operator override"}}}
    profiles = {"profiles": {"onebase": profile}}
    configure(profiles, {"health_command": ["old"]}, PurePosixPath("/pp"),
              PurePosixPath("/health"), PurePosixPath("/procedures"), "test")
    assert profile["delivery_classifications"]["1700"]["category"] == "unclassified"
    assert profile["delivery_classifications"]["1475"]["category"] == "docs_plans"


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("corruption", [None, "indented", "same-line", "case", "duplicate"])
def test_public_installer_preserves_pauses_and_rolls_back_launch_failure(tmp_path, monkeypatch, failure, corruption):
    data = tmp_path / "data"
    launch = tmp_path / "launch"
    procedures = tmp_path / "procedures"
    for directory in [data, launch, procedures]:
        directory.mkdir()
    binary = tmp_path / "pp"
    health = tmp_path / "health"
    for path in [binary, health]:
        path.write_bytes(b"fake executable")
    manifest = {"release_id": "test", "binary": str(binary), "health": str(health),
                "procedures": str(procedures),
                "artifacts": {str(binary): hashlib.sha256(binary.read_bytes()).hexdigest()}}
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    profiles = {"profiles": {"onebase": {"health_check": {"command": ["old", "-json"]},
                 "queues": [{"id": "fix"}, {"id": "plan"}]}, "other": {"unchanged": True}}}
    (data / "pipeline_profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
    (data / "pipelinectl-onebase.json").write_text(json.dumps({"health_command": ["old", "-json"],
                                                          "required_checks": ["build"]}), encoding="utf-8")
    for service in ["server", "bot", "worker"]:
        (launch / f"com.promptpilot.{service}.plist").write_bytes(plistlib.dumps({
            "ProgramArguments": ["/old/pp", service], "KeepAlive": True}))
    originals = {path: path.read_bytes() for directory in [data, launch] for path in directory.iterdir()}
    rows = [{"id": number, "title": "OneBase - " + stage, "paused": number == 3,
             "prompt": f"Read CLAUDE.md canonical .claude/skills/{stage}/SKILL.md"}
            for number, stage in release.STAGES.items()]
    decisions = finite_integration_decision(1482) + finite_integration_decision(2175)
    for row in rows:
        if row["id"] == 4:
            tail = decisions
            if corruption == "indented":
                tail = tail.replace("FINITE OWNER", " FINITE OWNER")
            elif corruption == "same-line":
                tail = tail.lstrip("\n")
            elif corruption == "case":
                tail = tail.replace("FINITE OWNER", "finite owner")
            elif corruption == "duplicate":
                tail = tail.rstrip("\n") + " FINITE OWNER INTEGRATION DECISION #2190\n"
            row["prompt"] += "\n\nОператорский релиз: old runtime." + tail
    with sqlite3.connect(data / "promptpilot.db") as connection:
        connection.executescript("CREATE TABLE task_series(id INTEGER, prompt TEXT, ended_at TEXT, paused INTEGER);"
                                 "CREATE TABLE tasks(series_id INTEGER, status TEXT, prompt TEXT);")
        for row in rows:
            connection.execute("INSERT INTO task_series VALUES(?,?,NULL,?)", (row["id"], row["prompt"], row["paused"]))
            connection.execute("INSERT INTO tasks VALUES(?,'pending',?)", (row["id"], row["prompt"]))
    restarted = []
    commands = []
    failed_once = False
    def run(command, **kwargs):
        nonlocal failed_once
        commands.append(command)
        if "bootstrap" in command:
            restarted.append(command[-1])
            if failure and not failed_once:
                failed_once = True
                raise RuntimeError("simulated launch failure")
    def api(path, payload=None):
        if path == "/api/worker/status":
            return {"state": "online", "paused": True, "pid": 2 if restarted else 1}
        if path == "/api/schedule":
            return rows
        assert path == "/api/tasks?status=running"
        return []
    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(sys, "argv", ["installer", "install", "--manifest", str(manifest_path),
                                    "--data", str(data), "--launch", str(launch)])
    if corruption:
        with pytest.raises(ValueError, match="finite integration"):
            release.main()
        assert not any("bootout" in command or "bootstrap" in command for command in commands)
        assert all(path.read_bytes() == value for path, value in originals.items())
    elif failure:
        with pytest.raises(RuntimeError, match="simulated launch failure"):
            release.main()
        assert all(path.read_bytes() == value for path, value in originals.items())
    else:
        release.main()
        assert json.loads((tmp_path / "installed.json").read_text())["worker"]["paused"] is True
        assert len(restarted) == 3
    with sqlite3.connect(data / "promptpilot.db") as connection:
        assert connection.execute("SELECT paused FROM task_series WHERE id=3").fetchone()[0] == 1
        for row in rows:
            prompt = connection.execute("SELECT prompt FROM task_series WHERE id=?", (row["id"],)).fetchone()[0]
            pending = connection.execute("SELECT prompt FROM tasks WHERE series_id=?", (row["id"],)).fetchone()[0]
            assert pending == prompt
            assert (prompt == row["prompt"]) is (failure or corruption is not None)
            if not failure and not corruption:
                assert str(data / "pipelinectl-onebase.json") in prompt
                assert "Все глобальные owner/allowlist" in prompt
                if row["id"] == 4:
                    assert prompt.endswith(decisions)
