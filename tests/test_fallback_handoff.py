"""Exercise the actual election -> dispatcher -> read-only gate CLI path."""

import copy
import io
import json
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from promptpilot import fallback_handoff as handoff
from promptpilot import pipeline_insights, project_pipeline as pp


HEAD = "a" * 40


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(tmp_path / "lease.key"))
    return {"repository": "ivanarama/onebase", "trusted_account": "ivanarama",
            "base_branch": "main", "health_command": ["go", "run", "./tools/pipelinehealth", "-json"],
            "fallback_handoff": "target-v1", "review_completion_gate": "target-v1"}


def health(target_stage="integration-review"):
    target = {"number": 42, "head": HEAD, "stage": target_stage, "review_depth": 2}
    integration = target_stage not in {"review", "merge"}
    reviewing = target_stage in handoff.REVIEW_STAGES
    return {"state": "yellow" if integration else "green",
            "findings": ([{"code": "single_flight_barrier", "severity": "yellow", "pr": 42}]
                         if integration else []),
            "integration_owner": target if integration else None,
            "review_candidates": [target] if reviewing else [],
            "content_review_candidates": [target] if target_stage == "review" else [],
            "merge_executable": [] if reviewing else [target]}


def invoke(argv):
    output = io.StringIO()
    with redirect_stdout(output):
        code = pp.run(argv)
    return code, json.loads(output.getvalue())


class ReadOnlyGitHub:
    def json(self, *args, **kwargs):
        assert args == ("api", "user"), f"unexpected GitHub operation: {args}"
        assert not kwargs, "no GitHub write input is permitted"
        return {"login": "ivanarama"}


@pytest.mark.parametrize("target_stage", [
    "review", "integration-review", "legacy-integration-review",
    "integration-merge-ready", "legacy-integration-merge-ready", "integration-merge-recovery",
])
def test_cli_dispatch_handoff_runs_exactly_election_and_fresh_gate(config, monkeypatch, target_stage):
    stage = "review" if target_stage in handoff.REVIEW_STAGES else "merge"
    snapshots = [health(target_stage), health(target_stage)]
    scans = []

    def run_health(*_, **kwargs):
        scans.append(kwargs)
        return snapshots.pop(0)

    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "run_health", run_health)
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    cleanup_reads = []
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: cleanup_reads.append(True) or [])
    monkeypatch.setattr(pp, "list_ship", lambda *_: pytest.fail("owner election must not rescan ship"))
    command = ["project-pipelinectl", "--config", "pipelinectl.json", "next", stage]
    queue = {"id": stage, "execution": {"mode": "auto", "command": command}}
    monkeypatch.setattr(pipeline_insights, "_matching_queue", lambda _: ("onebase", {}, queue))
    monkeypatch.setattr(pipeline_insights, "_tool_available", lambda *_: (True, ""))

    def preflight(_execution, actual, _directory):
        assert actual == command
        code, result = invoke(actual[1:])
        assert code == 0
        return result

    monkeypatch.setattr(pipeline_insights, "_tool_preflight", preflight)
    route = pipeline_insights.execution_route(SimpleNamespace(), "/the-skill")
    assert route["action"] == "prompt" and route["mode"] == "skill"
    assert route["next_already_run"] is True
    assert route["command"] == command
    assert '"next_already_run": true' in route["prompt"]
    assert "Не запускай next повторно" in route["prompt"]
    assert "Один envelope — один PR" in route["prompt"]
    assert route["preflight"]["target"] == route["target"]
    assert len(scans) == 1
    code, gated = invoke(route["gate_command"][1:])
    assert code == 0 and gated["action"] == "validated"
    assert gated["target"] == route["target"]
    assert gated["mutation_authorized"] is False
    assert len(scans) == 2 and not snapshots
    # Cleanup reads are deliberately preserved and are not pipelinehealth scans.
    assert len(cleanup_reads) == (2 if stage == "merge" else 0)


def test_documented_scan_budget_includes_productive_wake_up():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert "До первой мутации на доказанном handoff-пути выполняются ровно два" in readme
    assert "ещё один свежий scan для wake-up следующих этапов" in readme
    assert "три scan вместо прежних четырёх" in readme
    assert "`4 → 3`" in readme


@pytest.mark.parametrize("field,value", [
    ("number", 43), ("number", True), ("head", "b" * 40), ("head", "short"),
    ("stage", "legacy-integration-merge-ready"), ("stage", "review"),
])
def test_gate_rejects_changed_owner_or_executable_before_any_mutation(config, monkeypatch, field, value):
    original = health("integration-merge-ready")
    preflight = handoff.create(config, original, "merge", original["integration_owner"], "carry")
    fresh = copy.deepcopy(original)
    fresh["integration_owner"][field] = value
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: fresh)
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    code, result = invoke(["gate-fallback", "merge", "--lease", preflight["handoff"]["lease"]])
    assert code == 2 and result["action"] == "error"


@pytest.mark.parametrize("change", [
    {"state": "red"}, {"state": None}, {"findings": []}, {"findings": [None]},
    {"integration_owner": None}, {"merge_executable": []}, {"merge_executable": None},
    {"merge_executable": [{"number": 42, "head": "b" * 40, "stage": "integration-merge-ready"}]},
])
def test_incomplete_or_contradictory_health_cannot_create_handoff(config, change):
    source = health("integration-merge-ready")
    target = dict(source["integration_owner"])
    source.update(copy.deepcopy(change))
    with pytest.raises(pp.PipelineError):
        handoff.create(config, source, "merge", target, "carry")


@pytest.mark.parametrize("field", ["state", "findings", "merge_executable"])
def test_missing_health_fields_fail_closed(config, field):
    source = health("integration-merge-ready")
    del source[field]
    with pytest.raises(pp.PipelineError):
        handoff.create(config, source, "merge", source["integration_owner"], "carry")


@pytest.mark.parametrize("path,value", [
    (("handoff",), None), (("handoff", "protocol"), "other"),
    (("handoff", "stage"), "merge"), (("handoff", "repository"), "other/repo"),
    (("handoff", "lease"), "invalid"), (("target", "number"), 43),
    (("handoff", "target", "head"), "b" * 40),
])
def test_dispatch_blocks_malformed_handoff_in_auto_mode(config, monkeypatch, path, value):
    source = health()
    preflight = handoff.create(config, source, "review", source["integration_owner"], "integration")
    container = preflight
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    queue = {"id": "review", "execution": {"mode": "auto", "command": ["pp", "next", "review"]}}
    monkeypatch.setattr(pipeline_insights, "_matching_queue", lambda _: ("onebase", {}, queue))
    monkeypatch.setattr(pipeline_insights, "_tool_available", lambda *_: (True, ""))
    monkeypatch.setattr(pipeline_insights, "_tool_preflight", lambda *_: preflight)
    result = pipeline_insights.execution_route(SimpleNamespace(), "manual mutation fallback")
    assert result["action"] == "block"
    assert "prompt" not in result


def test_gate_rejects_expiry_config_change_and_new_cleanup(config, monkeypatch):
    source = health("integration-merge-ready")
    preflight = handoff.create(config, source, "merge", source["integration_owner"], "carry")
    token = preflight["handoff"]["lease"]
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: source)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [{"number": 9}])
    with pytest.raises(pp.PipelineError, match="cleanup"):
        handoff.gate(ReadOnlyGitHub(), config, "merge", token)
    changed = dict(config, base_branch="release")
    with pytest.raises(pp.PipelineError, match="configuration"):
        handoff.gate(ReadOnlyGitHub(), changed, "merge", token)
    lease = pp.decode_signed_lease(token)
    monkeypatch.setattr(handoff.time, "time", lambda: lease["expires_at"])
    with pytest.raises(pp.PipelineError, match="expired"):
        handoff.gate(ReadOnlyGitHub(), config, "merge", token)


def test_sync_config_change_and_expiry_during_scan_close_gate(config, monkeypatch):
    source = health()
    preflight = handoff.create(config, source, "review", source["integration_owner"], "integration")
    token = preflight["handoff"]["lease"]

    def changed(conf, **_):
        conf["trusted_account"] = "other"
        return source

    monkeypatch.setattr(pp, "run_health", changed)
    with pytest.raises(pp.PipelineError, match="configuration"):
        handoff.gate(ReadOnlyGitHub(), dict(config), "review", token)
    now = int(time.time())

    def expired(*_, **__):
        monkeypatch.setattr(handoff.time, "time", lambda: now + handoff.TTL + 1)
        return source

    monkeypatch.setattr(pp, "run_health", expired)
    with pytest.raises(pp.PipelineError, match="expired"):
        handoff.gate(ReadOnlyGitHub(), config, "review", token)


def test_content_fallback_keeps_target_lease_across_unrelated_owner(config):
    source = health("review")
    target = source["review_candidates"][0]
    fresh = health("integration-review")
    fresh["integration_owner"]["number"] = 90
    fresh["findings"][0]["pr"] = 90
    fresh["content_review_candidates"] = [target]
    handoff.health_gate(fresh, "review", target, election=False)
    with pytest.raises(pp.PipelineError):
        handoff.health_gate(fresh, "review", target, election=True)


def test_legacy_config_does_not_silently_opt_in(monkeypatch):
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: health())
    assert pp.next_review(object(), {}) == {
        "action": "fallback", "reason": "integration/base-sync state requires the full skill",
    }


def test_merge_cleanup_still_precedes_opted_in_election(config, monkeypatch):
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [{"number": 9}])
    monkeypatch.setattr(pp, "pending_merge_action", lambda *_: {"action": "cleanup"})
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: pytest.fail("cleanup precedes election"))
    assert pp.next_merge(object(), config) == {"action": "cleanup"}


@pytest.mark.parametrize("stage", ["review", "merge"])
def test_opted_in_red_health_never_routes_to_manual_mutations(config, monkeypatch, stage):
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: {"state": "red"})
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    with pytest.raises(pp.PipelineError, match="red"):
        (pp.next_review if stage == "review" else pp.next_merge)(object(), config)


def test_onebase_producer_parity_fixture(config):
    corpus = json.loads((Path(__file__).parent / "fixtures" / "fallback-handoff-v1.json").read_text(encoding="utf-8"))
    assert corpus["protocol"] == handoff.PROTOCOL and len(corpus["cases"]) == 7
    for case in corpus["cases"]:
        result = handoff.create(config, case["health"], case["stage"], case["target"], "parity")
        assert handoff.validate(result, case["stage"])["target"] == case["target"]
        handoff.health_gate(case["health"], case["stage"], case["target"], election=False)
        stale = dict(case["target"], head="c" * 40)
        with pytest.raises(pp.PipelineError):
            handoff.health_gate(case["health"], case["stage"], stale, election=False)


@pytest.mark.parametrize("field,value", [
    ("state", {}), ("findings", None),
    ("findings", [{"severity": [], "code": "single_flight_barrier", "pr": 42}]),
    ("review_candidates", [None]), ("review_candidates", {}),
    ("content_review_candidates", None),
    ("integration_owner", {"number": 42, "head": HEAD, "stage": {}}),
    ("merge_executable", [{"number": 42, "head": HEAD, "stage": "merge"}]),
])
@pytest.mark.parametrize("stage", ["review", "merge"])
def test_public_cli_returns_structured_error_for_malformed_health(config, monkeypatch, field, value, stage):
    source = health()
    source[field] = value
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: source)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    code, result = invoke(["next", stage])
    assert code == 2 and result["action"] == "error"


def test_gate_rejects_signed_review_token_and_forged_fallback_target(config):
    source = health()
    preflight = handoff.create(config, source, "review", source["integration_owner"], "integration")
    token = preflight["handoff"]["lease"]
    payload, signature = token.split(".")
    changed = pp.decode_lease(payload)
    changed["target"]["number"] = 43
    preflight["handoff"]["lease"] = pp.encode_lease(changed) + "." + signature
    with pytest.raises(pp.PipelineError, match="signature"):
        handoff.validate(preflight, "review")
    changed["purpose"] = "review"
    preflight["handoff"]["lease"] = pp.encode_signed_lease(changed)
    with pytest.raises(pp.PipelineError, match="invalid fallback lease"):
        handoff.validate(preflight, "review")


def test_fresh_ordinary_merge_priority_change_closes_target_gate(config):
    source = health("merge")
    target = source["merge_executable"][0]
    source["merge_executable"].insert(0, {"number": 9, "head": HEAD, "stage": "merge"})
    with pytest.raises(pp.PipelineError, match="exact executable"):
        handoff.health_gate(source, "merge", target, election=False)


def test_module_cli_helper_errors_are_json_not_tracebacks(config, tmp_path):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = subprocess.run([
        sys.executable, "-m", "promptpilot.project_pipeline", "--config", str(config_path),
        "gate-fallback", "review", "--lease", "invalid",
    ], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 2
    assert json.loads(result.stdout)["action"] == "error"
    assert not result.stderr
