"""Operator-owned Mac release: one binary, pinned procedures/health, recoverable switch.

No GitHub mutations. Requires an already paused, idle worker. Changes only the
seven named OneBase schedules and three PromptPilot LaunchAgents. Series pause
flags, unrelated profiles, lease keys, permissions and CI requirements survive.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import plistlib
import re
import sqlite3
import subprocess
import time
from urllib.request import Request, urlopen

STAGES = {1: "triage-issues", 2: "fix-approved", 3: "review-queue",
          4: "merge-shepherd", 9: "plan-approved", 11: "tail-issues", 13: "review-queue"}
ACTIVE_FIELDS = ["review_backlog", "reviewed_waiting_ship", "merge_candidates", "fix_candidates"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_bytes(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".release-tmp")
    with temporary.open("xb") as stream:
        stream.write(value)
    temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    temporary.replace(path)


def api(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    with urlopen(Request("http://127.0.0.1:8420" + path, data=data,
                         headers={"Content-Type": "application/json"}), timeout=20) as response:
        return json.load(response)


def split_finite_integration_decisions(prompt, stage):
    """Keep existing local operator records opaque, separate from release prose.

    This reads the trusted schedule prompt, never GitHub text. Copying a
    pointer does not validate its file or authorize an operation: MERGE must
    still verify the immutable scope/hash, exact source and all project gates.
    Unknown, conflicting or malformed records stop the upgrade before install.
    """
    first = re.search(r"(?m)^FINITE OWNER INTEGRATION DECISION", prompt)
    if first is None:
        return prompt, ""
    start = first.start() - 2
    if stage != "merge-shepherd" or start < 0 or prompt[start:first.start()] != "\n\n":
        raise ValueError("invalid finite integration decision placement or stage")
    decisions = prompt[start:]
    if len(decisions.encode("utf-8")) > 128 * 1024:
        raise ValueError("finite integration decisions exceed size limit")
    numbers = set()
    for record in re.split(r"\n[ \t]*\n", decisions.strip("\n")):
        record = record.strip("\n")
        if not record:
            continue
        header = re.fullmatch(r"FINITE OWNER INTEGRATION DECISION #([1-9][0-9]*), [^:\r\n]+: [^\r\n]+", record)
        pointers = list(re.finditer(
            r"Fully read (/[^\s,]+), SHA256 ([0-9a-f]{64})\. "
            r"Applies ONLY to source ([0-9a-f]{40}) and descendant main of minimum_base,", record))
        if header is None or len(pointers) != 1:
            raise ValueError("invalid finite integration decision record")
        number = header.group(1)
        path = PurePosixPath(pointers[0].group(1))
        source = pointers[0].group(3)
        if (number in numbers or ".." in path.parts or path.parent.name != "scopes"
                or re.fullmatch(rf"integration-{number}-[0-9]{{8}}-{source[:12]}\.json", path.name) is None):
            raise ValueError("conflicting finite integration scope identity")
        numbers.add(number)
        if len(numbers) > 32:
            raise ValueError("finite integration decisions exceed count limit")
    return prompt[:start], decisions


def rewrite_prompt(prompt, stage, procedures, release_id, health_config=None):
    prompt, decisions = split_finite_integration_decisions(prompt, stage)
    # Replace the canonical occurrence only, not the Codex adapter path. Old
    # operator hotfix annotations are ours, bounded by their exact prefix.
    prompt = prompt.split("\n\nОператорский runtime hotfix:", 1)[0]
    prompt = prompt.split("\n\nОператорский релиз:", 1)[0]
    pattern = rf"(?<!\S)(?:\.claude/skills/{stage}/SKILL\.md|/[^\s]+/{stage}/SKILL\.md)"
    path = str(procedures / ".claude" / "skills" / stage / "SKILL.md")
    prompt, count = re.subn(pattern, lambda _match: path, prompt)
    if count != 1:
        raise ValueError(f"expected exactly one canonical path for {stage}, got {count}")
    # Use the paired project policy too, rather than pairing a new SKILL with an
    # old CLAUDE clause. Existing checkout stays clean for sync-base preflight.
    prompt, count = re.subn(r"(?<!\S)(?:CLAUDE\.md|/[^\s]+/CLAUDE\.md)",
                           lambda _match: str(procedures / "CLAUDE.md"), prompt, count=1)
    if count != 1:
        raise ValueError("expected paired project policy path")
    result = prompt + (
        f"\n\nОператорский релиз: {release_id}. Процедуры и CLAUDE установлены как "
        "единый неизменяемый снимок. Относительные ссылки разрешай от указанного "
        "файла. Адаптер меняет только синтаксис и атрибуцию; каноническую логику "
        "бери из этого снимка. Все проверки identity/proof/ship/CI/HEAD/CAS "
        "сохраняются. Установка не является независимым ревью или разрешением "
        "на merge какого-либо PR.")
    if health_config is not None:
        result += (
            f" Проверку очереди `go run ./tools/pipelinehealth -json` выполняй "
            f"эквивалентной полной командой health_command из {health_config}: "
            "это закреплённый checker данного релиза с его -contract/transport/cache "
            "параметрами. Не подменяй его устаревшей сборкой из рабочего checkout. "
            "Все глобальные owner/allowlist и локальные mutation-гейты обязательны.")
    return result + decisions


def configure(profiles, config, binary, health, procedures, release_id):
    # Unlike the historical base_sync_merge switch, this cannot update a
    # branch or inherit ship from an old HEAD without the full fallback.
    config["ready_owner_merge"] = True
    profile = profiles["profiles"]["onebase"]
    # Queue recovery is a prerequisite of safe dispatch, not an optional
    # dashboard refresh. Keep hard floors and outstanding promises unchanged.
    if profile.get("github_budget", {}).get("costs"):
        profile["github_budget"]["essential_snapshot_headroom"] = True
    # These are reviewed observations, not an automatic title classifier.
    classification_path = Path(__file__).resolve().parents[1] / "docs/onebase-delivery-classifications-20260927.json"
    if classification_path.is_file():
        classification = json.loads(classification_path.read_text(encoding="utf-8"))
        if profile.get("repository") == classification["repository"]:
            configured = profile.setdefault("delivery_classifications", {})
            for number, item in classification["items"].items():
                configured.setdefault(number, item)  # Preserve operator overrides.
    for command in [profile["health_check"]["command"], config["health_command"]]:
        command[0] = str(health)
        if "-contract" in command:
            command[command.index("-contract") + 1] = str(procedures / ".claude/skills/review-queue/SKILL.md")
        else:
            command.extend(["-contract", str(procedures / ".claude/skills/review-queue/SKILL.md")])
    for queue in profile["queues"]:
        if queue["id"] in {"triage", "review", "merge", "fix"}:
            queue["item_blockers"] = True
        if queue["id"] == "fix":
            queue["item_error_holds"] = True
        if queue["id"] in {"review", "merge"}:
            # Empty/unchanged queues need no provider. A fresh work fingerprint
            # or successful predecessor still wakes the existing schedule.
            queue.setdefault("adaptive_cadence", {}).update({
                "idle_recurrence": "30m", "busy_recurrence": "10m",
                "backlog_above": 0, "empty_runs_before_idle": 2,
                "event_wake": True})
        if queue["id"] in {"fix", "plan"}:
            candidates = "fix_candidates" if queue["id"] == "fix" else "plan_candidates"
            exceptions = [{"key": "priority", "values": [0]}]
            if queue["id"] == "fix":
                exceptions.append({"key": "stage", "values": ["review"]})
            queue.setdefault("dispatch_gate", {}).update({
                "defer_for": "10m", "backpressure": {
                    "max_active": 10, "active_fields": ACTIVE_FIELDS,
                    "candidate_field": candidates, "allow_when_match": exceptions}})
        execution = queue.get("execution")
        if execution:
            # The server and worker now probe/run the very same frozen binary.
            for key in ["command", "probe_command"]:
                command = execution[key]
                if command[:3] == ["{python}", "-m", "promptpilot.project_pipeline"]:
                    execution[key] = [str(binary), "pipelinectl", *command[3:]]
                elif len(command) > 1 and command[1] == "pipelinectl":
                    execution[key] = [str(binary), *command[1:]]
    profile["release_id"] = release_id
    return profiles, config


def prepare(args):
    artifacts = [args.binary, args.health, Path(__file__).resolve(),
                 Path(__file__).resolve().parents[1] / "docs/onebase-delivery-classifications-20260927.json",
                 *sorted(args.procedures.rglob("*"))]
    artifacts = [path for path in artifacts if path.is_file()]
    manifest = {"release_id": args.root.name, "prepared_at": datetime.now(timezone.utc).isoformat(),
                "pp_commit": args.pp_commit, "procedures_commit": args.procedures_commit,
                "health_commit": args.health_commit, "binary": str(args.binary),
                "health": str(args.health), "procedures": str(args.procedures),
                "artifacts": {str(path): sha(path) for path in artifacts}}
    for stage in set(STAGES.values()):
        if str(args.procedures / ".claude/skills" / stage / "SKILL.md") not in manifest["artifacts"]:
            raise ValueError(f"missing stage {stage}")
    if str(args.procedures / "CLAUDE.md") not in manifest["artifacts"]:
        raise ValueError("missing project policy")
    manifest_path = args.root / "release.json"
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    print(manifest_path)


def validate_manifest(manifest):
    for path, expected in manifest["artifacts"].items():
        if sha(path) != expected:
            raise ValueError(f"release artifact changed: {path}")


def install(args):
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    previous_worker = api("/api/worker/status")
    if not previous_worker.get("paused") or api("/api/tasks?status=running"):
        raise RuntimeError("worker must be paused and idle; no agent is interrupted")
    binary, health, procedures = (Path(manifest[key]) for key in ["binary", "health", "procedures"])
    subprocess.run([str(binary), "--help"], check=True, capture_output=True)
    uid = str(__import__("os").getuid())
    backups = args.manifest.parent / "before"
    backups.mkdir()  # Existing backup means a previous/partial install; do not overwrite.
    backups.chmod(0o700)
    paths = [args.data / "pipeline_profiles.json", args.data / "pipelinectl-onebase.json"]
    paths += [args.launch / f"com.promptpilot.{service}.plist" for service in ["server", "bot", "worker"]]
    old_files = {path: path.read_bytes() for path in paths}
    for path, value in old_files.items():
        (backups / path.name).write_bytes(value)
    rows = [row for row in api("/api/schedule") if row["id"] in STAGES]
    if len(rows) != len(STAGES) or any(row.get("ended_at") or not row["title"].startswith("OneBase - ") for row in rows):
        raise ValueError("the expected seven active OneBase series are not present")
    (backups / "series.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    patches = [(row["id"], row["prompt"], rewrite_prompt(row["prompt"], STAGES[row["id"]],
               procedures, manifest["release_id"], args.data / "pipelinectl-onebase.json")) for row in rows]
    profiles, config = configure(json.loads(old_files[paths[0]]), json.loads(old_files[paths[1]]),
                                 binary, health, procedures, manifest["release_id"])
    new_files = {paths[0]: json.dumps(profiles, ensure_ascii=False, indent=2).encode("utf-8"),
                 paths[1]: json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")}
    for path in paths[2:]:
        plist = plistlib.loads(old_files[path])
        command = plist["ProgramArguments"]
        index = 2 if command[0] == "/usr/bin/caffeinate" else 0
        if not command[index].endswith("/pp"):
            raise ValueError("unexpected LaunchAgent executable")
        command[index] = str(binary)
        new_files[path] = plistlib.dumps(plist)
    installed_files = []
    db_changed = False
    restarted = []
    def restart(path):
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True, capture_output=True)
    try:
        # Narrow transactional prompt CAS. Never restore the whole live DB.
        with sqlite3.connect(args.data / "promptpilot.db", timeout=10) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]:
                raise RuntimeError("task started during release preparation")
            for series_id, old, new in patches:
                if connection.execute("UPDATE task_series SET prompt=? WHERE id=? AND prompt=? AND ended_at IS NULL",
                                      (new, series_id, old)).rowcount != 1:
                    raise RuntimeError("concurrent series prompt edit")
                if connection.execute("SELECT count(*) FROM tasks WHERE series_id=? AND status IN ('pending','rate_limited') AND prompt!=?",
                                      (series_id, old)).fetchone()[0]:
                    raise RuntimeError("unexpected pending occurrence prompt")
                connection.execute("UPDATE tasks SET prompt=? WHERE series_id=? AND status IN ('pending','rate_limited') AND prompt=?",
                                   (new, series_id, old))
        db_changed = True
        for path, value in new_files.items():
            if path.read_bytes() != old_files[path]:
                raise RuntimeError("concurrent configuration file edit")
            atomic_bytes(path, value)
            installed_files.append(path)
        for path in paths[2:]:
            restarted.append(path)
            restart(path)
        deadline = time.monotonic() + 45
        while True:
            try:
                status = api("/api/worker/status")
                if (status["state"] == "online" and status["paused"]
                        and status.get("pid") != previous_worker.get("pid")):
                    break
            except Exception:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("release heartbeat missing")
            time.sleep(2)
        # Make rollback inputs auditable; leave resume to the operator after
        # verifying health/config. No series run_now/resume or ship changes.
        (args.manifest.parent / "installed.json").write_text(json.dumps({
            "installed_at": datetime.now(timezone.utc).isoformat(), "release_id": manifest["release_id"],
            "worker": status, "series": [row["id"] for row in rows]}, indent=2), encoding="utf-8")
        print("INSTALLED; worker remains paused for verification; backups preserved")
    except Exception:
        for path in reversed(installed_files):
            if path.read_bytes() != new_files[path]:
                raise RuntimeError("rollback refused: concurrent file edit")
            atomic_bytes(path, old_files[path])
        if db_changed:
            with sqlite3.connect(args.data / "promptpilot.db", timeout=10) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for series_id, old, new in patches:
                    if connection.execute("UPDATE task_series SET prompt=? WHERE id=? AND prompt=?",
                                          (old, series_id, new)).rowcount != 1:
                        raise RuntimeError("rollback refused: concurrent prompt edit")
                    connection.execute("UPDATE tasks SET prompt=? WHERE series_id=? AND status IN ('pending','rate_limited') AND prompt=?",
                                       (old, series_id, new))
        for path in restarted:
            restart(path)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    command = commands.add_parser("prepare")
    for name in ["root", "binary", "health", "procedures"]:
        command.add_argument("--" + name, type=Path, required=True)
    for name in ["pp-commit", "procedures-commit", "health-commit"]:
        command.add_argument("--" + name, required=True)
    command = commands.add_parser("install")
    command.add_argument("--manifest", type=Path, required=True)
    command.add_argument("--data", type=Path, required=True)
    command.add_argument("--launch", type=Path, required=True)
    args = parser.parse_args()
    (prepare if args.action == "prepare" else install)(args)


if __name__ == "__main__":
    main()
