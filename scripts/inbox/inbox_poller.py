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
ATTACH_ROOT = Path.home() / ".promptpilot" / "inbox"

NO_REPLY_RE = re.compile(
    r"^(no-?reply|donotreply|do-not-reply|mail-daemon|bounce[^@]*)@", re.I)
BULK_HINT_HEADERS = ("List-Unsubscribe", "List-Id", "X-Mailinglist")


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


def triage_prompt(card: dict, catalog: dict) -> str:
    projects = "\n".join(
        f"- {name}: {desc}" for name, desc in catalog.get("projects", {}).items())
    body = card["body"]
    if len(body) > 6000:
        body = body[:6000] + "\n…(обрезано)"
    attachments = ""
    if card.get("attachments"):
        listed = "\n".join(f"- {path}" for path in card["attachments"])
        attachments = ("\nВложения (сохранены на диск, при необходимости открой "
                       "и посмотри):\n" + listed + "\n")
    return (
        "Ты — triage-ассистент диспетчерской. Определи по обращению проект и "
        "подготовь техническое задание.\n\n"
        "Каталог проектов:\n" + (projects or "(каталог пуст)") + "\n\n"
        f"Обращение (канал: email)\nОт: {card['from']}\n"
        f"Тема: {card['subject']}\nДата: {card['date']}\nТекст:\n{body}\n"
        f"{attachments}\n"
        "Правило: служебные уведомления и рассылки (не требуют человека) — "
        "ПРОЕКТ: НЕТ, ТИП: вопрос, ПРИОРИТЕТ: 10, без ТЗ.\n\n"
        "Ответь строго в этом формате:\n"
        "ПРОЕКТ: <имя из каталога, либо НЕТ>\n"
        "УВЕРЕННОСТЬ: <0.0..1.0>\n"
        "ТИП: <баг|задача|вопрос|фича>\n"
        "ПРИОРИТЕТ: <1..10, где 1 — срочнее>\n"
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
    state = {"processed": [], "created": {}}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    state.setdefault("processed", [])
    state.setdefault("created", {})
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


def run_once(env: dict, dry: bool) -> None:
    catalog = json.loads((ROOT / env.get("CATALOG", "projects.json"))
                         .read_text(encoding="utf-8"))
    state = load_state()
    daily_cap = int(env.get("MAX_PER_DAY", "20"))
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
            prompt = triage_prompt(full, catalog)
            if dry:
                print("DRY: задача не создаётся, промпт:\n" + prompt[:1500])
                continue
            task_id = create_task(env, prompt)
            print(f"  -> Задача триажа #{task_id} создана")
            state["processed"].append(full["message_id"])
            note_created(state)
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
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "PP_API")
               if not env.get(k)]
    if missing:
        print("Нет обязательных ключей .env: " + ", ".join(missing))
        return 2
    while True:
        try:
            run_once(env, dry=args.dry)
        except Exception as exc:
            print(f"!! проход не удался: {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
