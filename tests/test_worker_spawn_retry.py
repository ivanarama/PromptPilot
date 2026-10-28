# -*- coding: utf-8 -*-
"""U16: конкурентный спавн тяжёлого бинарника (goose.exe 255MB) падал
FileNotFoundError при существующем файле — один повтор через паузу."""

import pytest

from promptpilot import worker as W


def test_existing_exe_gets_one_retry(tmp_path):
    exe = tmp_path / "heavy.exe"
    exe.write_bytes(b"x")
    calls = {"n": 0, "slept": []}

    def build():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError("transient spawn pressure")
        return "TREE"

    out = W._start_owned_with_spawn_retry(
        build, [str(exe)], sleep=lambda s: calls["slept"].append(s),
        exists=lambda p: True)
    assert out == "TREE"
    assert calls["n"] == 2 and calls["slept"] == [W.SPAWN_RETRY_DELAY_S]


def test_missing_exe_fails_immediately(tmp_path):
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        raise FileNotFoundError("no cli")

    with pytest.raises(FileNotFoundError):
        W._start_owned_with_spawn_retry(
            build, [str(tmp_path / "nope.exe")], sleep=lambda s: None,
            exists=lambda p: False)
    assert calls["n"] == 1


def test_retry_also_fails_after_second_attempt(tmp_path):
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        raise FileNotFoundError("still under pressure")

    with pytest.raises(FileNotFoundError):
        W._start_owned_with_spawn_retry(
            build, [str(tmp_path / "heavy.exe")], sleep=lambda s: None,
            exists=lambda p: True)
    assert calls["n"] == 2


def test_cancel_during_backoff_never_starts_second_process(tmp_path):
    calls = []
    cancelled = False
    def build():
        calls.append("spawn")
        raise FileNotFoundError("transient")
    def sleep(seconds):
        nonlocal cancelled
        calls.append("wait")
        cancelled = True
    with pytest.raises(W._SpawnCancelled):
        W._start_owned_with_spawn_retry(build, [str(tmp_path / "exists.exe")],
            sleep=sleep, exists=lambda _: True, cancel_check=lambda: cancelled)
    assert calls == ["spawn", "wait"]


def test_cancelled_worker_task_does_not_retry_provider_spawn(isolated_db, tmp_path, monkeypatch):
    from promptpilot.models import TaskCreate, TaskStatus
    executable = tmp_path / "heavy.exe"
    executable.write_bytes(b"not executed")
    task = isolated_db.create_task(TaskCreate(prompt="x", provider="heavy"))
    monkeypatch.setattr(W, "build_cmd", lambda *a, **kw: [str(executable)])
    calls = []
    def spawn(*a, **kw):
        calls.append("spawn")
        raise FileNotFoundError("transient")
    monkeypatch.setattr(W.OwnedProcess, "start", spawn)
    monkeypatch.setattr(W.time, "sleep", lambda seconds: isolated_db.request_cancel(task.id))
    W.execute_task(isolated_db.get_next_runnable())
    assert isolated_db.get_task(task.id).status is TaskStatus.CANCELLED
    assert calls == ["spawn"]
