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
ограничение AUTO_MAX_PER_DAY. Только для известных отправителей (явный
ALLOW_FROM или sender_map): решение «запускать» принимает модель по тексту
письма, и от незнакомца оно не должно запускать агента без человека.

Игровые заявки ([KT], открыты всем): никакого автозапуска; папка — всегда
KT_WORKING_DIR, что бы ни написал триаж; отклонённое хранителем концепции
кнопку «Запустить» не получает.

Карточка показывает ТЗ целиком — ровно то, что уйдёт исполнителю. Папка
обычного обращения должна лежать внутри PROJECTS_ROOT.

Токен: INBOX_BOT_TOKEN в .env (свой бот от @BotFather, чтобы не делить
long-polling с основным ботом PromptPilot). Запуск:
  py -3.11 scripts/inbox/inbox_bot.py
"""

import email.message
import html
import json
import os
import re
import smtplib
import sys
import threading
import time
import urllib.parse
import urllib.request
from email.utils import parseaddr
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
    for key in ("ПРОЕКТ", "УВЕРЕННОСТЬ", "ТИП", "ПРИОРИТЕТ", "РАБОЧАЯ ПАПКА",
                "ВЕРДИКТ", "КАТЕГОРИЯ"):
        # The last line wins: a model may echo the answer format first.
        matches = re.findall(rf"^{key}:\s*(.+)$", text, re.M | re.I)
        fields[key.lower()] = matches[-1].strip() if matches else ""
    head = re.split(r"\s+[—–-]\s+|[,(.;!]", fields["вердикт"], maxsplit=1)[0]
    head = head.strip().strip("*").strip().upper()
    fields["вердикт"] = head if head in ("ПРИНЯТЬ", "ОТКЛОНИТЬ", "УТОЧНИТЬ") else ""
    spec_index = text.upper().find("ТЕХНИЧЕСКОЕ ЗАДАНИЕ:")
    spec = text[spec_index + len("ТЕХНИЧЕСКОЕ ЗАДАНИЕ:"):].strip() \
        if spec_index >= 0 else text.strip()
    return {"fields": fields, "spec": spec}


def _address(raw: str) -> str:
    return parseaddr(raw or "")[1].strip().lower()


def sender_known(env: dict, config: dict, sender: str) -> bool:
    """Named explicitly: an ALLOW_FROM entry or a sender_map rule.

    An empty ALLOW_FROM lets every letter in, so it cannot vouch for anyone.
    """
    address = _address(sender)
    if not address:
        return False
    domain = address.rpartition("@")[2]
    entries = [item.strip().lower() for item in env.get("ALLOW_FROM", "").split(",")
               if item.strip()]
    if any(address == item or domain == item for item in entries):
        return True
    sender_map = {str(key).lower() for key in (config.get("sender_map") or {})}
    return address in sender_map or f"*@{domain}" in sender_map


def execution_dir(env: dict, parsed: dict, meta: dict) -> str:
    """Folder for the execution task; ValueError if it may not be used.

    The folder comes from the triage answer, i.e. from a model that read a
    stranger's letter. A game proposal always runs in KT_WORKING_DIR; any
    other request must stay inside PROJECTS_ROOT.
    """
    if meta.get("kt"):
        path = env.get("KT_WORKING_DIR", "")
        if not path or not Path(path).is_dir():
            raise ValueError("KT_WORKING_DIR не задан или не существует")
        return path
    path = parsed["fields"].get("рабочая папка", "")
    root = env.get("PROJECTS_ROOT", "")
    if not path or not Path(path).is_dir():
        raise ValueError(f"папка «{path or '—'}» не найдена")
    if not root:
        raise ValueError("PROJECTS_ROOT не задан — папку из триажа не с чем сверить")
    real_path = os.path.normcase(os.path.realpath(path))
    real_root = os.path.normcase(os.path.realpath(root))
    try:
        inside = os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:  # different drives on Windows
        inside = False
    if not inside:
        raise ValueError(f"папка «{path}» вне PROJECTS_ROOT")
    return path


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
    path = execution_dir(env, parsed, meta)
    project = ("Сказки Королевства" if meta.get("kt")
               else parsed["fields"].get("проект", ""))
    prompt = (f"Проект: {project}\nРабочая папка: {path}\n\n"
              f"Техническое задание:\n{parsed['spec']}" + VERDICT_CONTRACT)
    provider = env.get("EXEC_PROVIDER", env.get("PP_PROVIDER", "claude-z"))
    if meta.get("kt"):
        provider = env.get("KT_EXEC_PROVIDER") or provider
    payload = {
        "prompt": prompt,
        "provider": provider,
        "priority": int(env.get("EXEC_PRIORITY", "3")),
        "working_dir": path,
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
            "reply_to": meta.get("reply_to", ""),
            "subject": meta.get("subject", ""),
            "message_id": meta.get("message_id", ""),
            "kt": bool(meta.get("kt")),
        }
    return int(task["id"])


# --- SMTP-ответ -------------------------------------------------------------

def base_subject(subject: str) -> str:
    cleaned = subject.strip()
    while True:
        stripped = re.sub(r"^(fwd?|fw)\s*:\s*", "", cleaned, flags=re.I)
        if stripped == cleaned:
            break
        cleaned = stripped
    return re.sub(r"\s+", " ", cleaned).strip()


def reply_address(env: dict, meta: dict) -> str:
    """Where a reply to the author goes, or "" when there is nobody to answer.

    A letter from the site form comes FROM the form service; the player's
    own address, if the form asked for it, is in Reply-To. Answering From
    would send the draft to the form service.
    """
    if meta.get("reply_to"):
        return _address(meta["reply_to"])
    sender = _address(meta.get("from", ""))
    form_domain = env.get("KT_FORM_DOMAIN", "formsubmit.co").strip().lower()
    host = sender.rpartition("@")[2]
    if host == form_domain or host.endswith("." + form_domain):
        return ""
    return sender


def send_reply(env: dict, meta: dict, reply_text: str) -> None:
    recipient = reply_address(env, meta)
    if not recipient:
        raise ValueError("у заявки нет адреса автора (форма не передала Reply-To)")
    message = email.message.EmailMessage()
    message["From"] = (f"{env.get('SMTP_FROM_NAME', 'PromptPilot')} "
                       f"<{env['IMAP_USER']}>" if env.get("SMTP_FROM_NAME")
                       else env["IMAP_USER"])
    message["To"] = recipient
    message["Subject"] = "Re: " + base_subject(meta["subject"])
    if meta.get("message_id") and meta["message_id"].startswith("<"):
        message["In-Reply-To"] = meta["message_id"]
        message["References"] = meta["message_id"]
    signature = env.get("REPLY_SIGNATURE", "")
    body = reply_text.strip()
    if signature:
        body += "\n\n" + signature
    message.set_content(body)
    # Mail.ru безопасность иногда волнами отвергает вход автоматики —
    # повторяем с паузой: блокировка отпускает через десятки секунд.
    last_exc = None
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(env.get("SMTP_HOST", "smtp.mail.ru"),
                                  int(env.get("SMTP_PORT", "465")), timeout=30) as smtp:
                smtp.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
                smtp.send_message(message)
            return
        except Exception as exc:
            last_exc = exc
            print(f"  !! SMTP попытка {attempt + 1} не прошла: {exc}", flush=True)
            time.sleep(20 * (attempt + 1))
    raise last_exc


# --- карточки ---------------------------------------------------------------

SPEC_CHUNK = 3000  # Telegram caps a message at 4096 characters after parsing


def _esc(value) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


def card_messages(env: dict, meta: dict, parsed: dict, task_id: int) -> list[str]:
    """A finished triage as Telegram messages: header, then the WHOLE spec.

    The executor gets exactly this spec. The card used to show its first 1200
    characters, so an instruction placed further down went to the agent
    unseen. Everything is HTML-escaped: a sender like «Имя <a@b.c>» or a "<"
    in the spec made Telegram reject the card, and the card was lost.
    """
    fields = parsed["fields"]
    if meta.get("kt"):
        header = (
            f"<b>🎲 Заявка в игру</b> · задача #{task_id}\n"
            f"Тема: {_esc(meta.get('subject'))}\n"
            f"Хранитель концепции: <b>{_esc(fields.get('вердикт'))}</b>"
            f" · {_esc(fields.get('категория'))} · приоритет {_esc(fields.get('приоритет'))}\n"
            f"Папка: <code>{_esc(env.get('KT_WORKING_DIR'))}</code>"
        )
    else:
        header = (
            f"<b>Новое обращение</b> · задача #{task_id}\n"
            f"От: {_esc(meta.get('from'))}\n"
            f"Тема: {_esc(meta.get('subject'))}\n\n"
            f"Проект: <b>{_esc(fields.get('проект'))}</b>"
            f" · уверенность {_esc(fields.get('уверенность'))}"
            f" · {_esc(fields.get('тип'))} · приоритет {_esc(fields.get('приоритет'))}\n"
            f"Папка: <code>{_esc(fields.get('рабочая папка'))}</code>"
        )
    spec = parsed["spec"] or ""
    parts = [spec[i:i + SPEC_CHUNK] for i in range(0, len(spec), SPEC_CHUNK)] or [""]
    messages = []
    for index, part in enumerate(parts, start=1):
        label = f"ТЗ, часть {index}/{len(parts)}" if len(parts) > 1 else "ТЗ"
        body = f"<b>{label}</b>\n<pre>{html.escape(part)}</pre>"
        messages.append(f"{header}\n\n{body}" if index == 1 else body)
    messages[-1] += ("\n\n▶ Запустить — автономное исполнение ровно этого ТЗ"
                     + (" в папке игры" if meta.get("kt") else " в папке проекта"))
    return messages


def draft_messages(meta: dict, exec_id, reply: str, has_address: bool) -> list[str]:
    """The reply draft, whole: «📤 Отправить» mails exactly this text.

    Only its first 2500 characters used to be shown while the whole result
    was mailed to an outside address.
    """
    header = (f"<b>Черновик ответа</b> по «{_esc(meta.get('subject'))}» "
              f"(задача #{exec_id})")
    parts = [reply[i:i + SPEC_CHUNK] for i in range(0, len(reply), SPEC_CHUNK)] or [""]
    messages = []
    for index, part in enumerate(parts, start=1):
        label = f", часть {index}/{len(parts)}" if len(parts) > 1 else ""
        body = f"<pre>{html.escape(part)}</pre>"
        messages.append(f"{header}{label}:\n\n{body}" if index == 1
                        else f"<b>Черновик{label}</b>\n{body}")
    if not has_address:
        messages[-1] += "\n\nУ заявки нет адреса автора — отправить некуда."
    return messages


def send_messages(token: str, chat_id, messages: list[str], keyboard=None) -> dict:
    """Send in order; the keyboard goes on the last message, which is returned."""
    sent = None
    for index, text in enumerate(messages):
        extra = {"reply_markup": keyboard} if keyboard and index == len(messages) - 1 else {}
        sent = tg("sendMessage", token, chat_id=chat_id, text=text,
                  parse_mode="HTML", **extra)
    return sent


MAX_SEND_ATTEMPTS = 5


def deliver(record: dict, flag: str, send, *args, **kwargs) -> None:
    """Call send(*args, **kwargs) and set the flag only once it went through.

    The flag used to be set before sending, so a message Telegram refused was
    lost for good. A message that keeps failing is given up after
    MAX_SEND_ATTEMPTS passes instead of being retried forever.
    """
    try:
        send(*args, **kwargs)
    except Exception as exc:
        record["send_errors"] = record.get("send_errors", 0) + 1
        print(f"!! Telegram: попытка {record['send_errors']} не удалась: "
              f"{type(exc).__name__}: {exc}", flush=True)
        if record["send_errors"] < MAX_SEND_ATTEMPTS:
            return
    record[flag] = True


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

def mark_stage(env: dict, triage_task_id, stage: str) -> None:
    """Отметить стадию предложения в фиде сайта (стена предложений)."""
    feed_path = env.get("KT_SITE_FEED")
    if not feed_path:
        return
    path = Path(feed_path)
    if not path.exists():
        return
    try:
        feed = json.loads(path.read_text(encoding="utf-8"))
        for item in feed.get("items", []):
            if str(item.get("task_id")) == str(triage_task_id):
                item["stage"] = stage
        path.write_text(json.dumps(feed, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"!! mark_stage: {exc}", flush=True)


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
            mark_stage(env, card["meta"].get("triage_task_id"), "в работе")
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
            reply_text = draft_text(task)
            send_reply(env, meta, reply_text)
            tg("editMessageText", token, chat_id=chat_id,
               message_id=callback["message"]["message_id"],
               text=f"📤 Ответ отправлен автору ({reply_address(env, meta)}).")
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


TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def draft_text(task: dict) -> str:
    """The reply as it is shown and as it is mailed — one function for both."""
    text = (task.get("result") or "").split(META_CUT)[0]
    return re.sub(r"^⚠️.*\n?", "", text).strip()


def watch_progress(env: dict, state: dict, config: dict, token: str) -> None:
    """Триаж завершился -> карточка; исполнение завершилось -> черновик ответа."""
    chat_id = env.get("TG_CHAT_ID")
    if not chat_id:
        return
    for triage_id, card in list(state["cards"].items()):
        if card.get("sent"):
            continue
        task = pp_request(env, f"/api/tasks/{triage_id}")
        if task["status"] not in TERMINAL_STATUSES:
            continue
        meta = card["meta"]
        if task["status"] != "completed":
            deliver(card, "sent", tg, "sendMessage", token, chat_id=chat_id,
                    text=f"Триаж #{triage_id} завершился со статусом {task['status']}.")
            continue
        result = task.get("result") or ""
        parsed = parse_triage(result)
        if meta.get("kt") and parsed["fields"].get("вердикт") == "ОТКЛОНИТЬ":
            deliver(card, "sent", tg, "sendMessage", token, chat_id=chat_id,
                    text=f"🎲 Заявка «{meta.get('subject', '')}» (задача #{triage_id}) "
                         "отклонена хранителем концепции — запускать нечего.")
            continue
        warning = ""
        # Game proposals never start on their own: anyone can send one.
        if not meta.get("kt") and auto_matches(config, parsed):
            if not sender_known(env, config, meta.get("from", "")):
                warning = ("Авто-правило совпало, но отправителя нет в ALLOW_FROM "
                           "или sender_map — нужен ручной запуск.")
            elif auto_today(state) >= int(env.get("AUTO_MAX_PER_DAY", "3")):
                warning = "Авто-лимит на сегодня исчерпан — нужен ручной запуск."
            else:
                try:
                    exec_id = create_execution(env, parsed, meta, state)
                except ValueError as exc:
                    warning = f"Автозапуск отменён: {exc}."
                else:
                    note_auto(state)
                    deliver(card, "sent", tg, "sendMessage", token, chat_id=chat_id,
                            text=f"🤖 Авто-проект: обращение «{meta.get('subject', '')}» "
                                 f"запущено как задача #{exec_id}.")
                    continue
        messages = card_messages(env, meta, parsed, int(triage_id))
        if warning:
            messages[0] = f"⚠ {html.escape(warning)}\n\n{messages[0]}"
        deliver(card, "sent", send_card, token, chat_id, messages, card, result)
    for exec_id, meta in list(state["execs"].items()):
        if meta.get("drafted"):
            continue
        task = pp_request(env, f"/api/tasks/{exec_id}")
        if task["status"] not in TERMINAL_STATUSES:
            continue
        if task["status"] != "completed" or (task.get("verdict") or "").upper() \
                in ("НЕ СМОГ", "НУЖЕН ЧЕЛОВЕК"):
            deliver(meta, "drafted", tg, "sendMessage", token, chat_id=chat_id,
                    text=f"Задача #{exec_id} не готова к отправке "
                         f"(статус {task['status']}, вердикт {task.get('verdict') or '—'}). "
                         "Ответ автору не формирую.")
            continue
        has_address = bool(reply_address(env, meta))
        messages = draft_messages(meta, exec_id, draft_text(task), has_address)
        keyboard = reply_keyboard(int(exec_id)) if has_address else None
        deliver(meta, "drafted", send_messages, token, chat_id, messages, keyboard)
    save_state(state)


def send_card(token: str, chat_id, messages: list[str], card: dict, result: str) -> None:
    """Send a triage card and remember what the ▶ button will run."""
    sent = send_messages(token, chat_id, messages, watch_keyboard())
    card["spec"] = result
    card["tg_message_id"] = sent["message_id"]


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
                    "reply_to": event.get("reply_to", ""),
                    "subject": event["subject"],
                    "message_id": event.get("message_id", ""),
                    # The bot must know a game proposal from a client request:
                    # different trust, different folder, no autostart.
                    "kt": bool(event.get("kt")),
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
