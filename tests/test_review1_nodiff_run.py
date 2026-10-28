"""Ревью reliability-v1, п.1: no-diff привязан к конкретному executor run.

Общий git status не доказывает авторство изменений. Теперь снимок дерева
пишется в событие executor.dispatched (HEAD + сигнатуры грязи), а PASS
сравнивает «до/после»: чужая заранее существовавшая грязь не даёт автопасс;
неизвестное происхождение — человеку (review.diff_origin_unknown);
allow_no_diff остаётся явным человеческим подтверждением.
"""

import subprocess

from promptpilot import db, workflows
from promptpilot.models import (
    WorkflowCreate,
    WorkflowStartRequest,
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
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return repo


def _wf(isolated_db, repo, slug):
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="no-diff по run'у",
        repository_path=str(repo),
        candidate_branch="feature/r1",
        config={
            "automation": {"enabled": True},
            "roles": {"executor": {"provider": None},
                      "reviewer": {"provider": None}},
            "gate": {"enabled": False},
            "limits": {"max_rounds": 5},
        },
    ))


def _run_executor_with(write=None):
    """Завершить executor-ран; write() вызывается ПОСЛЕ старта, ДО финала —
    как настоящий агент, меняющий файлы в своём ране."""
    def step(isolated_db):
        task = isolated_db.get_next_runnable()
        assert task is not None, "executor должен быть в очереди"
        workflows.sync_task(task.id)
        if write:
            write()
        isolated_db.mark_completed(task.id, "executor report", exit_code=0)
        isolated_db.set_verdict(task.id, "ГОТОВО")
        workflows.sync_task(task.id)
        workflows.advance_linked_task(task.id)
    return step


def _finish_reviewer(isolated_db):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id, REVIEWER_PASS, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)


def _events(isolated_db, wf):
    return [e.event_type for e in
            isolated_db.list_workflow_events(wf.id, limit=100)]


def test_foreign_preexisting_dirt_does_not_authorize_pass(
        isolated_db, tmp_path):
    """Репро-сценарий: чужой грязный файл существует ДО диспатча, исполнитель
    не меняет ничего → PASS блокируется no-diff, автопасса нет."""
    repo = _git_repo(tmp_path)
    (repo / "someone-else.txt").write_text("чужие заметки", encoding="utf-8")
    wf = _wf(isolated_db, repo, "r1-foreign-dirt")
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)

    _run_executor_with()(isolated_db)
    _finish_reviewer(isolated_db)

    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"
    assert "review.no_diff" in _events(isolated_db, wf)
    assert "review.passed" not in _events(isolated_db, wf)


def test_own_change_during_run_authorizes_pass(isolated_db, tmp_path):
    """Исполнитель меняет файл ВНУТРИ своего рана → PASS проходит."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "r1-own-change")
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)

    _run_executor_with(
        write=lambda: (repo / "app.py").write_text(
            "print('v2 by executor')", encoding="utf-8"),
    )(isolated_db)
    _finish_reviewer(isolated_db)

    assert isolated_db.get_workflow(wf.id).status.value == "completed"
    assert "review.passed" in _events(isolated_db, wf)


def test_foreign_dirt_touched_by_executor_counts(isolated_db, tmp_path):
    """Исполнитель ДОПИСАЛ чужой грязный файл в своём ране → сигнатура
    изменилась относительно снимка → это работа исполнителя, PASS проходит."""
    repo = _git_repo(tmp_path)
    (repo / "notes.txt").write_text("чужое начало", encoding="utf-8")
    wf = _wf(isolated_db, repo, "r1-touched")
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)

    _run_executor_with(
        write=lambda: (repo / "notes.txt").write_text(
            "чужое начало + правка исполнителя", encoding="utf-8"),
    )(isolated_db)
    _finish_reviewer(isolated_db)

    assert isolated_db.get_workflow(wf.id).status.value == "completed"


def test_legacy_round_without_snapshot_requires_human(isolated_db, tmp_path, monkeypatch):
    """Изменения есть, снимка нет (легаси-раунд) → происхождение неизвестно,
    автопасса нет: review.diff_origin_unknown, человек."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "r1-legacy")
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)

    _run_executor_with(
        write=lambda: (repo / "app.py").write_text(
            "print('v2')", encoding="utf-8"),
    )(isolated_db)
    # события append-only: имитируем легаси-раунд отключением поиска снимка
    monkeypatch.setattr(workflows, "_executor_before_snapshot",
                        lambda *a, **k: None)
    _finish_reviewer(isolated_db)

    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"
    assert "review.diff_origin_unknown" in _events(isolated_db, wf)
    assert "review.passed" not in _events(isolated_db, wf)


def test_truncated_snapshot_unknown_origin(isolated_db, tmp_path, monkeypatch):
    """Обрезанный снимок + изменения → происхождение недоказуемо → человек."""
    repo = _git_repo(tmp_path)
    wf = _wf(isolated_db, repo, "r1-truncated")
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)

    _run_executor_with(
        write=lambda: (repo / "app.py").write_text(
            "print('v2')", encoding="utf-8"),
    )(isolated_db)

    def truncated_snapshot(conn, round_id, run_id):
        # monkeypatch уже подменяет поиск снимков — читаем событие напрямую
        row = conn.execute(
            """SELECT payload_json FROM workflow_events
               WHERE round_id = ? AND event_type = 'executor.dispatched'
               ORDER BY seq DESC LIMIT 1""", (round_id,)).fetchone()
        snap = dict(db._json_load(row["payload_json"], {}).get("tree_snapshot") or {})
        snap["truncated"] = True
        return snap

    monkeypatch.setattr(workflows, "_executor_before_snapshot",
                        truncated_snapshot)
    _finish_reviewer(isolated_db)

    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"
    assert "review.diff_origin_unknown" in _events(isolated_db, wf)


def test_allow_no_diff_overrides_proven_empty_diff(isolated_db, tmp_path):
    """Явное человеческое allow_no_diff принимает PASS при ДОКАЗАННО пустом
    diff (база рана известна, сравнение до/после пусто). Неизвестная база
    allow_no_diff не подтверждает — см. test_checkpoint_hardening."""
    from promptpilot.models import ReviewVerdict, WorkflowReviewDecision
    repo = _git_repo(tmp_path)
    (repo / "foreign.txt").write_text("чужое", encoding="utf-8")
    wf = _wf(isolated_db, repo, "r1-allow")
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)
    _run_executor_with()(isolated_db)
    _finish_reviewer(isolated_db)
    assert isolated_db.get_workflow(wf.id).status.value == "awaiting_human"

    current = isolated_db.get_workflow(wf.id)
    approved = workflows.record_review(wf.id, WorkflowReviewDecision(
        expected_version=current.state_version,
        verdict=ReviewVerdict.PASS,
        summary="Человек проверил лично",
        allow_no_diff=True,
    ))
    assert approved.status.value == "completed"


def test_committed_during_run_counts_as_change(isolated_db, tmp_path):
    """Исполнитель закоммитил работу в своём ране (head снятка..HEAD) —
    это его изменения, PASS проходит."""
    repo = _git_repo(tmp_path)

    def commit_as_executor():
        (repo / "app.py").write_text("print('v2 committed')", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "by executor"],
                       check=True, capture_output=True)

    wf = _wf(isolated_db, repo, "r1-committed")
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(wf.id)
    _run_executor_with(write=commit_as_executor)(isolated_db)
    _finish_reviewer(isolated_db)

    assert isolated_db.get_workflow(wf.id).status.value == "completed"
