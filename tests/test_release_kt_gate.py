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


@pytest.fixture
def release_repo(release_kt, tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True, encoding="utf8").strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/game.gd").write_text("extends Node\n", encoding="utf8")
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf8")
    (tmp_path / "export_presets.cfg").write_text('version/code=1\nversion/name="0.1.0"\n', encoding="utf8")
    (tmp_path / ".gitignore").write_text("builds/\n", encoding="utf8")
    git("add", ".")
    git("commit", "-qm", "initial")
    monkeypatch.setattr(release_kt, "GAME", tmp_path)
    monkeypatch.setattr(release_kt, "GODOT", pathlib.Path("fake-godot"))
    original = release_kt.run
    calls = []
    def transport(cmd, cwd=None, timeout=600):
        calls.append((cmd, cwd))
        if cmd[0] == "fake-godot":
            if "--export-release" in cmd:
                pathlib.Path(cmd[-1]).write_bytes(b"APK from checked tree")
            return 0, "PASSED"
        if cmd[:2] == ["git", "push"] or cmd[0] == "gh":
            return 0, "isolated: no network"
        return original(cmd, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(release_kt, "run", transport)
    return tmp_path, git, calls


@pytest.mark.parametrize("when", ["before_bump", "before_publish"])
def test_gate_rejects_staged_unchecked_source(release_kt, release_repo, when):
    root, git, calls = release_repo
    proof = release_kt.gate()
    if when == "before_publish":
        version = release_kt.bump(False, proof)
    (root / "src/game.gd").write_text("BROKEN AFTER GATE\n", encoding="utf8")
    git("add", "src/game.gd")
    with pytest.raises(SystemExit, match="посторонние"):
        if when == "before_bump":
            release_kt.bump(False, proof)
        else:
            release_kt.publish(version, False, False, proof=proof)
    assert not git("tag")
    assert not any(cmd[:2] == ["git", "push"] for cmd, _ in calls)


@pytest.mark.parametrize("when", ["before_bump", "before_publish"])
def test_gate_rejects_moved_head(release_kt, release_repo, when):
    _, git, calls = release_repo
    proof = release_kt.gate()
    version = release_kt.bump(False, proof) if when == "before_publish" else None
    git("commit", "--allow-empty", "-qm", "head moved")
    with pytest.raises(SystemExit, match="HEAD изменился"):
        if when == "before_bump":
            release_kt.bump(False, proof)
        else:
            release_kt.publish(version, False, False, proof=proof)
    assert not any(cmd[:2] == ["git", "push"] for cmd, _ in calls)


def test_release_without_export_ignores_old_apk(release_kt, release_repo):
    root, git, calls = release_repo
    (root / "builds").mkdir()
    (root / "builds/fairytale_rpg.apk").write_bytes(b"stale APK")
    proof = release_kt.gate()
    version = release_kt.bump(False, proof)
    release_kt.publish(version, False, True, proof=proof)
    gh_call = next(cmd for cmd, _ in calls if cmd[:3] == ["gh", "release", "create"])
    assert "--verify-tag" in gh_call and "--verify" not in gh_call
    assert not any(arg.endswith(".apk") for arg in gh_call)
    assert git("rev-parse", "v0.1.1") == git("rev-parse", "HEAD")
    assert git("show", "v0.1.1:src/game.gd") == "extends Node"


def test_export_uses_tested_versioned_checkout(release_kt, release_repo):
    root, git, calls = release_repo
    proof = release_kt.gate()
    version = release_kt.bump(False, proof)
    release_kt.publish(version, True, True, proof=proof)
    export = next(cmd for cmd, _ in calls if "--export-release" in cmd)
    checkout = export[export.index("--path") + 1]
    assert pathlib.Path(checkout) != root
    tests = [cmd for cmd, _ in calls if cmd[0] == "fake-godot" and "res://tests/test_runner.tscn" in cmd]
    assert tests[-1][tests[-1].index("--path") + 1] == checkout
    push = next(cmd for cmd, _ in calls if cmd[:2] == ["git", "push"])
    assert "--atomic" in push
    assert git("rev-parse", "HEAD") + ":refs/heads/main" in push
    assert git("show", "HEAD:VERSION") == "0.1.1"
    assert 'version/name="0.1.1"' in git("show", "HEAD:export_presets.cfg")
    assert list((root / "builds").glob("fairytale_rpg-0.1.1-*.apk"))


def test_final_commit_gate_failure_never_tags_or_pushes(release_kt, release_repo, monkeypatch):
    _, git, calls = release_repo
    proof = release_kt.gate()
    version = release_kt.bump(False, proof)
    original = release_kt.run
    def fail_final(cmd, cwd=None, timeout=600):
        if cmd[0] == "fake-godot":
            return 0, "SCRIPT ERROR: failed\nPASSED"
        return original(cmd, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(release_kt, "run", fail_final)
    with pytest.raises(SystemExit, match="Godot не прошёл"):
        release_kt.publish(version, False, True, proof=proof)
    assert not git("tag")
    assert not any(cmd[:2] == ["git", "push"] or cmd[0] == "gh" for cmd, _ in calls)
