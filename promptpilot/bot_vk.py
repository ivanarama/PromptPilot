"""VK-бот управления пайплайном (Bots Long Poll, без прокси из РФ).

Транспорт — только stdlib (urllib): groups.getLongPollServer → a_check long
poll → messages.send. Токен и id сообщества — env PP_VK_TOKEN / PP_VK_GROUP_ID
(пусто → запуск отказывается с внятной причиной). Белый список — env
PP_VK_ALLOWED_IDS (id через запятую) или {DB_DIR}/vk_config.json вида
{"allowed_ids":[...]}; вход по паролю PP_VK_PASSWORD дописывает id в файл.

Команды v1 (управление пайплайном):
  Статус / Задачи / Задача N / Миссии / Миссия <код> /
  «резюм <текст...>» — продолжить стоящий воркфлоу с адресным заданием (16.6).
Фоновый поток присылает владельцу события: awaiting_human, каскадные блокеры,
завершение/падение воркфлоу.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import db, workflows
from .config import DB_DIR

logger = logging.getLogger("promptpilot.bot_vk")

VK_API_VERSION = "5.199"
POLL_WAIT_SECONDS = 25
NOTIFY_INTERVAL_SECONDS = 20
NOTIFY_EVENT_TYPES = {
    "cascade.open_blocker",
    "cascade.all_slots_skipped",
    "cascade.cadence_deferred",
    "workflow.awaiting_human",
    "review.awaiting_decision",
    "review.passed",
    "review.revision_required",
    "stage.advanced",
}
REPLY_LEN_LIMIT = 3500

VK_TOKEN = os.environ.get("PP_VK_TOKEN", "").strip()
VK_GROUP_ID = os.environ.get("PP_VK_GROUP_ID", "").strip()
VK_ALLOWED_ENV = os.environ.get("PP_VK_ALLOWED_IDS", "").strip()
VK_PASSWORD = os.environ.get("PP_VK_PASSWORD", "").strip()

_vk_config_path = DB_DIR / "vk_config.json"
_pw_granted: dict[int, float] = {}
_PW_TTL = 3600.0


# ── авторизация ──────────────────────────────────────────────────────────────

def _allowed_ids() -> set[int]:
    ids: set[int] = set()
    for part in VK_ALLOWED_ENV.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    try:
        data = json.loads(_vk_config_path.read_text(encoding="utf-8"))
        for value in data.get("allowed_ids") or []:
            if isinstance(value, int):
                ids.add(value)
    except (OSError, ValueError):
        pass
    return ids


def _persist_allowed(user_id: int) -> None:
    data = {"allowed_ids": sorted(_allowed_ids() | {user_id})}
    _vk_config_path.parent.mkdir(parents=True, exist_ok=True)
    _vk_config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_authorized(user_id: int) -> bool:
    return user_id in _allowed_ids()


def _pw_ok(user_id: int) -> bool:
    exp = _pw_granted.get(user_id)
    return exp is not None and exp > time.monotonic()


def _pw_grant(user_id: int) -> None:
    _pw_granted[user_id] = time.monotonic() + _PW_TTL


# ── транспорт (инъектируется — тесты подменяют) ──────────────────────────────

class VKTransport:
    """Настоящий VK-транспорт: long poll + messages.send, только stdlib."""

    def __init__(self, token: str, group_id: str):
        self.token = token
        self.group_id = group_id
        self._server: dict | None = None

    def _api(self, method: str, **params) -> dict:
        params = {"access_token": self.token, "v": VK_API_VERSION, **params}
        data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(
            f"https://api.vk.com/method/{method}", data=data)
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if "error" in payload:
            raise RuntimeError(f"VK {method}: {payload['error']}")
        return payload.get("response") or {}

    def _long_poll_server(self) -> dict:
        # lp_version=2 — как у vk_api: без версии long poll не отдаёт
        # события message_event (нажатия callback-кнопок)
        self._server = self._api(
            "groups.getLongPollServer",
            group_id=self.group_id, lp_version="2")
        return self._server

    def reconnect(self) -> None:
        """Сбросить long poll сессию — лекарство от оцепеневшего ts."""
        self._server = None

    def poll(self, timeout: int = POLL_WAIT_SECONDS) -> list[dict]:
        if not self._server:
            self._long_poll_server()
        assert self._server is not None
        query = urllib.parse.urlencode({
            "act": "a_check", "key": self._server["key"],
            "ts": self._server["ts"], "wait": timeout, "mode": 2,
            "version": "2",
        })
        url = f"{self._server['server']}?{query}"
        with urllib.request.urlopen(url, timeout=timeout + 10) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if payload.get("failed"):
            self._long_poll_server()
            return []
        self._server["ts"] = payload.get("ts", self._server["ts"])
        return payload.get("updates") or []

    def send(self, peer_id: int, text: str,
             keyboard: list[list[dict]] | None = None,
             attachment: str = "") -> None:
        # random_id обязан быть int64: склейка времени+peer переполняла
        # разряд (23 знака) и ВК молча отвергал ответ — владелец видел тишину
        params: dict = {
            "peer_id": peer_id, "message": text[:REPLY_LEN_LIMIT],
            "random_id": random.getrandbits(62),
        }
        if attachment:
            params["attachment"] = attachment
        if keyboard:
            params["keyboard"] = json.dumps(
                {"inline": False, "buttons": keyboard}, ensure_ascii=False)
        self._api("messages.send", **params)

    def answer_event(self, event_id: str, user_id: int, peer_id: int) -> None:
        """Подтвердить нажатие callback-кнопки, иначе у владельца виснет."""
        self._api(
            "messages.sendMessageEventAnswer",
            event_id=event_id, user_id=user_id, peer_id=peer_id,
        )

    # ── документы сообщества: оф-сайт память конвейера (ключ с docs) ──

    def docs_upload(self, title: str, content: bytes,
                    mime: str = "text/plain") -> dict:
        """Загрузить файл в документы сообщества, вернуть сохранённый doc."""
        server = self._api("docs.getWallUploadServer",
                           group_id=self.group_id)
        url = server.get("upload_url")
        if not url:
            raise RuntimeError("VK docs: нет upload_url")
        boundary = "----pp" + str(random.getrandbits(48))
        filename = _ascii_filename(title)
        parts = [
            (f'--{boundary}\r\n'
             f'Content-Disposition: form-data; name="file"; '
             f'filename="{filename}"\r\n'
             f'Content-Type: {mime}\r\n\r\n').encode(),
            content,
            f'\r\n--{boundary}--\r\n'.encode(),
        ]
        request = urllib.request.Request(
            url, data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            uploaded = json.loads(response.read().decode("utf-8", "replace"))
        file_ref = uploaded.get("file")
        if not file_ref:
            raise RuntimeError(f"VK docs upload: {uploaded}")
        saved = self._api("docs.save", file=file_ref,
                          group_id=self.group_id, title=title)
        # docs.save отдаёт либо {'doc': {...}}, либо список
        if isinstance(saved, list):
            return saved[0] if saved else {}
        return saved.get("doc") or saved

    def docs_list(self, count: int = 200) -> list[dict]:
        """Документы сообщества (для поиска старых бэкапов)."""
        result = self._api("docs.get", owner_id=-int(self.group_id),
                           count=count)
        return result.get("items") or []

    def docs_delete(self, owner_id: int, doc_id: int) -> None:
        self._api("docs.delete", owner_id=owner_id, doc_id=doc_id)

    def download(self, url: str, limit: int = 50 * 1024 * 1024) -> bytes:
        with urllib.request.urlopen(url, timeout=90) as response:
            data = response.read(limit)
        return data

    def docs_upload_for_message(self, peer_id: int, title: str,
                                content: bytes) -> str:
        """Документ-вложение в сообщение: путь через messages-upload
        работает БЕЗ включённого раздела «Документы» сообщества
        (хранилищный путь требует его, живая ошибка 15). Возвращает
        строку вложения doc<owner>_<id> для messages.send."""
        server = self._api("docs.getMessagesUploadServer",
                           peer_id=peer_id, type="doc")
        url = server.get("upload_url")
        if not url:
            raise RuntimeError("VK docs: нет upload_url (messages)")
        boundary = "----pp" + str(random.getrandbits(48))
        safe_title = title.replace('"', "'")[:100]
        filename = _ascii_filename(title)
        parts = [
            (f'--{boundary}\r\n'
             f'Content-Disposition: form-data; name="file"; '
             f'filename="{filename}"\r\n'
             f'Content-Type: text/plain\r\n\r\n').encode(),
            content,
            f'\r\n--{boundary}--\r\n'.encode(),
        ]
        request = urllib.request.Request(
            url, data=b"".join(parts),
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=60) as response:
            uploaded = json.loads(response.read().decode("utf-8", "replace"))
        file_ref = uploaded.get("file")
        if not file_ref:
            raise RuntimeError(f"VK docs upload(messages): {uploaded}")
        saved = self._api("docs.save", file=file_ref, title=safe_title)
        doc = saved.get("doc") if isinstance(saved, dict) else (
            saved[0] if isinstance(saved, list) and saved else {})
        if not doc:
            raise RuntimeError(f"VK docs.save(messages): {saved}")
        return f"doc{doc.get('owner_id')}_{doc.get('id')}"

    def wall_post(self, message: str, attachments: str = "") -> dict:
        """Пост на стене закрытой группы — лента конвейера для владельца."""
        params: dict = {
            "owner_id": -int(self.group_id), "from_group": 1,
            "message": message[:4000],
        }
        if attachments:
            params["attachments"] = attachments
        return self._api("wall.post", **params)

    def photo_upload_for_message(self, peer_id: int, data: bytes,
                                 ext: str = "png") -> str:
        """Фото-вложение в сообщение владельцу. photos.getWallUploadServer
        групповым токеном запрещён (живая ошибка 27), messages-путь —
        работает. Возвращает photo<owner>_<id>."""
        server = self._api("photos.getMessagesUploadServer",
                           peer_id=peer_id)
        url = server.get("upload_url")
        if not url:
            raise RuntimeError("VK photos: нет upload_url")
        boundary = "----pp" + str(random.getrandbits(48))
        parts = [
            (f'--{boundary}\r\n'
             f'Content-Disposition: form-data; name="photo"; '
             f'filename="pp-photo.{ext}"\r\n'
             f'Content-Type: image/{ext}\r\n\r\n').encode(),
            data,
            f'\r\n--{boundary}--\r\n'.encode(),
        ]
        request = urllib.request.Request(
            url, data=b"".join(parts),
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=90) as response:
            uploaded = json.loads(response.read().decode("utf-8", "replace"))
        if not uploaded.get("photo"):
            raise RuntimeError(f"VK photos upload: {uploaded}")
        saved = self._api("photos.saveMessagesPhoto",
                          server=uploaded.get("server"),
                          photo=uploaded.get("photo"),
                          hash=uploaded.get("hash"))
        item = saved[0] if isinstance(saved, list) and saved else (
            saved if isinstance(saved, dict) else {})
        if not item.get("id"):
            raise RuntimeError(f"VK photos.saveMessagesPhoto: {saved}")
        return f"photo{item.get('owner_id')}_{item.get('id')}"


def _btn(label: str, cmd: str | None = None, color: str = "primary") -> dict:
    """Текст-кнопка обычной клавиатуры: нажатие = обычное message_new,
    доставка которого надёжна (callback-события message_event ВК терял
    молча — два живых инцидента). label обязан быть командой роутера."""
    del cmd  # текстовые кнопки несут команду самим текстом
    return {"action": {"type": "text", "label": label}, "color": color}


def main_keyboard() -> list[list[dict]]:
    """Обычная ВК-клавиатура (полоска над полем ввода). Каждый ответ бота
    прикладывает её заново — после ручного набора она возвращается со
    следующим сообщением бота."""
    return [
        [_btn("Статус"), _btn("Задачи")],
        [_btn("Миссии"), _btn("Находки"), _btn("Помощь")],
    ]


# ── команды ──────────────────────────────────────────────────────────────────

def _clip(text: str, limit: int = 700) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


_ROUND_STATE_RU = {
    "pending": "ожидает", "executing": "исполняется", "gating": "гейт",
    "reviewing": "ревью", "revision_required": "доработка",
    "completed": "завершён", "failed": "доработка", "cancelled": "отменён",
}


def _round_state(current) -> str:
    """Статус раунда по-русски: «failed» в терминах ядра = итерация
    доработки, не авария — и пугает владельца зря."""
    value = current.status.value if current else ""
    return _ROUND_STATE_RU.get(value, value)


_WF_STATE_RU = {
    "draft": "черновик", "planning": "планирование",
    "awaiting_plan_approval": "ждёт утверждения плана",
    "queued": "в очереди", "executing": "исполняется", "gating": "гейт",
    "reviewing": "ревью", "revision_required": "доработка после ревью",
    "awaiting_human": "СТОИТ, ждёт твоего решения",
    "completed": "завершена", "failed": "упала", "cancelled": "отменена",
}


def _work_line() -> str:
    """Что бежит прямо сейчас — единственная живая задача, не список."""
    from promptpilot.models import TaskStatus
    running = db.list_tasks(status=TaskStatus.RUNNING)
    if not running:
        return "ничего не исполняется"
    task = max(running, key=lambda t: t.started_at or t.created_at)
    started = task.started_at or task.created_at
    minutes = max(0, int((time.time() - started.timestamp()) // 60))
    return f"задача #{task.id} ({task.provider or '?'}), идёт {minutes} мин"


def _findings_brief(workflow_id: str) -> str:
    """Долг ревью одной строкой: срочные отдельно от фонового шума."""
    hot = background = 0
    for finding in db.list_workflow_findings(workflow_id):
        if finding.status.value not in {"open", "reopened"}:
            continue
        severity = finding.severity.value if finding.severity else "info"
        if severity in {"blocker", "high"}:
            hot += 1
        else:
            background += 1
    if not hot and not background:
        return "чисто"
    parts = []
    if hot:
        parts.append(f"🔥 {hot} срочных")
    if background:
        parts.append(f"{background} фоновых")
    return "долг ревью: " + ", ".join(parts)


def _wf_summary(workflow) -> str:
    stages = db.list_workflow_stages(workflow.id)
    done = sum(1 for s in stages
               if s.status.value in {"completed", "skipped"})
    current_stage = next(
        (s for s in stages if s.status.value == "executing"),
        next((s for s in stages if s.status.value != "completed"
              and s.status.value != "skipped"), None))
    rounds = db.list_workflow_rounds(workflow.id)
    current = next(
        (r for r in rounds if r.round_no == workflow.current_round), None)
    lines = [f"🛠 {workflow.slug} — {_WF_STATE_RU.get(workflow.status.value, workflow.status.value)}"]
    parts: list[str] = []
    if current_stage:
        title_bit = f" «{current_stage.title}»" if current_stage.title else ""
        parts.append(f"стадия {current_stage.code}{title_bit}"
                     f" ({done}/{len(stages)})")
    elif stages:
        parts.append(f"план {done}/{len(stages)} стадий завершено")
    if current:
        parts.append(f"раунд {workflow.current_round} — {_round_state(current)}")
    elif workflow.current_round:
        parts.append(f"раунд {workflow.current_round}")
    if parts:
        lines.append(", ".join(parts))
    lines.append(f"Сейчас: {_work_line()}")
    brief = _findings_brief(workflow.id)
    if brief != "чисто":
        lines.append(brief)
    if workflow.status.value == "awaiting_human":
        lines.append("⛔️ Ждёт твоего решения — кнопки под сообщением")
    return "\n".join(lines)


def cmd_status() -> str:
    return "\n\n".join(_wf_summary(w) for w in db.list_workflows()) \
        or "Воркфлоу нет."


# ── документы сообщества: бэкап памяти конвейера (оф-сайт) ───────────────────

BACKUP_DOC_PREFIX = "backup-"

_ASCII_MAP = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ы': 'y',
    'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def _ascii_filename(title: str) -> str:
    """upload-серверы ВК не переваривают не-ASCII имена файлов (живая
    ошибка no_file_no_tmp_dir) — filename транслитерируем, заголовок
    документа при этом может остаться русским."""
    slug = "".join(_ASCII_MAP.get(ch, ch) for ch in title.lower())
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug).strip("-.") or "file"
    return slug[:90]


def _backup_sources() -> list[tuple[str, bytes]]:
    """Что бэкапим: память каждой миссии + журнал дежурного."""
    sources: list[tuple[str, bytes]] = []
    list_memory = getattr(db, "list_memory", None)  # memory-API опционален
    for workflow in db.list_workflows():
        try:
            if list_memory is None:
                raise AttributeError  # нет memory-API — только журнал
            for note in list_memory(workflow.id):
                if (note.get("name") or "") != "memory.md":
                    continue  # рабочие записки-элиситы — не бэкап
                content = note.get("content") or ""
                if len(content.encode("utf-8")) < 64:
                    continue
                title = f"{BACKUP_DOC_PREFIX}{workflow.slug}-memory.md"
                sources.append((title[:100], content.encode("utf-8")))
        except Exception as exc:  # noqa: BLE001 - память опциональна
            logger.warning('vk backup memory: %s', exc)
            continue
    try:
        journal = Path(__file__).resolve().parents[2] / "watchdog" / "journal.md"
        if journal.is_file():
            sources.append((BACKUP_DOC_PREFIX + "watchdog-journal.md",
                            journal.read_bytes()))
    except OSError as exc:
        logger.warning('vk backup journal: %s', exc)
    return sources


def _backup_registry_path() -> Path:
    return DB_DIR / "vk-backup-registry.json"


def _load_backup_registry() -> dict:
    try:
        return json.loads(
            _backup_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cmd_backup(transport: VKTransport | None = None) -> str:
    """Выгрузить память конвейера в документы сообщества. Старый бэкап
    удаляется по ЛОКАЛЬНОМУ реестру id: docs.get групповым токеном
    недоступен (живая ошибка 27), а save/delete — доступны."""
    transport = transport or VKTransport(VK_TOKEN, VK_GROUP_ID)
    sources = _backup_sources()
    if not sources:
        return "Бэкапить нечего: памяти и журнала не нашлось."
    registry = _load_backup_registry()
    saved = []
    for title, content in sources:
        old = registry.get(title) or {}
        if old.get("owner_id") and old.get("id"):
            try:
                transport.docs_delete(int(old["owner_id"]), int(old["id"]))
            except Exception as exc:  # noqa: BLE001 - старый не мешает
                logger.warning("vk backup delete-old: %s", exc)
        try:
            doc = transport.docs_upload(title, content)
        except Exception as exc:  # noqa: BLE001 - владельцу видна причина
            saved.append(f"❌ {title}: {exc}")
            continue
        if doc.get("id") and doc.get("owner_id"):
            registry[title] = {"owner_id": doc["owner_id"], "id": doc["id"]}
        saved.append(f"✅ {title} ({len(content) // 1024} КБ)")
    try:
        _backup_registry_path().write_text(
            json.dumps(registry, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError as exc:
        logger.warning("vk backup registry: %s", exc)
    return "Бэкап в документах сообщества:\n" + "\n".join(saved)


def cmd_wall(transport: VKTransport | None = None) -> str:
    """Опубликовать сводку конвейера на стену закрытой группы."""
    transport = transport or VKTransport(VK_TOKEN, VK_GROUP_ID)
    try:
        post = transport.wall_post(_digest_text())
    except Exception as exc:  # noqa: BLE001 - владельцу видна причина
        return f"Не получилось: {exc}"
    pid = (post or {}).get("post_id")
    return "✅ Сводка на стене группы" + (f" (пост {pid})" if pid else "")


def cmd_screenshots(count: int = 3,
                    transport: VKTransport | None = None,
                    qa_dir: Path | None = None) -> str:
    """Последние QA-скриншоты — фото-вложениями в сообщение владельцу
    (стена для фото закрыта групповому токену, messages-путь работает)."""
    transport = transport or VKTransport(VK_TOKEN, VK_GROUP_ID)
    qa_dir = qa_dir or (Path(__file__).resolve().parents[2]
                        / "watchdog" / "qa")
    shots = sorted(
        [p for p in qa_dir.glob("*.png") if p.stat().st_size > 10_000],
        key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(count, 10))]
    if not shots:
        return f"QA-скриншотов в {qa_dir} не нашлось."
    peers = _owner_peers()
    if not peers:
        return "Нет авторизованных чатов — пришли боту любое сообщение."
    attachments = []
    for i, shot in enumerate(shots):
        if i:
            time.sleep(2)  # анти-флуд ВК: мгновенные повторные загрузки
            #                гасятся пустым photo (живой прогон 06.10)
        try:
            attachments.append(transport.photo_upload_for_message(
                peers[0], _photo_jpeg(shot.read_bytes()), ext="jpg"))
        except Exception as exc:  # noqa: BLE001 - одно фото не срывает отправку
            logger.warning("vk photo %s: %s", shot.name, exc)
    if not attachments:
        return "Не удалось загрузить ни одно фото (см. лог бота)."
    stamp = time.strftime("%d.%m %H:%M")
    sent = 0
    for peer_id in peers:
        try:
            transport.send(
                peer_id,
                f"📷 QA-скриншоты ({stamp}), последние {len(attachments)} шт.",
                None, attachment=",".join(attachments))
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("vk screenshots send: %s", exc)
    if not sent:
        return f"Фото загружены ({len(attachments)}), но не отправлены."
    return f"✅ Отправил {len(attachments)} QA-скриншотов."


def _photo_jpeg(data: bytes) -> bytes:
    """Перекодировать в обычный JPEG: upload-сервер ВК молча возвращает
    пустой photo для отдельных PNG (палитра/битность), JPEG RGB принимает
    всегда."""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            out = BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=88)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - без Pillow уйдут исходные байты
        return data


def _owner_peers() -> list[int]:
    """Авторизованные чаты владельца из vk-конфига."""
    try:
        with open(_vk_config_path, encoding="utf-8") as handle:
            cfg = json.loads(handle.read())
        return [int(p) for p in cfg.get("allowed_ids", [])]
    except (OSError, ValueError):
        return []


def _save_incoming_attachment(attachment: dict, inbox: Path) -> str | None:
    """Документ из чата → файл в inbox. Возвращает имя или None."""
    if attachment.get("type") != "doc":
        return None
    doc = attachment.get("doc") or {}
    url = str(doc.get("url") or "")
    if not url:
        return None
    try:
        transport = VKTransport(VK_TOKEN, VK_GROUP_ID)
        data = transport.download(url)
    except Exception:  # noqa: BLE001
        return None
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r'[<>:"/\\|?*]', "_", str(doc.get("title") or "file"))
    path = inbox / f"{stamp}-{safe}"
    path.write_bytes(data)
    return path.name


def cmd_findings() -> str:
    """Долг ревью по миссиям: срочные целиком, средние заголовками,
    фоновые счётчиком (П5b)."""
    blocks = []
    for workflow in db.list_workflows():
        findings = [f for f in db.list_workflow_findings(workflow.id)
                    if f.status.value in {"open", "reopened"}]
        if not findings:
            continue

        def severity(f) -> str:
            return f.severity.value if f.severity else ""

        hot = [f for f in findings if severity(f) in {"blocker", "high"}]
        medium = [f for f in findings if severity(f) == "medium"]
        background = sum(1 for f in findings
                         if severity(f) in {"low", "info"})
        parts = [f"🛠 {workflow.slug} — долг ревью:"]
        if hot:
            parts.append("🔥 СРОЧНЫЕ (закрыть в первую очередь):")
            parts += [f"  • {f.fingerprint}: {_clip(f.title, 90)}"
                      for f in hot[:10]]
            if len(hot) > 10:
                parts.append(f"  …и ещё {len(hot) - 10}")
        parts.append(f"medium: {len(medium)}, фоновых (low/info): {background}")
        parts += [f"  – {f.fingerprint}: {_clip(f.title, 80)}"
                  for f in medium[:8]]
        if len(medium) > 8:
            parts.append(f"  …и ещё {len(medium) - 8}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks) or "Открытых находок нет — всё закрыто."


def _digest_text() -> str:
    """Утренний дайджест (П5b). Квен-M9: срочные находки — в начале (лимит
    ВК 3500 режет хвост), обрезка помечена явно."""
    lines = ["☀️ Утренний дайджест конвейера", ""]
    lines.append(cmd_findings())
    lines.append("")
    lines.append(cmd_status())
    try:
        from . import usage_conveyor

        usage = usage_conveyor.summary()
        lines.append(
            f"Токены: сегодня {(usage['today']['total'] / 1e6):.1f}M, "
            f"неделя {(usage['week']['total'] / 1e6):.1f}M, "
            f"всего {(usage['total']['total'] / 1e6):.1f}M")
    except Exception:  # noqa: BLE001,S110 - дайджест живёт без статистики
        pass
    text = "\n".join(lines)
    if len(text) > REPLY_LEN_LIMIT - 60:
        text = (text[:REPLY_LEN_LIMIT - 60]
                + "\n…(дайджест обрезан; «находки» — полный список)")
    return text


def cmd_tasks() -> str:
    from promptpilot.models import TaskStatus
    running = db.list_tasks(status=TaskStatus.RUNNING)
    pending = db.list_tasks(status=TaskStatus.PENDING)
    lines = ["Бегут:"] + [
        f"  #{t.id} {t.provider or ''} {_clip(str(t.prompt), 60)}"
        for t in running[:8]
    ] or []
    lines.append("Очередь:")
    lines += [f"  #{t.id} {_clip(str(t.prompt), 60)}"
              for t in pending[:8]]
    if not running and not pending:
        lines.append("  (пусто)")
    return "\n".join(lines)


def cmd_task(task_id: int) -> str:
    task = db.get_task(task_id)
    if not task:
        return f"Задача #{task_id} не найдена."
    result = _clip(str(task.result or task.error or ""), 500)
    return (
        f"Задача #{task.id}\nСтатус: {task.status.value}\n"
        f"Провайдер: {task.provider or '—'}\n"
        f"Вердикт: {task.verdict or '—'}\n"
        f"Результат:\n{result}"
    )


def cmd_missions() -> str:
    return cmd_status()


def cmd_mission(prefix: str) -> str:
    workflow = None
    for candidate in db.list_workflows():
        if candidate.id.startswith(prefix) or candidate.slug == prefix:
            workflow = candidate
            break
    if not workflow:
        return f"Воркфлоу «{prefix}» не найден."
    detail = _wf_summary(workflow)
    detail += f"\nЦель: {_clip(workflow.objective, 160)}\nid: {workflow.id}"
    if workflow.status.value == "awaiting_human":
        detail += "\n\n" + _standstill_reason(workflow)
    return detail


def _awaiting_workflows() -> list:
    return [w for w in db.list_workflows()
            if w.status.value == "awaiting_human"]


def _standstill_reason(workflow) -> str:
    """Последний вопрос/блокер стоящего воркфлоу из событий."""
    for event in _events_tail(workflow.id, 50):
        if event.event_type not in {"workflow.awaiting_human",
                                    "cascade.open_blocker",
                                    "review.awaiting_decision"}:
            continue
        payload = event.payload or {}
        for key in ("reason", "question", "text", "notes"):
            value = str(payload.get(key) or "").strip()
            if value:
                return f"Причина стойки: {_clip(value, 600)}"
        break
    return ("Причина стойки не найдена в событиях — смотри последнюю"
            " задачу исполнителя командой «Задачи».")


def _events_tail(workflow_id: str, limit: int) -> list:
    """Последние события миссии. tail-пейджинг — наше ядро-расширение;
    на стоке без него читаем первой страницой и берём хвост."""
    try:
        return db.list_workflow_events(workflow_id, limit=limit, tail=True)
    except TypeError:
        rows = db.list_workflow_events(workflow_id, limit=1000)
        return list(rows or [])[-limit:]


def cmd_standstill() -> str:
    targets = _awaiting_workflows()
    if not targets:
        return "✅ Конвейер не стоит — всё работает."
    workflow = targets[0]
    return (f"⛔️ {workflow.slug} стоит, раунд {workflow.current_round}.\n"
            f"{_standstill_reason(workflow)}\n\n"
            "Ответь текстом «резюм <твоё решение>» — или кнопкой "
            "«▶ Продолжить конвейер», чтобы пустить как есть.")


def cmd_continue() -> str:
    return cmd_resume(
        "Владелец подтвердил продолжение через VK-кнопку «▶ Продолжить "
        "конвейер»: продолжай по плану; новый блокер — снова вопрос "
        "владельцу.")


def cmd_resume(text: str) -> str:
    targets = _awaiting_workflows()
    if not targets:
        return "Нет воркфлоу в awaiting_human — резюмить нечего."
    if len(targets) > 1:
        return ("Несколько стоящих воркфлоу, уточни: "
                + ", ".join(t.slug for t in targets))
    workflow = targets[0]
    from promptpilot.models import WorkflowHumanInput
    try:
        workflows.human_input(workflow.id, WorkflowHumanInput(
            expected_version=workflow.state_version,
            text=f"{text} (через VK-бот, владелец)",
            resume=True,
        ))
    except Exception as exc:  # noqa: BLE001 - владельцу важна любая причина
        return f"Не получилось: {type(exc).__name__}: {exc}"
    fresh = db.get_workflow(workflow.id)
    stages = db.list_workflow_stages(workflow.id)
    stage = next(
        (s for s in stages if s.status.value == "executing"),
        next((s for s in stages
              if s.status.value not in {"completed", "skipped"}), None))
    stage_bit = (f"стадия {stage.code}"
                 + (f" «{stage.title}»" if stage.title else "")) if stage else "план завершён"
    state = _WF_STATE_RU.get(fresh.status.value, fresh.status.value)
    return (f"✅ Конвейер продолжен: {workflow.slug}\n"
            f"{stage_bit}, раунд {fresh.current_round} — {state}.\n"
            f"Задание исполнителю: {_clip(text, 300)}")


def _reply_keyboard(force_actions: bool = False) -> list[list[dict]]:
    """Главная клавиатура; при стойке (или событии-блокере) сверху кнопки
    действий — «Что спросили» / «Продолжить конвейер»."""
    rows: list[list[dict]] = []
    if force_actions or _awaiting_workflows():
        rows.append([
            _btn("Что спросили", "стойка", "secondary"),
            _btn("Продолжить конвейер", "продолжить", "positive"),
        ])
    rows.extend(main_keyboard())
    return rows


def handle_text(user_id: int, text: str) -> tuple[str, list[list[dict]] | None]:
    """Маршрутизатор команд. Возвращает (ответ, клавиатура|None)."""
    text = (text or "").strip()
    low = text.lower()
    if low in {"/start", "начало", "start", "меню",
               "помощь", "help", "/help"}:
        return (
            "PromptPilot VK 🤖\n"
            "Кнопки — прямо в сообщениях, просто жми. То же самое можно"
            " писать текстом:\n"
            "• Статус — миссии, стадии, что бежит сейчас\n"
            "• Задачи — очередь и бегущие задачи\n"
            "• Задача 390 — детали конкретной задачи\n"
            "• Миссии — сводка; Миссия <код> — детали\n"
            "• резюм <текст> — снять стойку конвейера заданием"
        ), _reply_keyboard()
    if low in {"статус", "/status"}:
        return cmd_status(), _reply_keyboard()
    if low in {"задачи", "/tasks"}:
        return cmd_tasks(), _reply_keyboard()
    if low.startswith("задача "):
        part = text.split(maxsplit=1)[1].strip()
        if part.isdigit():
            return cmd_task(int(part)), _reply_keyboard()
    if low in {"миссии", "/missions"}:
        return cmd_missions(), _reply_keyboard()
    if low.startswith("миссия "):
        return cmd_mission(text.split(maxsplit=1)[1].strip()), _reply_keyboard()
    if low in {"находки", "долг", "findings"}:
        return cmd_findings(), _reply_keyboard()
    if low in {"бэкап", "backup", "бекап"}:
        return cmd_backup(), _reply_keyboard()
    if low in {"сводка", "стена", "wall"}:
        return cmd_wall(), _reply_keyboard()
    if low.startswith(("скриншоты", "скрины")):
        part = text.split(maxsplit=1)
        count = int(part[1]) if len(part) > 1 and part[1].isdigit() else 3
        return cmd_screenshots(count), _reply_keyboard()
    if low in {"стойка", "/стойка", "что спросили"}:
        return cmd_standstill(), _reply_keyboard()
    if low in {"продолжить", "продолжить конвейер"}:
        return cmd_continue(), _reply_keyboard()
    if low.startswith(("резюм ", "resume ")):
        return cmd_resume(text.split(maxsplit=1)[1].strip()), _reply_keyboard()
    return handle_text(user_id, "помощь")


# ── уведомления ──────────────────────────────────────────────────────────────

# человекочитаемое название события + нужны ли кнопки действий
_RU_EVENTS = {
    "cascade.open_blocker": ("⛔️ Конвейер встал", True),
    "workflow.awaiting_human": ("⛔️ Ждёт решения владельца", True),
    "review.awaiting_decision": ("❓ Ревью ждёт решения", True),
    "review.revision_required": ("🔁 Ревью вернуло на доработку", False),
    "review.passed": ("✅ Ревью пройдено", False),
    "stage.advanced": ("➡️ Началась новая стадия", False),
    "cascade.cadence_deferred": ("⏰ Ревью отложено до льготного окна", False),
    "cascade.all_slots_skipped": ("⏭ Ревью-слоты пропущены", False),
}

# П1 «здоровье конвейера»: бот — внешний наблюдатель, живёт независимо от
# сервера/воркера (БД читает напрямую) и сообщит об их тихой смерти
HEALTH_PROCESS_NAMES = (
    "worker", "cascade", "verdict_watcher", "bot", "quota_failover", "server",
)
HEALTH_LABELS = {
    "worker": "воркер задач", "cascade": "каскад ревью",
    "verdict_watcher": "вотчер вердиктов", "bot": "Telegram-бот",
    "quota_failover": "вотчер лестницы смен", "server": "сервер API",
}
HEALTH_CHECK_EVERY_DRAINS = 15   # ~5 минут при дренаже раз в 20 с
STAND_ALERT_MINUTES = 30          # инцидент 07.10: стойка >30 мин = тревога
STAND_REPEAT_HOURS = 2            # напоминание о длящейся стойке
# квен-H3: якорь = корень репозитория, как у api.py и лаунчеров;
# DB_DIR.parent без PP_DATA_DIR уезжает в домашний каталог
HEALTH_PIDS_PATH = Path(__file__).resolve().parent.parent / ".pp-pids.json"
DIGEST_HOUR = 9                  # П5b: утренний дайджест в 09:00 локально


def process_deaths() -> list[str]:
    """Были живы (pid известен), но мертвы. pid=0 — не тревога."""
    try:
        with open(HEALTH_PIDS_PATH, encoding="utf-8-sig") as handle:
            pids = json.loads(handle.read())
    except (OSError, ValueError):
        return []
    dead = []
    for name in HEALTH_PROCESS_NAMES:
        pid = int(pids.get(name) or 0)
        if not pid:
            continue  # не запущен (выключенный Telegram-бот) — не тревога
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, 0, pid)
            if not handle:
                dead.append(name)
            else:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001,S110 - не-Windows: считаем живым
            pass
    return dead


class _NotifyLoop(threading.Thread):
    def __init__(self, transport: VKTransport, peers: dict[int, None]):
        super().__init__(daemon=True, name="vk-notify")
        self.transport = transport
        self.peers = peers
        self._last_seq = self._load_last_seq()
        self._drain_count = 0
        self._reported_dead: set[str] = set()
        self._stands: dict[str, float] = {}
        self._stands_alerted: dict[str, float] = {}
        self._last_digest_date = ""

    def _load_last_seq(self) -> int:
        seq = 0
        for workflow in db.list_workflows():
            events = _events_tail(workflow.id, 1)
            for event in events:
                seq = max(seq, int(event.seq or 0))
        return seq

    def run(self) -> None:
        while True:
            time.sleep(NOTIFY_INTERVAL_SECONDS)
            try:
                self._drain()
            except Exception as exc:  # noqa: BLE001 - фон не должен умирать
                logger.warning("vk-notify: %s: %s", type(exc).__name__, exc)

    def _drain(self) -> None:
        if not self.peers:
            return
        self._drain_count += 1
        if self._drain_count % HEALTH_CHECK_EVERY_DRAINS == 0:
            self._check_processes()
            self._check_stands()
        self._maybe_morning_digest()
        messages: list[tuple[str, bool]] = []
        for workflow in db.list_workflows():
            events = db.list_workflow_events(
                workflow.id, after_seq=self._last_seq, limit=100)
            for event in events:
                self._last_seq = max(self._last_seq, int(event.seq or 0))
                if event.event_type not in NOTIFY_EVENT_TYPES:
                    continue
                title, needs_actions = _RU_EVENTS.get(
                    event.event_type, (event.event_type, False))
                detail = ""
                payload = event.payload or {}
                reason = str(payload.get("reason") or payload.get("reasonText") or "")
                if reason:
                    detail = f": {_clip(reason, 160)}"
                messages.append(
                    (f"🛠 {workflow.slug}\n{title}{detail}", needs_actions))
        for peer_id in list(self.peers):
            for text, needs_actions in messages:
                self.transport.send(
                    peer_id, text,
                    _reply_keyboard(force_actions=needs_actions)
                    if needs_actions else None)

    def _maybe_morning_digest(self) -> None:
        """П5b: раз в сутки утром — сводка владельцу (без повторов)."""
        if time.localtime().tm_hour != DIGEST_HOUR:
            return
        today = time.strftime("%Y-%m-%d")
        if self._last_digest_date == today:
            return
        try:
            text = _digest_text()
        except Exception as exc:  # noqa: BLE001 - дайджест не роняет дренаж
            logger.warning("vk digest: %s: %s", type(exc).__name__, exc)
            return
        full_text = text
        if len(full_text) > REPLY_LEN_LIMIT - 60:
            # длинный дайджест: полный текст — документом-вложением
            # (путь messages-upload не требует раздела «Документы»)
            text = (text[:REPLY_LEN_LIMIT - 60]
                    + "\n…(полный дайджест — в прикреплённом документе)")
        sent_ok = True
        for peer_id in list(self.peers):
            try:
                attachment = ""
                if full_text is not text:
                    attachment = self.transport.docs_upload_for_message(
                        peer_id, "promptpilot-дайджест.md",
                        full_text.encode("utf-8"))
                self.transport.send(peer_id, text, main_keyboard(),
                                    attachment=attachment)
            except Exception as exc:  # noqa: BLE001 - квен-M8: сбой VK
                sent_ok = False               # не гасит дайджест навсегда
                logger.warning("vk digest send: %s", exc)
        if sent_ok:
            self._last_digest_date = today  # квен-M8: фиксация ПОСЛЕ отправки
            # лента конвейера: сводка дня — постом на стену закрытой группы
            try:
                self.transport.wall_post(full_text)
            except Exception as exc:  # noqa: BLE001 - стена не роняет цикл
                logger.warning("vk wall digest: %s", exc)
            # оф-сайт бэкап памяти раз в сутки, следом за дайджестом
            try:
                note = cmd_backup(self.transport)
                for peer_id in list(self.peers):
                    self.transport.send(peer_id, note[:REPLY_LEN_LIMIT])
            except Exception as exc:  # noqa: BLE001 - бэкап не роняет цикл
                logger.warning("vk auto-backup: %s", exc)

    def _check_stands(self) -> None:
        """Инцидент 07.10 (гнев владельца): конвейер стоял 2+ часа в
        revision_required/awaiting_human, бот молчал — уведомления покрывали
        только события, а не «долго стоит». Теперь: стоит дольше 30 минут —
        тревога, дальше напоминание каждые 2 часа, живое = молчим."""
        for workflow in db.list_workflows():
            status = workflow.status.value
            if status not in {"awaiting_human", "revision_required"}:
                self._stands.pop(workflow.id, None)
                continue
            first = self._stands.setdefault(workflow.id, time.time())
            now = time.time()
            if now - first < STAND_ALERT_MINUTES * 60:
                continue
            alerted_at = self._stands_alerted.get(workflow.id)
            if (alerted_at is not None
                    and now - alerted_at < STAND_REPEAT_HOURS * 3600):
                continue  # анти-спам: повтор не чаще раза в 2 часа
            self._stands_alerted[workflow.id] = time.time()
            label = ("ждёт твоего решения" if status == "awaiting_human"
                     else "вернулся на доработку и стоит без диспетча")
            for peer_id in list(self.peers):
                try:
                    self.transport.send(
                        peer_id,
                        f"⚠️ {workflow.slug}: {label} уже "
                        f"{int((time.time() - first) // 60)} мин (раунд "
                        f"{workflow.current_round}). «Статус» покажет детали; "
                        f"awaiting_human снимается кнопкой «Продолжить "
                        f"конвейер» или резюмом.",
                        _reply_keyboard(force_actions=True))
                except Exception as exc:  # noqa: BLE001 - стойка важнее
                    logger.warning("vk stand send: %s", exc)

    def _check_processes(self) -> None:
        """П1: тихая смерть процесса конвейера = сообщение владельцу;
        восстановление — отдельное сообщение, повторы не спамятся."""
        try:
            deaths = set(process_deaths())
        except Exception as exc:  # noqa: BLE001 - монитор не роняет дренаж
            logger.warning("vk health: %s: %s", type(exc).__name__, exc)
            return
        fresh = deaths - self._reported_dead
        recovered = self._reported_dead - deaths
        delivered = True
        for peer_id in list(self.peers):
            try:
                if fresh:
                    names = ", ".join(
                        f"{HEALTH_LABELS.get(n, n)}" for n in sorted(fresh))
                    self.transport.send(
                        peer_id,
                        f"🚨 Конвейер: мертвы — {names}. "
                        f"Дежурный цикл поднимет; проверь «Статус» позже.")
                for name in sorted(recovered):
                    self.transport.send(
                        peer_id,
                        f"✅ Конвейер: {HEALTH_LABELS.get(name, name)} снова жив.")
            except Exception as exc:  # noqa: BLE001 - квен-M8: алерт живёт
                delivered = False
                logger.warning("vk health send: %s", exc)
        if delivered:  # квен-M8: состояние меняем только после доставки
            self._reported_dead = deaths


# ── цикл бота ────────────────────────────────────────────────────────────────

def _process_update(transport: VKTransport, update: dict,
                    peers: dict[int, None]) -> None:
    """Один апдейт long poll: нажатие инлайн-кнопки или текстовое сообщение."""
    utype = update.get("type")
    if utype == "message_event":
        obj = update.get("object") or {}
        user_id = int(obj.get("user_id") or 0)
        peer_id = int(obj.get("peer_id") or user_id)
        event_id = str(obj.get("event_id") or "")
        if not user_id:
            return
        if not is_authorized(user_id):
            transport.answer_event(event_id, user_id, peer_id)
            transport.send(peer_id, "Нет доступа.")
            return
        peers[peer_id] = None
        # подтверждаем нажатие сразу: статусы и резюм бывают небыстрые;
        # отказ подтверждения (просроченное событие) не роняет обработку
        try:
            transport.answer_event(event_id, user_id, peer_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("vk answer_event %s: %s", event_id, exc)
        cmd = str((obj.get("payload") or {}).get("cmd") or "помощь")
        try:
            reply, keyboard = handle_text(user_id, cmd)
        except Exception as exc:  # noqa: BLE001 - владелец видит причину
            reply, keyboard = f"Ошибка: {type(exc).__name__}: {exc}", None
        transport.send(peer_id, reply, keyboard or _reply_keyboard())
        return
    if utype != "message_new":
        return
    message = (update.get("object") or {}).get("message") or {}
    user_id = int(message.get("from_id") or 0)
    peer_id = int(message.get("peer_id") or user_id)
    text = str(message.get("text") or "")
    if not user_id:
        return
    if not is_authorized(user_id):
        if VK_PASSWORD and _pw_ok(user_id):
            _persist_allowed(user_id)
            transport.send(peer_id, "✅ Доступ сохранён.", main_keyboard())
            peers[peer_id] = None
            return
        if VK_PASSWORD and text.strip() == VK_PASSWORD:
            _pw_grant(user_id)
            transport.send(peer_id, "Пароль принят. Отправь /start.",
                           main_keyboard())
            return
        transport.send(peer_id, "Нет доступа.")
        return
    peers[peer_id] = None
    # файлы из чата → inbox конвейера (канал «с телефона в проект»)
    saved_files = []
    for attachment in message.get("attachments") or []:
        try:
            name = _save_incoming_attachment(
                attachment, DB_DIR / "inbox")
            if name:
                saved_files.append(name)
        except Exception as exc:  # noqa: BLE001 - приём не роняет команды
            logger.warning("vk inbox: %s", exc)
    if saved_files and not text.strip():
        transport.send(
            peer_id,
            "📥 Сохранено: " + ", ".join(saved_files)
            + f"\nКаталог: {DB_DIR / 'inbox'}",
            main_keyboard())
        return
    try:
        reply, keyboard = handle_text(user_id, text)
    except Exception as exc:  # noqa: BLE001 - владелец видит причину
        reply, keyboard = f"Ошибка: {type(exc).__name__}: {exc}", None
    transport.send(peer_id, reply, keyboard or _reply_keyboard())


def _other_bot_instance() -> int | None:
    """pid чужого живого bot-vk (инцидент 07.10: четыре экземпляра от
    неудачных рестартов — ВК доставляет каждое событие каждому long-poll,
    и владелец получал каждый ответ четыре раза)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             ("(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
              " | Where-Object { $_.CommandLine -match 'bot-vk' }).ProcessId")],
            capture_output=True, text=True, timeout=30, check=False)
        pids = {int(value) for value in result.stdout.split()}
    except (OSError, ValueError):
        return None
    own = {os.getpid(), os.getppid()}
    others = sorted(pids - own - {0})
    return others[0] if others else None


def run_vk_bot(transport: VKTransport | None = None) -> None:
    if not VK_TOKEN or not VK_GROUP_ID:
        raise SystemExit(
            "VK-бот не запущен: задай PP_VK_TOKEN (ключ сообщества) и "
            "PP_VK_GROUP_ID. Белый список — PP_VK_ALLOWED_IDS или файл "
            f"{_vk_config_path}.")
    already = _other_bot_instance()
    if already is not None:
        raise SystemExit(
            f"VK-бот уже запущен (pid {already}) — этот экземпляр "
            "закрывается: второй экземпляр дублирует каждый ответ.")
    transport = transport or VKTransport(VK_TOKEN, VK_GROUP_ID)
    peers: dict[int, None] = {}
    notify = _NotifyLoop(transport, peers)
    notify.start()
    logger.info("VK-бот запущен (group %s)", VK_GROUP_ID)
    cycles = 0
    seen_updates = 0
    while True:
        try:
            updates = transport.poll()
        except Exception as exc:  # noqa: BLE001 - long poll терпит сбои сети
            logger.warning("vk poll: %s: %s", type(exc).__name__, exc)
            time.sleep(5)
            continue
        cycles += 1
        seen_updates += len(updates)
        # сердцебиение раз в ~5 мин: пустой err.log перестал быть двусмысленным
        if cycles % 12 == 0:
            logger.warning("vk poll alive: cycles=%d updates=%d",
                           cycles, seen_updates)
        # раз в ~20 мин принудительное переподключение: застоявшаяся
        # сессия long poll молча перестаёт получать события (боевой инцидент)
        if cycles % 48 == 0:
            logger.warning("vk poll: периодический reconnect (stale guard)")
            transport.reconnect()
        for update in updates:
            # каждое нажатие/сообщение видно в err.log — диагностика кнопок
            logger.warning("vk update: %s", update.get("type"))
            try:
                _process_update(transport, update, peers)
            except Exception as exc:  # noqa: BLE001 - один апдейт не роняет бота
                logger.warning("vk update failed: %s: %s",
                               type(exc).__name__, exc)
