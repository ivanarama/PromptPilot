"""Inbox poller (MVP, docs/INBOX_TRIAGE_PLAN.md этап 1).

Забирает новые письма из IMAP-ящика, нормализует каждое в карточку обращения
и создаёт в PromptPilot задачу триажа: дешёвая модель определяет проект
(по каталогу projects.json) и собирает ТЗ. Результат прилетает в Telegram
как обычное уведомление задачи.

Подстраховка от спама (в порядке срабатывания):
- X-Spam-Flag: yes — пропуск без задачи;
- служебные/рассылки (Auto-Submitted, List-Unsubscribe/List-Id, Precedence
  bulk, no-reply отправители) — пропуск без задачи;
- ALLOW_FROM в .env: если задан, задачи создаются только от этих адресов/
  доменов, остальное помечается обработанным;
- MAX_PER_DAY: дневной лимит задач триажа (по умолчанию 20).

Дедупликация — по Message-ID в .state.json, а не по флагу UNSEEN: письмо,
прочитанное человеком в веб-почте до poller'а, всё равно будет обработано.

Запуск: py -3.11 scripts/inbox/inbox_poller.py [--dry] [--once]
Конфиг: scripts/inbox/.env (см. inbox.example.env). Секреты в git не попадают.
"""

import argparse
import email
import imaplib
import json
import re
import sys
import time
import urllib.request
from datetime import date
from email import policy
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".state.json"
EVENTS_FILE = ROOT / "events.jsonl"
ATTACH_ROOT = Path.home() / ".promptpilot" / "inbox"

NO_REPLY_RE = re.compile(
    r"^(no-?reply|donotreply|do-not-reply|mail-daemon|bounce[^@]*)@", re.I)
BULK_HINT_HEADERS = ("List-Unsubscribe", "List-Id", "X-Mailinglist")


def outbox_dir(env: dict) -> Path:
    return Path(env.get("OUTBOX_DIR", str(ROOT / "outbox")))


def safe_filename(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\n\r\t]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:limit].rstrip(" .")


def save_spec(env: dict, item: dict, task: dict) -> Path:
    """Готовое ТЗ обращения — в markdown-файл бэклога (outbox)."""
    status = task.get("status")
    stamp = (task.get("created_at") or "")[:10]
    name = f"{stamp} #{task['id']} — {safe_filename(item['subject'])}.md"
    path = outbox_dir(env) / name
    meta = (
        f"# {item['subject']}\n\n"
        f"- Задача PromptPilot: #{task['id']}\n"
        f"- От: {item['from']}\n"
        f"- Дата обращения: {item['date'] or task.get('created_at', '')}\n"
        f"- Статус триажа: {status} · вердикт: {task.get('verdict') or '—'}\n\n"
        "---\n\n"
    )
    body = (task.get("result") or "").strip()
    if status != "completed":
        body = f"Триаж не завершился (статус {status}):\n\n{task.get('error') or body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(meta + body + "\n", encoding="utf-8")
    return path


def flush_saves(env: dict, state: dict) -> None:
    """Сохранить ТЗ задач, завершившихся с прошлого прохода."""
    still_pending = []
    for item in state.get("pending_saves", []):
        try:
            request = urllib.request.Request(
                f"{env['PP_API'].rstrip('/')}/api/tasks/{item['task_id']}")
            token = env.get("PP_API_TOKEN", "")
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(request, timeout=15) as response:
                task = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"!! не удалось прочитать задачу #{item['task_id']}: {exc}")
            still_pending.append(item)
            continue
        if task["status"] in ("pending", "running", "retry", "scheduled"):
            still_pending.append(item)
            continue
        path = save_spec(env, item, task)
        print(f"  -> ТЗ сохранено: {path.name}")
    state["pending_saves"] = still_pending


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def message_body(msg: email.message.Message) -> str:
    """text/plain целиком; иначе срез текста из html без внешних зависимостей."""
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and plain is None:
                plain = part.get_content()
            elif ctype == "text/html" and html is None:
                html = part.get_content()
        text = plain if plain not in (None, "") else (html or "")
    else:
        text = msg.get_content()
    text = str(text)
    if "<html" in text.lower() or "<br" in text.lower():
        text = re.sub(r"(?is)<(style|script).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def save_attachments(msg: email.message.Message, message_id: str) -> list[str]:
    """Сохранить вложения; вернуть пути — исполнитель/аудитор смогут открыть."""
    safe_dir = re.sub(r"[^A-Za-z0-9_.-]", "_", message_id.strip("<>"))[:80]
    target = ATTACH_ROOT / safe_dir
    saved = []
    for index, part in enumerate(msg.walk()):
        filename = part.get_filename() or ""
        disp = str(part.get("Content-Disposition") or "")
        payload = part.get_payload(decode=True)
        if payload is None or "attachment" not in disp and not filename:
            continue
        name = decode_mime(filename) or f"attachment-{index + 1}.bin"
        name = re.sub(r"[^A-Za-zА-Яа-я0-9_. -]", "_", name)
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        path.write_bytes(payload)
        saved.append(str(path))
    return saved


def bulk_reason(msg: email.message.Message, sender: str) -> str | None:
    """Причина считать письмо служебным/спамом, или None для живого обращения."""
    if (msg.get("X-Spam-Flag") or "").strip().lower() == "yes":
        return "помечен спамом отправителем"
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    if any(msg.get(header) for header in BULK_HINT_HEADERS):
        return "рассылка (List-*)"
    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "junk"):
        return f"Precedence: {precedence}"
    address = (sender.split("<")[-1].strip(">") if "<" in sender else sender).strip().lower()
    if NO_REPLY_RE.match(address):
        return "no-reply отправитель"
    return None


def sender_allowed(sender: str, allow_from: str) -> bool:
    """ALLOW_FROM: адреса/домены через запятую; пусто — принимать от всех."""
    entries = [item.strip().lower() for item in allow_from.split(",") if item.strip()]
    if not entries:
        return True
    address = (sender.split("<")[-1].strip(">") if "<" in sender else sender).lower()
    return any(address == item or address.endswith("@" + item) for item in entries)


# --- Каталог проектов из файловой системы (PROJECTS_ROOT) -------------------

DOC_NAMES = ("README.md", "README.txt", "readme.md", "PLAN.md", "план.md")
PROJECT_MARKERS = (".git", "docs", "src", "bsl", "build", "epf")


def looks_like_project(d: Path) -> bool:
    try:
        names = {entry.name for entry in d.iterdir()}
    except OSError:
        return False
    if names & set(PROJECT_MARKERS):
        return True
    return any((d / doc).is_file() for doc in DOC_NAMES)


def read_description(d: Path) -> str:
    for doc in DOC_NAMES:
        path = d / doc
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:300]
        except OSError:
            return ""
        text = re.sub(r"\s+", " ", text).strip(" #-*")
        if len(text) > 40:  # заголовок-пустышка не считаем описанием
            return text[:160]
    return ""


def build_catalog(env: dict, state: dict, force: bool = False) -> list[dict]:
    """Проекты = папки PROJECTS_ROOT (уровень 1 + уровень 2 у групп).

    Описание — первые строки README/PLAN.md. Кэш в state на день,
    пересобрать принудительно: --refresh.
    """
    today = date.today().isoformat()
    cached = state.get("catalog_cache") or {}
    if not force and cached.get("date") == today:
        return cached.get("items", [])
    root = Path(env.get("PROJECTS_ROOT", r"C:\Projects"))
    items: list[dict] = []
    if root.is_dir():
        try:
            level1 = sorted(d for d in root.iterdir()
                            if d.is_dir() and not d.name.startswith((".", "_")))
        except OSError:
            level1 = []
        for d1 in level1:
            if looks_like_project(d1):
                items.append({"name": d1.name, "path": str(d1),
                              "desc": read_description(d1)})
                continue
            try:
                level2 = sorted(c for c in d1.iterdir()
                                if c.is_dir() and not c.name.startswith((".", "_")))
            except OSError:
                continue
            for d2 in level2:
                if looks_like_project(d2):
                    items.append({"name": f"{d1.name}/{d2.name}",
                                  "path": str(d2), "desc": read_description(d2)})
    state["catalog_cache"] = {"date": today, "items": items}
    return items


def catalog_text(items: list[dict], limit: int = 7000) -> str:
    """Имя [путь] — описание; с капом, чтобы раздуть промпт до отказа."""
    lines = []
    size = 0
    for item in items:
        line = f"- {item['name']} [{item['path']}]"
        if item["desc"]:
            line += f" — {item['desc']}"
        size += len(line) + 1
        if size > limit:
            lines.append(f"- …и ещё {len(items) - len(lines)} проектов")
            break
        lines.append(line)
    return "\n".join(lines)


def resolve_known_project(card: dict, items: list[dict],
                          sender_map: dict) -> dict | None:
    """Проект, указанный человеком: маркер в письме или правило отправителя."""
    hay = f"{card['subject']}\n{card['body'][:1500]}".lower()
    for match in re.finditer(r"(?:\[?\s*проект\s*:\s*([^#\]\n]+)|#проект:([\w/\\.-]+))",
                             hay, re.I):
        wanted = (match.group(1) or match.group(2)).strip().lower().rstrip(".")
        for item in items:
            if wanted and (item["name"].lower().endswith(wanted)
                           or wanted in item["name"].lower()):
                return item
    address = (card["from"].split("<")[-1].strip(">") if "<" in card["from"]
               else card["from"]).lower()
    wanted = sender_map.get(address) or sender_map.get("*@" + address.split("@")[-1])
    if wanted:
        for item in items:
            if item["name"].lower().endswith(str(wanted).lower()):
                return item
    return None


def connect(env: dict) -> imaplib.IMAP4_SSL:
    imaplib.Commands["ID"] = ("AUTH", "SELECTED", "NONAUTH")
    client = imaplib.IMAP4_SSL(env["IMAP_HOST"], int(env.get("IMAP_PORT", "993")))
    client.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    # Mail.ru отбрасывает клиентов без IMAP ID (RFC 2971).
    client._simple_command("ID", '("name" "InboxPilot" "version" "1.0")')
    client.response("ID")
    return client


def fetch_new_cards(client: imaplib.IMAP4_SSL, state: dict) -> list[dict]:
    """Все письма, чей Message-ID ещё не обработан (независимо от UNSEEN)."""
    client.select("INBOX")
    typ, data = client.search(None, "ALL")
    cards = []
    for num in (data[0] or b"").split():
        typ, md = client.fetch(num, "(BODY.PEEK[HEADER])")
        msg = email.message_from_bytes(md[0][1], policy=policy.default)
        card = {
            "num": num.decode(),
            "message_id": msg.get("Message-ID") or f"<no-id-{num.decode()}>",
            "from": decode_mime(msg.get("From")),
            "subject": decode_mime(msg.get("Subject")) or "(без темы)",
            "date": msg.get("Date") or "",
        }
        if card["message_id"] in state["processed"]:
            continue
        cards.append(card)
    return cards


def fetch_full(client: imaplib.IMAP4_SSL, card: dict) -> dict:
    typ, md = client.fetch(card["num"], "(RFC822)")
    msg = email.message_from_bytes(md[0][1], policy=policy.default)
    card["body"] = message_body(msg)
    card["attachments"] = save_attachments(msg, card["message_id"])
    return card


def triage_prompt(card: dict, catalog: str, known: dict | None = None) -> str:
    body = card["body"]
    if len(body) > 6000:
        body = body[:6000] + "\n…(обрезано)"
    attachments = ""
    if card.get("attachments"):
        listed = "\n".join(f"- {path}" for path in card["attachments"])
        attachments = ("\nВложения (сохранены на диск, при необходимости открой "
                       "и посмотри):\n" + listed + "\n")
    if known:
        project_block = (
            f"Проект УКАЗАН ЧЕЛОВЕКОМ: {known['name']}\n"
            f"Корень проекта: {known['path']}\n"
            f"Описание проекта: {known['desc'] or '(нет)'}\n\n"
            "Проект уже выбран — НЕ выбирай его сам. Составь ТЗ, опираясь на "
            "контекст проекта; в ТЗ обязательно укажи «Рабочая папка: "
            f"{known['path']}».\nЕсли из текста обращения очевидно, что оно "
            "вообще не про этот проект, — так и напиши отдельной строкой "
            "«ПРОЕКТ НЕПОДОХОДИТ» перед ТЗ.\n"
        )
        header = (
            "Ты — ассистент диспетчерской. Для обращения подготовь "
            "техническое задание.\n\n" + project_block + "\n"
            f"Обращение (канал: email)\nОт: {card['from']}\n"
            f"Тема: {card['subject']}\nДата: {card['date']}\nТекст:\n{body}\n"
            f"{attachments}\n"
            "Ответь строго в этом формате:\n"
            f"ПРОЕКТ: {known['name']}\n"
            "УВЕРЕННОСТЬ: <0.0..1.0>\n"
            "ТИП: <баг|задача|вопрос|фича>\n"
            "ПРИОРИТЕТ: <1..10, где 1 — срочнее>\n"
            "РАБОЧАЯ ПАПКА: <корень проекта>\n"
            "ТЕХНИЧЕСКОЕ ЗАДАНИЕ:\n"
            "Контекст: <1-3 предложения>\n"
            "Что нужно: <по пунктам, с опорой на структуру проекта>\n"
            "Критерий готовности: <как проверить, что сделано>\n"
            "Открытые вопросы: <что неясно, или «нет»>\n\n"
            "Последней строкой напиши вердикт:\n"
            "ИТОГ: ГОТОВО — триаж завершён"
        )
        return header

    return (
        "Ты — triage-ассистент диспетчерской. Определи по обращению проект "
        "(это реальная папка на диске) и подготовь техническое задание.\n\n"
        "Каталог проектов (имя [путь] — описание):\n" + catalog + "\n\n"
        f"Обращение (канал: email)\nОт: {card['from']}\n"
        f"Тема: {card['subject']}\nДата: {card['date']}\nТекст:\n{body}\n"
        f"{attachments}\n"
        "Правило: служебные уведомления и рассылки (не требуют человека) — "
        "ПРОЕКТ: НЕТ, ТИП: вопрос, ПРИОРИТЕТ: 10, без ТЗ.\n"
        "Если ни один проект не подходит — ПРОЕКТ: НЕТ.\n\n"
        "Ответь строго в этом формате:\n"
        "ПРОЕКТ: <имя из каталога, либо НЕТ>\n"
        "УВЕРЕННОСТЬ: <0.0..1.0>\n"
        "ТИП: <баг|задача|вопрос|фича>\n"
        "ПРИОРИТЕТ: <1..10, где 1 — срочнее>\n"
        "РАБОЧАЯ ПАПКА: <путь проекта из каталога, если ПРОЕКТ не НЕТ>\n"
        "ТЕХНИЧЕСКОЕ ЗАДАНИЕ:\n"
        "Контекст: <1-3 предложения>\n"
        "Что нужно: <по пунктам>\n"
        "Критерий готовности: <как проверить, что сделано>\n"
        "Открытые вопросы: <что неясно, или «нет»>\n\n"
        "Последней строкой напиши вердикт:\n"
        "ИТОГ: ГОТОВО — триаж завершён"
    )


def create_task(env: dict, prompt: str) -> int:
    payload = {
        "prompt": prompt,
        "provider": env.get("PP_PROVIDER", "claude-z"),
        "priority": int(env.get("PP_PRIORITY", "5")),
        "working_dir": env.get("PP_WORKING_DIR", str(ROOT)),
        "tg_chat_id": int(env["TG_CHAT_ID"]) if env.get("TG_CHAT_ID") else None,
    }
    if env.get("PP_TRIAGE_SKIP_PERMISSIONS") == "1":
        # Иначе агент не может открыть сохранённые скриншоты вложений.
        payload["skip_permissions"] = True
    payload = {k: v for k, v in payload.items() if v is not None}
    request = urllib.request.Request(
        env["PP_API"].rstrip("/") + "/api/tasks",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = env.get("PP_API_TOKEN", "")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=15) as response:
        task = json.loads(response.read().decode("utf-8"))
    return int(task["id"])


def load_state() -> dict:
    state = {"processed": [], "created": {}, "pending_saves": []}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    state.setdefault("processed", [])
    state.setdefault("created", {})
    state.setdefault("pending_saves", [])
    return state


def save_state(state: dict) -> None:
    state["processed"] = state["processed"][-500:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def tasks_created_today(state: dict) -> int:
    today = date.today().isoformat()
    return state["created"].get(today, 0)


def note_created(state: dict) -> None:
    today = date.today().isoformat()
    state["created"] = {today: state["created"].get(today, 0) + 1}


def log_event(event: dict) -> None:
    """Журнал для inbox_bot (этап 2): append-only JSONL, читает его хвостом."""
    event = dict(event, ts=time.time())
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_once(env: dict, dry: bool, refresh: bool = False) -> None:
    config = json.loads((ROOT / env.get("CATALOG", "projects.json"))
                        .read_text(encoding="utf-8"))
    sender_map = config.get("sender_map", {})
    state = load_state()
    daily_cap = int(env.get("MAX_PER_DAY", "20"))
    if not dry:
        flush_saves(env, state)
    items = build_catalog(env, state, force=refresh)
    catalog = catalog_text(items)
    client = connect(env)
    try:
        for card in fetch_new_cards(client, state):
            client.store(card["num"], "+FLAGS", "\\Seen")
            full = fetch_full(client, card)
            reason = bulk_reason(full, full["from"])
            if reason:
                print(f"Пропущен как не-обращение ({reason}): {full['from']} — {full['subject']}")
                state["processed"].append(full["message_id"])
                continue
            if not sender_allowed(full["from"], env.get("ALLOW_FROM", "")):
                print(f"Пропущен: отправитель вне ALLOW_FROM — {full['from']}")
                state["processed"].append(full["message_id"])
                continue
            if not dry and tasks_created_today(state) >= daily_cap:
                print(f"Дневной лимит {daily_cap} исчерпан — письмо ждёт следующего прохода: {full['subject']}")
                continue
            print(f"Новое обращение: {full['from']} — {full['subject']}")
            if full["attachments"]:
                print(f"  вложений: {len(full['attachments'])}")
            known = resolve_known_project(full, items, sender_map)
            if known:
                print(f"  проект указан человеком: {known['name']}")
            prompt = triage_prompt(full, catalog, known)
            if dry:
                print("DRY: задача не создаётся, промпт:\n" + prompt[:1500])
                continue
            task_id = create_task(env, prompt)
            print(f"  -> Задача триажа #{task_id} создана")
            state["processed"].append(full["message_id"])
            note_created(state)
            state["pending_saves"].append({
                "task_id": task_id,
                "from": full["from"],
                "subject": full["subject"],
                "date": full["date"],
            })
            log_event({
                "type": "triage_created",
                "task_id": task_id,
                "from": full["from"],
                "subject": full["subject"],
                "date": full["date"],
                "message_id": full["message_id"],
                "attachments": full.get("attachments", []),
            })
    finally:
        save_state(state)
        try:
            client.logout()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="не создавать задачи")
    parser.add_argument("--once", action="store_true", help="один проход")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--refresh", action="store_true",
                        help="пересобрать каталог проектов с диска")
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "PP_API")
               if not env.get(k)]
    if missing:
        print("Нет обязательных ключей .env: " + ", ".join(missing))
        return 2
    while True:
        try:
            run_once(env, dry=args.dry, refresh=args.refresh)
        except Exception as exc:
            print(f"!! проход не удался: {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
