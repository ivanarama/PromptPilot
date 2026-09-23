"""Inbox Bot (docs/INBOX_TRIAGE_PLAN.md, этап 2).

Дочитывает журнал events.jsonl от poller'а и ведёт обращение в Telegram:

- триаж завершён -> карточка с кнопками «▶ Запустить» / «✖ Отклонить»;
- «▶ Запустить» -> задача исполнения в папке проекта из ТЗ
  (skip_permissions: нажатие кнопки и есть согласие на автономный запуск);
- задача исполнения завершена -> черновик ответа автору письма с кнопками
  «📤 Отправить автору» / «✖ Не отправлять»;
- «📤 Отправить» -> письмо по SMTP с In-Reply-To на исходное обращение.

Авто-проекты: секция "auto" в projects.json
  {"auto": {"Обмены/Пилот_ЕдиныйЗагрузчик": {"types": ["баг"], "min_confidence": 0.85}}}
— совпало (тип + уверенность) -> исполнение запускается без кнопки,
ограничение AUTO_MAX_PER_DAY.

Токен: INBOX_BOT_TOKEN в .env (свой бот от @BotFather, чтобы не делить
long-polling с основным ботом PromptPilot). Запуск:
  py -3.11 scripts/inbox/inbox_bot.py
"""

import email.message
import json
import re
import smtplib
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".tg_state.json"
EVENTS_FILE = ROOT / "events.jsonl"
META_CUT = "--- Meta ---"
AUTO_META = "auto"


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def tg(method: str, token: str, **params):
    data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"telegram {method}: {payload}")
    return payload.get("result")


def pp_request(env: dict, path: str, method: str = "GET", payload: dict | None = None):
    request = urllib.request.Request(env["PP_API"].rstrip("/") + path, method=method)
    token = env.get("PP_API_TOKEN", "")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30, data=data) as response:
        return json.loads(response.read().decode("utf-8"))


# --- состояние --------------------------------------------------------------

STATE_LOCK = threading.Lock()


def load_state() -> dict:
    state = {"updates_offset": 0, "events_offset": 0, "cards": {}, "execs": {},
             "auto_count": {}}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    for key in ("cards", "execs", "auto_count"):
        state.setdefault(key, {})
    state.setdefault("updates_offset", 0)
    state.setdefault("events_offset", 0)
    return state


def save_state(state: dict) -> None:
    with STATE_LOCK:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                              encoding="utf-8")


def auto_today(state: dict) -> int:
    today = time.strftime("%Y-%m-%d")
    return state["auto_count"].get(today, 0)


def note_auto(state: dict) -> None:
    today = time.strftime("%Y-%m-%d")
    state["auto_count"] = {today: state["auto_count"].get(today, 0) + 1}


# --- разбор результата триажа ----------------------------------------------

def parse_triage(result: str) -> dict:
    """ПРОЕКТ/РАБОЧАЯ ПАПКА/ТЗ из текста результата (предупреждения #89 — мимо)."""
    text = (result or "").split(META_CUT)[0]
    index = text.find("ПРОЕКТ:")
    if index >= 0:
        text = text[index:]
    fields = {}
    for key in ("ПРОЕКТ", "УВЕРЕННОСТЬ", "ТИП", "ПРИОРИТЕТ", "РАБОЧАЯ ПАПКА"):
        match = re.search(rf"^{key}:\s*(.+)$", text, re.M | re.I)
        fields[key.lower()] = match.group(1).strip() if match else ""
    spec_index = text.upper().find("ТЕХНИЧЕСКОЕ ЗАДАНИЕ:")
    spec = text[spec_index + len("ТЕХНИЧЕСКОЕ ЗАДАНИЕ:"):].strip() \
        if spec_index >= 0 else text.strip()
    return {"fields": fields, "spec": spec}


def auto_matches(config: dict, parsed: dict) -> bool:
    rules = config.get(AUTO_META, {})
    project = parsed["fields"].get("проект", "")
    rule = rules.get(project)
    if not rule:
        return False
    try:
        confidence = float(str(parsed["fields"].get("уверенность", "0")).replace(",", "."))
    except ValueError:
        confidence = 0.0
    triage_type = parsed["fields"].get("тип", "").lower()
    types = [str(item).lower() for item in rule.get("types", [])]
    return (not types or triage_type in types) and \
        confidence >= float(rule.get("min_confidence", 0.85))


# --- задачи -----------------------------------------------------------------

VERDICT_CONTRACT = (
    "\n\nПоследней строкой ответа напиши ровно одну из:\n"
    "ИТОГ: ГОТОВО — сделано\n"
    "ИТОГ: УЖЕ СДЕЛАНО — оказалось, что уже исправлено\n"
    "ИТОГ: НУЖЕН ЧЕЛОВЕК — нужно решение или доступ человека\n"
    "ИТОГ: НЕ СМОГ — не получилось\n"
    "После двоеточия можно коротко пояснить причину."
)


def create_execution(env: dict, parsed: dict, meta: dict, state: dict) -> int:
    path = parsed["fields"].get("рабочая папка", "")
    project = parsed["fields"].get("проект", "")
    prompt = (f"Проект: {project}\nРабочая папка: {path}\n\n"
              f"Техническое задание:\n{parsed['spec']}" + VERDICT_CONTRACT)
    payload = {
        "prompt": prompt,
        "provider": env.get("EXEC_PROVIDER", env.get("PP_PROVIDER", "claude-z")),
        "priority": int(env.get("EXEC_PRIORITY", "3")),
        "working_dir": path if Path(path).is_dir() else env.get("PP_WORKING_DIR", str(ROOT)),
        "skip_permissions": env.get("EXEC_SKIP_PERMISSIONS", "1") == "1",
        "tg_chat_id": int(env["TG_CHAT_ID"]) if env.get("TG_CHAT_ID") else None,
    }
    if env.get("EXEC_MODEL"):
        payload["model"] = env["EXEC_MODEL"]
    payload = {k: v for k, v in payload.items() if v is not None}
    task = pp_request(env, "/api/tasks", "POST", payload)
    with STATE_LOCK:
        state["execs"][str(task["id"])] = {
            "triage_task_id": meta.get("triage_task_id"),
            "from": meta.get("from", ""),
            "subject": meta.get("subject", ""),
            "message_id": meta.get("message_id", ""),
        }
    return int(task["id"])


# --- SMTP-ответ -------------------------------------------------------------

def sender_address(raw: str) -> str:
    return raw.split("<")[-1].strip("> ").strip() if "<" in raw else raw.strip()


def base_subject(subject: str) -> str:
    cleaned = subject.strip()
    while True:
        stripped = re.sub(r"^(fwd?|fw)\s*:\s*", "", cleaned, flags=re.I)
        if stripped == cleaned:
            break
        cleaned = stripped
    return re.sub(r"\s+", " ", cleaned).strip()


def send_reply(env: dict, meta: dict, reply_text: str) -> None:
    message = email.message.EmailMessage()
    message["From"] = (f"{env.get('SMTP_FROM_NAME', 'PromptPilot')} "
                       f"<{env['IMAP_USER']}>" if env.get("SMTP_FROM_NAME")
                       else env["IMAP_USER"])
    message["To"] = sender_address(meta["from"])
    message["Subject"] = "Re: " + base_subject(meta["subject"])
    if meta.get("message_id") and meta["message_id"].startswith("<"):
        message["In-Reply-To"] = meta["message_id"]
        message["References"] = meta["message_id"]
    signature = env.get("REPLY_SIGNATURE", "")
    body = reply_text.strip()
    if signature:
        body += "\n\n" + signature
    message.set_content(body)
    with smtplib.SMTP_SSL(env.get("SMTP_HOST", "smtp.mail.ru"),
                          int(env.get("SMTP_PORT", "465")), timeout=30) as smtp:
        smtp.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
        smtp.send_message(message)


# --- карточки ---------------------------------------------------------------

def spec_card_text(meta: dict, parsed: dict, task_id: int) -> str:
    fields = parsed["fields"]
    return (
        f"<b>Новое обращение</b> · задача #{task_id}\n"
        f"От: {meta['from']}\n"
        f"Тема: {meta['subject']}\n\n"
        f"Проект: <b>{fields.get('проект', '—')}</b>"
        f" · уверенность {fields.get('уверенность', '—')}"
        f" · {fields.get('тип', '—')} · приоритет {fields.get('приоритет', '—')}\n"
        f"Папка: <code>{fields.get('рабочая папка', '—')}</code>\n\n"
        f"<pre>{parsed['spec'][:1200]}</pre>\n\n"
        "▶ Запустить — автономное исполнение в папке проекта"
    )


def watch_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "▶ Запустить", "callback_data": "run"},
        {"text": "✖ Отклонить", "callback_data": "rej"},
    ]]}


def reply_keyboard(exec_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "📤 Отправить автору", "callback_data": f"snd:{exec_id}"},
        {"text": "✖ Не отправлять", "callback_data": f"dis:{exec_id}"},
    ]]}


# --- циклы ------------------------------------------------------------------

def handle_callback(env: dict, state: dict, config: dict, callback: dict) -> None:
    token = env["INBOX_BOT_TOKEN"]
    chat_id = str(callback["message"]["chat"]["id"])
    if env.get("TG_CHAT_ID") and chat_id != str(env["TG_CHAT_ID"]):
        return  # чужой чат — игнорируем
    data = callback.get("data", "")
    callback_id = callback["id"]
    try:
        if data in ("run", "rej"):
            card_message_id = str(callback["message"]["message_id"])
            card = next((item for item in state["cards"].values()
                         if str(item.get("tg_message_id")) == card_message_id), None)
            if data == "rej":
                if card:
                    state["cards"].pop(str(card["meta"]["triage_task_id"]), None)
                tg("editMessageText", token, chat_id=chat_id,
                   message_id=int(card_message_id), text="✖ Обращение отклонено.")
                save_state(state)
                tg("answerCallbackQuery", token, callback_query_id=callback_id)
                return
            if not card or not card.get("spec"):
                tg("answerCallbackQuery", token, callback_query_id=callback_id,
                   text="Карточка устарела")
                return
            parsed = parse_triage(card["spec"])
            exec_id = create_execution(env, parsed, card["meta"], state)
            tg("editMessageText", token, chat_id=chat_id,
               message_id=int(card_message_id),
               text=f"▶ Запущено: задача исполнения #{exec_id}. "
                    f"По завершении пришлю черновик ответа.")
            save_state(state)
            tg("answerCallbackQuery", token, callback_query_id=callback_id)
            return
        elif data.startswith("snd:"):
            exec_id = data.split(":", 1)[1]
            meta = state["execs"].get(exec_id, {})
            task = pp_request(env, f"/api/tasks/{exec_id}")
            reply_text = (task.get("result") or "").split(META_CUT)[0]
            reply_text = re.sub(r"^⚠️.*\n?", "", reply_text)
            send_reply(env, meta, reply_text)
            tg("editMessageText", token, chat_id=chat_id,
               message_id=callback["message"]["message_id"],
               text=f"📤 Ответ отправлен автору ({meta.get('from', '?')}).")
            save_state(state)
        elif data.startswith("dis:"):
            tg("editMessageText", token, chat_id=chat_id,
               message_id=callback["message"]["message_id"],
               text="Ответ не отправлен.")
        tg("answerCallbackQuery", token, callback_query_id=callback_id)
    except Exception as exc:
        try:
            tg("answerCallbackQuery", token, callback_query_id=callback_id,
               text=f"Ошибка: {str(exc)[:150]}")
        except Exception:
            pass
        print(f"!! callback {data}: {type(exc).__name__}: {exc}", flush=True)


def watch_progress(env: dict, state: dict, config: dict, token: str) -> None:
    """Триаж завершился -> карточка; исполнение завершилось -> черновик ответа."""
    chat_id = env.get("TG_CHAT_ID")
    if not chat_id:
        return
    for triage_id, card in list(state["cards"].items()):
        if card.get("sent"):
            continue
        task = pp_request(env, f"/api/tasks/{triage_id}")
        if task["status"] not in ("completed", "failed", "cancelled"):
            continue
        card["sent"] = True
        if task["status"] != "completed":
            tg("sendMessage", token, chat_id=chat_id,
               text=f"Триаж #{triage_id} завершился со статусом {task['status']}.")
            continue
        parsed = parse_triage(task.get("result") or "")
        if auto_matches(config, parsed):
            if auto_today(state) < int(env.get("AUTO_MAX_PER_DAY", "3")):
                exec_id = create_execution(env, parsed, card["meta"], state)
                note_auto(state)
                tg("sendMessage", token, chat_id=chat_id,
                   text=f"🤖 Авто-проект: обращение «{card['meta']['subject']}» "
                        f"запущено как задача #{exec_id}.")
            else:
                tg("sendMessage", token, chat_id=chat_id,
                   text=f"Авто-лимит исчерпан, обращение «{card['meta']['subject']}» "
                        "ждёт ручного запуска.")
            continue
        sent = tg("sendMessage", token, chat_id=chat_id,
                  text=spec_card_text(card["meta"], parsed, int(triage_id)),
                  parse_mode="HTML", reply_markup=watch_keyboard())
        card["spec"] = task.get("result") or ""
        card["tg_message_id"] = sent["message_id"]
        state["cards"][triage_id] = card
    for exec_id, meta in list(state["execs"].items()):
        if meta.get("drafted"):
            continue
        task = pp_request(env, f"/api/tasks/{exec_id}")
        if task["status"] not in ("completed", "failed", "cancelled"):
            continue
        meta["drafted"] = True
        if task["status"] != "completed" or (task.get("verdict") or "").upper() \
                in ("НЕ СМОГ", "НУЖЕН ЧЕЛОВЕК"):
            tg("sendMessage", token, chat_id=chat_id,
               text=f"Задача #{exec_id} не готова к отправке "
                    f"(статус {task['status']}, вердикт {task.get('verdict') or '—'}). "
                    "Ответ автору не формирую.")
            continue
        reply = (task.get("result") or "").split(META_CUT)[0][:3500]
        tg("sendMessage", token, chat_id=chat_id,
           text=f"<b>Черновик ответа</b> по «{meta['subject']}» "
                f"(задача #{exec_id}):\n\n<pre>{reply[:2500]}</pre>",
           parse_mode="HTML", reply_markup=reply_keyboard(int(exec_id)))
    save_state(state)


def tail_events(state: dict, token: str, chat_id: str) -> None:
    if not EVENTS_FILE.exists():
        return
    with STATE_LOCK:
        offset = state["events_offset"]
        lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
        fresh, state["events_offset"] = lines[offset:], len(lines)
    for line in fresh:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "triage_created":
            continue
        with STATE_LOCK:
            state["cards"][str(event["task_id"])] = {
                "meta": {
                    "triage_task_id": event["task_id"],
                    "from": event["from"],
                    "subject": event["subject"],
                    "message_id": event.get("message_id", ""),
                },
            }
    if fresh:
        tg("sendMessage", token, chat_id=chat_id,
           text=f"📥 Новых обращений: {len(fresh)} — слежу за триажем.")


def updates_loop(env: dict, state: dict, config: dict, token: str) -> None:
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": json.dumps(["callback_query"])}
            if state["updates_offset"]:
                params["offset"] = state["updates_offset"]
            updates = tg("getUpdates", token, **params) or []
            for update in updates:
                state["updates_offset"] = update["update_id"] + 1
                callback = update.get("callback_query")
                if callback:
                    handle_callback(env, state, config, callback)
                save_state(state)
        except Exception as exc:
            print(f"!! updates: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)


def watch_loop(env: dict, state: dict, config: dict, token: str) -> None:
    chat_id = env.get("TG_CHAT_ID", "")
    while True:
        try:
            tail_events(state, token, chat_id)
            watch_progress(env, state, config, token)
        except Exception as exc:
            print(f"!! watch: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(20)


def main() -> int:
    env = load_env(ROOT / ".env")
    token = env.get("INBOX_BOT_TOKEN") or env.get("PP_TG_TOKEN", "")
    missing = [key for key in ("INBOX_BOT_TOKEN", "PP_API", "TG_CHAT_ID")
               if not env.get(key) and key != "INBOX_BOT_TOKEN"]
    if not token:
        print("Нет INBOX_BOT_TOKEN в .env — создай бота у @BotFather и вставь "
              "токен (PP_TG_TOKEN не подставляй, чтобы не делить long-polling "
              "с основным ботом PromptPilot).")
        return 2
    if missing:
        print("Нет обязательных ключей .env: " + ", ".join(missing))
        return 2
    config_path = ROOT / env.get("CATALOG", "projects.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    state = load_state()
    print("inbox_bot: запущен", flush=True)
    threads = [
        threading.Thread(target=updates_loop,
                         args=(env, state, config, token), daemon=True),
        threading.Thread(target=watch_loop,
                         args=(env, state, config, token), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("inbox_bot: остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
