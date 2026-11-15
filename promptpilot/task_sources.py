"""Explicit, bounded GET-only peers for the business-task read model.

No source is discovered automatically. Credentials stay in this backend and
never become part of public source descriptors, items, history, or errors.
This module never imports remote queues or calls workflow reconciliation.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

HTTP_TIMEOUT = 2
MAX_BODY = 2 * 1024 * 1024
MAX_CONFIG = 256 * 1024
MAX_SOURCES = 16
MAX_PAGES = 10
PAGE_SIZE = 100
_IO_SLOTS = threading.BoundedSemaphore(16)
LEGACY_MESSAGE = ("Старый API источника. Доступны только Workflow. "
                  "Для полного списка задач обновите PromptPilot на источнике.")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_OBJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class _Unavailable(Exception):
    def __init__(self, code="unavailable", message="Источник недоступен"):
        self.code, self.message = code, message


class _NotFound(_Unavailable):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, repr=False)
class _Source:
    id: str
    label: str
    base_url: str
    token_env: str | None = None
    token_file: Path | None = None

    def public(self, status="online", **extra):
        return {"id": self.id, "label": self.label, "status": status,
                "readonly": True, **extra}


def _error(code, message, source_id=None):
    result = {"code": code, "message": message}
    if source_id:
        result["source_id"] = source_id
    return result


def _read_bounded(path, limit):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("size limit")
    return raw


def _json(raw):
    def invalid_constant(_value):
        raise ValueError("non-finite JSON")
    return json.loads(raw, parse_constant=invalid_constant)


def _base_url(value):
    if (not isinstance(value, str) or len(value) > 2048
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError("URL")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "\\" in value
            or "%" in parsed.path or ".." in parsed.path.split("/")):
        raise ValueError("URL")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("port")
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    # Remote HTTP credentials must go through an explicitly prepared TLS
    # endpoint or a loopback tunnel, not plaintext across computers.
    if parsed.scheme == "http" and not loopback:
        raise ValueError("non-loopback HTTP")
    if port == config.PORT and (loopback or host == str(config.HOST).lower()):
        raise ValueError("local source")
    return value.rstrip("/")


def _load(sources_file=None):
    path = Path(sources_file or os.environ.get("PP_TASK_SOURCES_FILE")
                or config.DB_DIR / "task-sources.json")
    try:
        raw = _read_bounded(path, MAX_CONFIG)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError):
        return [], [_error("source_config_unavailable", "Не удалось прочитать список источников")]
    try:
        data = _json(raw)
        if not isinstance(data, dict) or set(data) - {"version", "sources"}:
            raise ValueError("format")
        if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
            raise ValueError("version")
        entries = data["sources"]
        if not isinstance(entries, list) or len(entries) > 64:
            raise ValueError("sources")
    except (ValueError, KeyError, TypeError, RecursionError):
        return [], [_error("source_config_invalid", "Неверный формат списка источников")]
    ids = Counter(v.get("id") for v in entries
                  if isinstance(v, dict) and isinstance(v.get("id"), str))
    sources, errors, reported_duplicates = [], [], set()
    for entry in entries:
        source_id = entry.get("id") if isinstance(entry, dict) else None
        valid_id = (isinstance(source_id, str) and _SOURCE_ID.fullmatch(source_id)
                    and source_id != "local")
        if not valid_id:
            errors.append(_error("source_config_invalid", "Недопустимый идентификатор источника"))
            continue
        if ids[source_id] > 1:
            if source_id not in reported_duplicates:
                errors.append(_error("source_duplicate", "Повторяющийся источник пропущен", source_id))
                reported_duplicates.add(source_id)
            continue
        try:
            if set(entry) - {"id", "label", "base_url", "token_env", "token_file"}:
                raise ValueError("unknown fields")
            label = entry["label"]
            if (not isinstance(label, str) or not label.strip() or len(label) > 100
                    or any(ord(c) < 32 or ord(c) == 127 for c in label)):
                raise ValueError("label")
            base_url = _base_url(entry["base_url"])
            token_env, token_file = entry.get("token_env"), entry.get("token_file")
            if token_env is not None and token_file is not None:
                raise ValueError("ambiguous credential")
            if token_env is not None and (not isinstance(token_env, str)
                                           or not _ENV_NAME.fullmatch(token_env)):
                raise ValueError("environment")
            if token_file is not None:
                if not isinstance(token_file, str) or not token_file or len(token_file) > 4096:
                    raise ValueError("credential path")
                token_file = Path(token_file)
                if not token_file.is_absolute():
                    token_file = path.parent / token_file
            if len(sources) >= MAX_SOURCES:
                raise ValueError("source limit")
            sources.append(_Source(source_id, label.strip(), base_url, token_env, token_file))
        except (ValueError, KeyError, TypeError):
            errors.append(_error("source_config_invalid", "Источник пропущен: проверьте его настройки", source_id))
    return sources, errors


def _token(source):
    try:
        if source.token_env is not None:
            token = os.environ.get(source.token_env)
        elif source.token_file is not None:
            token = _read_bounded(source.token_file, 8192).decode("utf-8").strip()
        else:
            return None
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError("credential")
        return token
    except (OSError, ValueError):
        raise _Unavailable("credentials_unavailable", "Не настроен доступ к источнику") from None


class _Client:
    def __init__(self, source):
        self.source, self.token, self.requests = source, _token(source), 0
        # Never inherit proxy URLs/passwords or implicit authentication handlers.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _read(self, request, url, cancelled, deadline):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or cancelled.is_set():
                raise _Unavailable("timeout", "Источник не ответил вовремя")
            with self.opener.open(request, timeout=min(HTTP_TIMEOUT, remaining)) as response:
                if response.geturl() != url:
                    raise _Unavailable("redirect_refused", "Перенаправление источника запрещено")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
                    raise _Unavailable("response_too_large", "Ответ источника превышает предел")
                content_type = response.headers.get_content_type()
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
                raw = bytearray()
                while True:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        raise _Unavailable("timeout", "Источник не ответил вовремя")
                    # read1 returns a received chunk rather than waiting for an
                    # entire body whose sender can keep the socket alive forever.
                    chunk = response.read1(min(65536, MAX_BODY + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY:
                        raise _Unavailable("response_too_large", "Ответ источника превышает предел")
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise _Unavailable("timeout", "Источник не ответил вовремя")
                return _json(raw)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 404:
                raise _NotFound("not_found", "Объект источника не найден") from None
            raise _Unavailable() from None
        except _Unavailable:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise _Unavailable("invalid_response", "Не удалось прочитать данные источника") from None

    def get(self, path, query=None):
        if self.requests >= MAX_PAGES:
            raise _Unavailable("request_limit", "Достигнут предел чтения источника")
        self.requests += 1
        url = self.source.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, method="GET", headers=headers)
        # A socket timeout is not a wall deadline (DNS and drip-fed headers/body
        # can exceed it). Never join a timed-out I/O worker in the API thread.
        # Cancellation is checked by chunk; stubborn OS/DNS/header I/O may live
        # longer, but at most sixteen such GETs exist across all callers.
        slots = _IO_SLOTS
        if not slots.acquire(blocking=False):
            raise _Unavailable("read_capacity", "Чтение источников временно занято")
        deadline = time.monotonic() + HTTP_TIMEOUT
        cancelled, reply = threading.Event(), queue.Queue(maxsize=1)

        def perform():
            try:
                value = self._read(request, url, cancelled, deadline)
                result = True, value
            except _Unavailable as exc:
                result = False, exc
            except Exception:
                # No raw transport/parser exception is public data.
                result = False, _Unavailable("invalid_response", "Не удалось прочитать данные источника")
            finally:
                slots.release()
            if not cancelled.is_set():
                reply.put_nowait(result)

        try:
            threading.Thread(target=perform, daemon=True, name="task-source-get").start()
        except RuntimeError:
            slots.release()
            raise _Unavailable("read_capacity", "Чтение источников временно занято") from None
        try:
            ok, result = reply.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            cancelled.set()
            raise _Unavailable("timeout", "Источник не ответил вовремя") from None
        if time.monotonic() >= deadline:
            cancelled.set()
            raise _Unavailable("timeout", "Источник не ответил вовремя")
        if not ok:
            raise result
        return result


def _identity(kind, object_id):
    if kind == "task":
        return type(object_id) is int and 0 < object_id <= 2**63 - 1
    return kind == "workflow" and isinstance(object_id, str) and bool(_OBJECT_ID.fullmatch(object_id))


def _item(raw, source):
    from .task_feed import namespace_item
    if (not isinstance(raw, dict) or raw.get("source_id") != "local"
            or not _identity(raw.get("kind"), raw.get("object_id"))):
        raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
    return namespace_item(raw, source.id, source.label, readonly=True)


def _list_one(source):
    from .task_feed import project_workflow
    try:
        client = _Client(source)
        legacy, items, offset, truncated = False, [], 0, False
        try:
            page = client.get("/api/task-feed", {"local_only": "true", "limit": PAGE_SIZE, "offset": 0})
        except _NotFound:
            legacy = True
            page = client.get("/api/workflows", {"limit": PAGE_SIZE, "offset": 0})
        seen = set()
        while True:
            rows = page if legacy else page.get("items") if isinstance(page, dict) else None
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
            if not legacy:
                total = page.get("total")
                if (type(total) is not int or total < offset + len(rows)
                        or not rows and total > offset):
                    raise _Unavailable("invalid_response", "Источник вернул неполный список задач")
            for row in rows:
                if legacy:
                    if not isinstance(row, dict) or not _identity("workflow", row.get("id")):
                        raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
                    item = project_workflow(row, source_id=source.id,
                                            source_label=source.label, readonly=True)
                else:
                    if isinstance(row, dict) and row.get("source_id") != "local":
                        truncated = True  # Do not recursively import a peer's other peers.
                        continue
                    item = _item(row, source)
                if item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)
                else:
                    truncated = True
            offset += len(rows)
            total = page.get("total") if isinstance(page, dict) else None
            more = len(rows) == PAGE_SIZE if legacy else offset < total
            if not more:
                break
            if client.requests >= MAX_PAGES:
                truncated = True
                break
            page = client.get("/api/workflows" if legacy else "/api/task-feed",
                              {"limit": PAGE_SIZE, "offset": offset,
                               **({} if legacy else {"local_only": "true"})})
        status = "partial" if legacy or truncated else "online"
        message = LEGACY_MESSAGE if legacy else "Показана только доступная часть списка"
        public = source.public(status, api="workflows" if legacy else "task-feed",
                               capabilities={"tasks": not legacy, "workflows": True},
                               **({"message": message} if status == "partial" else {}))
        return items, public
    except (_Unavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
        error = exc if isinstance(exc, _Unavailable) else _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
        return [], source.public("unavailable", error=_error(error.code, error.message))


def list_source_items(*, sources_file=None):
    """Return {items, sources, errors}; no credentials, URLs, or raw peer config."""
    sources, errors = _load(sources_file)
    items, states = [], []
    if sources:
        with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
            for rows, state in pool.map(_list_one, sources):
                items.extend(rows)
                states.append(state)
    return {"items": items, "sources": states, "errors": errors}


def _history(runs, events, rounds=()):
    from .task_feed import project_history
    return project_history(runs, events, rounds)


def _public_detail(raw, source):
    from .task_feed import project_artifact
    if not isinstance(raw, dict) or not isinstance(raw.get("history"), dict):
        raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
    history = raw["history"]
    runs, events, artifacts = history.get("runs", []), history.get("events", []), raw.get("artifacts", [])
    if any(not isinstance(v, list) or len(v) > 1000 for v in [runs, events, artifacts]):
        raise _Unavailable("invalid_response", "История источника превышает предел")
    result = raw.get("result") or {}
    safe_result = {"available": result.get("available") is True}
    if type(result.get("characters")) is int and result["characters"] >= 0:
        safe_result["characters"] = result["characters"]
    if isinstance(result.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", result["sha256"]):
        safe_result["sha256"] = result["sha256"]
    for field in ["completed_at", "verdict"]:
        if isinstance(result.get(field), str):
            safe_result[field] = result[field][:100]
    return {"item": _item(raw.get("item"), source), "history": _history(runs, events),
            "result": safe_result, "artifacts": [project_artifact(v) for v in artifacts]}


def _legacy_detail(client, source, object_id):
    from .task_feed import project_workflow
    base = "/api/workflows/" + urllib.parse.quote(object_id, safe="")
    row = client.get(base)
    if not isinstance(row, dict) or row.get("id") != object_id:
        raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
    rounds = client.get(base + "/rounds")
    events = client.get(base + "/events", {"limit": 200})
    if not isinstance(rounds, list) or not isinstance(events, list):
        raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
    runs, truncated = [], len(rounds) > 5 or len(events) >= 200
    # At most nine GETs including the failed upgraded-peer probe. Old endpoints
    # may ignore limits, so also bound arrays after the bounded body read.
    for round_row in rounds[:5]:
        round_id = round_row.get("id") if isinstance(round_row, dict) else None
        if not isinstance(round_id, str) or not _OBJECT_ID.fullmatch(round_id):
            raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
        page = client.get(base + "/rounds/" + urllib.parse.quote(round_id, safe="") + "/runs")
        if not isinstance(page, list):
            raise _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
        truncated = truncated or len(page) > 100
        runs.extend(page[:100])
    history = _history(runs, events[:200], rounds[:5])
    history["truncated"] = truncated
    return {"item": project_workflow(row, source_id=source.id,
                                     source_label=source.label, readonly=True),
            "history": history, "result": {"available": False}, "artifacts": []}


def get_source_detail(source_id, kind, object_id, *, sources_file=None):
    """Get registered remote detail only; unknown IDs never become caller URLs."""
    sources, errors = _load(sources_file)
    source = next((v for v in sources if v.id == source_id), None)
    if source is None:
        return {"item": None, "source": {"id": source_id if isinstance(source_id, str)
                                          and _SOURCE_ID.fullmatch(source_id) else "unknown",
                                          "status": "unavailable", "readonly": True},
                "error": _error("source_not_found", "Источник не зарегистрирован"), "errors": errors}
    if not _identity(kind, object_id):
        return {"item": None, "source": source.public("unavailable"),
                "error": _error("not_found", "Объект источника не найден")}
    try:
        client = _Client(source)
        path = "/api/task-feed/local/" + kind + "/" + urllib.parse.quote(str(object_id), safe="")
        try:
            detail = _public_detail(client.get(path), source)
            if (detail["item"]["kind"] != kind
                    or detail["item"]["object_id"] != object_id):
                raise _Unavailable("invalid_response", "Источник вернул другой объект")
            state = source.public()
        except _NotFound:
            if kind != "workflow":
                raise
            detail = _legacy_detail(client, source, object_id)
            state = source.public("partial", message=LEGACY_MESSAGE)
        return {**detail, "source": state}
    except (_Unavailable, ValueError, TypeError, KeyError, AttributeError) as exc:
        error = exc if isinstance(exc, _Unavailable) else _Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
        return {"item": None, "source": source.public("unavailable"),
                "error": _error(error.code, error.message)}
