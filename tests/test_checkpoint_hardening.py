"""Дожимка ревью PR #172: барьер чекпоинта и доказательная база рана.

Три дыры, закрытые этим файлом:
1. git add -- <файлы> + git commit без pathspec: в коммит чекпоинта
   попадает ВСЁ подготовленное (заранее staged чужое) — коммит обязан
   включать только разрешённые файлы независимо от индекса.
2. Переход следующей стадии в queued писался в БД ДО чекпоинта:
   параллельный тик запускал исполнителя, пока коммит ещё идёт; при сбое
   record_review возвращал устаревший статус; resume не перепроверял
   чекпоинт. Теперь переход доступен только после успешного барьера.
3. Снимок брался при диспатче из working_dir, а worktree создаётся
   воркером позже: уход ветки между ними приписывался исполнителю.
   Теперь воркер пишет снимок фактического старта рана (run.snapshot);
   при неизвестной базе (снимка нет) авто-PASS запрещён даже с
   allow_no_diff — только человеку/перезапуску.
"""

import subprocess
import threading

from promptpilot import workflows
from promptpilot.models import (
    ReviewVerdict,
    WorkflowCreate,
    WorkflowHumanInput,
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
    WorkflowReviewDecision,
    WorkflowStartRequest,
)

REVIEWER_PASS = (
    "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\nИТОГ: ГОТОВО — аудит завершён"
)
REVIEWER_DEFER = (
    "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: HUMAN_REQUIRED\nИТОГ: нужен человек"
)

STAGES = [
    {"code": "S1", "title": "Стадия 1", "objective": "o",
     "stage_type": "integration", "allowed_paths": ["app.py"],
     "deliverables": []},
    {"code": "S2", "title": "Стадия 2", "objective": "o",
     "stage_type": "integration", "allowed_paths": ["app.py"],
     "deliverables": []},
]


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
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return repo


def _wf(isolated_db, repo, slug, checkpoint=True):
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="барьер чекпоинта",
        repository_path=str(repo),
        candidate_branch="feature/hard",
        config={
            "planning": {"enabled": True, "require_approval": True},
            "automation": {"enabled": True, "stage_checkpoint": checkpoint},
            "roles": {"executor": {"provider": None},
                      "reviewer": {"provider": None}},
            "gate": {"enabled": False},
            "limits": {"max_rounds": 6},
        },
    ))


def _start(isolated_db, wf):
    import json as _json
    workflows.dispatch_planner(wf.id, WorkflowPlanDispatch(expected_version=0))
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(
        task.id,
        "WORKFLOW_PLAN_JSON_BEGIN\n"
        + _json.dumps({"stages": STAGES}) + "\nWORKFLOW_PLAN_JSON_END\n"
        + "ИТОГ: ГОТОВО", exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    workflows.approve_plan(wf.id, WorkflowPlanApproval(
        expected_version=isolated_db.get_workflow(wf.id).state_version))
    workflows.advance_workflow(wf.id)


def _claim_and_finish(isolated_db, result, write=None):
    """Завершить текущий ран; write() — работа «агента» внутри рана."""
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    if write:
        write()
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def _events(isolated_db, wf):
    return list(isolated_db.list_workflow_events(wf.id, limit=200))


def _types(isolated_db, wf):
    return [e.event_type for e in _events(isolated_db, wf)]


def _commit_files(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True).stdout.split()


def test_pre_staged_foreign_file_stays_out_of_checkpoint_commit(
        isolated_db, tmp_path):
    """Репро 1: чужой файл, ЗАРАНЕЕ ПОДГОТОВЛЕННЫЙ в индекс (staged), не
    должен попадать в коммит чекпоинта — коммит включает только разрешённые
    файлы этапа независимо от содержимого индекса."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "hard-staged")
    _start(isolated_db, wf)
    _claim_and_finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2 by executor", encoding="utf-8"))
    # чужой актор подготовил свой файл в индекс ДО ревью
    (repo / "foreign.txt").write_text("чужой staged", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", "foreign.txt"],
                   check=True, capture_output=True)

    _claim_and_finish(isolated_db, REVIEWER_PASS)

    assert _commit_files(repo) == ["app.py"], (
        "в коммите чекпоинта только доказанные файлы этапа, "
        "не весь подготовленный индекс"
    )
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True).stdout
    assert "foreign.txt" in status, "чужой staged-файл не должен быть потерян"
    assert "foreign.txt" not in subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True).stdout


def test_record_review_returns_awaiting_human_when_checkpoint_fails(
        isolated_db, tmp_path, monkeypatch):
    """Репро 2b: при сбое чекпоинта record_review обязан возвращать
    фактический статус (awaiting_human), а не устаревший queued/completed."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "hard-return")
    _start(isolated_db, wf)
    _claim_and_finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2", encoding="utf-8"))
    # вердикт HUMAN_REQUIRED оставляет решение человеку без автоприменения
    _claim_and_finish(isolated_db, REVIEWER_DEFER)

    monkeypatch.setattr(
        workflows, "_stage_checkpoint_commit",
        lambda *a, **k: False)  # чекпоинт стабильно падает
    current = isolated_db.get_workflow(wf.id)
    returned = workflows.record_review(wf.id, WorkflowReviewDecision(
        expected_version=current.state_version,
        verdict=ReviewVerdict.PASS,
        summary="человек принял",
    ))
    assert returned.status.value == "awaiting_human", (
        "возвращать надо честный статус после барьера, а не объект из "
        "транзакции до чекпоинта"
    )
    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"
    assert "stage.advanced" not in _types(isolated_db, wf)


def test_next_stage_not_claimable_during_slow_checkpoint(
        isolated_db, tmp_path, monkeypatch):
    """Репро 2a: пока чекпоинт идёт (медленный git), параллельный тик
    контроллера не должен видеть следующую стадию доступной — переход
    становится виден только после успешного барьера."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "hard-barrier")
    _start(isolated_db, wf)
    _claim_and_finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2", encoding="utf-8"))

    started = threading.Event()
    release = threading.Event()
    real_commit = workflows._stage_checkpoint_commit

    def slow_commit(*args, **kwargs):
        started.set()
        release.wait(30)
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(workflows, "_stage_checkpoint_commit", slow_commit)
    errors = []

    def reviewer_side():
        try:
            _claim_and_finish(isolated_db, REVIEWER_PASS)
        except Exception as exc:  # noqa: BLE001 — наружу только фейл теста
            errors.append(exc)

    reviewer = threading.Thread(target=reviewer_side)
    reviewer.start()
    try:
        assert started.wait(30), "чекпоинт должен стартовать при PASS"
        # параллельный тик: статус и очередь глазами другого процесса
        assert isolated_db.get_workflow(wf.id).status.value == (
            "awaiting_human"), "миссия не должна уходить в очередь мимо барьера"
        assert "stage.advanced" not in _types(isolated_db, wf)
        assert isolated_db.get_next_runnable() is None, (
            "исполнитель следующей стадии не должен быть доступен, "
            "пока чекпоинт не завершён"
        )
    finally:
        release.set()
    reviewer.join(60)
    assert not errors, errors

    events = _events(isolated_db, wf)
    checkpoint_seq = [e.seq for e in events if e.event_type == "stage.checkpoint"]
    advanced_seq = [e.seq for e in events if e.event_type == "stage.advanced"]
    assert checkpoint_seq and advanced_seq
    assert max(checkpoint_seq) < min(advanced_seq), (
        "переход следующей стадии пишется только после успешного чекпоинта"
    )
    assert isolated_db.get_workflow(wf.id).status.value in (
        "queued", "executing")


def test_checkpoint_failure_then_resume_retries_barrier(
        isolated_db, tmp_path, monkeypatch):
    """Репро 2c: после сбоя чекпоинта resume обязан ПОВТОРИТЬ барьер и
    только после успеха перевести следующую стадию — а не переоткрыть
    раунд заново мимо чекпоинта."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "hard-resume")
    _start(isolated_db, wf)
    _claim_and_finish(isolated_db, "executor", write=lambda: (
        repo / "app.py").write_text("v2", encoding="utf-8"))

    real_commit = workflows._stage_checkpoint_commit
    calls = []

    def flaky_commit(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return False  # первый чекпоинт падает
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(workflows, "_stage_checkpoint_commit", flaky_commit)
    _claim_and_finish(isolated_db, REVIEWER_PASS)
    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"
    assert "stage.advanced" not in _types(isolated_db, wf)
    monkeypatch.setattr(workflows, "_stage_checkpoint_commit", real_commit)

    current = isolated_db.get_workflow(wf.id)
    workflows.human_input(wf.id, WorkflowHumanInput(
        expected_version=current.state_version,
        text="починил git, продолжай",
        resume=True,
    ))

    assert len(calls) >= 1, "resume должен повторить чекпоинт"
    after = isolated_db.get_workflow(wf.id)
    assert after.status.value in ("queued", "executing"), (
        "после успешного повтора барьера миссия продолжает следующую стадию"
    )
    types = _types(isolated_db, wf)
    assert "stage.checkpoint" in types, "успешный повтор зафиксирован"
    assert "stage.advanced" in types
    seq = {e.event_type: e.seq for e in _events(isolated_db, wf)}
    assert seq["stage.checkpoint"] < seq["stage.advanced"]


def test_branch_move_between_dispatch_and_run_start_not_attributed(
        isolated_db, tmp_path):
    """Репро 3: ветка ушла между диспатчем и фактическим стартом рана —
    уход ветки не считается работой исполнителя: снимок пишет воркер на
    старте (run.snapshot), атрибуция и чекпоинт — только по этому снимку."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "hard-window")
    _start(isolated_db, wf)

    def executor_run():
        task = isolated_db.get_next_runnable()
        assert task is not None
        workflows.sync_task(task.id)
        # пока задача стоит в очереди, ветка ушла вперёд (чужой коммит)
        (repo / "bump.txt").write_text("двинулась ветка", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "branch move"],
                       check=True, capture_output=True)
        # воркер создал worktree и снял фактическое дерево старта рана
        workflows.record_run_snapshot(task.id, str(repo))
        # и только теперь исполнитель делает СВОЮ работу
        (repo / "app.py").write_text("v2 by executor", encoding="utf-8")
        isolated_db.mark_completed(task.id, "executor", exit_code=0)
        isolated_db.set_verdict(task.id, "ГОТОВО")
        workflows.sync_task(task.id)
        workflows.advance_linked_task(task.id)

    executor_run()
    _claim_and_finish(isolated_db, REVIEWER_PASS)

    assert _commit_files(repo) == ["app.py"], (
        "чекпоинт коммитит только работу исполнителя, не чужой сдвиг ветки"
    )
    current = isolated_db.get_workflow(wf.id)
    assert current.status.value in ("queued", "executing"), (
        "доказанная атрибуция не должна блокировать приём стадии"
    )


def test_allow_no_diff_cannot_override_unknown_base(
        isolated_db, tmp_path, monkeypatch):
    """Репро 3b: база рана неизвестна (снимка до запуска нет) —
    авто-PASS запрещён даже с явным allow_no_diff: подтверждение
    «принимаю без изменений» не может подтверждать неизвестное."""
    repo = _git_repo(tmp_path)
    wf = isolated_db.create_workflow(WorkflowCreate(
        slug="hard-unknown",
        objective="неизвестная база рана",
        repository_path=str(repo),
        candidate_branch="feature/hard6",
        config={
            "automation": {"enabled": True},
            "roles": {"executor": {"provider": None},
                      "reviewer": {"provider": None}},
            "gate": {"enabled": False},
            "limits": {"max_rounds": 5},
        },
    ))
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)

    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    (repo / "app.py").write_text("v2", encoding="utf-8")
    isolated_db.mark_completed(task.id, "executor", exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    _claim_and_finish(isolated_db, REVIEWER_DEFER)

    # легаси-раунд: ни диспатч-снимка, ни снимка старта (события append-only
    # — отключаем поиск снимков патчем, как в test_review1)
    for name in ("_executor_before_snapshot", "_executor_dispatch_snapshot"):
        monkeypatch.setattr(workflows, name, lambda *a, **k: None,
                            raising=False)

    current = isolated_db.get_workflow(wf.id)
    returned = workflows.record_review(wf.id, WorkflowReviewDecision(
        expected_version=current.state_version,
        verdict=ReviewVerdict.PASS,
        summary="человек принимает без изменений",
        allow_no_diff=True,
    ))
    assert returned.status.value == "awaiting_human", (
        "неизвестная база рана не подтверждается allow_no_diff"
    )
    assert "review.diff_origin_unknown" in _types(isolated_db, wf)
    assert "review.passed" not in _types(isolated_db, wf)
