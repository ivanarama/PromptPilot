"""Inputs and outputs of flows (promptpilot/flows.py).

Everything here touches the outside world: a mailbox that anyone can write
to, a public page that anyone can read, a GitLab project, an SMTP server.
The rules of engagement live here once instead of in every intake script:

- a letter is judged by its headers before its body is fetched; its sender
  must be the one the flow expects (a form service, an allow-list), and
  where configured, proven by DKIM of *our* receiving server;
- text that came from outside is published only as plain text, without
  links, within a length cap;
- a reply goes only to the author of the request, never to an address a
  template produced; header values cannot carry line breaks;
- tokens and passwords come from the environment and never appear on a
  command line.
"""

import email
import imaplib
import json
import os
import re
import smtplib
import subprocess
import tempfile
import time
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import quote


class ConnectorError(RuntimeError):
    """The outside service refused or is unreachable; the step decides what next."""

# Cyrillic look-alikes of Latin capitals: [КТ] typed on a Russian layout and
# [KT] are the same marker.
_HOMOGLYPHS = str.maketrans("АВСЕНКМОРТХ", "ABSEHKMOPTX")
_NO_REPLY = re.compile(r"^(no-?reply|donotreply|do-not-reply|mail-daemon|bounce[^@]*)@", re.I)
_BULK_HEADERS = ("List-Unsubscribe", "List-Id", "X-Mailinglist")


def decode_mime(value) -> str:
    if not value:
        return ""
    out = []
    for text, charset in decode_header(str(value)):
        out.append(text.decode(charset or "utf-8", errors="replace")
                   if isinstance(text, bytes) else text)
    return "".join(out)


def address_of(value: str) -> str:
    """Bare lowercased address from a From/Reply-To value ("Имя <a@b.c>")."""
    return parseaddr(value or "")[1].strip().lower()


def marker_of(subject: str) -> str:
    """Two-letter project marker from a subject ("[KT] …", "[КТ] …") or ""."""
    norm = (subject or "").strip().upper().translate(_HOMOGLYPHS)
    match = re.match(r"^\[([A-Z]{2})\]", norm)
    return match.group(1) if match else ""


def clean_public_text(text: str, limit: int) -> str:
    """Text from a stranger shown on a public page: no links, one line, bounded."""
    text = re.sub(r"(?i)\b(?:https?://|www\.)\S+", "[ссылка]", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def message_body(msg) -> str:
    """text/plain, else text of the HTML part — without external libraries."""
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            ctype = part.get_content_type()
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


def dkim_passed(msg, authserv: str, domain: str) -> bool:
    """Authentication-Results of OUR receiving server confirm DKIM of domain.

    Only the header whose authserv-id is the configured server counts: a
    sender can write an Authentication-Results header of its own.
    """
    signer = re.compile(r"header\.(?:d=|i=@?)(?:[\w-]+\.)*" + re.escape(domain) + r"\b")
    for header in msg.get_all("Authentication-Results") or []:
        value = " ".join(str(header).split()).lower()
        server, _, results = value.partition(";")
        if server.strip() != authserv.lower():
            continue
        for clause in results.split(";"):
            clause = clause.strip()
            if clause.startswith("dkim=pass") and signer.search(clause):
                return True
    return False


def _in_domain(address: str, domain: str) -> bool:
    host = address.rpartition("@")[2]
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def sender_allowed(address: str, allow_from) -> bool:
    """The address, or its domain, is on the list. An empty list vouches for nobody."""
    entries = [entry.strip().lower() for entry in allow_from or [] if entry.strip()]
    return bool(address) and any(address == entry or _in_domain(address, entry)
                                 for entry in entries)


def letter_rejection(msg, source) -> str | None:
    """Why this letter is not a request for the flow (source: EmailInput), or None."""
    subject = decode_mime(msg.get("Subject"))
    sender = decode_mime(msg.get("From"))
    address = address_of(sender)
    if source.subject_marker and marker_of(subject) != source.subject_marker.upper():
        return "другая тема"
    if (msg.get("X-Spam-Flag") or "").strip().lower() == "yes":
        return "помечено спамом"
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    if any(msg.get(header) for header in _BULK_HEADERS) or \
            (msg.get("Precedence") or "").strip().lower() in ("bulk", "junk"):
        return "рассылка"
    if source.require_from_domain:
        if not _in_domain(address, source.require_from_domain):
            return f"не с {source.require_from_domain} (отправитель {address or '?'})"
        if source.dkim_authserv and not dkim_passed(
                msg, source.dkim_authserv, source.require_from_domain):
            return f"нет подписи DKIM {source.require_from_domain}"
    elif _NO_REPLY.match(address):
        return "no-reply отправитель"
    # allow_from_mode "mark": the letter is taken, input.sender_allowed says who vouched
    if source.allow_from and source.allow_from_mode == "reject" and \
            not sender_allowed(address, source.allow_from):
        return f"отправитель {address or '?'} не в allow_from"
    return None


def letter_attachments(msg, limit: int = 20, max_bytes: int = 25_000_000) -> list[tuple[str, bytes]]:
    """Attachments of a letter: (safe file name, content), at most ``limit``."""
    parts, total = [], 0
    for part in msg.iter_attachments():
        if len(parts) >= limit:
            break
        payload = part.get_payload(decode=True)
        if not payload or total + len(payload) > max_bytes:
            continue
        total += len(payload)
        name = decode_mime(part.get_filename()) or f"attachment-{len(parts) + 1}"
        name = re.sub(r"[^\w.\-() ]+", "_", Path(name).name).strip(". ") or "attachment"
        parts.append((f"{len(parts) + 1:02d}-{name[:80]}", payload))
    return parts


def write_attachments(directory: Path, parts: list[tuple[str, bytes]]) -> list[str]:
    """Save attachments into the item's own folder; their paths."""
    saved = []
    for name, payload in parts:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        target.write_bytes(payload)
        saved.append(str(target))
    return saved


def letter_request(msg, source) -> dict:
    """The part of a letter a flow item carries: plain values, bounded."""
    body = message_body(msg)[:source.body_limit]
    subject = decode_mime(msg.get("Subject")) or "(без темы)"
    author = ""
    if source.author_pattern:
        match = re.search(source.author_pattern, body)
        author = match.group(1).strip()[:60] if match else ""
    title = re.sub(r"^\[[^\]]+\]\s*", "", subject).strip()
    generic = {entry.strip().lower() for entry in source.generic_subjects or []}
    if not title or title.lower() in generic:
        # A form sends one fixed subject; the first real line says more.
        title = next((line.strip() for line in body.splitlines()
                      if len(line.strip()) > 15), title or subject)
    sender = address_of(decode_mime(msg.get("From")))
    return {
        "message_id": msg.get("Message-ID") or "",
        "from": sender,
        "reply_to": address_of(decode_mime(msg.get("Reply-To"))),
        "subject": subject,
        "title": title[:120],
        "author": author,
        "body": body,
        "date": msg.get("Date") or "",
        "sender_allowed": sender_allowed(sender, source.allow_from),
    }


def poll_mailbox(source, seen: set, credentials: tuple[str, str], *,
                 connect=None, keep_attachments: bool = False) -> tuple[list[dict], list[str]]:
    """New requests from the mailbox and the Message-IDs judged this time.

    Nothing is marked read (BODY.PEEK). A letter seen before costs one small
    fetch of its Message-ID; a new one is judged by its headers, and only a
    letter that passes letter_rejection is fetched whole. Judged ids are
    returned so the caller never judges a rejected letter again either.
    """
    user, password = credentials
    client = (connect or _connect_imap)(source, user, password)
    requests, judged = [], []
    try:
        for folder in source.folders:
            typ, _ = client.select(folder, readonly=True)
            if typ != "OK":
                continue
            typ, data = client.search(None, "ALL")
            for num in (data[0] or b"").split():
                typ, md = client.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                probe = email.message_from_bytes(md[0][1], policy=policy.default)
                message_id = (probe.get("Message-ID") or "").strip() or f"<{folder}-{num.decode()}>"
                if message_id in seen:
                    continue
                judged.append(message_id)
                typ, md = client.fetch(num, "(BODY.PEEK[HEADER])")
                header = email.message_from_bytes(md[0][1], policy=policy.default)
                if letter_rejection(header, source):
                    continue
                typ, md = client.fetch(num, "(BODY.PEEK[])")
                full = email.message_from_bytes(md[0][1], policy=policy.default)
                if letter_rejection(full, source):
                    continue
                request = letter_request(full, source)
                request["message_id"] = message_id
                if keep_attachments:
                    # bytes, not paths: they are saved into the item's folder once
                    # the item exists (flows._poll_mail)
                    request["_attachments"] = letter_attachments(full)
                requests.append(request)
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return requests, judged


def _connect_imap(source, user: str, password: str):
    imaplib.Commands["ID"] = ("AUTH", "SELECTED", "NONAUTH")
    client = imaplib.IMAP4_SSL(source.host, source.port)
    client.login(user, password)
    try:
        # Mail.ru drops clients that do not identify themselves (RFC 2971).
        client._simple_command("ID", '("name" "PromptPilot" "version" "1.0")')
        client.response("ID")
    except Exception:
        pass
    return client


# --- public JSON feed -------------------------------------------------------------

EXTERNAL_STAGE_PREFIXES = ("в релизе",)


def write_json_feed(path: str, entries: list[dict]) -> bool:
    """Rewrite a public feed, keeping stages other tools wrote into it.

    A release script marks entries «в релизе vX» in the file itself; a plain
    rewrite from the flow's state would erase that within one pass. The file
    is written only when its entries change (it may live in a site's git
    repository), and atomically: the page never reads half a file.
    """
    target = Path(path)
    external, current_items = {}, None
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
        current_items = current.get("items")
        for entry in current_items or []:
            stage = entry.get("stage") if isinstance(entry, dict) else None
            if isinstance(stage, str) and stage.startswith(EXTERNAL_STAGE_PREFIXES):
                external[str(entry.get("item_id", entry.get("task_id")))] = stage
    except (OSError, ValueError, AttributeError):
        pass
    for entry in entries:
        if str(entry.get("item_id")) in external:
            entry["stage"] = external[str(entry["item_id"])]
    if current_items == entries:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"updated": time.strftime("%Y-%m-%d %H:%M"), "items": entries},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    temporary.replace(target)
    return True


# --- GitLab ------------------------------------------------------------------------

def _curl(cmd: list[str], data: bytes | None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=data, capture_output=True, timeout=180)


def gitlab_request(connection, method: str, path: str, payload: dict | None = None):
    """One GitLab API call through curl; parsed JSON, or ConnectorError.

    curl, not urllib: it takes the system certificate store, which a
    corporate GitLab behind its own CA needs (the oneservice scripts learned
    this). The token goes in a header file, not on the command line.
    """
    base = (connection.url or os.environ.get(connection.url_env, "")).rstrip("/")
    token = os.environ.get(connection.token_env, "")
    if not base or not token:
        raise ConnectorError(f"GitLab: не задан адрес или {connection.token_env}")
    url = f"{base}/api/v4/projects/{connection.project}{path}"
    handle, header_file = tempfile.mkstemp(suffix=".hdr", prefix="pp-gitlab-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as header:
            header.write(f"PRIVATE-TOKEN: {token}\nContent-Type: application/json\n")
        cmd = ["curl", "-sS", "-m", "120", "-X", method, "-H", f"@{header_file}",
               "-w", "\n%{http_code}"]
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            cmd += ["--data-binary", "@-"]
        result = _curl(cmd + [url], data)
    finally:
        try:
            os.unlink(header_file)
        except OSError:
            pass
    output = (result.stdout or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        error = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ConnectorError(f"GitLab {method} {path}: curl {result.returncode}: {error[:300]}")
    body, _, status = output.rstrip().rpartition("\n")
    if not status.isdigit():
        body, status = output, "0"
    if not 200 <= int(status) < 300:
        raise ConnectorError(f"GitLab {method} {path}: HTTP {status}: {body.strip()[:300]}")
    try:
        return json.loads(body) if body.strip() else {}
    except ValueError as exc:
        raise ConnectorError(f"GitLab {method} {path}: ответ не JSON: {body[:200]}") from exc


def gitlab_issues(connection, source) -> list[dict]:
    """Open issues that carry every label of the input and none of its exclusions."""
    query = "state=opened&per_page=100&order_by=created_at&sort=asc"
    if source.labels:
        query += "&labels=" + quote(",".join(source.labels), safe=",")
    issues = gitlab_request(connection, "GET", f"/issues?{query}")
    requests = []
    for issue in issues if isinstance(issues, list) else []:
        labels = issue.get("labels") or []
        if any(label in labels for label in source.exclude_labels):
            continue
        author = issue.get("author") or {}
        requests.append({
            "iid": int(issue["iid"]),
            "title": (issue.get("title") or "")[:250],
            "body": (issue.get("description") or "")[:source.body_limit],
            "author": author.get("name") or author.get("username") or "",
            "labels": labels,
            "web_url": issue.get("web_url") or "",
            "created_at": issue.get("created_at") or "",
        })
    return requests


def gitlab_apply(connection, issue: int | None, *, create: dict | None = None,
                 comment: str = "", labels: str | None = None, add_labels: str = "",
                 remove_labels: str = "", assignee_id: str = "", state: str = "") -> dict:
    """Do the operations of a gitlab step on one issue, in order; its iid and URL."""
    result: dict = {}
    if create is not None:
        created = gitlab_request(connection, "POST", "/issues", {
            "title": create["title"][:250] or "(без названия)",
            "description": create.get("description", ""),
            **({"labels": create["labels"]} if create.get("labels") else {}),
        })
        issue = int(created["iid"])
        result["web_url"] = created.get("web_url", "")
    if issue is None:
        raise ConnectorError("GitLab: не указан issue")
    result["iid"] = int(issue)
    if comment:
        gitlab_request(connection, "POST", f"/issues/{issue}/notes", {"body": comment})
    update: dict = {}
    if labels is not None:
        update["labels"] = labels
    if add_labels:
        update["add_labels"] = add_labels
    if remove_labels:
        update["remove_labels"] = remove_labels
    if assignee_id:
        update["assignee_ids"] = [int(assignee_id)]
    if state:
        update["state_event"] = state
    if update:
        updated = gitlab_request(connection, "PUT", f"/issues/{issue}", update)
        if isinstance(updated, dict) and updated.get("web_url"):
            result["web_url"] = updated["web_url"]
    return result


# --- a reply by mail ------------------------------------------------------------------

def base_subject(subject: str) -> str:
    """The subject without Re:/Fwd: prefixes and line breaks."""
    cleaned = re.sub(r"\s+", " ", subject or "").strip()
    while True:
        stripped = re.sub(r"^(re|fwd?|fw)\s*:\s*", "", cleaned, flags=re.I)
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


def reply_address(request: dict, source) -> str:
    """Where a reply to the request goes, or "" when there is nobody to answer.

    A letter from a form service comes FROM the service; the author's own
    address, if the form asked for it, is in Reply-To.
    """
    for candidate in (request.get("reply_to"), request.get("from")):
        address = address_of(candidate or "")
        if not address or _NO_REPLY.match(address):
            continue
        if source.require_from_domain and _in_domain(address, source.require_from_domain):
            continue
        return address
    return ""


def send_reply(source, request: dict, *, text: str, subject: str, smtp_host: str,
               smtp_port: int, from_name: str, credentials: tuple[str, str],
               smtp_factory=None) -> str:
    """Answer the author of the request; the address it went to."""
    recipient = reply_address(request, source)
    if not recipient:
        raise ConnectorError("у заявки нет адреса автора, на который можно ответить")
    user, password = credentials
    message = EmailMessage()
    message["From"] = f"{from_name} <{user}>" if from_name else user
    message["To"] = recipient
    message["Subject"] = re.sub(r"[\r\n]+", " ", subject).strip()[:250]
    message_id = (request.get("message_id") or "").strip()
    if message_id.startswith("<") and "\n" not in message_id:
        message["In-Reply-To"] = message_id
        message["References"] = message_id
    message.set_content(text.strip() + "\n")
    factory = smtp_factory or smtplib.SMTP_SSL
    last_error = None
    for attempt in range(2):  # mail.ru turns automation away in waves
        try:
            with factory(smtp_host, smtp_port, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
            return recipient
        except (OSError, smtplib.SMTPException) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(5)
    raise ConnectorError(f"SMTP: {type(last_error).__name__}: {last_error}")
