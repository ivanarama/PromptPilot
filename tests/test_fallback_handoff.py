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


def pre_review_sync(**changes):
    value = {
        "intent_comment_id": 101,
        "done_comment_id": 102,
        "from": "b" * 40,
        "to": HEAD,
        "base": "c" * 40,
        "identity_sha256": "d" * 64,
        "intent_created_at": "2026-09-17T08:00:00Z",
        "done_created_at": "2026-09-17T08:02:00Z",
    }
    value.update(changes)
    return value


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("PP_PIPELINE_LEASE_KEY_FILE", str(tmp_path / "lease.key"))
    return {"repository": "ivanarama/onebase", "trusted_account": "ivanarama",
            "base_branch": "main", "health_command": ["go", "run", "./tools/pipelinehealth", "-json"],
            "fallback_handoff": "target-v1", "review_completion_gate": "target-v1"}


def health(target_stage="integration-review"):
    target = {"number": 42, "head": HEAD, "stage": target_stage, "review_depth": 2}
    if target_stage == handoff.PRE_REVIEW_VALIDATION_STAGE:
        target["pre_review_sync"] = pre_review_sync()
    integration = target_stage in handoff.INTEGRATION_STAGES
    reviewing = target_stage in handoff.REVIEW_STAGES
    return {"state": "yellow" if integration else "green",
            "findings": ([{"code": "single_flight_barrier", "severity": "yellow", "pr": 42}]
                         if integration else []),
            "integration_owner": target if integration else None,
            "review_candidates": [target] if reviewing else [],
            "content_review_candidates": ([target]
                                          if target_stage in handoff.CONTENT_REVIEW_STAGES
                                          else []),
            "merge_executable": [] if reviewing else [target]}


@pytest.mark.parametrize("change", [
    {"number": 99}, {"head": "b" * 40}, {"review_depth": 2},
    {"stage": "integration-review"},
])
def test_parallel_review_allowlist_cannot_expand_content_authority(change):
    value = health()
    content = {"number": 43, "head": "c" * 40,
               "stage": "review", "review_depth": 0}
    value["content_review_candidates"] = [content]
    value["parallel_review_candidates"] = [dict(content, **change)]
    with pytest.raises(pp.PipelineError, match="parallel REVIEW"):
        handoff.validate_health(value)


def test_parallel_review_allowlist_requires_integration_review_owner():
    value = health("integration-merge-ready")
    content = {"number": 43, "head": "c" * 40,
               "stage": "review", "review_depth": 0}
    value["content_review_candidates"] = [content]
    value["review_candidates"] = [content]
    value["parallel_review_candidates"] = [content]
    with pytest.raises(pp.PipelineError, match="no integration-review owner"):
        handoff.validate_health(value)


def test_parallel_review_cannot_duplicate_integration_owner():
    value = health()
    duplicate = {"number": 42, "head": HEAD,
                 "stage": "review", "review_depth": 0}
    value["content_review_candidates"] = [duplicate]
    value["parallel_review_candidates"] = [duplicate]
    with pytest.raises(pp.PipelineError, match="includes its integration owner"):
        handoff.validate_health(value)


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
    "review", "pre-review-validation", "integration-review", "legacy-integration-review",
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
    assert "gate_command ровно один раз" in route["prompt"]
    assert "сохрани stdout и $LASTEXITCODE" in route["prompt"]
    assert "запрещено бросать исключение только по exit code до разбора ответа" in route["prompt"]
    assert "не повторяй gate_command и next" in route["prompt"]
    assert "ИТОГ: НЕ СМОГ (gate-fallback: <точный error" in route["prompt"]
    assert "Один envelope — один PR" in route["prompt"]
    if stage == "merge":
        assert "pp:base-sync-done заканчивается ИТОГ: ГОТОВО" in route["prompt"]
        assert "не объявляет PR влитым" in route["prompt"]
    else:
        assert "Для MERGE:" not in route["prompt"]
    if target_stage == handoff.PRE_REVIEW_VALIDATION_STAGE:
        assert "все восемь полей pre_review_sync" in route["prompt"]
        assert "полную стабильную GraphQL-проверку" in route["prompt"]
        assert "проверь весь diff" in route["prompt"]
        assert "нельзя завершать через быстрый action=audit" in route["prompt"]
        assert "До gate_command, оставаясь полностью read-only" in route["prompt"]
        assert "После gate_command сначала" not in route["prompt"]
        assert route["prompt"].index("До gate_command") < route["prompt"].index(
            "Непосредственно перед первой мутацией")
    else:
        assert "все восемь полей pre_review_sync" not in route["prompt"]
    assert route["preflight"]["target"] == route["target"]
    assert len(scans) == 1
    code, gated = invoke(route["gate_command"][1:])
    assert code == 0 and gated["action"] == "validated"
    assert gated["target"] == route["target"]
    assert gated["mutation_authorized"] is False
    assert len(scans) == 2 and not snapshots
    # Cleanup reads are deliberately preserved and are not pipelinehealth scans.
    assert len(cleanup_reads) == (2 if stage == "merge" else 0)


def test_documented_scan_budget_uses_local_productive_wake_up():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert "До первой мутации на доказанном handoff-пути выполняются ровно два" in readme
    assert "следующий этап будится локально" in readme
    assert "два scan вместо прежних четырёх" in readme
    assert "`4 → 2`" in readme
    assert "Профиль без\nлокального графа сохраняет совместимый третий scan" in readme


def test_cli_gate_uses_signed_election_checkout_after_detached_audit(
        config, monkeypatch, tmp_path):
    base = tmp_path / "base"
    audit = tmp_path / "detached-audit"
    base.mkdir()
    audit.mkdir()
    config["sync_base_before_health"] = True
    config["health_command"] = ["fixture-health"]
    snapshot = health()
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    calls = []

    def subprocess_run(command, **kwargs):
        location = Path(kwargs.get("cwd") or Path.cwd())
        calls.append((command, location))
        if command[-2:] == ["branch", "--show-current"]:
            output = "main\n" if location == base else ""
        elif command == ["fixture-health"]:
            output = json.dumps(snapshot)
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(pp.subprocess, "run", subprocess_run)
    monkeypatch.chdir(base)
    code, elected = invoke(["--config", str(tmp_path / "config.json"), "next", "review"])
    assert code == 0 and elected["action"] == "fallback"
    lease = pp.decode_signed_lease(elected["handoff"]["lease"])
    assert lease["health_checkout"] == str(base.resolve())
    calls.clear()
    monkeypatch.chdir(audit)
    code, gated = invoke(["--config", str(tmp_path / "config.json"),
                          "gate-fallback", "review", "--lease", elected["handoff"]["lease"]])
    assert code == 0 and gated["action"] == "validated"
    assert gated["mutation_authorized"] is False
    assert len(calls) == 5 and all(location == base for _, location in calls)
    assert Path.cwd() == audit


def test_cli_gate_still_rejects_dirty_pinned_checkout(config, monkeypatch, tmp_path):
    config["sync_base_before_health"] = True
    monkeypatch.chdir(tmp_path)
    elected = handoff.create(config, health(), "review", health()["integration_owner"], "audit")
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    def subprocess_run(command, **kwargs):
        output = "main\n" if command[-2:] == ["branch", "--show-current"] else " M file.go\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    monkeypatch.setattr(pp.subprocess, "run", subprocess_run)
    code, error = invoke(["gate-fallback", "review", "--lease", elected["handoff"]["lease"]])
    assert code == 2 and "tracked changes" in error["error"]


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


def test_pre_review_fallback_keeps_metadata_bound_across_unrelated_owner(config):
    source = health(handoff.PRE_REVIEW_VALIDATION_STAGE)
    target = source["review_candidates"][0]
    preflight = handoff.create(config, source, "review", target, "provenance")
    lease = handoff.validate(preflight, "review")
    assert lease["target"] == handoff.identity(target)
    assert lease["target"]["pre_review_sync"] == pre_review_sync()

    fresh = health("integration-review")
    fresh["integration_owner"]["number"] = 90
    fresh["findings"][0]["pr"] = 90
    fresh["content_review_candidates"] = [target]
    handoff.health_gate(fresh, "review", lease["target"], election=False)
    with pytest.raises(pp.PipelineError):
        handoff.health_gate(fresh, "review", lease["target"], election=True)


@pytest.mark.parametrize("field,value", [
    ("intent_comment_id", 201),
    ("done_comment_id", 202),
    ("from", "e" * 40),
    ("to", "e" * 40),
    ("base", "e" * 40),
    ("identity_sha256", "e" * 64),
    ("intent_created_at", "2026-09-17T08:00:01Z"),
    ("done_created_at", "2026-09-17T08:02:01Z"),
])
def test_pre_review_fresh_gate_compares_every_metadata_field(config, field, value):
    source = health(handoff.PRE_REVIEW_VALIDATION_STAGE)
    target = source["review_candidates"][0]
    preflight = handoff.create(config, source, "review", target, "provenance")
    expected = handoff.validate(preflight, "review")["target"]
    fresh = copy.deepcopy(source)
    for candidate in fresh["review_candidates"] + fresh["content_review_candidates"]:
        candidate["pre_review_sync"][field] = value
        if field == "to":
            candidate["head"] = value
    with pytest.raises(pp.PipelineError, match="exact executable"):
        handoff.health_gate(fresh, "review", expected, election=False)


@pytest.mark.parametrize("mutation", [
    lambda target: target.pop("pre_review_sync"),
    lambda target: target["pre_review_sync"].pop("base"),
    lambda target: target["pre_review_sync"].update(extra="x"),
    lambda target: target["pre_review_sync"].update(intent_comment_id=True),
    lambda target: target["pre_review_sync"].update(done_comment_id=0),
    lambda target: target["pre_review_sync"].update(done_comment_id=2**63),
    lambda target: target["pre_review_sync"].update(**{"from": "B" * 40}),
    lambda target: target["pre_review_sync"].update(identity_sha256="d" * 63),
    lambda target: target["pre_review_sync"].update(intent_created_at="not-a-time"),
    lambda target: target["pre_review_sync"].update(done_created_at="2026-09-17 08:02:00Z"),
    lambda target: target["pre_review_sync"].update(
        done_created_at="2026-09-17T07:59:59Z"),
    lambda target: target["pre_review_sync"].update(to="e" * 40),
])
def test_pre_review_target_schema_fails_closed(mutation):
    target = health(handoff.PRE_REVIEW_VALIDATION_STAGE)["review_candidates"][0]
    mutation(target)
    with pytest.raises(pp.PipelineError):
        handoff.identity(target)


def test_pre_review_stage_cannot_be_integration_owner_or_merge_executable():
    source = health(handoff.PRE_REVIEW_VALIDATION_STAGE)
    target = source["review_candidates"][0]
    as_owner = copy.deepcopy(source)
    as_owner["integration_owner"] = target
    as_owner["findings"] = [
        {"code": "single_flight_barrier", "severity": "yellow", "pr": 42},
    ]
    with pytest.raises(pp.PipelineError, match="owner"):
        handoff.validate_health(as_owner)

    in_merge = copy.deepcopy(source)
    in_merge["merge_executable"] = [target]
    with pytest.raises(pp.PipelineError, match="merge_executable"):
        handoff.validate_health(in_merge)


def test_next_review_forces_targeted_full_skill_for_pre_review(config, monkeypatch):
    source = health(handoff.PRE_REVIEW_VALIDATION_STAGE)
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: source)
    monkeypatch.setattr(
        pp, "stable_timeline",
        lambda *_: pytest.fail("pre-review validation must not enter fast audit"),
    )

    result = pp.next_review(object(), config)

    assert result["action"] == "fallback"
    assert result["target"] == handoff.identity(source["review_candidates"][0])
    assert result["handoff"]["protocol"] == handoff.PROTOCOL


def test_next_review_pre_review_fails_closed_without_target_protocol(monkeypatch):
    source = health(handoff.PRE_REVIEW_VALIDATION_STAGE)
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: source)
    with pytest.raises(pp.PipelineError, match="exact-target"):
        pp.next_review(object(), {})


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


def test_signed_fallback_cli_accepts_opaque_file_and_rejects_tampering(config, monkeypatch, tmp_path, isolated_db):
    source = health("integration-merge-ready")
    result = handoff.create(config, source, "merge", source["integration_owner"], "carry")
    argument = pipeline_insights._lease_argument(SimpleNamespace(id=123), result["handoff"]["lease"], "gate")
    assert argument[0] == "--lease-file"
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: source)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    code, gated = invoke(["gate-fallback", "merge", *argument])
    assert code == 0 and gated["action"] == "validated"
    assert gated["mutation_authorized"] is False
    path = Path(argument[1])
    token = path.read_text(encoding="ascii")
    path.write_text(("A" if token[0] != "A" else "B") + token[1:], encoding="ascii")
    code, refused = invoke(["gate-fallback", "merge", *argument])
    assert code == 2 and "signature" in refused["error"]


def test_cli_missing_lease_file_is_structured_refusal(tmp_path):
    code, result = invoke(["gate-fallback", "merge", "--lease-file", str(tmp_path / "missing.lease")])
    assert code == 2 and result["action"] == "error"


@pytest.mark.parametrize("action", ["merge", "cleanup"])
@pytest.mark.parametrize("completion_action", ["completed", "error"])
def test_opted_in_direct_merge_completion_never_launches_provider(
        monkeypatch, action, completion_action):
    queue = {"id": "merge", "execution": {"mode": "auto", "direct_complete": True,
             "command": ["ctl", "next", "merge"]}}
    monkeypatch.setattr(pipeline_insights, "_matching_queue", lambda _: ("example", {}, queue))
    monkeypatch.setattr(pipeline_insights, "_tool_available", lambda *_: (True, ""))
    commands = []
    def preflight(_execution, command, _directory):
        commands.append(command)
        if len(commands) == 1:
            return {"action": action, "lease": "opaque", "target": {"number": 42}}
        assert command == ["ctl", "complete", "merge-cleanup" if action == "cleanup" else "merge",
                           "--lease", "opaque"]
        return {"action": completion_action, "error": "fresh gate refused"}
    monkeypatch.setattr(pipeline_insights, "_tool_preflight", preflight)
    route = pipeline_insights.execution_route(SimpleNamespace(), "/skill")
    assert len(commands) == 2
    if completion_action == "completed":
        assert route["action"] == "complete_empty" and route["verdict"] == "ГОТОВО"
    else:
        assert route["action"] == "defer" and "fresh gate refused" in route["reason"]


def test_cli_blocked_by_pending_ci_returns_wait_before_full_fallback(config, monkeypatch):
    config["required_checks"] = ["build", "lint"]
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: health("merge"))
    monkeypatch.setattr(pp, "list_ship", lambda *_: [{
        "number": 42, "head": {"sha": HEAD}, "title": "pending CI"}])
    monkeypatch.setattr(pp, "stable_timeline", lambda *_: {
        "headRefOid": HEAD, "state": "OPEN", "isDraft": False,
        "baseRefName": "main", "labelsComplete": True, "labels": ["ship"],
        "edges": [{"cursor": "c1", "node": {"__typename": "PullRequestCommit",
                   "id": "head-anchor", "commit": {"oid": HEAD}}}],
    })
    monkeypatch.setattr(pp, "proof", lambda *_: {"review": 1})
    monkeypatch.setattr(pp, "trusted_ship_authorized", lambda *_: True)
    monkeypatch.setattr(pp, "pr_checks", lambda *_: (
        {"mergeStateStatus": "BLOCKED", "mergeable": "MERGEABLE"},
        [{"name": "build", "status": "IN_PROGRESS"},
         {"name": "lint", "conclusion": "SUCCESS"}]))
    monkeypatch.setattr(pp, "fallback_target", lambda *_: pytest.fail("CI wait launched fallback"))
    code, result = invoke(["next", "merge"])
    assert code == 0
    assert result == {"action": "wait", "number": 42,
                      "reason": "required CI checks are still running"}


@pytest.mark.parametrize("owner_stage", ["integration-review", "legacy-integration-review"])
@pytest.mark.parametrize("legacy_null", [False, True])
def test_merge_dispatch_waits_for_review_owner_without_provider(
        config, monkeypatch, owner_stage, legacy_null):
    source = health(owner_stage)
    if legacy_null:
        source["merge_executable"] = None
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: source)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "list_ship", lambda *_: pytest.fail("must not bypass owner"))
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    queue = {"id": "merge", "execution": {
        "mode": "auto", "command": ["ctl", "next", "merge"]}}
    monkeypatch.setattr(pipeline_insights, "_matching_queue", lambda _: ("example", {}, queue))
    monkeypatch.setattr(pipeline_insights, "_tool_available", lambda *_: (True, ""))

    def preflight(*_):
        code, result = invoke(["next", "merge"])
        assert code == 0
        return result

    monkeypatch.setattr(pipeline_insights, "_tool_preflight", preflight)
    route = pipeline_insights.execution_route(SimpleNamespace(), "/skill")
    assert route["action"] == "complete_empty"
    assert route["preflight"]["number"] == 42
    assert "waiting for integration REVIEW" in route["reason"]


@pytest.mark.parametrize("stage", ["review", "merge"])
def test_opted_in_red_health_never_routes_to_manual_mutations(config, monkeypatch, stage):
    monkeypatch.setattr(pp, "run_health", lambda *_, **__: {"state": "red"})
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    with pytest.raises(pp.PipelineError, match="red"):
        (pp.next_review if stage == "review" else pp.next_merge)(object(), config)


def test_onebase_producer_parity_fixture(config):
    corpus = json.loads((Path(__file__).parent / "fixtures" / "fallback-handoff-v1.json").read_text(encoding="utf-8"))
    assert corpus["protocol"] == handoff.PROTOCOL and len(corpus["cases"]) == 8
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


def test_fresh_ordinary_merge_priority_change_keeps_exact_target_gate(config):
    source = health("merge")
    target = source["merge_executable"][0]
    source["merge_executable"].insert(0, {"number": 9, "head": HEAD, "stage": "merge"})
    handoff.health_gate(source, "merge", target, election=False)
    with pytest.raises(pp.PipelineError, match="exact executable"):
        handoff.health_gate(source, "merge", target, election=True)


def test_public_merge_gate_keeps_elected_target_after_unrelated_reorder(config, monkeypatch):
    source = health("merge")
    target = source["merge_executable"][0]
    preflight = handoff.create(config, source, "merge", target, "needs full skill")
    fresh = copy.deepcopy(source)
    fresh["merge_executable"].insert(0, {"number": 9, "head": HEAD, "stage": "merge"})
    monkeypatch.setattr(pp, "load_config", lambda _: dict(config))
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: fresh)
    monkeypatch.setattr(pp, "GitHub", ReadOnlyGitHub)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])

    code, result = invoke(["gate-fallback", "merge", "--lease", preflight["handoff"]["lease"]])
    assert code == 0
    assert result["action"] == "validated"
    assert result["target"] == handoff.identity(target)
    assert result["mutation_authorized"] is False


def test_fresh_ordinary_merge_still_closes_when_owner_or_target_changes():
    source = health("merge")
    target = source["merge_executable"][0]
    source["merge_executable"] = [{"number": 9, "head": HEAD, "stage": "merge"}]
    with pytest.raises(pp.PipelineError, match="exact executable"):
        handoff.health_gate(source, "merge", target, election=False)

    source = health("integration-merge-ready")
    target = source["integration_owner"]
    source["merge_executable"].insert(0, {"number": 9, "head": HEAD, "stage": "merge"})
    with pytest.raises(pp.PipelineError):
        handoff.health_gate(source, "merge", target, election=False)


def test_module_cli_helper_errors_are_json_and_visible_without_tracebacks(config, tmp_path):
    config_path = tmp_path / "pipelinectl.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    result = subprocess.run([
        sys.executable, "-m", "promptpilot.project_pipeline", "--config", str(config_path),
        "gate-fallback", "review", "--lease", "invalid",
    ], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["action"] == "error"
    assert payload["error"]
    assert result.stderr.strip() == f"pipelinectl gate-fallback: {payload['error']}"
    assert "Traceback" not in result.stderr
