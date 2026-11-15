"""Owner-edited peer registry. Credentials are write-only, never public config.

Read-only peer traffic reuses task_sources' fixed GET client and body/deadline
bounds. Registry writers serialize across processes and compare raw revisions.
Old/uncertain credential files remain private; they are never rolled back or
deleted because an atomic registry replacement may already reference them.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from . import config, task_sources as peers


class SettingsError(Exception):
    def __init__(self, status_code, message):
        self.status_code, self.message = status_code, message
        super().__init__(message)


class SourceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_-]*$")
    label: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=2048)
    type: Literal["promptpilot"] = "promptpilot"
    token: SecretStr | None = None
    auth_mode: Literal["preserve", "none", "token"] = "preserve"


class SourceUpdate(SourceDraft):
    revision: str = Field(min_length=1, max_length=64)


def _path(sources_file=None):
    return Path(sources_file or os.environ.get("PP_TASK_SOURCES_FILE")
                or config.DB_DIR / "task-sources.json").absolute()


def _revision(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else "missing"


def _safe_path(path):
    for part in [path, *path.parents]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SettingsError(409, "Путь настроек не должен быть ссылкой")


def _validate_windows_acl(metadata, sid, *, parent=False):
    """Same policy for actual Windows metadata and platform-independent tests."""
    trusted_owners = {sid, "S-1-5-18", "S-1-5-32-544"} if parent else {sid}
    trusted_access = {sid, "S-1-5-18"}
    if parent:
        trusted_access |= {"S-1-5-32-544", "S-1-3-4"}
    rules = metadata.get("access") if isinstance(metadata, dict) else None
    if (not isinstance(rules, list) or len(rules) > 1024 or not isinstance(metadata.get("owner"), str)
            or metadata["owner"] not in trusted_owners
            or not parent and metadata.get("protected") is not True):
        raise SettingsError(409, "Настройте закрытый доступ к хранилищу источников")
    for rule in rules:
        if (not isinstance(rule, dict) or type(rule.get("allow")) is not bool
                or type(rule.get("rights")) is not int or not isinstance(rule.get("sid"), str)):
            raise SettingsError(409, "Не удалось проверить права хранилища источников")
        if rule["allow"] and rule["sid"] not in trusted_access:
            # Concrete filesystem writes plus GENERIC_WRITE/GENERIC_ALL.
            if not parent or rule["rights"] & 0x500D0156:
                raise SettingsError(409, "Настройте закрытый доступ к хранилищу источников")


def _windows_acl(path, *, create=False, directory=False, parent=False):
    from .workorder_private import owner_sid
    sid = owner_sid()
    if create:
        suffix = "(OI)(CI)F" if directory else "F"
        # Grant our explicit owner first; Python's fresh Windows0700 directory
        # may carry explicit Administrators/OWNER_RIGHTS ACEs as well.
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"*{sid}:{suffix}", f"*S-1-5-18:{suffix}", "/Q"],
                       check=True, capture_output=True, timeout=15)
        # Elevated Windows tokens can create files owned by Administrators.
        # Adopt ONLY this freshly created path, never an existing parent/key.
        subprocess.run(["icacls", str(path), "/setowner", "*" + sid, "/Q"],
                       check=True, capture_output=True, timeout=15)
        subprocess.run(["icacls", str(path), "/remove:g", "*S-1-5-32-544", "*S-1-3-4", "/Q"],
                       check=True, capture_output=True, timeout=15)
    # A PS7 parent may leave incompatible modules ahead of WindowsPowerShell5
    # modules. Isolate ONLY this child before invoking any auto-loaded cmdlet.
    command = r"""$ErrorActionPreference='Stop';$env:PSModulePath=[IO.Path]::Combine($PSHOME,'Modules');
$a=Get-Acl -LiteralPath $env:PP_SOURCE_SETTINGS_ACL_PATH;
@{owner=$a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;protected=$a.AreAccessRulesProtected;
access=@($a.Access|ForEach-Object{@{sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;
allow=$_.AccessControlType -eq 'Allow';rights=[long]$_.FileSystemRights}})}|ConvertTo-Json -Depth 4 -Compress"""
    process = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "PP_SOURCE_SETTINGS_ACL_PATH": str(path)}, check=True, capture_output=True, timeout=15)
    try:
        if len(process.stdout) > 256 * 1024:
            raise ValueError()
        metadata = json.loads(process.stdout)
    except (ValueError, TypeError):
        raise SettingsError(503, "Не удалось проверить права хранилища источников") from None
    _validate_windows_acl(metadata, sid, parent=parent)


def _private(path, *, create=False, directory=False, parent=False):
    _safe_path(path)
    info = path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise SettingsError(409, "Недопустимое хранилище настроек")
    if os.name == "nt":
        _windows_acl(path, create=create, directory=directory, parent=parent)
    elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & (0o022 if parent else 0o077):
        raise SettingsError(409, "Настройте закрытый доступ к хранилищу источников")


def _raw(path):
    _safe_path(path)
    try:
        if not path.is_file():
            if path.exists():
                raise SettingsError(409, "Недопустимый файл настроек")
            return None
        return peers._read_bounded(path, peers.MAX_CONFIG)
    except (OSError, ValueError):
        raise SettingsError(503, "Не удалось прочитать настройки источников") from None


def _strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _registry(path):
    raw = _raw(path)
    if raw is None:
        return raw, {"version": 1, "sources": []}, []
    try:
        data = _strict_json(raw)
        if (type(data) is not dict or set(data) - {"version", "sources"}
                or type(data.get("version", 1)) is not int or data.get("version", 1) != 1
                or type(data.get("sources")) is not list or len(data["sources"]) > peers.MAX_SOURCES):
            raise ValueError()
        sources, seen = [], set()
        # Validate the SAME bytes used for revision/CAS, never reopen a second
        # registry generation merely to validate the first generation.
        for entry in data["sources"]:
            if (type(entry) is not dict or set(entry) - {"id", "label", "base_url", "token_env", "token_file"}
                    or not isinstance(entry.get("id"), str) or not peers._SOURCE_ID.fullmatch(entry["id"])
                    or entry["id"] == "local" or entry["id"] in seen):
                raise ValueError()
            seen.add(entry["id"])
            label = entry["label"]
            if (not isinstance(label, str) or not label.strip() or len(label) > 100
                    or any(ord(c) < 32 or ord(c) == 127 for c in label)):
                raise ValueError()
            url = peers._base_url(entry["base_url"])
            env, file = entry.get("token_env"), entry.get("token_file")
            if env is not None and (not isinstance(env, str) or not peers._ENV_NAME.fullmatch(env)):
                raise ValueError()
            if file is not None:
                if not isinstance(file, str) or not file or len(file) > 4096 or env is not None:
                    raise ValueError()
                file = Path(file)
                if not file.is_absolute():
                    file = path.parent / file
            sources.append(peers._Source(entry["id"], label.strip(), url, env, file))
        return raw, data, sources
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SettingsError(409, "Список источников повреждён. Исправьте файл настроек перед сохранением") from None


def _descriptor(source, path):
    has_token = source.token_env is not None or source.token_file is not None
    auth_kind = "env" if source.token_env else "file" if source.token_file else "none"
    if source.token_file and source.token_file.parent == path.parent / (path.name + ".credentials"):
        auth_kind = "managed"
    return {"id": source.id, "label": source.label, "base_url": source.base_url,
            "type": "promptpilot", "has_token": has_token, "auth_kind": auth_kind}


def get_settings(*, sources_file=None):
    path = _path(sources_file)
    try:
        raw, _data, sources = _registry(path)
        _private(path.parent, directory=True, parent=True)
        return {"revision": _revision(raw), "editable": True,
                "sources": [_descriptor(source, path) for source in sources], "errors": []}
    except (SettingsError, OSError, subprocess.SubprocessError) as error:
        message = error.message if isinstance(error, SettingsError) else "Не удалось открыть настройки источников"
        return {"revision": None, "editable": False, "sources": [], "errors": [{"message": message}]}


@contextmanager
def _locked(path):
    _private(path.parent, directory=True, parent=True)
    lock = path.with_name(path.name + ".lock")
    _safe_path(lock)
    descriptor, created = _open_lock(lock)
    with os.fdopen(descriptor, "r+b") as stream:
        _private(lock, create=created)
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SettingsError(409, "Настройки уже сохраняются. Обновите список") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    # Never unlink the lock inode: another process may already hold/open it.


def _open_lock(path):
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600), True
    except FileExistsError:
        return os.open(path, flags), False


def _sync(directory):
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _exclusive(path, raw):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        _private(path, create=True)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _managed_token(path, token):
    directory = path.parent / (path.name + ".credentials")
    _safe_path(directory)
    created = not directory.exists()
    if created:
        directory.mkdir(mode=0o700)
    _private(directory, create=created, directory=True)
    if created:
        _sync(path.parent)  # Persist the key-directory entry before publishing a registry reference.
    key = directory / (secrets.token_hex(24) + ".key")
    _exclusive(key, token.encode("ascii"))
    _sync(directory)
    return str(key)


def _edit(request, existing, *, path, save_token):
    if request.id == "local":
        raise SettingsError(400, "Идентификатор local зарезервирован")
    try:
        url = peers._base_url(request.base_url)
        label = request.label.strip()
        if not label or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ValueError()
        token = request.token.get_secret_value() if request.token is not None else ""
        if token and (len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError()
    except ValueError:
        raise SettingsError(400, "Проверьте адрес, название и токен источника") from None
    existing = existing or {}
    old_auth = {key: existing[key] for key in ["token_env", "token_file"] if existing.get(key) is not None}
    if request.auth_mode == "none" and token:
        raise SettingsError(400, "Выберите токен или доступ без токена")
    if url != existing.get("base_url", "").rstrip("/") and old_auth and not token and request.auth_mode != "none":
        raise SettingsError(400, "Для нового адреса укажите новый токен или выберите доступ без токена")
    result = {"id": request.id, "label": label, "base_url": url}
    if request.auth_mode != "none":
        if token:
            if save_token:
                result["token_file"] = _managed_token(path, token)
        elif request.auth_mode == "token" and not old_auth:
            raise SettingsError(400, "Укажите токен источника")
        else:
            result.update(old_auth)
    return result, token


def _replace_registry(path, old_raw, data):
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    if len(raw) > peers.MAX_CONFIG:
        raise SettingsError(400, "Список источников превышает предел")
    temporary = path.with_name(path.name + "." + secrets.token_hex(16) + ".writing")
    try:
        _exclusive(temporary, raw)
        if _raw(path) != old_raw:
            raise SettingsError(409, "Настройки изменились. Обновите список")
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()  # This invocation's never-referenced registry temp only.


def upsert_source(source_id, request: SourceUpdate, *, sources_file=None):
    if request.id != source_id:
        raise SettingsError(400, "Идентификатор источника не совпадает")
    path = _path(sources_file)
    try:
        with _locked(path):
            raw, data, _sources = _registry(path)
            if request.revision != _revision(raw):
                raise SettingsError(409, "Настройки изменились. Обновите список")
            previous = next((s for s in data["sources"] if s["id"] == source_id), None)
            if previous is None and len(data["sources"]) >= peers.MAX_SOURCES:
                raise SettingsError(400, "Достигнут предел источников")
            updated, _token = _edit(request, previous, path=path, save_token=True)
            data["sources"] = [updated if entry["id"] == source_id else entry for entry in data["sources"]]
            if previous is None:
                data["sources"].append(updated)
            _replace_registry(path, raw, data)
            return get_settings(sources_file=path)
    except (OSError, subprocess.SubprocessError):
        raise SettingsError(503, "Сохранение не подтверждено. Обновите список перед повторной попыткой") from None


def delete_source(source_id, revision, *, sources_file=None):
    path = _path(sources_file)
    try:
        with _locked(path):
            raw, data, _sources = _registry(path)
            if revision != _revision(raw):
                raise SettingsError(409, "Настройки изменились. Обновите список")
            remaining = [source for source in data["sources"] if source["id"] != source_id]
            if len(remaining) == len(data["sources"]):
                raise SettingsError(404, "Источник не найден")
            data["sources"] = remaining
            _replace_registry(path, raw, data)
            return get_settings(sources_file=path)
    except (OSError, subprocess.SubprocessError):
        raise SettingsError(503, "Удаление не подтверждено. Обновите список перед повторной попыткой") from None


def probe_source(*, source_id=None, draft: SourceDraft | None = None, sources_file=None):
    if (source_id is None) == (draft is None):
        raise SettingsError(400, "Выберите сохранённый источник или новые настройки")
    path = _path(sources_file)
    _raw_data, data, sources = _registry(path)
    if draft is not None:
        previous = next((s for s in data["sources"] if s["id"] == draft.id), None)
        edited, token = _edit(draft, previous, path=path, save_token=False)
        source = peers._Source(edited["id"], edited["label"], edited["base_url"], edited.get("token_env"),
            (path.parent / edited["token_file"]) if edited.get("token_file") else None)
    else:
        source = next((s for s in sources if s.id == source_id), None)
        if source is None:
            raise SettingsError(404, "Источник не найден")
        token = ""
    try:
        # A supplied draft secret is memory-only, never persisted during probe.
        client = peers._Client(source)
        if token:
            client.token = token
        try:
            page = client.get("/api/task-feed", {"local_only": "true", "limit": 1, "offset": 0})
        except peers._NotFound:
            page = client.get("/api/workflows", {"limit": 1, "offset": 0})
            if not isinstance(page, list) or any(not isinstance(v, dict) or not peers._identity("workflow", v.get("id")) for v in page):
                raise peers._Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
            return {"status": "partial", "message": peers.LEGACY_MESSAGE, "api": "workflows",
                    "capabilities": {"tasks": False, "workflows": True}}
        if (not isinstance(page, dict) or type(page.get("total")) is not int
                or not isinstance(page.get("items"), list) or len(page["items"]) > 1
                or page["total"] < len(page["items"]) or not page["items"] and page["total"] > 0):
            raise peers._Unavailable("invalid_response", "Источник вернул неподдерживаемые данные")
        for item in page["items"]:
            peers._item(item, source)
        return {"status": "online", "message": "Полный список задач доступен", "api": "task-feed",
                "capabilities": {"tasks": True, "workflows": True}}
    except peers._Unavailable as error:
        return {"status": "unavailable", "message": error.message, "api": None,
                "capabilities": {"tasks": False, "workflows": False}}
