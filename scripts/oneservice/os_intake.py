"""Oneservice intake (docs/ONESERVICE_PIPELINE_PLAN.md, этап 1).

Два входа — один бэклог (GitLab issues проекта oneservice-cc_v2):

  py -3.11 scripts/oneservice/os_intake.py email [--once]
  py -3.11 scripts/oneservice/os_intake.py tg

Почта: письма (кроме [KT]-игровых и служебных) становятся issue с label
«подано». Telegram: после пароля команды (OS_TG_PASSWORD) любое сообщение
становится issue; фото/документы прикрепляются. Отправители логируются в
.os_senders.json — белый список строится потом по факту запросов.

Секреты — scripts/oneservice/.env (в git не попадают).
"""

import email
import imaplib
import json
import re
import smtplib
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from email import policy
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".os_state.json"
SENDERS_FILE = ROOT / ".os_senders.json"
ALLOWED_FILE = ROOT / ".os_tg_allowed.json"
ATTACH_ROOT = Path.home() / ".promptpilot" / "oneservice"

NOISE_SENDER_DOMAINS = (
    "e.mail.ru", "id.mail.ru", "notify.mail.ru", "agent.mail.ru",
)
GAME_MARKER = "[KT]"
OS_MARKER = "[OS]"

_HOMOGLYPHS = str.maketrans("АВСЕНКМОРТХ", "ABSEHKMOPTX")


def marker_of(subject: str) -> str:
    """Маркер проекта из темы; кириллица/латиница равнозначны."""
    norm = (subject or "").strip().upper().translate(_HOMOGLYPHS)
    m = re.match(r"^\[([A-Z]{2})\]", norm)
    return m.group(1) if m else ""  # задачи oneservice помечаются темой; [KT] — игра;
# всё остальное остаётся в общем бэклоге (inbox_poller: каталог проектов)


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


# --- GitLab -----------------------------------------------------------------

def gl(env: dict, method: str, path: str, payload: dict | None = None,
       raw_file: tuple[str, bytes] | None = None):
    """GitLab API через curl: надёжный транспорт без зависимости от
    TLS-отпечатков и хрупкой сборки URL. JSON-тело передаётся через stdin
    (--data-binary @-), файлы — multipart через временный файл."""
    url = f"{env['GITLAB_URL'].rstrip('/')}/api/v4{path}"
    input_data = b""
    if raw_file is not None:
        field, blob = raw_file
        tmp = ATTACH_ROOT / f"upload-{time.time_ns()}-{field}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(blob)
        cmd = ["curl", "-sS", "-m", "120", "-X", "POST",
               "-H", f"PRIVATE-TOKEN: {env['GITLAB_TOKEN']}",
               "-F", f"file=@{tmp}", url]
    else:
        cmd = ["curl", "-sS", "-m", "120", "-X", method,
               "-H", f"PRIVATE-TOKEN: {env['GITLAB_TOKEN']}",
               "-H", "Content-Type: application/json"]
        if payload is not None:
            input_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        cmd += ["--data-binary", "@-", url]
    result = subprocess.run(cmd, input=input_data, capture_output=True, timeout=180)
    if raw_file is not None:
        tmp.unlink(missing_ok=True)
    out = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise RuntimeError(f"curl {method} {path}: {result.stderr or result.returncode}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"GitLab {method} {path} вернул не-JSON: {out[:200]}")
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"GitLab {method} {path}: {data['error']}")
    return data


def project_api(env: dict, path: str, method: str = "GET",
                payload: dict | None = None, raw_file: tuple[str, bytes] | None = None):
    # GITLAB_PROJECT — уже URL-encoded путь, напр. 1c%2Foneservice-cc_v2.
    # Числовой id на этом корпоративном GitLab прокси отдаёт 404.
    return gl(env, method, f"/projects/{env['GITLAB_PROJECT']}{path}",
              payload=payload, raw_file=raw_file)


def pp_request(env: dict, path: str, method: str = "GET",
               payload: dict | None = None) -> dict:
    """Локальная очередь PromptPilot: задачи хранителя исполняет воркер."""
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


def ensure_labels(env: dict, labels: dict[str, str]) -> None:
    try:
        existing = {item["title"] for item in project_api(env, "/labels")}
    except Exception:
        return  # роль бота не позволяет видеть метки — не мешаем
    for name, color in labels.items():
        if name in existing:
            continue
        try:
            project_api(env, "/labels", "POST", {"name": name, "color": color})
        except Exception:
            pass


def create_issue(env: dict, title: str, description: str,
                 labels: list[str]) -> dict:
    issue = project_api(env, "/issues", "POST", {
        "title": title[:250],
        "description": description,
    })
    if labels:
        try:
            issue = project_api(env, f"/issues/{issue['iid']}", "PUT",
                                {"labels": ",".join(labels)}) or issue
        except Exception:
            pass  # роль бота Guest — метки выставит человек/скрипт с правами
    return issue


def upload_file(env: dict, filename: str, blob: bytes) -> str:
    result = project_api(env, "/uploads", "POST", raw_file=(filename, blob))
    mark = result.get("full_path") or result.get("url") or ""
    return f"[{filename}]({mark})"


# --- почта ------------------------------------------------------------------

def connect_imap(env: dict) -> imaplib.IMAP4_SSL:
    imaplib.Commands["ID"] = ("AUTH", "SELECTED", "NONAUTH")
    client = imaplib.IMAP4_SSL(env["IMAP_HOST"], int(env.get("IMAP_PORT", "993")))
    client.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
    client._simple_command("ID", '("name" "OsIntake" "version" "1.0")')
    client.response("ID")
    return client


def decode_mime(value):
    if not value:
        return ""
    out = []
    for text, charset in decode_header(value):
        out.append(text.decode(charset or "utf-8", errors="replace")
                   if isinstance(text, bytes) else text)
    return "".join(out)


def message_body(msg) -> str:
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and \
                        "attachment" not in str(part.get("Content-Disposition") or ""):
                    return str(part.get_content()).strip()
            html = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html = str(part.get_content())
                    break
            text = re.sub(r"(?is)<(style|script).*?>.*?</\1>", " ", html)
            text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
            return re.sub(r"<[^>]+>", " ", text).strip()
        return str(msg.get_content()).strip()
    except Exception:
        return "(тело письма не разобралось — смотри вложения/исходник)"


def bulk_or_noise(msg, sender: str) -> str | None:
    if (msg.get("X-Spam-Flag") or "").strip().lower() == "yes":
        return "спам"
    if (msg.get("Auto-Submitted") or "no").strip().lower() not in ("", "no"):
        return "автоуведомление"
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return "рассылка"
    address = (sender.split("<")[-1].strip("> ") if "<" in sender else sender).lower()
    if any(address.endswith("@" + dom) or address.endswith(dom)
           for dom in NOISE_SENDER_DOMAINS):
        return "служебное письмо почтовика"
    return None


def process_email_mode(env: dict, dry: bool) -> None:
    state = load_state(STATE_FILE)
    daily_note = ""
    ensure_labels(env, {
        "подано": "#8fbcdb",
        "целесообразность": "#e5a353",
        "триаж-ТЗ": "#c9a227",
        "в работе": "#6699cc",
        "ревью": "#9a6bd6",
        "готово-к-мержу": "#44aa66",
        "блокирована платформой": "#dd4444",
        "отклонено": "#888888",
    })
    client = connect_imap(env)
    created = 0
    try:
        for folder in [f.strip() for f in env.get("IMAP_FOLDERS", "INBOX").split(",") if f.strip()]:
            typ, _ = client.select(folder)
            if typ != "OK":
                continue
            typ, data = client.search(None, "ALL")
            for num in (data[0] or b"").split():
                typ, md = client.fetch(num, "(BODY.PEEK[HEADER])")
                msg = email.message_from_bytes(md[0][1], policy=policy.default)
                message_id = msg.get("Message-ID") or f"<{folder}-{num.decode()}>"
                if message_id in state["processed"]:
                    continue
                subject = decode_mime(msg.get("Subject")) or "(без темы)"
                sender = decode_mime(msg.get("From"))
                client.select(folder)  # fetch HEADER переключил папку
                if marker_of(subject) == "KT":
                    state["processed"].append(message_id)  # игровой конвейер
                    continue
                if marker_of(subject) != "OS":
                    # Не помечено [OS] — это не задача oneservice: письмо
                    # остаётся в общем бэклоге (inbox_poller сам определит
                    # проект по каталогу). OS-конвейер его не трогает.
                    state["processed"].append(message_id)
                    mark_seen(client, folder, num)
                    continue
                noise = bulk_or_noise(msg, sender)
                if noise:
                    print(f"Пропущен ({noise}): {sender} — {subject}")
                    state["processed"].append(message_id)
                    mark_seen(client, folder, num)
                    continue
                full = client.fetch(num, "(RFC822)")[1][0][1]
                msg = email.message_from_bytes(full, policy=policy.default)
                body = message_body(msg)
                print(f"Новая задача: {sender} — {subject}")
                if dry:
                    print("DRY: issue не создаётся")
                    continue
                desc = (f"**Автор:** {sender}\n**Канал:** email\n"
                        f"**Дата:** {msg.get('Date') or ''}\n\n---\n\n{body}")
                issue = create_issue(env, subject, desc, ["подано"])
                print(f"  -> issue #{issue['iid']}: {issue['web_url']}")
                log_sender(SENDERS_FILE, sender)
                state["processed"].append(message_id)
                mark_seen(client, folder, num)
                created += 1
    finally:
        save_state(STATE_FILE, state)
        try:
            client.logout()
        except Exception:
            pass
    if not created:
        print("Новых задач по почте нет.")


def mark_seen(client: imaplib.IMAP4_SSL, folder: str, num: str) -> None:
    client.select(folder)
    client.store(num, "+FLAGS", "\\Seen")


# --- Telegram ---------------------------------------------------------------

TG_ALLOWED: dict = {}


def tg(method: str, token: str, **params):
    """Telegram API через curl (стабильнее urllib в этой сети).
    Внимание: порядок (method, token) — вызовы идут tg("getUpdates", token)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    cmd = ["curl", "-sS", "-m", "45", "-X", "POST",
           "-H", "Content-Type: application/json",
           "--data-binary", "@-", url]
    input_data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(cmd, input=input_data, capture_output=True, timeout=50)
    payload = json.loads((result.stdout or b"{}").decode("utf-8", "replace").strip() or "{}")
    if not payload.get("ok"):
        raise RuntimeError(f"telegram {method}: {payload}")
    return payload.get("result")


def tg_download(token: str, file_id: str) -> tuple[str, bytes]:
    info = tg("getFile", token, file_id=file_id)
    path = info["file_path"]
    name = path.rsplit("/", 1)[-1]
    with urllib.request.urlopen(
            f"https://api.telegram.org/file/bot{token}/{path}", timeout=60) as r:
        return name, r.read()


def allowed(chats: dict, chat_id: str) -> bool:
    return str(chat_id) in chats


def handle_update(env: dict, state: dict, update: dict) -> None:
    token = env["TG_BOT_TOKEN"]
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not chat_id:
        return
    person = ((message.get("from") or {}).get("username")
              or (message.get("from") or {}).get("first_name") or "без имени")
    text = (message.get("text") or "").strip()

    if not allowed(TG_ALLOWED, chat_id):
        if text and text == env.get("OS_TG_PASSWORD", ""):
            TG_ALLOWED[chat_id] = {"who": person, "since": time.strftime("%Y-%m-%d")}
            save_state(ALLOWED_FILE, TG_ALLOWED)
            tg("sendMessage", token, chat_id=chat_id,
               text="Пароль верный. Пиши задачу одним сообщением — станет issue в GitLab.")
            print(f"допущен чат {chat_id} ({person})")
        else:
            tg("sendMessage", token, chat_id=chat_id,
               text="Это бот команды oneservice. Введи пароль команды одним сообщением.")
        return

    if text.startswith("/start"):
        tg("sendMessage", token, chat_id=chat_id,
           text="Ты уже допущен. Пиши задачу одним сообщением (можно с фото/файлом).")
        return

    title = text.splitlines()[0][:120] if text else "(без текста)"
    description = (f"**Автор:** {person} (TG чат {chat_id})\n"
                   f"**Канал:** Telegram\n"
                   f"**Дата:** {time.strftime('%Y-%m-%d %H:%M')}\n\n---\n\n{text}")
    attachments = []
    for key in ("photo", "document"):
        media = message.get(key)
        if media:
            file_id = (media[-1]["file_id"] if key == "photo" else media["file_id"])
            try:
                filename, blob = tg_download(token, file_id)
                attachments.append(upload_file(env, filename, blob))
            except Exception as exc:
                attachments.append(f"(файл не прикрепился: {exc})")
    if attachments:
        description += "\n\n**Вложения:**\n" + "\n".join(attachments)
    issue = create_issue(env, title, description, ["подано"])
    tg("sendMessage", token, chat_id=chat_id,
       text=f"✅ Задача принята: #{issue['iid']}\n{issue['web_url']}")
    print(f"TГ -> issue #{issue['iid']} от {person}")


def tg_loop(env: dict) -> None:
    token = env["TG_BOT_TOKEN"]
    global TG_ALLOWED
    TG_ALLOWED = load_state(ALLOWED_FILE)
    offset = 0
    print("tg: long-poll запущен", flush=True)
    while True:
        try:
            params = {"timeout": 25, "offset": offset,
                      "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            updates = tg("getUpdates", token, **params) or []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(env, dict(env), update)
                except Exception as exc:
                    print(f"!! update: {type(exc).__name__}: {exc}", flush=True)
        except Exception as exc:
            print(f"!! tg poll: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)


# --- хранитель целесообразности (этап 2) ------------------------------------

KEEPER_STATE = ROOT / ".os_keeper.json"
OS_REPO = Path(r"C:\Projects\oneservice-cc_v2")
VERDICT_CONTRACT = (
    "\n\nПоследней строкой ответа напиши ровно одну из:\n"
    "ИТОГ: ГОТОВО — решение принято\n"
    "ИТОГ: НЕ СМОГ — не получилось\n"
)


def load_keeper_state() -> dict:
    state = {"sent": {}}
    if KEEPER_STATE.exists():
        state.update(json.loads(KEEPER_STATE.read_text(encoding="utf-8")))
    state.setdefault("sent", {})
    state.setdefault("processed", [])
    return state


def repo_context(env: dict) -> str:
    """Каркас репозитория для ТЗ: имена файлов до второго уровня."""
    root = Path(env.get("OS_WORKING_DIR", str(OS_REPO)))
    lines = []
    try:
        for d in sorted(root.iterdir()):
            if d.name.startswith("."):
                continue
            if d.is_dir():
                children = [c.name for c in sorted(d.iterdir())][:12]
                lines.append(f"{d.name}/: " + ", ".join(children))
            else:
                lines.append(d.name)
    except OSError:
        pass
    return "\n".join(lines[:80])


def keeper_prompt(env: dict, issue: dict) -> str:
    body = (issue.get("description") or "")[:5000]
    return (
        "Ты — хранитель целесообразности проекта oneservice-cc_v2 "
        "(1С-конфигурация). Оцени обращение и подготовь решение.\n\n"
        f"Структура проекта:\n{repo_context(env)}\n\n"
        f"Обращение (GitLab issue #{issue['iid']}, автор: "
        f"{issue['author'].get('name') or issue['author'].get('username')}):\n"
        f"{body}\n\n"
        "Реши:\n"
        "- ЦЕЛЕСООБРАЗНО — реальная задача этого сервиса, принимаем в работу;\n"
        "- НЕ ЦЕЛЕСООБРАЗНО — не про сервис, дубль или мусор;\n"
        "- ПЛАТФОРМА — упирается в ошибку/ограничение платформы, нужен issue "
        "в бэклог платформы.\n\n"
        "Ответь строго в формате:\n"
        "ВЕРДИКТ: ЦЕЛЕСООБРАЗНО|НЕ ЦЕЛЕСООБРАЗНО|ПЛАТФОРМА\n"
        "ПРИЧИНА: <1-3 предложения, вежливо, для автора обращения>\n"
        "ТЕХНИЧЕСКОЕ ЗАДАНИЕ:\n"
        "Контекст: <кратко>\n"
        "Что нужно: <по пунктам, с файлами конфигурации>\n"
        "Критерий готовности: <как проверить>\n\n"
        + VERDICT_CONTRACT
    )


def keeper_pass(env: dict, state: dict, dry: bool) -> None:
    """Один проход хранителя: раздача задач PP + перенос вердиктов в GitLab."""
    # 1. новые issues с label «подано» -> задача хранителя в PP
    for issue in project_api(env, "/issues?labels=%D0%BF%D0%BE%D0%B4%D0%B0%D0%BD%D0%BE&state=opened"):
        iid = str(issue["iid"])
        if iid in state["sent"]:
            continue
        if dry:
            print(f"DRY: хранитель взял бы issue #{iid}")
            continue
        payload = {
            "prompt": keeper_prompt(env, issue),
            "provider": env.get("OS_PROVIDER", "agy"),
            "working_dir": env.get("OS_WORKING_DIR", str(OS_REPO)),
            "priority": 2,
        }
        task = pp_request(env, "/api/tasks", "POST", payload)
        state["sent"][iid] = task["id"]
        project_api(env, f"/issues/{iid}", "PUT", {"labels": "целесообразность"})
        print(f"хранитель: issue #{iid} -> задача #{task['id']}")
    # 2. завершённые задачи хранителя -> комментарий и метки в GitLab
    for iid, pp_task_id in list(state["sent"].items()):
        task = pp_request(env, f"/api/tasks/{pp_task_id}")
        if task["status"] not in ("completed", "failed", "cancelled"):
            continue
        result = (task.get("result") or "").split("--- Meta ---")[0].strip()
        verdict_match = re.search(r"^ВЕРДИКТ:\s*(.+)$", result, re.M)
        verdict = verdict_match.group(1).strip().upper() if verdict_match else "НЕ СМОГ"
        note = f"🧊 **Хранитель целесообразности** (задача #{pp_task_id})\n\n{result[:3500]}"
        if "ПЛАТФОРМА" in verdict:
            note += "\n\n⚓ Эскалация: требуется issue в бэклоге платформы (onebase)."
            project_api(env, f"/issues/{iid}/notes", "POST", {"body": note})
            project_api(env, f"/issues/{iid}", "PUT",
                        {"labels": "блокирована платформой"})
        elif "ЦЕЛЕСООБРАЗНО" in verdict and "НЕ " not in verdict.upper().replace(
                "ЦЕЛЕСООБРАЗНО", ""):
            project_api(env, f"/issues/{iid}/notes", "POST", {"body": note})
            project_api(env, f"/issues/{iid}", "PUT", {"labels": "триаж-ТЗ"})
        else:
            project_api(env, f"/issues/{iid}/notes", "POST", {"body": note})
            project_api(env, f"/issues/{iid}", "PUT", {"labels": "отклонено"})
        state["sent"].pop(iid)
        print(f"хранитель: issue #{iid} — {verdict}")


def keeper_loop(env: dict, interval: int) -> None:
    while True:
        try:
            state = load_keeper_state()
            keeper_pass(env, state, dry=False)
            save_state(KEEPER_STATE, state)
        except Exception as exc:
            print(f"!! keeper: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval)


# --- общее ------------------------------------------------------------------

def load_state(path: Path) -> dict:
    state = {"processed": []}
    if path.exists():
        state.update(json.loads(path.read_text(encoding="utf-8")))
    state.setdefault("processed", [])
    return state


def save_state(path: Path, state: dict) -> None:
    state["processed"] = state["processed"][-2000:]
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def log_sender(path: Path, sender: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[sender] = data.get(sender, 0) + 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    env = load_env(ROOT / ".env")
    if mode == "email":
        process_email_mode(env, "--dry" in sys.argv)
        return 0
    if mode == "tg":
        tg_loop(env)
        return 0
    if mode == "keeper":
        interval = int(env.get("KEEPER_INTERVAL", "120"))
        while True:
            try:
                state = load_keeper_state()
                keeper_pass(env, state, dry="--dry" in sys.argv)
                save_state(KEEPER_STATE, state)
            except Exception as exc:
                print(f"!! keeper: {type(exc).__name__}: {exc}", flush=True)
            if "--once" in sys.argv:
                return 0
            time.sleep(interval)
    print("использование: os_intake.py email [--dry] | os_intake.py tg | "
          "os_intake.py keeper [--once] [--dry]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
