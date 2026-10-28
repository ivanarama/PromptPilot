"""Task rights: what an agent run may do, independent of the CLI.

A run that asked for narrow rights must never start wide: every path either
confines the provider or refuses the task (fail closed).
"""

import asyncio
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from promptpilot import api, config, worker, workflows
from promptpilot.cli import cli
from promptpilot.models import (
    TaskCreate, TaskStatus, WorkflowCreate, WorkflowRole, WorkflowStartRequest,
    WorkflowTaskDispatch,
)


@pytest.fixture
def providers(tmp_path, monkeypatch):
    """Custom providers next to the built-ins, in an isolated data dir."""
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "GUARD", "auto")
    custom = {
        "agy": {"cmd": "agy -p {prompt} --dangerously-skip-permissions",
                "description": "no rights map"},
        "declared": {"cmd": "tool run {prompt}",
                     "rights": {"read": ["--read-only"], "none": ["--no-tools"]}},
    }
    (tmp_path / "providers.json").write_text(json.dumps(custom), encoding="utf-8")
    return tmp_path


def args_before_prompt(cmd, prompt="do it"):
    # Main now sends Claude prompts via stdin, keeping options out of cmd.exe's
    # argument-length limit. In that case the whole argv is CLI options.
    return cmd[:cmd.index(prompt)] if prompt in cmd else cmd


def contains_run(values, run):
    """`run` appears in `values` as consecutive items (flag pairs stay pairs)."""
    return any(values[i:i + len(run)] == run for i in range(len(values) - len(run) + 1))


# --- build_cmd ------------------------------------------------------------------

@pytest.mark.parametrize(("rights", "expected"), [
    ("none", ["--restricted", "--tools", ""]),
    ("read", ["--restricted", "--tools", "Read,Glob,Grep"]),
    ("write", ["--restricted", "--tools", "Read,Glob,Grep,Edit,Write",
               "--permission-mode", "acceptEdits"]),
    ("full", ["--dangerously-skip-permissions"]),
])
def test_claude_rights(providers, rights, expected):
    cmd = config.build_cmd("claude", "do it", rights=rights)

    assert contains_run(args_before_prompt(cmd), expected)


def test_rights_win_over_skip_permissions(providers):
    cmd = config.build_cmd("claude", "do it", skip_permissions=True, rights="read")

    assert "--dangerously-skip-permissions" not in cmd
    assert "--restricted" in cmd


def test_guard_rides_along_only_for_acting_rights(providers):
    def has_guard(rights):
        return "--settings" in config.build_cmd("claude", "do it", rights=rights)

    assert has_guard("write") and has_guard("full")
    assert not has_guard("read") and not has_guard("none")


def test_codex_narrow_rights_require_external_tool_confinement(providers):
    full = config.build_cmd("codex", "do it", rights="full")
    assert "--dangerously-bypass-approvals-and-sandbox" in full
    for unsupported in ("none", "read", "write"):
        for session in (None, "s-1"):
            with pytest.raises(config.RightsUnsupported):
                config.build_cmd("codex", "do it", rights=unsupported, session_id=session)


def test_unknown_provider_only_runs_as_configured(providers):
    assert config.build_cmd("agy", "do it", rights="full") == [
        "agy", "-p", "do it", "--dangerously-skip-permissions"]
    for narrow in ("none", "read", "write"):
        with pytest.raises(config.RightsUnsupported):
            config.build_cmd("agy", "do it", rights=narrow)


def test_declared_unknown_wrapper_map_is_not_confinement_proof(providers):
    for rights in ("none", "read", "write"):
        with pytest.raises(config.RightsUnsupported):
            config.build_cmd("declared", "do it", rights=rights)


# --- worker: fail closed ------------------------------------------------------------

def test_worker_refuses_rights_the_provider_cannot_honour(isolated_db, providers, monkeypatch):
    monkeypatch.setattr(worker.OwnedProcess, "start",
                        lambda *a, **k: pytest.fail("no process may start"))
    task = isolated_db.create_task(TaskCreate(prompt="look", provider="agy", rights="read"))

    worker.execute_task(isolated_db.get_next_runnable())

    failed = isolated_db.get_task(task.id)
    assert failed.status is TaskStatus.FAILED
    assert "Права «read»" in failed.error


def test_worker_refuses_to_confine_an_open_herdr_session(isolated_db, providers, monkeypatch):
    import promptpilot.herdr_exec as herdr_exec
    monkeypatch.setattr(herdr_exec, "run_in_herdr",
                        lambda *a, **k: pytest.fail("no herdr call may happen"))
    task = isolated_db.create_task(TaskCreate(
        prompt="look", provider="herdr-session", herdr_target="w1:p1", rights="read"))

    worker.execute_task(isolated_db.get_next_runnable())

    assert isolated_db.get_task(task.id).status is TaskStatus.FAILED


def test_verdict_repair_runs_without_tools(isolated_db, providers, monkeypatch):
    seen = []
    monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **k: seen.append(cmd) or
                        SimpleNamespace(stdout="ИТОГ: ГОТОВО", returncode=0))
    task = SimpleNamespace(model=None, effort=None)

    worker._repair_verdict(task, "claude", config.load_providers()["claude"], "report", ".")

    assert seen and "--tools" in seen[0] and "" in seen[0]


def test_verdict_repair_cannot_widen_an_existing_codex_read_task(isolated_db, providers, monkeypatch):
    seen = []
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: seen.append(a))
    task = SimpleNamespace(model=None, effort=None, rights="read")
    assert worker._repair_verdict(task, "codex", config.load_providers()["codex"], "report", ".") == ""
    assert seen == []  # No permissive fallback when the narrow contract is unavailable.


def test_recurring_task_keeps_its_rights(isolated_db, providers):
    first = isolated_db.create_task(TaskCreate(prompt="nightly", recurrence="6h", rights="read"))
    running = isolated_db.get_next_runnable()
    isolated_db.mark_completed(running.id, "done")

    worker._recur_after_run(isolated_db.get_task(first.id))

    upcoming = [task for task in isolated_db.list_tasks()
                if task.id != first.id and task.series_id == first.series_id]
    assert [task.rights for task in upcoming] == ["read"]


# --- API and CLI --------------------------------------------------------------------

def call(method, path, **kwargs):
    async def _run():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(_run())


def test_api_refuses_unsupported_rights_and_lists_supported(isolated_db, providers):
    refused = call("POST", "/api/tasks", json={"prompt": "x", "provider": "agy", "rights": "read"})
    accepted = call("POST", "/api/tasks", json={"prompt": "x", "provider": "claude", "rights": "read"})
    listed = call("GET", "/api/providers").json()

    assert refused.status_code == 400
    assert accepted.status_code == 201 and accepted.json()["rights"] == "read"
    assert listed["claude"]["rights"] == ["none", "read", "write", "full"]
    assert listed["agy"]["rights"] == ["full"]
    assert listed["codex"]["rights"] == ["full"]


def test_cli_add_with_rights(isolated_db, providers):
    runner = CliRunner()

    refused = runner.invoke(cli, ["add", "look", "-c", "agy", "--rights", "read"])
    added = runner.invoke(cli, ["add", "look", "-c", "claude", "--rights", "read"])

    assert refused.exit_code != 0 and "не умеет" in refused.output
    assert added.exit_code == 0
    assert isolated_db.list_tasks()[0].rights == "read"


# --- workflow roles -----------------------------------------------------------------


def test_planner_dispatch_preserves_configured_rights(isolated_db, providers):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="planner-rights", objective="plan", repository_path=str(providers),
        candidate_branch="main", config={"planning": {"enabled": True},
        "roles": {"planner": {"provider": "claude", "rights": "none"}}}))
    response = call("POST", f"/api/workflows/{workflow.id}/plan/dispatch", json={"expected_version": 0})
    assert response.status_code == 200, response.text
    task = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert task.rights == "none" and not task.skip_permissions

def test_workflow_roles_carry_rights_and_reviewer_cannot_act(isolated_db, providers):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="rights", objective="x", repository_path=str(providers),
        candidate_branch="main",
        config={"schema_version": 1,
                "automation": {"enabled": True},
                "roles": {"executor": {"provider": "claude", "rights": "write"},
                          "reviewer": {"provider": "claude", "rights": "read"}}}))
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))

    started = workflows.advance_workflow(workflow.id)

    executor_task = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert executor_task.rights == "write"
    with pytest.raises(isolated_db.WorkflowConflictError, match="full rights"):
        workflows.dispatch_task(workflow.id, WorkflowTaskDispatch(
            expected_version=started.state_version, role=WorkflowRole.REVIEWER,
            prompt="review", rights="full"))


def test_claude_none_rights_really_pass_an_empty_tools_list(providers):
    """`--tools ""` must reach the CLI as an empty argument, not vanish."""
    cmd = config.build_cmd("claude", "do it", rights="none")
    rendered = subprocess.list2cmdline(cmd)

    assert '--tools ""' in rendered


@pytest.mark.parametrize("session_id", [None, "session-1"])
@pytest.mark.parametrize("template", [
    "codex exec --dangerously-bypass-approvals-and-sandbox {prompt}",
    "codex exec --yolo {prompt}",
    "codex exec -c sandbox_mode=\\\"danger-full-access\\\" {prompt}",
    "codex exec --config=sandbox_mode=\\\"danger-full-access\\\" {prompt}",
    "codex exec --sandbox danger-full-access {prompt}",
    "codex exec -sdanger-full-access {prompt}",
    "codex --profile wide exec {prompt}",
    "codex exec -- {prompt}",
    "claude --dangerously-skip-permissions -p {prompt}",
    "claude --permission-mode=bypassPermissions -p {prompt}",
    "claude --tools Bash,Read -p {prompt}",
    "claude --settings unsafe.json -p {prompt}",
    "claude -- {prompt}",
])
def test_narrow_rights_reject_conflicting_templates_for_exec_and_resume(providers, monkeypatch, template, session_id):
    monkeypatch.setattr(config, "load_providers", lambda: {"custom": {"cmd": template}})
    with pytest.raises(config.RightsUnsupported):
        config.build_cmd("custom", "do it", rights="read", session_id=session_id, skip_permissions=True)


@pytest.mark.parametrize("template", ["python wrapper.py {prompt}", "sh -c 'codex exec {prompt}'", "codex-wrapper exec {prompt}"])
def test_kind_does_not_make_a_wrapper_restricted(providers, monkeypatch, template):
    monkeypatch.setattr(config, "load_providers", lambda: {"wrapper": {"cmd": template, "kind": "codex"}})
    with pytest.raises(config.RightsUnsupported):
        config.build_cmd("wrapper", "do it", rights="read")


def test_claude_narrow_rights_remove_mcp_tools(providers):
    for rights in ("none", "read", "write"):
        assert contains_run(config.build_cmd("claude", "x", rights=rights), ["--disallowedTools", "mcp__*"])


def test_conflicting_template_is_refused_by_api_and_worker_before_spawn(isolated_db, providers, monkeypatch):
    custom = {"wide": {"cmd": "codex exec --dangerously-bypass-approvals-and-sandbox {prompt}"}}
    monkeypatch.setattr(config, "load_providers", lambda: custom)
    monkeypatch.setattr(api, "load_providers", lambda: custom)
    monkeypatch.setattr(worker, "load_providers", lambda: custom)
    refused = call("POST", "/api/tasks", json={"prompt": "x", "provider": "wide", "rights": "read"})
    assert refused.status_code == 400
    monkeypatch.setattr(worker.OwnedProcess, "start", lambda *a, **kw: pytest.fail("must refuse before spawn"))
    task = isolated_db.create_task(TaskCreate(prompt="x", provider="wide", rights="read"))
    worker.execute_task(isolated_db.get_next_runnable())
    assert isolated_db.get_task(task.id).status is TaskStatus.FAILED
