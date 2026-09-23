"""Inbox poller (MVP, docs/INBOX_TRIAGE_PLAN.md этап 1).

Забирает непрочитанные письма из IMAP-ящика, нормализует каждое в карточку
обращения и создаёт в PromptPilot задачу триажа: дешёвая модель определяет
проект (по каталогу projects.json) и собирает ТЗ. Результат прилетает в
Telegram как обычное уведомление задачи.

Запуск: py -3.11 scripts/inbox/inbox_poller.py [--dry] [--once]
Конфиг: scripts/inbox/.env (см. inbox.example.env). Секреты в git не попадают.
"""

import argparse
import email
import imaplib
import json
import sys
import time
import urllib.request
from email import policy
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".state.json"


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
        import re
        text = re.sub(r"(?is)<(style|script).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def connect(env: dict) -> imaplib.IMAP4_SSL:
    imaplib.Commands["ID"] = ("AUTH", "SELECTED", "NONAUTH")
    client = imaplib.IMAP4_SSL(env["IMAP_HOST"], int(env.get("IMAP_PORT", "993")))
    client.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    # Mail.ru отбрасывает клиентов без IMAP ID (RFC 2971).
    client._simple_command("ID", '("name" "InboxPilot" "version" "1.0")')
    client.response("ID")
    return client


def fetch_unread(client: imaplib.IMAP4_SSL) -> list[dict]:
    client.select("INBOX")
    typ, data = client.search(None, "UNSEEN")
    cards = []
    for num in (data[0] or b"").split():
        typ, md = client.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(md[0][1], policy=policy.default)
        cards.append({
            "num": num.decode(),
            "message_id": msg.get("Message-ID") or f"<no-id-{num.decode()}>",
            "from": decode_mime(msg.get("From")),
            "subject": decode_mime(msg.get("Subject")) or "(без темы)",
            "date": msg.get("Date") or "",
            "body": message_body(msg),
        })
    return cards


def triage_prompt(card: dict, catalog: dict) -> str:
    projects = "\n".join(
        f"- {name}: {desc}" for name, desc in catalog.get("projects", {}).items())
    body = card["body"]
    if len(body) > 6000:
        body = body[:6000] + "\n…(обрезано)"
    return (
        "Ты — triage-ассистент диспетчерской. Определи по обращению проект и "
        "подготовь техническое задание.\n\n"
        "Каталог проектов:\n" + (projects or "(каталог пуст)") + "\n\n"
        f"Обращение (канал: email)\nОт: {card['from']}\n"
        f"Тема: {card['subject']}\nДата: {card['date']}\nТекст:\n{body}\n\n"
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


def create_task(env: dict, card: dict, prompt: str) -> int:
    payload = {
        "prompt": prompt,
        "provider": env.get("PP_PROVIDER", "claude-z"),
        "priority": int(env.get("PP_PRIORITY", "5")),
        "working_dir": env.get("PP_WORKING_DIR", str(ROOT)),
        "tg_chat_id": int(env["TG_CHAT_ID"]) if env.get("TG_CHAT_ID") else None,
    }
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
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"processed": []}


def save_state(state: dict) -> None:
    state["processed"] = state["processed"][-500:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def run_once(env: dict, dry: bool) -> None:
    catalog = json.loads((ROOT / env.get("CATALOG", "projects.json"))
                         .read_text(encoding="utf-8"))
    state = load_state()
    client = connect(env)
    try:
        for card in fetch_unread(client):
            if card["message_id"] in state["processed"]:
                client.store(card["num"], "+FLAGS", "\\Seen")
                continue
            print(f"Новое обращение: {card['from']} — {card['subject']}")
            prompt = triage_prompt(card, catalog)
            if dry:
                print("DRY: задача не создаётся, промпт:\n" + prompt[:1500])
                continue
            else:
                task_id = create_task(env, card, prompt)
                print(f"  -> Задача триажа #{task_id} создана")
            state["processed"].append(card["message_id"])
            client.store(card["num"], "+FLAGS", "\\Seen")
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
