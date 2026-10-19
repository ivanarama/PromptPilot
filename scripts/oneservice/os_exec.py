"""Oneservice executor (этапы 3–4 конвейера).

Ведёт задачи, принятые хранителем (label «триаж-ТЗ»), через исполнение:

  claim    — берёт одну задачу: assignee = бот, метка «в работе», ветка
             task/<iid> от базовой ветки (OS_BASE_BRANCH или ветка по
             умолчанию на origin), запускает исполнителя (через PromptPilot)
             с ТЗ хранителя. Одновременно в работе и на ревью — одна задача
             (MVP: один чекаут репозитория).
  watch    — завершённая задача исполнителя → ревью независимой моделью
             (diff ветки с базовой). PASS → «готово-к-мержу»; замечания →
             новый запуск исполнителя с этими замечаниями (не более
             OS_MAX_ROUNDS раундов, дальше «нужен человек»). Эскалация на
             платформу — только по явной строке «ПЛАТФОРМА: …» в ответе;
             сбой исполнения — «нужен человек».
  merge    --issue N — только для «готово-к-мержу» (иначе --force):
             gate (OS_GATE_COMMAND) → merge ветки в базовую → push →
             закрытие issue → запись в docs/CHANGELOG-TEAM.md → уведомление
             автору в Telegram (чат запомнен при приёме заявки).

Запуск: py -3.11 scripts/oneservice/os_exec.py claim|watch|merge ...
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXEC_STATE = ROOT / ".os_exec.json"

sys.path.insert(0, str(ROOT))
from os_intake import (  # noqa: E402
    LABEL_HUMAN, ROOT as INTAKE_ROOT, author_chat, load_env, project_api,
    pp_request,
)

# Stages that occupy the single shared checkout.
BUSY_STAGES = ("в работе", "ревью")
KEEPER_NOTE_MARK = "🧊 **Хранитель целесообразности**"
META_CUT = "--- Meta ---"


def max_rounds(env: dict) -> int:
    return int(env.get("OS_MAX_ROUNDS", "2"))


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


def base_branch(env: dict, repo_dir: Path) -> str:
    """The branch tasks start from and merge into.

    OS_BASE_BRANCH, else the default branch of origin. It used to be whatever
    the shared checkout was on — after a claim that is the previous task's
    branch, so tasks grew out of each other and merged into each other.
    """
    configured = env.get("OS_BASE_BRANCH", "").strip()
    if configured:
        return configured
    code, out = git(repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    ref = out.strip()
    if code == 0 and ref.startswith("origin/"):
        return ref.split("/", 1)[1]
    return "main"


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


def task_spec(env: dict, issue: dict) -> str:
    """What the executor must build: the keeper's ТЗ, else the issue text.

    The keeper posts its ТЗ as an issue note. The executor used to receive
    only the original request, so the keeper's work never reached it — and
    the raw request is exactly the text written by an outside person.
    """
    bot_id = str(env.get("GITLAB_BOT_USER_ID", "")).strip()
    try:
        notes = project_api(
            env, f"/issues/{issue['iid']}/notes?sort=desc&order_by=created_at&per_page=50")
    except Exception as exc:
        print(f"  !! заметки issue #{issue['iid']}: {exc}")
        notes = []
    for note in notes or []:
        body = note.get("body") or ""
        author_id = str((note.get("author") or {}).get("id", ""))
        if not body.startswith(KEEPER_NOTE_MARK) or (bot_id and author_id != bot_id):
            continue
        index = body.upper().find("ТЕХНИЧЕСКОЕ ЗАДАНИЕ:")
        if index >= 0:
            return body[index:].strip()
    return issue_spec(issue)


def executor_prompt(env: dict, issue: dict, branch: str,
                    remarks: str = "", round_no: int = 1) -> str:
    iid = issue["iid"]
    prompt = (
        f"Ты работаешь в репозитории oneservice-cc_v2 на ветке {branch}.\n"
        f"Техническое задание (GitLab issue #{iid}):\n\n{task_spec(env, issue)}\n\n"
    )
    if remarks:
        prompt += (
            f"Раунд {round_no}. Независимое ревью вернуло замечания к тому, что "
            "уже сделано в этой ветке. Исправь их:\n"
            f"{remarks}\n\n"
        )
    prompt += (
        "Правила:\n"
        "- работай только в этой ветке; базовую ветку не трогай;\n"
        "- внеси изменения по ТЗ и сделай git commit с сообщением "
        f"\"task #{iid}: <кратко>\";\n"
        "- ничего не пушь на remote;\n"
        "- если задача упирается в ошибку/ограничение платформы — ничего "
        "не коммить, а начни ответ со строки ПЛАТФОРМА: <что именно>.\n"
        "Последней строкой ответа напиши: ИТОГ: ГОТОВО — изменения закоммичены."
    )
    return prompt


def dispatch_executor(env: dict, prompt: str) -> int:
    task = pp_request(env, "/api/tasks", "POST", {
        "prompt": prompt,
        "provider": env.get("OS_PROVIDER", "agy"),
        "working_dir": str(repo()),
        "priority": 2,
    })
    return int(task["id"])


# --- claim ------------------------------------------------------------------

def claim(env: dict, state: dict, issue_iid: int | None = None) -> None:
    repo_dir = repo()
    busy = [iid for iid, w in state["work"].items() if w.get("stage") in BUSY_STAGES]
    if busy:
        print(f"Занято: issue #{', #'.join(busy)} — в работе или на ревью (один чекаут)")
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

    base = base_branch(env, repo_dir)
    branch = f"task/{issue_iid}"
    git(repo_dir, "fetch", "origin")  # offline is fine: the local base is used then
    code, _ = git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if code == 0:
        code, out = git(repo_dir, "checkout", branch)
    else:
        start = f"origin/{base}"
        if git(repo_dir, "rev-parse", "--verify", "--quiet", start)[0] != 0:
            start = base
        code, out = git(repo_dir, "checkout", "-b", branch, start)
    if code != 0:
        raise RuntimeError(f"не могу перейти на ветку {branch}: {out}")
    code, out = git(repo_dir, "push", "-u", "origin", branch)
    if code != 0:
        print(f"  (ветка не запушена: {out.strip()[:200]})")

    assignee = env.get("GITLAB_BOT_USER_ID", "")
    if assignee:
        project_api(env, f"/issues/{issue_iid}", "PUT",
                    {"assignee_ids": [int(assignee)]})
    project_api(env, f"/issues/{issue_iid}", "PUT", {"labels": "в работе,триаж-ТЗ"})

    task_id = dispatch_executor(env, executor_prompt(env, issue, branch))
    state["work"][str(issue_iid)] = {
        "stage": "в работе", "branch": branch, "base": base,
        "pp_task": task_id, "rounds": 1,
        "author": issue_meta(issue), "title": issue["title"],
    }
    save_state(state)
    print(f"🚀 issue #{issue_iid} в работе: ветка {branch} от {base}, задача #{task_id}")


# --- watch ------------------------------------------------------------------

def parse_verdict(result: str) -> str:
    matches = re.findall(r"^ИТОГ:\s*(.+)$", result or "", re.M)
    return matches[-1].strip().upper() if matches else ""


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
        if task["status"] != "completed":
            # A quota or timeout failure says nothing about the platform: it
            # used to open a «platform limitation» issue in onebase.
            finish_human(env, int(iid), w, f"исполнитель не завершился (статус {task['status']})")
            continue
        result = (task.get("result") or "").split(META_CUT)[0]
        platform = re.search(r"^ПЛАТФОРМА:\s*(.+)$", result, re.M)
        if platform:
            escalate_platform(env, int(iid), w, platform.group(1).strip())
            continue
        if parse_verdict(result).startswith("ГОТОВО"):
            start_review(env, state, int(iid), w)
        else:
            finish_human(env, int(iid), w, "исполнитель не справился")


def start_review(env: dict, state: dict, iid: int, w: dict) -> None:
    reviewer = env.get("OS_REVIEW_PROVIDER", "claude-z")
    base = w.get("base") or "main"
    prompt = (
        f"Ты — ревьюер репозитория oneservice-cc_v2. Проверь изменения в ветке "
        f"{w['branch']} относительно {base}.\n\n"
        f"Сначала выполни: git -C . diff {base}...HEAD\n"
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
    result = (task.get("result") or "").split(META_CUT)[0]
    decisions = re.findall(r"^РЕШЕНИЕ:\s*(PASS|FAIL)\b", result, re.M | re.I)
    passed = bool(decisions) and decisions[-1].upper() == "PASS"
    remarks_match = re.search(r"^ЗАМЕЧАНИЯ:\s*([\s\S]*?)(?:\nИТОГ:|$)", result, re.M | re.I)
    remarks = remarks_match.group(1).strip() if remarks_match else ""
    note = (f"🔍 **Ревью** ({env.get('OS_REVIEW_PROVIDER', 'claude-z')}, задача #{w['review_task']})\n\n"
            f"РЕШЕНИЕ: {'PASS' if passed else 'FAIL'}\n\n{remarks}")
    project_api(env, f"/issues/{iid}/notes", "POST", {"body": note})
    if passed or int(w.get("rounds", 1)) >= max_rounds(env):
        label = "готово-к-мержу" if passed else LABEL_HUMAN
        project_api(env, f"/issues/{iid}", "PUT", {"labels": f"в работе,{label}"})
        w["stage"] = label
        print(f"issue #{iid}: ревью {'PASS' if passed else 'FAIL (предел раундов)'}")
    else:
        redo(env, iid, w, remarks or "Ревью не прошло; замечания не сформулированы — "
                                     "перечитай ТЗ и проверь результат целиком.")
        project_api(env, f"/issues/{iid}/notes", "POST", {
            "body": f"↩ Замечания ревью вернули задачу исполнителю (раунд {w['rounds']})."})
        print(f"issue #{iid}: замечания ревью — исполнитель, раунд {w['rounds']}")
    save_state(state)


def redo(env: dict, iid: int, w: dict, remarks: str) -> None:
    """Send the task back to the executor WITH the reviewer's remarks.

    The loop used to flip the stage to «в работе» and wait for the old,
    already finished executor task — the same diff went to review again and
    nothing got fixed.
    """
    repo_dir = repo()
    code, out = git(repo_dir, "checkout", w["branch"])
    if code != 0:
        raise RuntimeError(f"не могу вернуться на ветку {w['branch']}: {out}")
    issue = project_api(env, f"/issues/{iid}")
    w["rounds"] = int(w.get("rounds", 1)) + 1
    w["pp_task"] = dispatch_executor(
        env, executor_prompt(env, issue, w["branch"], remarks=remarks, round_no=w["rounds"]))
    w["stage"] = "в работе"
    w["review_task"] = None
    project_api(env, f"/issues/{iid}", "PUT", {"labels": "в работе,триаж-ТЗ"})


def issue_spec_of(env: dict, iid: int) -> str:
    issue = project_api(env, f"/issues/{iid}")
    return task_spec(env, issue)[:4000]


def finish_human(env: dict, iid: int, w: dict, reason: str) -> None:
    project_api(env, f"/issues/{iid}/notes", "POST", {
        "body": f"⚠ Автоматическое исполнение не удалось ({reason}). Нужен человек."})
    project_api(env, f"/issues/{iid}", "PUT", {"labels": f"{LABEL_HUMAN},в работе"})
    w["stage"] = LABEL_HUMAN
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

def notify_author(env: dict, iid: int, text: str) -> None:
    """Уведомить автора задачи в TG — в чат, запомненный при приёме заявки.

    Раньше чат искался в состоянии работы (tg_chat_id), куда его никто не
    записывал, — уведомление не уходило никогда.
    """
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = author_chat(iid)
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


def do_merge(env: dict, state: dict, iid: int, force: bool = False) -> None:
    w = state["work"].get(str(iid)) or {}
    if w.get("stage") != "готово-к-мержу" and not force:
        raise RuntimeError(
            f"issue #{iid} не прошёл ревью (стадия «{w.get('stage') or 'нет'}»); "
            "влить всё равно — --force")
    branch = w.get("branch") or f"task/{iid}"
    repo_dir = repo()
    base = w.get("base") or base_branch(env, repo_dir)
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
    changelog_entry(env, repo_dir, iid, w, base)
    project_api(env, f"/issues/{iid}", "PUT", {"state_event": "close"})
    project_api(env, f"/issues/{iid}/notes", "POST", {
        "body": f"📦 Задача выполнена и влита в {base} (запись в CHANGELOG-TEAM)."})
    # Автообновление дашборда и фида после мержа
    try:
        dash = Path(__file__).resolve().parent / "os_dashboard.py"
        run_shell(f'"{sys.executable}" "{dash}"', cwd=Path(__file__).resolve().parent.parent)
    except Exception as exc:
        print(f"  !! dashboard refresh: {exc}", flush=True)
    notify_author(env, iid,
                  f"🎉 Твоя задача «{w.get('title', '')}» выполнена и влита "
                  f"в основную ветку. Спасибо за вклад!")
    state["work"].setdefault(str(iid), w)["stage"] = "в релизе"
    save_state(state)
    print(f"📦 issue #{iid}: смержено в {base}, закрыто")


def changelog_entry(env: dict, repo_dir: Path, iid: int, w: dict, base: str) -> None:
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
    for args in (("add", "docs/CHANGELOG-TEAM.md"),
                 ("commit", "-m", f"docs: changelog task #{iid}"),
                 # the base branch the merge went into — it used to push
                 # «main» even when the base was another branch
                 ("push", "origin", base)):
        code, out = git(repo_dir, *args)
        if code != 0:
            print(f"  !! changelog: git {args[0]} не прошёл: {out.strip()[:300]}")
            return


# --- main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("claim", "watch", "merge"))
    parser.add_argument("--issue", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--force", action="store_true",
                        help="merge: влить задачу, не прошедшую ревью")
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
        do_merge(env, state, args.issue, force=args.force)
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
