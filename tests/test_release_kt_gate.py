"""Гейт релиза «Сказок Королевства»: разбор вывода Godot, чистый checkout и версия APK."""

import importlib.util
import pathlib
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "inbox" / "release_kt.py"


@pytest.fixture(scope="module")
def release_kt():
    spec = importlib.util.spec_from_file_location("release_kt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_errors_are_fatal_even_when_suite_printed_passed(release_kt):
    # Ровно так выглядел прогон, который гейт считал успешным
    out = ("SCRIPT ERROR: Invalid access to property or key 'icon_path' on a base object of type 'Dictionary'.\n"
           "   ALL 37 TEST SUITES PASSED FLAWLESSLY!  ")
    assert release_kt.find_fatal_lines(out) == [out.splitlines()[0]]


def test_parse_errors_and_failed_result_are_fatal(release_kt):
    out = ('SCRIPT ERROR: Parse Error: Identifier "SettingsManager" not declared in the current scope.\n'
           "[TEST] RESULT: FAILED (провалов проверок: 1, ошибок скриптов: 0)\n"
           "[TEST] TIMEOUT: прогон не завершился за 120 с")
    assert len(release_kt.find_fatal_lines(out)) == 3


def test_clean_run_has_no_fatal_lines(release_kt):
    out = ("[TEST] 1. Testing Audio & Music...\n"
           "   ALL 42 TEST SUITES PASSED FLAWLESSLY!  \n"
           "ERROR: 3 resources still in use at exit (run with --verbose for details).")
    assert release_kt.find_fatal_lines(out) == []


def test_run_reports_timeout_instead_of_raising(release_kt):
    code, out = release_kt.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert code == 124
    assert "таймаут" in out


def test_uncommitted_game_files_lists_new_code_but_ignores_other_files(release_kt, tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "core" / "settings_manager.gd").write_text("extends Node\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    assert release_kt.uncommitted_game_files(tmp_path) == ["src/core/settings_manager.gd"]


def test_bump_export_presets_sets_name_and_increments_code(release_kt):
    text = 'version/code=7\nversion/name="1.0"\nlauncher_icons/main_192=""\n'
    bumped = release_kt.bump_export_presets(text, "0.1.1")
    assert "version/code=8" in bumped
    assert 'version/name="0.1.1"' in bumped
    assert 'launcher_icons/main_192=""' in bumped
