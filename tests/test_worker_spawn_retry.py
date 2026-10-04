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
