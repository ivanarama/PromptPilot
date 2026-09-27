#!/usr/bin/env python3
"""Portfolio Supervisor — «одна задача → N параллельных воркфлоу» для PromptPilot.

Работает ПОВЕРХ существующего REST API, ничего в ядре не меняя:
  1. По спеке (JSON) создаёт ветки pkg/<id> от базовой и git-worktree на каждую.
  2. Создаёт по воркфлоу на пакет (один этап, свой гейт, свои allowed_paths),
     роли/автоматика наследуются из шаблона (воркфлоу reader).
  3. Слежение: завершённые пакеты попадают в ПОЕЗД СЛИЯНИЙ — серийный
     --no-ff merge в integration-ветку + гейт пакета после каждого слияния.
  4. Конфликт/падение гейта на поезде → MERGE_FAILED + эскалация в лог.
  5. Все пакеты слиты → финальный SHA integration-ветки.

Запуск:  python portfolio-supervisor.py <spec.json>
Ключевой принцип: пакеты ФАЙЛОВО-НЕПЕРЕСЕКАЮЩИЕСЯ; общие файлы
(settings.gradle.kts, libs.versions.toml, :app) — только в одном пакете.
"""

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL = "http://127.0.0.1:8420"
TEMPLATE_WORKFLOW = "wf_1e0bd7a5cfb14d13a60c596e06c85fd9"  # reader — донор конфига
REPO = r"C:\Users\Nachfin\Desktop\Projets\BookApp"
BASE_BRANCH = "main"
INTEGRATION_BRANCH = "integration/security-quickwins"
INTEGRATION_DIR = REPO + "-integration"
WORKTREE_PREFIX = REPO + "-pkg-"
LOG_PATH = Path.home() / ".promptpilot" / "portfolio.log"
MERGE_GATE = r".\gradlew.bat :feature:translation:test :app:assembleDebug --console=plain --no-daemon"

REVIEWER_PROMPT = """Ты — независимый аудитор этапа «{{stage_code}}: {{stage_title}}».
Цель этапа: {{stage_goal}}. Репозиторий: {{repository_path}}, ветка: {{candidate_branch}}.

Проверь диф `git diff main...HEAD` против цели этапа и allowed-путей.
ЭКОНОМИЯ ТОКЕНОВ: гейт уже прогнал сборку/тесты (evidence ниже) — НЕ запускай gradlew,
аудируй диф и файлы напрямую. Без сабагентов. Отчёт — максимум 15 строк.

Gate evidence: {{gate_evidence}}
Незакрытые замечания прошлых аудитов (каждое включи со status resolved/open):
{{open_findings}}

Последние две строки отчёта — строго машинный контракт (без них отчёт невалиден):
AUDIT_FINDINGS_JSON: [...]
AUDIT_VERDICT: PASS|REVISION_REQUIRED|HUMAN_REQUIRED"""


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(
        BASE_URL + path,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def git(*args: str, cwd: str = REPO, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}…: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def run_gate(workdir: str, gate: str) -> tuple[bool, str]:
    log(f"  гейт в {Path(workdir).name}: {gate}")
    result = subprocess.run(
        [gate], cwd=workdir, capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=True, timeout=2400)
    ok = result.returncode == 0
    tail = (result.stdout or result.stderr)[-500:].replace("\n", " | ")
    return ok, tail


def setup_git(spec: dict) -> None:
    branches = git("branch", "--list", "pkg/*").split()
    for pkg in spec["packages"]:
        branch = f"pkg/{pkg['id']}"
        if branch not in branches:
            git("branch", branch, spec.get("base_branch", BASE_BRANCH))
            log(f"ветка {branch} создана от {spec.get('base_branch', BASE_BRANCH)}")
        wt = WORKTREE_PREFIX + pkg["id"]
        if not Path(wt).exists():
            git("worktree", "add", wt, branch)
            log(f"worktree {wt} → {branch}")
        pkg["worktree"] = wt
        pkg["branch"] = branch
    # integration-ветка и её worktree
    if not git("branch", "--list", INTEGRATION_BRANCH):
        git("branch", INTEGRATION_BRANCH, spec.get("base_branch", BASE_BRANCH))
        log(f"ветка {INTEGRATION_BRANCH} создана")
    if not Path(INTEGRATION_DIR).exists():
        git("worktree", "add", INTEGRATION_DIR, INTEGRATION_BRANCH)
        log(f"worktree {INTEGRATION_DIR} → {INTEGRATION_BRANCH}")


def build_config(template: dict, pkg: dict) -> dict:
    cfg = json.loads(json.dumps(template))  # deep copy
    cfg["planning"] = {"enabled": False}
    cfg["gate"] = {"enabled": True, "commands": [pkg["gate"]],
                   "timeout_seconds": 2400, "stop_on_failure": True}
    cfg["stage"] = {"code": pkg["id"].upper(), "title": pkg["title"],
                    "objective": pkg["objective"],
                    "allowed_paths": pkg.get("allowed_paths", ["**"])}
    base = spec_base_branch()
    diff_cmd = f"git diff {base}...HEAD"
    cfg["roles"]["executor"]["prompt_template"] = (
        f"{pkg['prompt']}\n\nРепозиторий: {{{{repository_path}}}}. "
        f"Ветка: {{{{candidate_branch}}}} (коммить в неё, push не делай).\n"
        f"Правила: меняй ТОЛЬКО файлы из списка выше; не трогай jadx-out/, apktool-*/.\n"
        f"Отчёт — максимум 10 строк: что сделано, команды и результаты, commit SHA.\n"
        f"Последней строкой — вердикт: ИТОГ: ГОТОВО — <суть> (или ИТОГ: НЕ УДАЛОСЬ — <почему>).\n"
        f"Диф для ревьюера: {diff_cmd}"
    )
    cfg["roles"]["reviewer"]["prompt_template"] = (
        REVIEWER_PROMPT.replace("git diff main...HEAD", diff_cmd)
    )
    return cfg


def spec_base_branch() -> str:
    return BASE_BRANCH


def create_workflows(spec: dict, template: dict) -> None:
    for pkg in spec["packages"]:
        cfg = build_config(template, pkg)
        created = api("/api/workflows", "POST", {
            "slug": pkg["slug"],
            "objective": pkg["objective"],
            "repository_path": pkg["worktree"],
            "candidate_branch": pkg["branch"],
            "config": cfg,
        })
        pkg["workflow_id"] = created["id"]
        api(f"/api/workflows/{created['id']}/start",
            "POST", {"expected_version": created["state_version"]})
        log(f"воркфлоу {created['id']} ({pkg['slug']}) создан и запущен; worktree={pkg['worktree']}")


def merge_train(spec: dict, template: dict) -> None:
    pending = [p for p in spec["packages"] if p.get("status") == "completed"
               and not p.get("merged")]
    for pkg in pending:
        log(f"ПОЕЗД: слияние {pkg['branch']} в {INTEGRATION_BRANCH}")
        try:
            git("merge", "--no-ff", pkg["branch"], "-m",
                f"merge: {pkg['id']} — {pkg['title']}", cwd=INTEGRATION_DIR)
        except RuntimeError as e:
            pkg["status"] = "MERGE_CONFLICT"
            log(f"  >>> КОНФЛИКТ слияния: {e} — эскалация человеку")
            continue
        ok, tail = run_gate(INTEGRATION_DIR, spec.get("merge_gate", MERGE_GATE))
        if ok:
            pkg["merged"] = True
            log(f"  гейт после слияния — OK; {pkg['id']} в поезде")
        else:
            git("merge", "--abort", cwd=INTEGRATION_DIR, check=False)
            pkg["status"] = "MERGE_GATE_FAILED"
            log(f"  >>> ГЕЙТ ПОЕЗДА ПАЛ, merge отменён: {tail}")


def main() -> None:
    spec_path = sys.argv[1]
    spec = json.load(open(spec_path, encoding="utf-8"))
    log(f"надсмотрщик стартовал: {len(spec['packages'])} пакетов, база={BASE_BRANCH}")

    template = api(f"/api/workflows/{TEMPLATE_WORKFLOW}")["config"]
    setup_git(spec)
    create_workflows(spec, template)

    while True:
        time.sleep(60)
        for pkg in spec["packages"]:
            if pkg.get("status") in ("completed", "MERGE_CONFLICT",
                                     "MERGE_GATE_FAILED", "WORKFLOW_FAILED",
                                     "WORKFLOW_CANCELLED"):
                continue
            wf = api(f"/api/workflows/{pkg['workflow_id']}")
            status = wf["status"]
            if status == "queued":
                # Баг воркера «resume-not-noticed»: queued после resume может
                # не диспетчиться — надсмотрщик обязан пинать sync'ом.
                try:
                    api(f"/api/workflows/{pkg['workflow_id']}/sync", "POST",
                        {"expected_version": wf["state_version"]})
                    log(f"пакет {pkg['id']}: queued → sync-пинок")
                except Exception as e:  # noqa: BLE001
                    log(f"пакет {pkg['id']}: sync err {e}")
                continue
            if status == "completed":
                pkg["status"] = "completed"
                log(f"пакет {pkg['id']} ЗАВЕРШЁН ({pkg['workflow_id']})")
            elif status in ("failed", "cancelled"):
                pkg["status"] = "WORKFLOW_" + status.upper()
                log(f"пакет {pkg['id']} упал ({status}) — эскалация")
            else:
                log(f"пакет {pkg['id']}: {status}…")
        merge_train(spec, json.loads(json.dumps(template)))
        resolved = all(
            p.get("merged") or (p.get("status") or "").startswith(("MERGE_", "WORKFLOW_"))
            for p in spec["packages"]
        )
        if resolved:
            break
    merged = [p["id"] for p in spec["packages"] if p.get("merged")]
    problems = [p["id"] for p in spec["packages"] if not p.get("merged")]
    sha = git("rev-parse", "HEAD", cwd=INTEGRATION_DIR)
    log(f"ИТОГ: слиты {merged}; проблемы: {problems}; integration HEAD {sha}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
