import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import onebase_merge_checks as checks


def plan(check, packages=(), race=False):
    return argparse.Namespace(check=check, package=list(packages), race=race)


def test_fixed_commands_keep_original_test_limits_and_race():
    assert checks.command(plan("packages", ["./internal/configcheck"], True)) == [
        "go", "test", "-race", "-count=1", "-timeout=10m", "-p=2", "-json", "./internal/configcheck"]
    assert checks.command(plan("build")) == ["go", "build", "./..."]


@pytest.mark.parametrize("requested", [plan("ui"), plan("ui", race=True), plan("trade"), plan("modules"), plan("packages"),
                                      plan("packages", ["./..."]), plan("packages", ["./internal/api"]),
                                      plan("packages", ["-exec"]), plan("vet", race=True),
                                      plan("build", ["./internal/configcheck"])])
def test_unsupported_checks_always_require_fresh(requested):
    with pytest.raises(checks.FreshRequired):
        checks.command(requested)


def completed(reason="scan_in_progress"):
    return {"id": 27, "series_id": 4, "status": "completed", "started_at": "start", "verdict": "ПУСТО",
            "result": "Local checks succeeded\nИТОГ: ПУСТО — нет допуска: " + reason + "\n\n--- Meta ---\nTokens: 1"}


def test_only_different_completed_source_wait_can_supply_a_retry():
    assert checks.origin_eligible(completed(), 29, "start")
    assert not checks.origin_eligible(completed(), 27, "start")
    assert not checks.origin_eligible(completed(), 29, "other attempt")
    for updates in ({"status": "running"}, {"series_id": 7}, {"verdict": "ERR"},
                    {"result": completed()["result"] + "\n--- Meta ---\nextra"},
                    {"result": "ИТОГ: ПУСТО — ожидание подтверждения обновления ветки"}):
        assert not checks.origin_eligible({**completed(), **updates}, 29, "start")
    assert not checks.origin_eligible(completed("unknown"), 29, "start")


def test_signed_identity_includes_origin_and_raw_output():
    args = [b"key", "tree-command-inputs", 27, "start", 42, b"pass", b""]
    valid = checks.signature(*args)
    for i, different in enumerate([b"other", "changed inputs", 28, "restart", 43, b"skip", b"diagnostic"]):
        changed = args.copy()
        changed[i] = different
        assert checks.signature(*changed) != valid


def test_evidence_rechecks_package_terminals_and_retains_skips_for_fresh_run():
    pkg = "github.com/ivantit66/onebase/internal/metadata"
    test = {"Package": pkg, "Test": "TestLoad", "Action": "pass"}
    terminal = {"Package": pkg, "Action": "pass"}
    def raw(events):
        return b"\n".join(json.dumps(e).encode() for e in events)
    checks.verify_packages(raw([test, terminal]), ["./internal/metadata"])
    for events in ([test], [terminal], [test, terminal, terminal], [{**test, "Action": "skip"}, terminal]):
        with pytest.raises(checks.FreshRequired):
            checks.verify_packages(raw(events), ["./internal/metadata"])


def test_module_cache_is_checked_against_checkout_go_sum(tmp_path):
    cache = tmp_path / "cache"
    zhash = cache / "cache/download/example.org/mod/@v/v1.2.3.ziphash"
    zhash.parent.mkdir(parents=True)
    (tmp_path / "go.sum").write_text("example.org/mod v1.2.3 h1:original\n")
    _, absent = checks.module_inputs(tmp_path, cache)
    zhash.write_text("h1:original")
    inputs, present = checks.module_inputs(tmp_path, cache)
    assert inputs == [zhash]
    assert present != absent
    zhash.write_text("h1:replaced")
    with pytest.raises(checks.FreshRequired, match="differs"):
        checks.module_inputs(tmp_path, cache)
    (tmp_path / "go.sum").write_text("../outside v1.2.3 h1:original\n")
    with pytest.raises(checks.FreshRequired, match="module path"):
        checks.module_inputs(tmp_path, cache)


def test_service_and_feature_environments_force_fresh(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "test-fixture-private")
    with pytest.raises(checks.FreshRequired) as error:
        checks.safe_env()
    assert "test-fixture-private" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="private Unix vault permissions")
def test_corrupted_expired_and_unqualified_receipts_rerun(tmp_path, monkeypatch):
    v = checks.Vault(tmp_path / "vault")
    try:
        producer = SimpleNamespace(task_id=27, started_at="first")
        retry = SimpleNamespace(eligible=lambda task, start: (task, start) == (27, "first"))
        v.save("exact", producer, b"all logs", b"stderr")
        assert v.load("exact", retry) == (27, b"all logs", b"stderr")
        assert v.load("other tree", retry) is None
        assert v.load("exact", SimpleNamespace(eligible=lambda *_: False)) is None
        v.db.execute("UPDATE evidence SET stdout=?", (b"forged",))
        assert v.load("exact", retry) is None
        v.save("exact", producer, b"all logs", b"stderr")
        now = checks.time.time()
        monkeypatch.setattr(checks.time, "time", lambda: now + checks.LIFETIME + 2)
        assert v.load("exact", retry) is None
    finally:
        v.db.close()


def test_sandbox_denies_network_writes_and_other_home_contents(tmp_path):
    policy = checks.sandbox_profile([tmp_path / "worktree", tmp_path / ".git/objects"], tmp_path / "scratch")
    assert "(deny network*)" in policy and "(deny file-write*)" in policy
    assert 'subpath "' + str(Path.home()) + '"' not in policy
    assert "(allow file-read-metadata" in policy


def test_real_failure_is_terminal_even_with_sandbox_text_in_test_output(tmp_path):
    raw = tmp_path / "check.stdout"
    errors = tmp_path / "check.stderr"
    raw.write_text('{"Action":"fail","Test":"TestValidation"}\n')
    errors.write_text("operation not permitted\n")
    with pytest.raises(checks.CheckFailed):
        checks.check_exit(1, tmp_path, "check")
    raw.write_text("")
    with pytest.raises(checks.FreshRequired):
        checks.check_exit(1, tmp_path, "check")
    errors.write_text("./source.go:11:3: undefined: missing\n")
    with pytest.raises(checks.CheckFailed):
        checks.check_exit(1, tmp_path, "check")
