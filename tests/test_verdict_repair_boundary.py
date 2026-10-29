"""Exercise verdict restatement through a real claimed worker task."""
import os
import json
import subprocess
import sys

import pytest

from promptpilot import pipeline_insights, worker
from promptpilot.models import TaskCreate


def run_task(isolated_db, tmp_path, monkeypatch, report, *, repair=None,
             repair_exit=0, pipeline=True):
    created = isolated_db.create_task(TaskCreate(
        prompt="Example - REVIEW", recurrence="4h" if pipeline else None,
        provider="verdict-fixture", working_dir=str(tmp_path),
    ))
    task = isolated_db.get_next_runnable()
    assert task.id == created.id
    monkeypatch.setattr(pipeline_insights, "dispatch_gate", lambda _task: None)
    monkeypatch.setattr(pipeline_insights, "execution_route", lambda *_args, **_kwargs: {
        "action": "prompt", "mode": "skill", "prompt": task.prompt,
        "profile_id": "fixture", "queue_id": "review",
    })
    monkeypatch.setattr(worker, "load_providers", lambda: {"verdict-fixture": {}})
    monkeypatch.setattr(worker, "get_provider_env", lambda _: dict(os.environ, PYTHONIOENCODING="utf-8"))
    monkeypatch.setattr(worker, "build_cmd", lambda *_args, **_kwargs: [
        sys.executable, "-c", f"print({report!r})",
    ])
    monkeypatch.setattr(worker, "VERDICT_REPAIR", repair is not None)
    seen = []
    if repair is not None:
        def respond(command, **kwargs):
            seen.append(command)
            return subprocess.CompletedProcess(command, repair_exit, repair, "")

        monkeypatch.setattr(worker.subprocess, "run", respond)
    worker.execute_task(task)
    return isolated_db.get_task(task.id), seen


def test_pipeline_accepts_one_successful_explicit_restatement(isolated_db, tmp_path, monkeypatch):
    settled, calls = run_task(isolated_db, tmp_path, monkeypatch, "Work report without footer",
                             repair="ИТОГ: ГОТОВО — проверено")
    assert len(calls) == 1
    assert settled.status.value == "completed"
    assert settled.verdict == "ГОТОВО"


@pytest.mark.parametrize("repair,code", [
    ("ИТОГ: ГОТОВО", 1),
    ("Quoted old result:\n> ИТОГ: ГОТОВО", 0),
    ("ИТОГ: ГОТОВО\nCurrent work is incomplete", 0),
    ("ИТОГ: УСТАРЕЛО (gate-fallback: invented)", 0),
    (json.dumps({"type": "result", "subtype": "error_during_execution",
                 "is_error": True, "result": "ИТОГ: ГОТОВО"}), 0),
])
def test_pipeline_refuses_failed_quoted_or_nonclosing_restatement(
        isolated_db, tmp_path, monkeypatch, repair, code):
    settled, calls = run_task(isolated_db, tmp_path, monkeypatch, "Work report without footer",
                             repair=repair, repair_exit=code)
    assert len(calls) == 1
    assert settled.status.value == "failed"
    assert settled.verdict != "ГОТОВО"


@pytest.mark.parametrize("report", [
    "Historical result:\n> ИТОГ: ГОТОВО",
    "ИТОГ: ГОТОВО\nBut these steps remain unfinished",
])
def test_regular_worker_does_not_promote_quoted_or_old_verdict(
        isolated_db, tmp_path, monkeypatch, report):
    settled, _ = run_task(isolated_db, tmp_path, monkeypatch, report, pipeline=False)
    assert settled.status.value == "completed"  # Process completion remains independent.
    assert settled.verdict is None


@pytest.mark.parametrize("pipeline", [True, False])
@pytest.mark.parametrize("footer", [True, False])
@pytest.mark.parametrize("flags", [
    {"is_error": True, "subtype": "success"},
    {"is_error": False, "subtype": "error_during_execution"},
])
def test_original_structured_error_cannot_be_repaired_into_success(
        isolated_db, tmp_path, monkeypatch, pipeline, footer, flags):
    text = "Original execution failed" + ("\nИТОГ: ГОТОВО" if footer else "")
    report = json.dumps({"type": "result", "result": text, **flags})
    settled, calls = run_task(isolated_db, tmp_path, monkeypatch, report,
                             repair="ИТОГ: ГОТОВО", pipeline=pipeline)
    assert calls == []
    assert settled.status.value == "failed"
    assert settled.verdict is None
    assert "Original execution failed" in settled.error
