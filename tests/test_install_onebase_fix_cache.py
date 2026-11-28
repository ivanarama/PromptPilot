"""Exercise the installed commands against fresh HOME/SQLite, without GitHub or a production worker."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "tools/install_onebase_fix_cache.py"


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Station:
    def __init__(self, root):
        self.home = root / "new home"
        self.home.mkdir()
        self.work = self.home / "OneBase"
        self.data = self.home / ".promptpilot"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PP_")}
        self.env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            PP_DATA_DIR=str(self.data),
            PYTHONPATH=str(REPO),
            PYTHONIOENCODING="utf-8",
        )
        self.bin = self.home / ("pp.cmd" if os.name == "nt" else "pp")
        if os.name == "nt":
            self.bin.write_text(f'@"{sys.executable}" "{REPO / "main.py"}" %*\n')
        else:
            self.bin.write_text(
                "#!/bin/sh\nexec " + shlex.join([sys.executable, str(REPO / "main.py")]) + ' "$@"\n'
            )
            self.bin.chmod(0o700)
        self.checker = Path(sys.executable).resolve()
        for skill in ("fix-approved", "review-queue"):
            f = self.work / ".claude/skills" / skill / "SKILL.md"
            f.parent.mkdir(parents=True)
            f.write_text(skill, encoding="utf-8")
        budget = REPO / "docs/examples/onebase-fix-github-budget.json"
        self.bootstrap = [
            "bootstrap",
            "--working-dir",
            self.work,
            "--checker",
            self.checker,
            "--checker-sha256",
            checksum(self.checker),
            "--budget",
            budget,
            "--review-sha256",
            checksum(self.work / ".claude/skills/review-queue/SKILL.md"),
            "--fix-sha256",
            checksum(self.work / ".claude/skills/fix-approved/SKILL.md"),
            "--recurrence",
            "30m",
        ]
        result = self.cli(*self.bootstrap)
        assert (
            result["budget_policy_sha256"]
            == "4594620c770754856de292e6051d02195f829046fedfe6f7d15f3614d876b5be"
        )
        assert result["state"] == "paused"
        self.series = result["series_id"]
        self.build = [
            "prepare",
            "--working-dir",
            self.work,
            "--native",
            self.bin,
            "--native-sha256",
            checksum(self.bin),
            "--budget-policy-sha256",
            result["budget_policy_sha256"],
        ]

    def run(self, cmd, ok=True):
        result = subprocess.run(
            [str(x) for x in cmd], env=self.env, cwd=self.home, capture_output=True, timeout=45
        )
        if ok:
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            return json.loads(result.stdout)
        assert result.returncode != 0, result.stdout
        return result.stderr.decode(errors="replace")

    def cli(self, *args, ok=True):
        return self.run([sys.executable, "-IB", INSTALLER, *args], ok=ok)

    def release(self, *extra):
        rel = self.cli(*self.build, *extra)
        config = Path(rel["config"])
        assert checksum(config) == rel["config_sha256"]
        return ["--config", config, "--config-sha", rel["config_sha256"]]

    def rollback(self, args):
        return self.cli("rollback", "--working-dir", self.work, "--config-sha", args[-1])

    def sql(self, sql, *args):
        with sqlite3.connect(self.data / "promptpilot.db") as conn:
            return conn.execute(sql, args).fetchall()


def test_clean_bootstrap_immutable_install_update_repeat_and_rollback(tmp_path):
    mac = Station(tmp_path)
    assert mac.cli(*mac.bootstrap)["series_id"] == mac.series
    assert mac.series != 2
    original, paused, priority = mac.sql("SELECT prompt,paused,priority FROM task_series")[0]
    assert paused and priority == 1
    # Per-task owner conditions beyond the managed block survive upgrades and rollback.
    tailored = original + "\nLimit this occurrence to its existing owner conditions."
    mac.sql("UPDATE tasks SET prompt=?", tailored)
    args = mac.release()
    assert mac.release() == args
    assert mac.cli("preview", *args)["state"] == "preview"
    assert mac.sql("SELECT prompt FROM task_series")[0][0] == original
    assert mac.cli("apply", *args)["pending"] == [1]
    assert mac.cli("apply", *args)["changed"] is False
    assert mac.cli("verify", *args)["state"] == "verified"
    prompt = mac.sql("SELECT prompt FROM tasks")[0][0]
    assert prompt.startswith(tailored)
    assert prompt.count("<!-- pp:onebase-fix-cache config-sha256=") == 1
    assert "onebase_fix_cache_observe.py" in prompt
    launcher = args[1].with_name("onebase_fix_cache.py")
    report = mac.run([sys.executable, "-IB", launcher, *args, "--report"])
    assert report["observed_tasks"] == 0 and report["full_scan_claims"] == 0
    # A second build may qualify a new binary without changing any running task.
    new_bin = mac.bin.with_name("other-" + mac.bin.name)
    new_bin.write_bytes(mac.bin.read_bytes())
    new_bin.chmod(0o700)
    upgrade = mac.release("--cache-native", new_bin, "--cache-native-sha256", checksum(new_bin))
    assert upgrade != args and mac.cli("apply", *upgrade)["state"] == "applied"
    assert mac.cli("verify", *upgrade)["state"] == "verified"
    new_bin.unlink()
    assert mac.rollback(upgrade)["state"] == "rolled_back"
    assert mac.rollback(upgrade)["state"] == "rolled_back"
    assert mac.cli("verify", *args)["state"] == "verified"
    assert mac.rollback(args)["state"] == "rolled_back"
    assert mac.sql("SELECT prompt,paused FROM task_series") == [(original, 1)]
    assert mac.sql("SELECT prompt FROM tasks") == [(tailored,)]


def test_adopt_scope_and_running_task_fence(tmp_path):
    mac = Station(tmp_path)
    args = mac.release()
    before = mac.sql("SELECT prompt FROM tasks")[0][0]
    legacy = "<!-- onebase-fix-source-binding-20261126 -->\nordinary prior Source\n<!-- /onebase-fix-source-binding-20261126 -->"
    code = "1" * 64
    old_late = (
        "After all local checks and an initial successful task-bound Native Source publication in THIS invocation: "
        f"verify /local/wait_cache.py SHA256 {code} and invoke the normal pinned Python with -I plus that file, "
        "--source-receipt <JSON>, --task <actual Native task ID>, --started-at <actual Native UTC start>, --seconds <1 to 300>. "
        "Only proceed after the ordinary handoff."
    )
    scope = (
        "For the selected owner-approved target, keep its old /local/wait_cache.py and every existing test."
    )
    old_post = (
        "[Ordinary OneBase FIX post-push recovery v1] Preserve record/read/report, "
        "use once 'observe --source-receipt FILE --seconds N' (1..300 within the remaining task and lease budget) "
        "instead of the old passive observer, only for this post-push phase."
    )
    before += "\n" + legacy + "\n" + old_late + "\n" + old_post + "\n" + scope
    mac.sql("UPDATE task_series SET prompt=?", before)
    mac.sql("UPDATE tasks SET prompt=?,status='running',started_at='2030-01-01T01:00:00Z'", before)
    view = mac.cli("preview", *args)
    assert view["state"] == "adoption_required"
    assert "exact --replace-block-sha" in mac.cli("apply", *args, ok=False)
    assert mac.cli("apply", *args, "--replace-block-sha", view["replace_block_sha"])["unchanged_active"] == [
        1
    ]
    current = mac.sql("SELECT prompt FROM task_series")[0][0]
    assert current.count("/local/wait_cache.py") == 1 and scope in current and old_late not in current
    assert "Only proceed after the ordinary handoff." in current
    assert "use once the dedicated post-push observer" in current
    assert "only for this post-push phase." in current
    assert "Preserve record/read/report" in current
    assert mac.sql("SELECT prompt FROM tasks") == [(before,)]
    assert mac.cli("verify", *args)["state"] == "verified"
    assert mac.rollback(args)["state"] == "rolled_back"
    assert mac.sql("SELECT prompt FROM task_series") == [(before,)]


def test_modified_profile_pin_or_pending_drift_fails_closed(tmp_path):
    mac = Station(tmp_path)
    args = mac.release()
    assert mac.cli("apply", *args)["state"] == "applied"
    busted = mac.sql("SELECT prompt FROM tasks")[0][0].replace(
        "At the first permitted task-bound", "Changed task-bound"
    )
    mac.sql("UPDATE tasks SET prompt=?", busted)
    assert "did not inherit" in mac.cli("verify", *args, ok=False)
    profile = mac.data / "pipeline_profiles.json"
    record = json.loads(profile.read_text(encoding="utf-8"))
    record["profiles"]["onebase"]["github_budget"]["costs"]["skill"]["core"] += 1
    profile.write_text(json.dumps(record), encoding="utf-8")
    assert "budget policy differs" in mac.cli("verify", *args, ok=False)
    assert mac.sql("SELECT prompt FROM tasks") == [(busted,)]


def test_existing_v1_manifest_can_be_verified_after_rollback(tmp_path):
    mac = Station(tmp_path)
    new = mac.release()
    cfg = json.loads(new[1].read_bytes())
    cfg["version"] = 1
    del cfg["files"]["source_graphql_cost.py"], cfg["files"]["onebase_fix_lint_source.py"]
    raw = (json.dumps(cfg, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    sign = hashlib.sha256(raw).hexdigest()
    folder = new[1].parent.with_name(sign)
    folder.mkdir()
    conf = folder / "config.json"
    conf.write_bytes(raw)
    for name in cfg["files"]:
        (folder / name).write_bytes(new[1].with_name(name).read_bytes())

    old = ["--config", conf, "--config-sha", sign]
    mac.cli("apply", *old)
    before = mac.sql("SELECT prompt FROM task_series")
    assert "background insights scan yielded" not in before[0][0]
    mac.cli("apply", *new)
    assert "background insights scan yielded" in mac.sql("SELECT prompt FROM task_series")[0][0]
    mac.rollback(new)
    assert mac.cli("verify", *old)["state"] == "verified"
    assert mac.sql("SELECT prompt FROM task_series") == before
