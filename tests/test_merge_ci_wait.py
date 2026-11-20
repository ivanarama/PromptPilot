"""Exercise CI waiting from the ordinary signed fallback through the public CLI."""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from promptpilot import entrypoint, fallback_handoff, merge_ci_wait as ci
from promptpilot import pipeline_insights as pi, project_pipeline as pp
from tests.test_fallback_handoff import HEAD, health
from tests.test_merge_checkpoint import dispatch
from tests.test_merge_final_source import merge_dispatch as merge_dispatch
from tests.test_pipeline_source_refresh import continuation as continuation, targeted_merge as targeted_merge


class Actions:
    def __init__(self, repo):
        repository = {"full_name": repo, "id": 101}
        self.pr = {"state": "open", "draft": False, "merged": False, "number": 42,
                   "head": {"ref": "my-branch", "sha": HEAD, "repo": repository},
                   "base": {"ref": "main", "repo": repository}, "labels": [{"name": "ship"}]}
        self.run = {"id": 444, "workflow_id": 99, "run_attempt": 1, "repository": repository,
                    "path": ".github/workflows/ci.yml", "event": "workflow_dispatch",
                    "head_sha": HEAD, "head_branch": "my-branch", "head_repository": repository,
                    "pull_requests": [{"number": 42, "head": dict(self.pr["head"]), "base": dict(self.pr["base"])}],
                    "run_started_at": datetime.now(timezone.utc).isoformat(),
                    "status": "completed", "conclusion": "failure"}
        self.jobs = [{"name": "e2e", "status": "completed", "conclusion": "failure",
                      "head_sha": HEAD, "run_id": 444, "run_attempt": 1}]
        self.prefix = "repos/" + repo
        self.requests = []
        self.on_jobs = lambda: None

    def json(self, method, path):
        assert method == "api"
        self.requests.append(path)
        if path == "user":
            return {"login": "owner"}
        if path == self.prefix + "/pulls/42":
            return self.pr
        if path == f"{self.prefix}/actions/runs/{self.run['id']}":
            return self.run
        if path == f"{self.prefix}/actions/runs/{self.run['id']}/attempts/1/jobs?per_page=100":
            self.on_jobs()
            return {"total_count": len(self.jobs), "jobs": self.jobs}
        raise AssertionError(path)


@pytest.fixture
def elected(merge_dispatch, monkeypatch, tmp_path, capsys):
    f = merge_dispatch
    queue = f.profile["queues"][0]
    queue["item_blockers"] = True
    path = tmp_path / "pipelinectl.json"
    repo = f.profile["repository"]
    path.write_text(json.dumps({"repository": repo, "trusted_account": "owner",
                                "health_command": ["health"], "required_checks": ["e2e"],
                                "fallback_handoff": "target-v1"}), encoding="utf-8")
    queue["execution"].update(merge_confirmation="bounded-v1", command=["pipeline", "--config", str(path), "next", "merge"])
    live = health("merge")
    handoff = fallback_handoff.create(pp.load_config(str(path)), live, "merge", live["merge_executable"][0], "audit")
    monkeypatch.setattr(pi, "_tool_preflight", lambda *_: handoff)
    route = dispatch(f)
    assert route["action"] == "prompt", route
    gate = route["gate_command"][-1]
    monkeypatch.setenv("PP_TASK_ID", str(f.task.id))
    monkeypatch.setenv("PP_TASK_STARTED_AT", f.task.started_at.isoformat())
    gh = Actions(repo)
    monkeypatch.setattr(pp, "GitHub", lambda **_: gh)

    def call(verb, attempt=1, rid=444):
        argv = ["pp", "pipelinectl", "merge-ci-wait", verb, "--profile", "example", "--number", "42",
                "--head", HEAD, "--run-id", str(rid), "--attempt", str(attempt), "--lease-file", gate]
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as done:
            entrypoint.main()
        result = json.loads(capsys.readouterr().out)
        assert done.value.code == int(result["state"] == "refused")
        assert result["product_mutation_authorized"] is False
        return result

    return SimpleNamespace(f=f, gh=gh, repo=repo, target={"number": 42, "head": HEAD}, call=call, gate=gate,
                           config_path=path)


def test_one_shot_survives_unknown_send_and_late_terminal_readback(elected):
    e = elected
    first = e.call("claim")
    assert first["state"] == "claimed" and first["send_allowed"] is True and first["attempt"] == 2
    assert ci.active_wait(e.repo, e.target) == ci.series_wait(e.f.task.series_id, e.repo)
    assert ci.defer_time(e.f.task) is None

    assert e.call("observe", 2)["state"] == "unknown"  # process died on either side of send
    second = e.call("claim", rid=999)
    assert (second["state"], second["send_allowed"], second["run_id"]) == ("already_claimed", False, 444)
    assert e.call("observe", 2, rid=999)["state"] == "refused"
    e.gh.run.update(run_attempt=2, status="in_progress", conclusion=None)
    result = e.call("observe", 2)
    assert result["state"] == "waiting"
    e.f.db.mark_completed(e.f.task.id, "ИТОГ: ПУСТО", verdict="ПУСТО")
    due = ci.defer_time(e.f.task)
    assert due and time.time() < due.timestamp() <= time.time() + ci.POLL_SECONDS
    record = ci.series_wait(e.f.task.series_id, e.repo)
    assert ci.observe_run(e.gh, record, now=first["deadline"] + 1)["state"] == "timeout"
    e.gh.run.update(status="completed", conclusion="success")
    outcome = ci.observe_run(e.gh, record, now=first["deadline"] + 1)
    assert outcome["state"] == "success" and ci.save_observation(record, outcome)
    assert not ci.save_observation(record, outcome)  # stale continuation
    assert ci.defer_time(e.f.task) is None and ci.series_wait(e.f.task.series_id, e.repo) is None
    assert ci.active_wait(e.repo, e.target) is None
    assert e.call("claim")["state"] == "refused"  # old task no longer running


def test_claim_arms_wake_even_when_github_did_not_acknowledge(elected):
    e = elected
    assert e.call("claim")["send_allowed"]
    e.f.db.mark_completed(e.f.task.id, "ИТОГ: ПУСТО", verdict="ПУСТО")
    due = ci.defer_time(e.f.task)
    assert due and time.time() < due.timestamp() <= time.time() + ci.POLL_SECONDS
    e.f.db.set_setting(ci._receipt_key(e.f.task.series_id), "{")
    assert ci.defer_time(e.f.task) is None


@pytest.mark.parametrize("fault", ["run-sha", "workflow", "branch", "association", "repo", "test", "late", "pause",
                                   "config", "incarnation", "lease"])
def test_wrong_identity_or_lost_admission_is_never_claimed(elected, fault, monkeypatch):
    e = elected
    if fault == "run-sha":
        e.gh.run["head_sha"] = "f" * 40
    elif fault == "workflow":
        e.gh.run["path"] = ".github/workflows/release.yml"
    elif fault == "branch":
        e.gh.run["head_branch"] = "other"
    elif fault == "association":
        e.gh.run["pull_requests"] = []
    elif fault == "repo":
        e.gh.run["head_repository"] = {"full_name": "other/project"}
    elif fault == "test":
        e.gh.jobs[0]["name"] = "bench"
    elif fault == "late":
        e.gh.run["run_attempt"] = 2
    elif fault == "pause":
        e.gh.on_jobs = lambda: e.f.db.set_setting("worker_paused", "1")
    elif fault == "config":
        path = Path(e.f.profile["queues"][0]["execution"]["command"][2])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["required_checks"] = ["other-check"]
        e.gh.on_jobs = lambda: path.write_text(json.dumps(data), encoding="utf-8")
    elif fault == "incarnation":
        monkeypatch.setenv("PP_TASK_STARTED_AT", "2000-01-01T00:00:00+00:00")
    elif fault == "lease":
        token_file = Path(e.gate)
        token_file.write_text(token_file.read_text(encoding="ascii") + "x", encoding="ascii")
    result = e.call("claim")
    assert result["state"] == "refused" and not result["send_allowed"]
    assert ci.active_wait(e.repo, e.target) is None


def test_concurrent_claims_return_one_send_permission(elected):
    e = elected
    e.gh.on_jobs = Barrier(2).wait
    args = argparse.Namespace(profile="example", operation="claim", number=42, head=HEAD,
                              run_id=444, attempt=1, lease_file=e.gate)
    env = {"PP_TASK_ID": str(e.f.task.id), "PP_TASK_STARTED_AT": e.f.task.started_at.isoformat()}

    def claim():
        try:
            return ci._apply(args, env)["state"]
        except pp.PipelineError as exc:
            assert "concurrently" in str(exc)
            return "refused"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: claim(), [0, 1]))
    assert sorted(results) == ["claimed", "refused"]


@pytest.mark.parametrize("change", ["closed", "head", "hold", "new-attempt", "failure"])
def test_restart_of_external_attempt_does_not_resend_or_merge_another_head(elected, change):
    e = elected
    e.gh.run.update(run_attempt=2, status="in_progress", conclusion=None)
    assert e.call("observe", 2)["state"] == "waiting"
    assert e.call("claim")["state"] == "already_claimed"
    record = ci.active_wait(e.repo, e.target)
    if change == "closed":
        e.gh.pr["state"] = "closed"
    elif change == "head":
        e.gh.pr["head"]["sha"] = "f" * 40
    elif change == "hold":
        e.gh.pr["labels"].append({"name": "hold"})
    elif change == "new-attempt":
        e.gh.run.update(run_attempt=3, status="completed", conclusion="success")
    else:
        e.gh.run.update(status="completed", conclusion="failure")
    expected = {"new-attempt": "attempt_changed", "failure": "failed"}.get(change, "superseded")
    e.gh.requests.clear()
    outcome = ci.observe_run(e.gh, record)
    assert len(e.gh.requests) == (1 if expected == "superseded" else 2)
    assert outcome["state"] == expected and ci.save_observation(record, outcome)
    assert ci.active_wait(e.repo, dict(e.target, head="f" * 40)) is None
    assert ci.defer_time(e.f.task) is None

@pytest.mark.parametrize(("options", "expected"), [
    ([], "pipelinectl.json"), (["--config", "custom.json"], "custom.json"), (["--config=project"], "project"),
    (["--config", "a", "--config=b"], None), (["--config"], None), (["--config="], None),
])
def test_adapter_configuration_matches_the_project_cli(options, expected):
    cmd = ["pipelinectl", *options, "next", "merge"]
    if expected is None:
        with pytest.raises(pp.PipelineError):
            ci.adapter_config_path(cmd)
    else:
        assert ci.adapter_config_path(cmd) == expected


@pytest.mark.parametrize("scenario", ["legacy", "ready-owner", "cleanup", "first-changed", "hold", "typed",
                                     "config", "green", "excluded", "pending"])
def test_elects_exact_ci_diagnosis_with_the_normal_signed_gate_only(elected, monkeypatch, capsys, scenario):
    from promptpilot import review_routing_adapter

    e = elected
    if scenario == "pending":
        e.gh.run.update(status="in_progress", conclusion=None)
    state = e.call("observe")["state"]
    assert state == ("waiting" if scenario == "pending" else "failed")
    source = health("integration-merge-ready" if scenario == "ready-owner" else "merge")
    if scenario == "first-changed":
        source["merge_executable"].insert(0, dict(source["merge_executable"][0], number=100))
    if scenario == "hold":
        e.gh.pr["labels"].append({"name": "manual"})
    if scenario == "green":
        e.gh.run.update(conclusion="success")
    if scenario == "excluded":
        monkeypatch.setenv("PP_PIPELINE_EXCLUDED_NUMBERS", "[42]")

    def run_health(config, *, config_path):
        assert config_path == str(e.config_path)
        if scenario == "config":
            config["base_branch"] = "other"
        return source

    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [{"number": 9}] if scenario == "cleanup" else [])
    monkeypatch.setattr(pp, "run_health", run_health)
    monkeypatch.setattr(review_routing_adapter, "journal_directory", lambda *_: e.config_path.parent / "routing")
    marker = {"node": {"__typename": "IssueComment", "author": {"login": "owner"},
                       "body": "<!-- pp:review-routing-start -->"}}
    monkeypatch.setattr(pp, "stable_timeline", lambda *_: {"edges": [marker] if scenario == "typed" else []})
    argv = ["pp", "pipelinectl", "--config", str(e.config_path), "merge-ci-diagnose", "--number", "42", "--head", HEAD]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as done:
        entrypoint.main()
    assert done.value.code == 0
    result = json.loads(capsys.readouterr().out)
    if scenario in {"legacy", "ready-owner"}:
        lease = fallback_handoff.validate(result, "merge")
        assert lease["repository"] == e.repo and lease["target"] == fallback_handoff.identity(source["merge_executable"][0])
        assert not lease.get("target_reservation")
    else:
        expected = {"hold": "superseded", "green": "success"}.get(scenario, "unavailable")
        assert result["action"] == "wait" and result["state"] == expected and "handoff" not in result
