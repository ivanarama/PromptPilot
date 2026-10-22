"""Ревью reliability-v1, п.2: безопасный stage checkpoint.

Прежний чекпоинт делал git add -A по всему repository_path: чужая грязь
вне allowed_paths попадала в коммит стадии. Теперь коммитятся только файлы,
доказанно изменённые данным executor run'ом и входящие в allowed_paths;
неудача останавливает миссию до человека (следующая стадия не стартует).
"""

import subprocess

from promptpilot import db, workflows
from promptpilot.models import (
    WorkflowCreate,
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
)

REVIEWER_PASS = (
    "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО — аудит завершён"
)


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "app.py").write_text("v1", encoding="utf-8")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "notes.txt").write_text("стороннее", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return repo


def _wf(isolated_db, repo, slug, checkpoint):
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="безопасный чекпоинт",
        repository_path=str(repo),
        candidate_branch="feature/r2",
        config={
            "planning": {"enabled": True, "require_approval": True},
            "automation": {"enabled": True, "stage_checkpoint": checkpoint},
            "roles": {"executor": {"provider": None},
                      "reviewer": {"provider": None}},
            "gate": {"enabled": False},
            "limits": {"max_rounds": 5},
        },
    ))


STAGE = [{"code": "S1", "title": "Стадия", "objective": "o",
          "stage_type": "integration", "allowed_paths": ["app.py"],
          "deliverables": []}]


def _start(isolated_db, wf):
    import json as _json
    workflows.dispatch_planner(wf.id, WorkflowPlanDispatch(expected_version=0))
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(
        task.id,
        "WORKFLOW_PLAN_JSON_BEGIN\n"
        + _json.dumps({"stages": STAGE}) + "\nWORKFLOW_PLAN_JSON_END\n"
        + "ИТОГ: ГОТОВО", exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    workflows.approve_plan(wf.id, WorkflowPlanApproval(
        expected_version=isolated_db.get_workflow(wf.id).state_version))
    workflows.advance_workflow(wf.id)


def _finish(isolated_db, result, write=None):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    if write:
        write()
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)


def _events(isolated_db, wf):
    return [e.event_type for e in
            isolated_db.list_workflow_events(wf.id, limit=100)]


def test_checkpoint_commits_only_stage_files_not_foreign_dirt(
        isolated_db, tmp_path):
    """Репро №2: чужая грязь вне allowed_paths (vendor/notes.txt) НЕ
    попадает в чекпоинт-коммит; коммитится только правка этапа."""
    repo = _git_repo(tmp_path)
    # чужая грязь ДО старта
    (repo / "vendor" / "notes.txt").write_text(
        "чужие правки, не исполнителя", encoding="utf-8")

    wf = _wf(isolated_db, repo, "r2-foreign", True)
    _start(isolated_db, wf)
    _finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2 by executor", encoding="utf-8"))
    _finish(isolated_db, REVIEWER_PASS)

    files = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True).stdout.split()
    assert files == ["app.py"], f"в чекпоинте только файлы этапа: {files}"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True).stdout
    assert "vendor/notes.txt" in status, "чужая грязь осталась незатронутой"
    assert "stage.checkpoint" in _events(isolated_db, wf)


def test_checkpoint_failure_stops_next_stage(isolated_db, tmp_path, monkeypatch):
    """Неудача чекпоинта останавливает миссию: событие с причиной, миссия в
    awaiting_human, следующая стадия НЕ стартует."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "r2-fail", True)
    _start(isolated_db, wf)
    _finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2", encoding="utf-8"))

    def failing_commit(*args, **kwargs):
        return False  # ревью п.2: любой сбой чекпоинта

    monkeypatch.setattr(workflows, "_stage_checkpoint_commit", failing_commit)
    _finish(isolated_db, REVIEWER_PASS)

    current = isolated_db.get_workflow(wf.id)
    assert current.status.value == "awaiting_human", (
        "неудача чекпоинта должна останавливать миссию до человека"
    )
    types = _events(isolated_db, wf)
    assert "stage.checkpoint_failed" in types
    assert "stage.advanced" not in types, "следующая стадия не стартует"
    assert current.status.value != "completed"


def test_checkpoint_failure_recorded_when_git_add_fails(
        isolated_db, tmp_path, monkeypatch):
    """Реальный сбой git (add падает) — событие с деталью, миссия стоит."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "r2-gitfail", True)
    _start(isolated_db, wf)
    _finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2", encoding="utf-8"))

    import promptpilot.workflows as w
    real_run = subprocess.run

    def broken_run(args, *a, **k):
        # argv: [git, -C, <dir>, add, ...] — команда в args[3]
        if len(args) > 3 and args[3] == "add":
            class R: returncode = 128; stderr = "checkpoint add refused"; stdout = ""
            return R()
        return real_run(args, *a, **k)

    monkeypatch.setattr(w.subprocess, "run", broken_run)
    _finish(isolated_db, REVIEWER_PASS)

    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"
    failed = [e for e in isolated_db.list_workflow_events(wf.id, limit=100)
              if e.event_type == "stage.checkpoint_failed"
              and "error" in (e.payload or {})]
    assert failed, "нужен диагностический event с причиной"
    assert "git add" in (failed[-1].payload or {}).get("error", "")


def test_recovery_after_checkpoint_failure_via_resume(isolated_db, tmp_path):
    """Восстанавливаемость: после ручной починки (коммит) оператор
    резюмит — миссия завершается штатно."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "r2-recover", True)
    _start(isolated_db, wf)
    _finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2", encoding="utf-8"))

    # имитируем «починку руками» до ревью: коммит уже сделан оператором
    subprocess.run(["git", "-C", str(repo), "add", "--", "app.py"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "manual fix"],
                   check=True, capture_output=True)
    _finish(isolated_db, REVIEWER_PASS)
    # чекпоинт увидит changes=[] (всё закоммичено) → успех, миссия завершена
    assert isolated_db.get_workflow(wf.id).status.value == "completed"
