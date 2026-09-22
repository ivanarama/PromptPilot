"""Тесты epf_jobs: связь PP-задачи с проектом 1С-доработки."""

import pytest

from promptpilot.models import TaskCreate

pytestmark = pytest.mark.usefixtures("isolated_db")


def _make_task():
    from promptpilot import db
    return db.create_task(TaskCreate(prompt="доработай обработку"))


class TestEpfJobs:
    def test_create_and_get_by_task(self):
        from promptpilot import db
        task = _make_task()
        job_id = db.create_epf_job(
            task_id=task.id, project_dir="C:/prj/Обработка",
            original_name="Обработка.epf", base_key="stub",
            base_label="🧩 Авто (Stub-DB)", chat_id=42,
        )
        job = db.get_epf_job_by_task(task.id)
        assert job is not None
        assert job["id"] == job_id
        assert job["task_id"] == task.id
        assert job["status"] == "queued"
        assert job["chat_id"] == 42

    def test_get_epf_job_by_id_and_missing(self):
        from promptpilot import db
        task = _make_task()
        job_id = db.create_epf_job(
            task_id=task.id, project_dir="/p", original_name="x.epf",
            base_key="ut11", base_label="УТ", chat_id=None,
        )
        assert db.get_epf_job(job_id)["base_key"] == "ut11"
        assert db.get_epf_job(999999) is None
        assert db.get_epf_job_by_task(999999) is None

    def test_update_fields(self):
        from promptpilot import db
        task = _make_task()
        job_id = db.create_epf_job(
            task_id=task.id, project_dir="/p", original_name="x.epf",
            base_key="stub", base_label="stub", chat_id=1,
        )
        db.update_epf_job(job_id, {
            "status": "built", "epf_path": "/p/build/x.epf",
            "base_key": "ut11",  # недопустимое поле "hacker" игнорируется
        })
        job = db.get_epf_job(job_id)
        assert job["status"] == "built"
        assert job["epf_path"] == "/p/build/x.epf"
        assert job["base_key"] == "ut11"
        assert "updated_at" in job and "created_at" in job

    def test_update_noop_does_not_touch_row(self):
        from promptpilot import db
        task = _make_task()
        job_id = db.create_epf_job(
            task_id=task.id, project_dir="/p", original_name="x.epf",
            base_key="stub", base_label="stub", chat_id=1,
        )
        before = db.get_epf_job(job_id)
        db.update_epf_job(job_id, {"hacker": "yes"})
        assert db.get_epf_job(job_id) == before

    def test_multiple_jobs_latest_wins(self):
        from promptpilot import db
        task = _make_task()
        db.create_epf_job(task_id=task.id, project_dir="/p1",
                          original_name="a.epf", base_key="stub",
                          base_label="s", chat_id=1)
        db.create_epf_job(task_id=task.id, project_dir="/p2",
                          original_name="b.epf", base_key="ut11",
                          base_label="УТ", chat_id=1)
        assert db.get_epf_job_by_task(task.id)["project_dir"] == "/p2"
