#!/usr/bin/env python3
"""Verdict-repair watcher для PromptPilot.

Ловит события `executor.invalid_output` (исполнитель потерял строку вердикта
«ИТОГ:» — главная причина остановок пайплайна), отправляет хвост отчёта в
TypeSafe Jev (System One: https://docs.typesafe.ai) и при ОДНОЗНАЧНОМ ответе
сам реанимирует раунд через human-input resume — вместо ручного «Продолжить».

Политика безопасности:
  - авто-resume ТОЛЬКО если Jev уверен (choice confidence >= 0.85 и
    noul done >= 0.70) и вердикт — успешный (done/already);
  - «НУЖЕН ЧЕЛОВЕК»/неудача/неясность — только лог, решение за человеком;
  - каждое решение пишется в ~/.promptpilot/verdict-repair.log (аудит).

Запуск:  python verdict-repair-watcher.py
Ключ:    env TYPESAFE_API_KEY или файл C:\\Users\\Nachfin\\Desktop\\TypeSafe.txt
Стоп:    Ctrl+C
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
DB_PATH = os.path.join(HOME, ".promptpilot", "promptpilot.db")
LOG_PATH = os.path.join(HOME, ".promptpilot", "verdict-repair.log")
STATE_PATH = os.path.join(HOME, ".promptpilot", "verdict-repair-state.json")
KEY_FILE = r"C:\Users\Nachfin\Desktop\TypeSafe.txt"
BASE_URL = "http://127.0.0.1:8420"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
POLL_SECONDS = 20
MIN_CHOICE_CONF = 0.85
MIN_NOUL_DONE = 0.70
AUTO_VERDICTS = {"done", "already"}
RESUME_TEXT = "Продолжить работу с учётом сохранённого состояния"


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_key() -> str:
    env = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if env:
        return env
    with open(KEY_FILE, encoding="utf-8") as f:
        return f.read().strip()


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"last_seq": 0}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def db_connect():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout = 4000")
    return conn


def api(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(
        BASE_URL + path,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def ask_jev(key: str, report_tail: str, mode: str) -> dict | None:
    """Спросить Jev, что подразумевает отчёт. mode: 'executor' | 'reviewer'.
    None = сервис недоступен."""
    if mode == "reviewer":
        questions = {
            "done": {
                "type": "noul",
                "instructions": (
                    "Отчёт является валидным аудитом: содержит вердикт "
                    "(PASS/REVISION_REQUIRED/HUMAN_REQUIRED) и конкретные "
                    "замечания либо явное отсутствие замечаний?"
                ),
            },
            "verdict": {
                "type": "choice",
                "criteria": {
                    "valid_revision": "аудит валиден, требуются исправления работы",
                    "valid_pass": "аудит валиден, замечаний нет — работа принята",
                    "human": "аудит требует решения человека",
                    "garbage": "текст не является валидным аудитом",
                },
            },
        }
    else:
        questions = {
            "done": {
                "type": "noul",
                "instructions": (
                    "Отчёт агента-исполнителя однозначно заявляет, что "
                    "поставленная задача выполнена успешно (сборка/тесты "
                    "прошли, изменения сделаны)?"
                ),
            },
            "verdict": {
                "type": "choice",
                "criteria": {
                    "done": "работа выполнена успешно в этом запуске",
                    "already": "работа уже была сделана ранее, отчёт это подтверждает",
                    "human": "исполнитель просит вмешательства человека или задаёт вопрос",
                    "fail": "работа не удалась или не завершена",
                },
            },
        }
    body = json.dumps(
        {"state": report_tail, "model": "jev-latest", "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        TYPESAFE_URL,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log(f"  TypeSafe недоступен: {e}")
        return None


MAX_TIMEOUT_RESUMES_PER_TASK = 2


def timeout_resume_cap_reached(workflow_id: str, task_id: str) -> bool:
    """Не более 2 авто-resume на одну задачу: если таймаут повторяется —
    провайдер виснет системно, дальше только человек."""
    state = load_state()
    return state.get("timeout_resumes", {}).get(f"{workflow_id}:{task_id}", 0) >= MAX_TIMEOUT_RESUMES_PER_TASK


def bump_timeout_resume(workflow_id: str, task_id: str) -> None:
    state = load_state()
    counts = state.setdefault("timeout_resumes", {})
    counts[f"{workflow_id}:{task_id}"] = counts.get(f"{workflow_id}:{task_id}", 0) + 1
    if len(counts) > 100:  # не растём бесконечно
        for k in sorted(counts)[:-50]:
            counts.pop(k, None)
    save_state(state)


def resume_timeout(workflow_id: str, task_id: str) -> None:
    conn = db_connect()
    try:
        wf = conn.execute(
            "SELECT status, current_round, state_version FROM workflows WHERE id = ?",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    if not wf:
        return
    wf_status, wf_round, wf_version = wf
    if wf_status != "awaiting_human":
        log(f"  task#{task_id}: воркфлоу {wf_status} — timeout-resume не нужен")
        return
    body = {
        "expected_version": wf_version,
        "text": (
            "Продолжить работу с учётом сохранённого состояния\n\n"
            f"(авто-resume: задача #{task_id} убита воркером по 4-часовому таймауту; "
            "продолжаем с сохранённого состояния — при повторном таймауте будет "
            "эскалация человеку)"
        ),
        "resume": True,
    }
    for attempt in (1, 2):
        try:
            api(f"/api/workflows/{workflow_id}/human-input", "POST", body)
            break
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt == 1:
                conn = db_connect()
                try:
                    wf_version = conn.execute(
                        "SELECT state_version FROM workflows WHERE id = ?", (workflow_id,)
                    ).fetchone()[0]
                    body["expected_version"] = wf_version
                finally:
                    conn.close()
                continue
            log(f"  timeout-resume не прошёл: HTTP {e.code}")
            return
    bump_timeout_resume(workflow_id, task_id)
    log(f"  >>> TIMEOUT-RESUME отправлен: задача #{task_id} продолжена без человека (раунд {wf_round})")


DEAD_WORKFLOWS = {
    'wf_874980f3fc3f40868777032da8f6bea1',  # pkg-reverso-m2 (слит в main)
    'wf_84c029c14a82473c9b05876968819f19',  # pkg-backup-m3 (слит в main)
}


def try_repair(key: str, event: dict) -> None:
    """Обработать событие: потеря вердикта исполнителем/аудитором или таймаут."""
    payload = event.get("payload") or {}
    event_type = event.get("event_type", "")
    task_id = payload.get("task_id")
    reason = str(payload.get("reason") or "")
    error = str(payload.get("error") or "")
    workflow_id = event.get("workflow_id", "")

    mode = "executor"
    if event_type in ("reviewer.invalid_output", "automation.paused"):
        mode = "reviewer"

    # automation.paused интересует только в аудит-варианте; прочие причины
    # (конфликты версий и т.п.) — не наша епархия.
    if event_type == "automation.paused" and "AUDIT" not in reason.upper() and "аудитор" not in reason.lower():
        return
    if workflow_id in DEAD_WORKFLOWS:
        return  # мёртвый воркфлоу: работа слита в main
    if task_id is None:
        return

    # executor.failed: авто-resume ТОЛЬКО на таймауте (задача частично
    # выполнена, продолжение с сохранённого состояния осмысленно). Прочие
    # фейлы (CLI not found, повторный краш провайдера) — человеку.
    if event_type == "executor.failed":
        if "timed out" not in error.lower() and "timeout" not in error.lower():
            log(f"  task#{task_id}: executor.failed не по таймауту ({error[:80]}) — человеку")
            return
        if timeout_resume_cap_reached(workflow_id, str(task_id)):
            return
        resume_timeout(workflow_id, str(task_id))
        return

    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT provider, status, result FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        wf = conn.execute(
            "SELECT status, current_round, state_version FROM workflows WHERE id = ?",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row or not wf:
        log(f"  task#{task_id}: нет записи — пропуск")
        return
    provider, task_status, result = row
    wf_status, wf_round, wf_version = wf

    if wf_status != "awaiting_human":
        log(f"  task#{task_id}: воркфлоу уже {wf_status} — resume не нужен")
        return

    report = result or ""
    if len(report) < 80:
        log(f"  task#{task_id}: отчёт слишком короткий ({len(report)} симв) — не рискую")
        return
    tail = report[-4000:]

    answer = ask_jev(key, tail, mode)
    if answer is None:
        return
    answers = answer.get("answers", {})
    done = (answers.get("done") or {}).get("noul", 0.0)
    verdict = (answers.get("verdict") or {})
    choice = verdict.get("choice", "")
    conf = verdict.get("confidence", 0.0)
    usage = answer.get("usage", {})
    auto_ok = choice in (AUTO_VERDICTS if mode == "executor"
                         else {"valid_revision", "valid_pass"})
    log(
        f"  Jev[{mode}] по task#{task_id} ({provider}): choice={choice} conf={conf:.2f} "
        f"noul_done={done:.2f} tokens={usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')}"
    )

    if auto_ok and conf >= MIN_CHOICE_CONF and done >= MIN_NOUL_DONE:
        body = {
            "expected_version": wf_version,
            "text": (
                f"{RESUME_TEXT}\n\n"
                f"(авто-верdict: исполнитель {provider} потерял строку «ИТОГ», "
                f"но TypeSafe Jev распознал по отчёту задачи #{task_id} вердикт "
                f"«{choice}» с уверенностью {conf:.2f}; noul-выполнено={done:.2f})"
            ),
            "resume": True,
        }
        for attempt in (1, 2):
            try:
                api(f"/api/workflows/{workflow_id}/human-input", "POST", body)
                break
            except urllib.error.HTTPError as e:
                if e.code == 409 and attempt == 1:
                    conn = db_connect()
                    try:
                        wf_version = conn.execute(
                            "SELECT state_version FROM workflows WHERE id = ?", (workflow_id,)
                        ).fetchone()[0]
                        body["expected_version"] = wf_version
                    finally:
                        conn.close()
                    continue
                log(f"  resume не прошёл: HTTP {e.code}")
                return
        log(f"  >>> AUTO-RESUME отправлен: вердикт «{choice}» (conf {conf:.2f}) — раунд {wf_round} продолжен без человека")
    else:
        log(f"  >>> без действий: вердикт «{choice}»/уверенность недостаточна — решение за человеком")




SILENCE_LIMIT_SEC = 15 * 60
GUARD_KILLS: dict[str, int] = {}
MAX_GUARD_KILLS_PER_TASK = 2


def silence_guard(state: dict) -> None:
    """Задача running на codex-harness (mmx-m3), а rollout-сессия не пишет
    ≥15 минут → процесс ждёт мёртвый сетевой ответ. Добиваем процесс:
    воркер закроет задачу failed, вотчер авто-resume'ит (лимит 2/задача)."""
    import glob
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='running' AND provider='mmx-m3'"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return
    files = sorted(
        glob.glob("C:/Tools/codex-minimax/sessions/**/rollout-*.jsonl",
                  recursive=True),
        key=lambda p: os.path.getmtime(p))
    if not files:
        return
    newest = files[-1]
    age = time.time() - os.path.getmtime(newest)
    if age < SILENCE_LIMIT_SEC:
        return
    for (task_id,) in rows:
        guard_key = f"{task_id}"
        if GUARD_KILLS.get(guard_key, 0) >= MAX_GUARD_KILLS_PER_TASK:
            continue
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
            capture_output=True, text=True, timeout=30)
        killed = [x for x in out.stdout.split() if x.strip().isdigit()]
        GUARD_KILLS[guard_key] = GUARD_KILLS.get(guard_key, 0) + 1
        log(f"СТРАЖ: задача #{task_id} mmx-m3 молчит {int(age)//60} мин "
            f"(сессия {os.path.basename(newest)[:40]}) → codex убит ({','.join(killed)})")
        # Воркер пометит failed через пару секунд; сами делаем resume (как
        # timeout-resume), чтобы петля замкнулась без человека.
        time.sleep(12)
        wf_rows = None
        conn2 = db_connect()
        try:
            wf_rows = [r for r in conn2.execute(
                "SELECT id, status, state_version FROM workflows WHERE status='awaiting_human'"
            ).fetchall() if r[0] not in DEAD_WORKFLOWS]
        finally:
            conn2.close()
        for wf_id, wf_status, wf_version in wf_rows:
            note = (f"(авто-resume: страж убил зависший mmx-m3 процесса задачи #{task_id}; "
                    f"молчание {int(age)//60} мин)")
            body = {"expected_version": wf_version,
                    "text": RESUME_TEXT + "\n\n" + note,
                    "resume": True}
            for attempt in (1, 2):
                try:
                    api(f"/api/workflows/{wf_id}/human-input", "POST", body)
                    log(f"  >>> GUARD-RESUME отправлен: {wf_id} продолжен")
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 409 and attempt == 1:
                        try:
                            w2 = api(f"/api/workflows/{wf_id}")
                            body["expected_version"] = w2["state_version"]
                        except Exception:
                            pass
                        continue
                    log(f"  guard-resume не прошёл: HTTP {e.code}")


def main() -> None:
    key = load_key()
    state = load_state()
    log(f"watcher стартовал; last_seq={state.get('last_seq', 0)}; key: {'env' if os.environ.get('TYPESAFE_API_KEY') else KEY_FILE}")
    while True:
        try:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT seq, workflow_id, event_type, payload_json, created_at "
                    "FROM workflow_events WHERE seq > ? AND event_type IN "
                    "('executor.invalid_output', 'reviewer.invalid_output', "
                    "'automation.paused', 'executor.failed') ORDER BY seq",
                    (state.get("last_seq", 0),),
                ).fetchall()
                max_row = conn.execute("SELECT MAX(seq) FROM workflow_events").fetchone()[0]
            finally:
                conn.close()

            for seq, workflow_id, event_type, payload_raw, created_at in rows:
                # automation.paused бывает и с другими причинами — фильтруем
                # на аудитора внутри try_repair по reason.
                log(f"событие seq={seq} {event_type} ({created_at[:19]})")
                try:
                    payload = json.loads(payload_raw or "{}")
                except json.JSONDecodeError:
                    payload = {}
                try_repair(key, {"seq": seq, "workflow_id": workflow_id,
                                 "payload": payload, "event_type": event_type})
                state["last_seq"] = seq
                save_state(state)

            if max_row is not None and max_row > state.get("last_seq", 0):
                # продвигаем курсор и по неинтересным событиям
                state["last_seq"] = max_row
                save_state(state)
        except sqlite3.Error as e:
            log(f"DB: {e}")
        except Exception as e:  # noqa: BLE001 — вотчер не должен умирать
            log(f"цикл: {type(e).__name__}: {e}")

        # ── Страж тишины: codex-harness задача running, а сессия молчит ≥15 мин ──
        try:
            silence_guard(state)
        except Exception as e:
            log(f"страж: {type(e).__name__}: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    # Под pythonw (без консоли) stdout/stderr == None — печатать некуда.
    # Весь вывод всё равно дублируется в лог-файл, мусор отправляем в devnull.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stderr.reconfigure(encoding="utf-8")
    main()
