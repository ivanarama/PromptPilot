"""Inputs and outputs of flows (promptpilot/flows.py).

Everything here touches the outside world: a mailbox that anyone can write
to, a public page that anyone can read. The rules of engagement live here
once instead of in every intake script:

- a letter is judged by its headers before its body is fetched; its sender
  must be the one the flow expects (a form service, an allow-list), and
  where configured, proven by DKIM of *our* receiving server;
- text that came from outside is published only as plain text, without
  links, within a length cap.
"""

import email
import imaplib
import json
import os
import re
import time
from email import policy
from email.header import decode_header
from email.utils import parseaddr
from pathlib import Path

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
    if source.allow_from:
        entries = [entry.strip().lower() for entry in source.allow_from if entry.strip()]
        if not any(address == entry or _in_domain(address, entry) for entry in entries):
            return f"отправитель {address or '?'} не в allow_from"
    return None


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
    return {
        "message_id": msg.get("Message-ID") or "",
        "from": address_of(decode_mime(msg.get("From"))),
        "reply_to": address_of(decode_mime(msg.get("Reply-To"))),
        "subject": subject,
        "title": title[:120],
        "author": author,
        "body": body,
        "date": msg.get("Date") or "",
    }


def poll_mailbox(source, seen: set, credentials: tuple[str, str], *,
                 connect=None) -> tuple[list[dict], list[str]]:
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
