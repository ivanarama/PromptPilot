"""Oneservice executor (этапы 3–4 конвейера).

Ведёт задачи, принятые хранителем (label «триаж-ТЗ»), через исполнение:

  claim    — берёт одну задачу: assignee = бот, метка «в работе», ветка
             task/<iid>, запускает исполнителя (Gemini/agy через PromptPilot)
             в этой ветке. Одновременно в работе только одна задача (MVP:
             один чекаут репозитория).
  watch    — завершённая задача исполнителя → ревью независимой моделью
             (diff ветки с main). PASS → «готово-к-мержу»; замечания → назад
             «в работе» (не более OS_MAX_ROUNDS, дальше «нужен человек»).
             Вердикт ПЛАТФОРМА → issue в onebase + «блокирована платформой».
  merge    --issue N — gate (OS_GATE_COMMAND, если задан) → merge ветки в
             main → push → закрытие issue → запись в docs/CHANGELOG-TEAM.md
             (кто предложил, кто исполнил, кто ревьюил).

Запуск: py -3.11 scripts/oneservice/os_exec.py claim|watch|merge ...
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXEC_STATE = ROOT / ".os_exec.json"

from os_intake import (  # noqa: E402
    ATTACH_ROOT, ROOT as INTAKE_ROOT, load_env, project_api, pp_request,
)

MAX_ROUNDS = 2


def load_state() -> dict:
    state = {"work": {}}
    if EXEC_STATE.exists():
        state.update(json.loads(EXEC_STATE.read_text(encoding="utf-8")))
    state.setdefault("work", {})
    return state


def save_state(state: dict) -> None:
    EXEC_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def repo() -> Path:
    env = load_env(INTAKE_ROOT / ".env")
    return Path(env.get("OS_WORKING_DIR", r"C:\Projects\oneservice-cc_v2"))


def git(repo: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, encoding="utf-8",
                            errors="replace", timeout=300)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def gate(env: dict, repo: Path) -> None:
    """OS_GATE_COMMAND — синтакс-контроль/тесты; пусто = пропускаем с предупреждением."""
    cmd = env.get("OS_GATE_COMMAND", "").strip()
    if not cmd:
        print("    ⚠ OS_GATE_COMMAND не задан — gate пропущен")
        return
    code, out = run_shell(cmd, cwd=repo)
    if code != 0:
        raise RuntimeError(f"gate не прошёл (exit {code}): {out[-1500:]}")
    print("    gate ✔")


def run_shell(cmd: str, cwd: Path) -> tuple[int, str]:
    result = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                            encoding="utf-8", errors="replace", timeout=3600)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def issue_meta(issue: dict) -> str:
    author = issue.get("author") or {}
    return author.get("name") or author.get("username") or "аноним"


def issue_spec(issue: dict) -> str:
    return issue.get("description") or ""


# --- claim ------------------------------------------------------------------

def claim(env: dict, state: dict, issue_iid: int | None = None) -> None:
    repo_dir = repo()
    busy = [iid for iid, w in state["work"].items() if w.get("stage") == "в работе"]
    if busy:
        print(f"Занято: issue #{', #'.join(busy)} — в работе (один чекаут)")
        return
    if issue_iid is None:
        issues = project_api(
            env, "/issues?labels=%D1%82%D1%80%D0%B8%D0%B0%D0%B6-%D0%A2%D0%97&state=opened")
        if not issues:
            print("Нет задач с меткой «триаж-ТЗ».")
            return
        issue_iid = int(issues[0]["iid"])
    issue = project_api(env, f"/issues/{issue_iid}")
    if issue["state"] != "opened":
        print(f"issue #{issue_iid} не открыт")
        return
    if issue.get("assignee"):
        bot_id = str(env.get("GITLAB_BOT_USER_ID", ""))
        if str(issue["assignee"].get("id")) == bot_id:
            print(f"  issue #{issue_iid} уже закреплён за ботом — продолжаю")
        else:
            print(f"issue #{issue_iid} уже назначен на @{issue['assignee']['username']}")
            return

    code, base = git(repo_dir, "branch", "--show-current")
    base = base.strip() or "master"
    branch = f"task/{issue_iid}"
    code, out = git(repo_dir, "checkout", "-b", branch)
    if code != 0:
        code2, out2 = git(repo_dir, "checkout", branch)
        if code2 != 0:
            raise RuntimeError(f"не могу перейти на ветку {branch}: {out2}")
    code, _ = git(repo_dir, "push", "-u", "origin", branch)
    if code != 0:
        print(f"  (ветка не запушена: {out.strip()[:200]})")

    assignee = env.get("GITLAB_BOT_USER_ID", "")
    if assignee:
        project_api(env, f"/issues/{issue_iid}", "PUT",
                    {"assignee_ids": [int(assignee)]})
    project_api(env, f"/issues/{issue_iid}", "PUT",
                {"labels": "в работе,триаж-ТЗ",
                 "description": issue["description"]})

    spec = issue_spec(issue)
    prompt = (
        f"Ты работаешь в репозитории oneservice-cc_v2 на ветке {branch}.\n"
        f"Техническое задание (GitLab issue #{issue_iid}):\n\n{spec}\n\n"
        "Правила:\n"
        "- работай только в этой ветке; main не трогай;\n"
        "- внеси изменения по ТЗ и сделай git commit с сообщением "
        f"\"task #{issue_iid}: <кратко>\";\n"
        "- ничего не пушь на remote;\n"
        "- если задача упирается в ошибку/ограничение платформы — ничего "
        "не коммить, а начни ответ со строки ПЛАТФОРМА: <что именно>.\n"
        "Последней строкой ответа напиши: ИТОГ: ГОТОВО — изменения закоммичены."
    )
    payload = {
        "prompt": prompt,
        "provider": env.get("OS_PROVIDER", "agy"),
        "working_dir": str(repo_dir),
        "priority": 2,
    }
    task = pp_request(env, "/api/tasks", "POST", payload)
    state["work"][str(issue_iid)] = {
        "stage": "в работе", "branch": branch, "base": base,
        "pp_task": task["id"], "rounds": 1,
        "author": issue_meta(issue), "title": issue["title"],
    }
    save_state(state)
    print(f"🚀 issue #{issue_iid} в работе: ветка {branch}, задача #{task['id']}")


# --- watch ------------------------------------------------------------------

def parse_verdict(result: str) -> str:
    match = re.search(r"^ИТОГ:\s*(.+)$", result or "", re.M)
    return match.group(1).strip().upper() if match else ""


def watch(env: dict, state: dict, config: dict) -> None:
    for iid, w in list(state["work"].items()):
        if w["stage"] == "ревью" and w.get("review_task"):
            task = pp_request(env, f"/api/tasks/{w['review_task']}")
            if task["status"] in ("completed", "failed", "cancelled"):
                review_verdict(task, env, state, int(iid), w)
            continue
        if w["stage"] != "в работе":
            continue
        task = pp_request(env, f"/api/tasks/{w['pp_task']}")
        if task["status"] not in ("completed", "failed", "cancelled"):
            continue
        result = task.get("result") or ""
        if task["status"] != "completed" or "ПЛАТФОРМА" in result:
            reason = re.search(r"^ПЛАТФОРМА:\s*(.+)$", result, re.M)
            escalate_platform(env, int(iid), w,
                              reason.group(1).strip() if reason else "исполнение не удалось")
            continue
        verdict = parse_verdict(result)
        if verdict.startswith("ГОТОВО"):
            start_review(env, state, int(iid), w)
        elif verdict.startswith("ПЛАТФОРМА"):
            escalate_platform(env, int(iid), w, result[-500:])
        else:
            finish_human(env, int(iid), w, "исполнитель не справился")


def start_review(env: dict, state: dict, iid: int, w: dict) -> None:
    reviewer = env.get("OS_REVIEW_PROVIDER", "claude-z")
    prompt = (
        f"Ты — ревьюер репозитория oneservice-cc_v2. Проверь изменения в ветке "
        f"{w['branch']} относительно main.\n\n"
        "Сначала выполни: git -C . diff main...HEAD\n"
        "Затем сверь изменения с ТЗ ниже и с духом проекта (ламповая "
        "практичность, без лишнего).\n\n"
        f"ТЗ (issue #{iid}):\n{issue_spec_of(env, iid)}\n\n"
        "Ответь строго в формате:\n"
        "РЕШЕНИЕ: PASS|FAIL\n"
        "ЗАМЕЧАНИЯ: <конкретные замечания или «нет»>\n\n"
        "Последней строкой: ИТОГ: ГОТОВО — ревью завершено"
    )
    payload = {"prompt": prompt, "provider": reviewer,
               "working_dir": str(repo()), "priority": 2}
    task = pp_request(env, "/api/tasks", "POST", payload)
    w["stage"] = "ревью"
    w["review_task"] = task["id"]
    project_api(env, f"/issues/{iid}", "PUT", {"labels": "в работе,ревью"})
    save_state(state)
    print(f"🔍 issue #{iid}: ревью ({reviewer}), задача #{task['id']}")


def review_verdict(task: dict, env: dict, state: dict, iid: int, w: dict) -> None:
    result = (task.get("result") or "").split("--- Meta ---")[0]
    match = re.search(r"^РЕШЕНИЕ:\s*(PASS|FAIL)", result, re.M | re.I)
    passed = bool(match) and match.group(1).upper() == "PASS"
    remarks = re.search(r"^ЗАМЕЧАНИЯ:\s*([\s\S]*?)(?:\nИТОГ:|$)", result, re.M | re.I)
    note = (f"🔍 **Ревью** ({env.get('OS_REVIEW_PROVIDER', 'claude-z')}, задача #{w['review_task']})\n\n"
            f"РЕШЕНИЕ: {'PASS' if passed else 'FAIL'}\n\n{remarks.group(1).strip() if remarks else ''}")
    project_api(env, f"/issues/{iid}/notes", "POST", {"body": note})
    if passed or int(w.get("rounds", 1)) >= MAX_ROUNDS:
        label = "готово-к-мержу" if passed else "нужен человек"
        project_api(env, f"/issues/{iid}", "PUT", {"labels": f"в работе,{label}"})
        w["stage"] = "готово-к-мержу" if passed else "нужен человек"
        print(f"issue #{iid}: ревью {'PASS' if passed else 'FAIL (предел раундов)'}")
    else:
        w["stage"] = "в работе"
        w["rounds"] = int(w.get("rounds", 1)) + 1
        w["review_task"] = None
        project_api(env, f"/issues/{iid}/notes", "POST", {
            "body": f"↩ Замечания ревью вернули задачу в работу (раунд {w['rounds']})."})
        print(f"issue #{iid}: замечания ревью — возврат в работу, раунд {w['rounds']}")
    save_state(state)


def issue_spec_of(env: dict, iid: int) -> str:
    issue = project_api(env, f"/issues/{iid}")
    return (issue.get("description") or "")[:4000]


def finish_human(env: dict, iid: int, w: dict, reason: str) -> None:
    project_api(env, f"/issues/{iid}/notes", "POST", {
        "body": f"⚠ Автоматическое исполнение не удалось ({reason}). Нужен человек."})
    project_api(env, f"/issues/{iid}", "PUT", {"labels": "нужен человек,в работе"})
    w["stage"] = "нужен человек"
    print(f"issue #{iid}: нужен человек ({reason})")


def escalate_platform(env: dict, iid: int, w: dict, reason: str) -> None:
    onebase = env.get("OS_PLATFORM_REPO", "ivanarama/onebase")
    title = f"[oneservice #{iid}] Платформенное ограничение для задачи"
    result = subprocess.run(
        ["gh", "issue", "create", "--repo", onebase, "--title", title,
         "--body", reason[:800]],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode == 0:
        link = result.stdout.strip()
    else:
        link = f"не создан ({(result.stderr or result.stdout)[:150]})"
    project_api(env, f"/issues/{iid}/notes", "POST", {
        "body": f"⚓ Платформенная эскалация: issue в onebase {link}.\n\n{reason}"})
    project_api(env, f"/issues/{iid}", "PUT", {"labels": "блокирована платформой"})
    w["stage"] = "блокирована платформой"
    print(f"issue #{iid}: эскалация на платформу — {link}")


# --- merge ------------------------------------------------------------------

def notify_author(env: dict, iid: int, w: dict, text: str) -> None:
    """Уведомить автора задачи в TG (если задача пришла из Telegram)."""
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = w.get("tg_chat_id", "")
    if not (token and chat_id):
        return
    try:
        subprocess.run(["curl", "-sS", "-m", "20", "-X", "POST",
                        "-H", "Content-Type: application/json",
                        "--data-binary",
                        json.dumps({"chat_id": int(chat_id), "text": text},
                                   ensure_ascii=False),
                        f"https://api.telegram.org/bot{token}/sendMessage"],
                       capture_output=True, timeout=30)
    except Exception as exc:
        print(f"  !! TG notify: {exc}", flush=True)


def do_merge(env: dict, state: dict, iid: int) -> None:
    w = state["work"].get(str(iid)) or {}
    branch = w.get("branch") or f"task/{iid}"
    repo_dir = repo()
    base = w.get("base", "master")
    gate(env, repo_dir)
    code, out = git(repo_dir, "checkout", base)
    if code != 0:
        raise RuntimeError(f"checkout {base}: {out}")
    code, out = git(repo_dir, "merge", "--no-ff", branch, "-m",
                    f"Merge task #{iid}: {w.get('title', '')}")
    if code != 0:
        raise RuntimeError(f"merge конфликт: {out[-800:]}")
    code, out = git(repo_dir, "push", "origin", base)
    if code != 0:
        raise RuntimeError(f"push: {out[-800:]}")
    git(repo_dir, "branch", "-d", branch)
    changelog_entry(env, repo_dir, iid, w)
    project_api(env, f"/issues/{iid}", "PUT", {"state_event": "close"})
    project_api(env, f"/issues/{iid}/notes", "POST", {
        "body": f"📦 Задача выполнена и влита в main (v-запись в CHANGELOG-TEAM)."})
    notify_author(env, iid, w,
                  f"🎉 Твоя задача «{w.get('title', '')}» выполнена и влита "
                  f"в основную ветку. Спасибо за вклад!")
    state["work"][str(iid)]["stage"] = "в релизе"
    save_state(state)
    print(f"📦 issue #{iid}: смержено в main, закрыто")


def changelog_entry(env: dict, repo_dir: Path, iid: int, w: dict) -> None:
    doc = repo_dir / "docs" / "CHANGELOG-TEAM.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    entry = (f"\n## {today} — {w.get('title', '')} (issue #{iid})\n"
             f"- Предложил: {w.get('author', 'аноним')}\n"
             f"- Исполнил: {env.get('OS_PROVIDER', 'agy')} (задача PP #{w.get('pp_task', '—')})\n"
             f"- Ревью: {env.get('OS_REVIEW_PROVIDER', 'claude-z')}\n"
             f"- Ветка: {w.get('branch', '—')}\n")
    with doc.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    git(repo_dir, "add", "docs/CHANGELOG-TEAM.md")
    git(repo_dir, "commit", "-m", f"docs: changelog task #{iid}")
    git(repo_dir, "push", "origin", "main")


# --- main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("claim", "watch", "merge"))
    parser.add_argument("--issue", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=120)
    args = parser.parse_args()

    env = load_env(INTAKE_ROOT / ".env")
    state = load_state()
    if args.mode == "claim":
        claim(env, state, args.issue)
        save_state(state)
        return 0
    if args.mode == "merge":
        if not args.issue:
            print("merge требует --issue N")
            return 2
        do_merge(env, state, args.issue)
        return 0
    while True:
        try:
            watch(env, state, load_env(INTAKE_ROOT / ".env"))
            save_state(state)
        except Exception as exc:
            print(f"!! watch: {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
