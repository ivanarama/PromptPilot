"""Инструменты доработки внешних обработок 1С (.epf/.erf).

Портировано из отдельного бота «БотДоработкиОбработок»: декомпиляция через
1C Designer, распаковка обычных форм (Form.bin через v8unpack), подготовка
проекта (git + .v8-project.json + cc-1c-skills), сборка обратно в .epf.

Модуль автономен и не влияет на остальной PromptPilot: фича включается
только когда найден 1cv8.exe (см. is_available()).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from .config import DB_DIR, DEFAULT_CLI

logger = logging.getLogger(__name__)


class EpfError(Exception):
    """Ожидаемая ошибка работы с 1С (не найдена платформа, сбой сборки...)."""


# Одновременные декомпиляции/сборки в 1C Designer не допустимы: Designer
# блокирует информационную базу. Все тяжёлые операции сериализуем.
_designer_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════
#  Конфигурация 1С: PP_1C_* env → ~/.promptpilot/tg_config.json ("1c")
# ═══════════════════════════════════════════════════════════════════════

_PRESET_ENV_KEYS = {
    "ut11": "PP_1C_UT_BASE",
    "bp3": "PP_1C_BP_BASE",
    "erp": "PP_1C_ERP_BASE",
    "unf": "PP_1C_UNF_BASE",
    "aa": "PP_1C_AA_BASE",
}

# Публично: подписи пресетов для UI бота и веб-интерфейса
PRESET_LABELS = {
    "ut11": "💼 УТ 11",
    "bp3": "📒 БП 3.0",
    "erp": "🏭 ERP",
    "unf": "🏪 УНФ",
    "aa": "🚗 Альфа-Авто",
}
_PRESET_LABELS = PRESET_LABELS  # внутренние использования

_KNOWN_1C_KEYS = {
    "exe_path", "bases_root", "base_path",
    "ut11", "bp3", "erp", "unf", "aa",
    "skills_repo", "skills_dir",
}


def _tg_config_file() -> Path:
    return DB_DIR / "tg_config.json"


def _file_1c_section() -> dict:
    cfg_file = _tg_config_file()
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text("utf-8"))
            section = data.get("1c")
            if isinstance(section, dict):
                return section
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _cfg_value(env_key: str, file_key: str) -> str:
    env_val = os.environ.get(env_key, "")
    if env_val:
        return env_val
    return str(_file_1c_section().get(file_key, "") or "")


def load_1c_config() -> dict:
    """Настройки 1С: пути к платформе, базам-пресетам и cc-1c-skills.

    Приоритет: переменные окружения PP_1C_* → секция "1c" в
    ~/.promptpilot/tg_config.json. Читается при каждом вызове — правки
    конфига действуют без перезапуска.
    """
    presets = {}
    for key, env_name in _PRESET_ENV_KEYS.items():
        presets[key] = _cfg_value(env_name, key)
    return {
        "exe_path": _cfg_value("PP_1C_EXE_PATH", "exe_path"),
        "bases_root": _cfg_value("PP_1C_BASES_ROOT", "bases_root"),
        "base_path": _cfg_value("PP_1C_BASE_PATH", "base_path"),
        "presets": presets,
        "skills_repo": _cfg_value(
            "PP_1C_SKILLS_REPO", "skills_repo"
        ) or "https://github.com/ivanarama/cc-1c-skills.git",
        "skills_dir": _cfg_value("PP_1C_SKILLS_DIR", "skills_dir")
        or str(Path.home() / ".claude" / "skills" / "cc-1c-skills"),
    }


def env_overridden_keys() -> list[str]:
    """Ключи, заданные через PP_1C_* env — файл настроек их не перекроет."""
    overridden = [key for key, env_name in _PRESET_ENV_KEYS.items()
                  if os.environ.get(env_name)]
    for env_name in ("PP_1C_EXE_PATH", "PP_1C_BASES_ROOT", "PP_1C_BASE_PATH",
                     "PP_1C_SKILLS_REPO", "PP_1C_SKILLS_DIR"):
        if os.environ.get(env_name):
            overridden.append(env_name[len("PP_1C_"):].lower())
    return overridden


def save_1c_config(section: dict) -> None:
    """Сохранить секцию "1c" в tg_config.json (атомарно, другие ключи файла
    не трогаем). Действует сразу — load_1c_config читает файл при каждом вызове.
    """
    from .config import _atomic_write_json

    cfg_file = _tg_config_file()
    data: dict = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text("utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}

    clean = {
        k: str(v).strip()
        for k, v in (section or {}).items()
        if k in _KNOWN_1C_KEYS and str(v or "").strip()
    }
    data["1c"] = {**data.get("1c", {}), **clean}
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(cfg_file, data)


def find_v8_exe() -> Optional[Path]:
    """Найти 1cv8.exe: конфиг → реестр Windows → Program Files."""
    cfg = load_1c_config()
    if cfg["exe_path"]:
        p = Path(cfg["exe_path"])
        if p.exists():
            return p

    try:
        import winreg
    except ImportError:
        return None  # не Windows — фича недоступна

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\1C\1Cv8\InstallPath")
            val, _ = winreg.QueryValueEx(key, "")
            winreg.CloseKey(key)
            exe = Path(val) / "bin" / "1cv8.exe"
            if exe.exists():
                return exe
        except FileNotFoundError:
            continue

    for pf in (Path(r"C:\Program Files\1cv8"), Path(r"C:\Program Files (x86)\1cv8")):
        if pf.exists():
            versions = sorted(
                (d for d in pf.iterdir() if d.is_dir()),
                key=lambda d: d.name,
                reverse=True,
            )
            for v in versions:
                exe = v / "bin" / "1cv8.exe"
                if exe.exists():
                    return exe
    return None


def is_available() -> bool:
    """Фича-флаг: 1С-доработка показывается в боте только если есть платформа."""
    try:
        return find_v8_exe() is not None
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
#  Базы 1С: сканирование и разрешение ключей
# ═══════════════════════════════════════════════════════════════════════

def categorize_base_name(name: str) -> str:
    name_l = name.lower()
    if any(k in name_l for k in ("ут11", "ut11", "ут_11", "управление торговлей")) or (
        name_l.startswith(("ут", "ut")) and "11" in name_l
    ):
        return "УТ 11"
    if any(k in name_l for k in ("ут10", "ут_10", "упп")):
        return "УТ 10"
    if any(k in name_l for k in ("бп", "bp", "бух", "бухгалтерия")):
        return "БП 3.0"
    if any(k in name_l for k in ("erp", "ерп")):
        return "ERP"
    if any(k in name_l for k in ("унф", "unf")):
        return "УНФ"
    if any(k in name_l for k in ("аа", "альфа", "авто")):
        return "Альфа-Авто"
    if any(k in name_l for k in ("розница", "retail")):
        return "Розница"
    return "Другое"


def scan_available_bases() -> list[dict[str, str]]:
    """Базы с 1Cv8.1CD в PP_1C_BASES_ROOT (на один уровень вглубь)."""
    root = Path(load_1c_config()["bases_root"] or "")
    if not str(root) or not root.exists():
        return []

    result: list[dict[str, str]] = []
    try:
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            if (d / "1Cv8.1CD").exists():
                result.append({
                    "name": d.name, "path": str(d),
                    "category": categorize_base_name(d.name),
                })
            else:
                for sub in d.iterdir():
                    if sub.is_dir() and (sub / "1Cv8.1CD").exists():
                        result.append({
                            "name": f"{d.name}/{sub.name}", "path": str(sub),
                            "category": categorize_base_name(sub.name),
                        })
    except OSError as e:
        logger.warning("сканирование баз 1С: %s", e)
    return result


def resolve_base(base_key_or_path: str | None) -> tuple[Optional[str], str]:
    """Ключ (stub/ut11/...) или путь → (путь_или_None, подпись)."""
    if not base_key_or_path or base_key_or_path == "stub":
        return None, "🧩 Авто (Stub-DB)"

    cfg = load_1c_config()
    if base_key_or_path in _PRESET_LABELS:
        p = cfg["presets"].get(base_key_or_path, "")
        if p and Path(p).exists():
            return p, f"{_PRESET_LABELS[base_key_or_path]} ({Path(p).name})"

    candidate = Path(base_key_or_path)
    if candidate.exists() and (candidate / "1Cv8.1CD").exists():
        return str(candidate), f"📁 {candidate.name}"

    for b in scan_available_bases():
        if b["name"].lower() == base_key_or_path.lower():
            return b["path"], f"📁 {b['name']}"

    return None, f"⚠️ Не найдена: {base_key_or_path}"


def get_default_decompile_base() -> Optional[str]:
    """База для декомпиляции по умолчанию: PP_1C_BASE_PATH → пресеты → скан."""
    cfg = load_1c_config()
    if cfg["base_path"] and Path(cfg["base_path"]).exists():
        return cfg["base_path"]
    for key in ("bp3", "ut11", "erp"):
        p = cfg["presets"].get(key)
        if p and Path(p).exists():
            return p
    for b in scan_available_bases():
        if any(w in b["name"].lower() for w in ("сборк", "пуст")):
            return b["path"]
    return None


# ═══════════════════════════════════════════════════════════════════════
#  Имена проектов
# ═══════════════════════════════════════════════════════════════════════

def sanitize_project_name(filename: str) -> str:
    """Имя файла → безопасное имя каталога проекта."""
    name = Path(filename).stem
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    return name or "Обработка"


def safe_project_name(name: str) -> Optional[str]:
    """None, если имя нельзя использовать как каталог (path traversal)."""
    if not name or name != Path(name).name:
        return None
    if "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        return None
    return name


def unique_project_dir(projects_root: Path, name: str) -> Path:
    """Свободный каталог: <name>, <name>_2, <name>_3..."""
    project_dir = projects_root / name
    i = 2
    while project_dir.exists():
        project_dir = projects_root / f"{name}_{i}"
        i += 1
    return project_dir


# ═══════════════════════════════════════════════════════════════════════
#  Git и окружение проекта
# ═══════════════════════════════════════════════════════════════════════

def git_commit_all(project_dir: Path, message: str) -> bool:
    try:
        subprocess.run(
            ["git", "add", "-A"], cwd=str(project_dir),
            capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=str(project_dir), capture_output=True, timeout=30,
        )
        return True
    except Exception as e:
        logger.warning("git commit в %s: %s", project_dir, e)
        return False


def init_git(project_dir: Path, message: str = "Начальный импорт обработки") -> bool:
    try:
        subprocess.run(
            ["git", "init"], cwd=str(project_dir),
            check=True, capture_output=True, timeout=30,
        )
        (project_dir / ".gitignore").write_text(
            "**/Form.bin\n"
            "build/\n"
            "original/\n"
            ".claude/\n"  # junction на cc-1c-skills — не тащить весь репозиторий в проект
            "*.log\n"
            "_dump.log\n"
            "temp_*/\n"
            "__pycache__/\n"
            ".DS_Store\n"
            ".venv/\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=str(project_dir),
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", message], cwd=str(project_dir),
            check=True, capture_output=True, timeout=30,
        )
        return True
    except Exception as e:
        logger.error("git init в %s: %s", project_dir, e)
        return False


def setup_v8_project(project_dir: Path, base_path: str | None = None) -> None:
    """Создать .v8-project.json для cc-1c-skills."""
    v8_exe = find_v8_exe()
    bp = base_path or load_1c_config()["base_path"]

    project_config: dict = {}
    if v8_exe:
        project_config["v8path"] = str(v8_exe.parent.parent)
    if bp and Path(bp).exists():
        project_config["databases"] = {
            "dev": {"type": "file", "path": bp, "user": "", "password": ""}
        }
    (project_dir / ".v8-project.json").write_text(
        json.dumps(project_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def link_skills(project_dir: Path) -> bool:
    """Подключить cc-1c-skills к проекту (junction → fallback копирование)."""
    skills_source = Path(load_1c_config()["skills_dir"])
    if not skills_source.exists():
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1",
                 load_1c_config()["skills_repo"], str(skills_source)],
                check=True, capture_output=True, timeout=120,
            )
        except Exception as e:
            logger.error("клонирование cc-1c-skills: %s", e)
            return False

    skills_target = project_dir / ".claude" / "skills" / "cc-1c-skills"
    if skills_target.exists():
        return True

    skills_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Windows: junction не требует прав администратора
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(skills_target), str(skills_source)],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception as e:
        logger.warning("junction cc-1c-skills: %s — копирую", e)
        try:
            shutil.copytree(skills_source, skills_target)
            return True
        except Exception as e2:
            logger.error("копирование cc-1c-skills: %s", e2)
            return False


_CLAUDE_MD_TEMPLATE = """# Инструкции для AI-ассистента при работе с этим проектом обработки 1С

## Контекст
Это проект внешней обработки (или отчёта) 1С:Предприятие 8.3, разложенный в XML-исходники.

## Навыки (cc-1c-skills)
В проекте подключены навыки cc-1c-skills. Используй их для работы с 1С:
- `/form-info` — посмотреть структуру формы
- `/form-edit` — редактировать элементы формы
- `/form-compile` — создать Form.xml из JSON DSL
- `/epf-build` — собрать обработку в .epf
- `/epf-validate` — проверить структуру обработки
- `/meta-info` — информация об объекте метаданных

## Структура проекта
```
*.xml                    — корневой XML обработки (метаданные)
<ИмяОбработки>/
├── Ext/
│   └── ObjectModule.bsl — модуль объекта
└── Forms/
    └── <ИмяФормы>/
        ├── <ИмяФормы>.xml — метаданные формы
        └── Ext/
            ├── Form.xml   — структура управляемой формы (или Form.bin для обычной)
            └── Form/
                ├── Form.xml   — распакованная структура обычной формы
                └── Module.bsl — модуль формы
```

## Правила
1. Код на языке 1С (BSL) пиши в файлах .bsl
2. НЕ редактируй Form.xml управляемых форм вручную — используй `/form-edit` или `/form-compile`
3. Для обычных форм (Form.bin) редактируй Module.bsl в каталоге Ext/Form/
4. Коммить изменения в Git после каждого логического шага
5. Конфигурация проекта в .v8-project.json
"""


# ═══════════════════════════════════════════════════════════════════════
#  Декомпиляция и сборка (1C Designer)
# ═══════════════════════════════════════════════════════════════════════

def decompile_epf(
    epf_path: Path,
    output_dir: Path,
    base_path: str | None = None,
) -> tuple[bool, str]:
    """Декомпилировать .epf/.erf в иерархию XML через 1C Designer.

    Вызывается под designer_lock. Returns: (успех, лог_ошибки).
    """
    v8 = find_v8_exe()
    if not v8:
        return False, "Не найден 1cv8.exe (PP_1C_EXE_PATH)."

    bp = base_path or get_default_decompile_base()
    if not bp:
        return False, (
            "Не найдена база 1С для декомпиляции: задайте PP_1C_BASE_PATH "
            "или базу-пресет."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "_dump.log"

    cmd = [
        str(v8), "DESIGNER",
        "/F", bp,
        "/N", "",
        "/DisableStartupDialogs",
        "/DumpExternalDataProcessorOrReportToFiles",
        str(output_dir),
        str(epf_path),
        "-Format", "Hierarchical",
        "/Out", str(log_file),
    ]

    logger.info("декомпиляция 1С: %s", " ".join(cmd[:6]))
    try:
        with _designer_lock:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "Таймаут декомпиляции (300 сек.)"

    if list(output_dir.glob("*.xml")):
        return True, ""

    err_text = "Декомпиляция завершилась без XML-файлов."
    if log_file.exists():
        err_text = log_file.read_text("utf-8", errors="replace")[-1500:]
    return False, err_text


def build_epf(
    project_dir: Path,
    base_path: str | None = None,
    use_stub: bool = True,
) -> tuple[Optional[Path], str]:
    """Собрать .epf/.erf из XML-исходников.

    1) cc-1c-skills/epf-build.ps1 (генерирует stub-db на лету);
    2) прямой вызов 1cv8 Designer с указанной (или дефолтной) базой.
    """
    v8 = find_v8_exe()
    if not v8:
        return None, "Не найден 1cv8.exe (PP_1C_EXE_PATH)."

    xml_candidates = [
        f for f in project_dir.glob("*.xml")
        if not f.name.startswith(("_", "."))
    ]
    if not xml_candidates:
        return None, f"Не найден корневой XML в {project_dir}"

    root_xml = xml_candidates[0]
    name = root_xml.stem

    ext = ".epf"
    try:
        preview = root_xml.read_text("utf-8", errors="replace")[:1000]
        if "<ExternalReport" in preview:
            ext = ".erf"
    except OSError:
        pass

    build_dir = project_dir / "build"
    build_dir.mkdir(exist_ok=True)
    output_file = build_dir / f"{name}{ext}"

    skills_build_script = (
        Path(load_1c_config()["skills_dir"])
        / ".claude" / "skills" / "epf-build" / "scripts" / "epf-build.ps1"
    )

    err_msg = ""
    if skills_build_script.exists():
        ps_cmd = [
            "powershell.exe", "-NoProfile",
            "-File", str(skills_build_script),
            "-SourceFile", str(root_xml),
            "-OutputFile", str(output_file),
            "-Checks", "off",
        ]
        if base_path and base_path != "stub":
            ps_cmd.extend(["-InfoBasePath", base_path])

        logger.info("сборка 1С через cc-1c-skills: %s", root_xml.name)
        try:
            with _designer_lock:
                res = subprocess.run(
                    ps_cmd, capture_output=True, text=True,
                    timeout=300, encoding="utf-8", errors="replace",
                )
            if output_file.exists() and output_file.stat().st_size > 0:
                logger.info("сборка 1С OK: %s", output_file.name)
                return output_file, ""
            err_msg = (res.stdout or "") + "\n" + (res.stderr or "")
            logger.warning("cc-1c-skills сборка не удалась:\n%s", err_msg[-1000:])
        except subprocess.TimeoutExpired:
            err_msg = "таймаут epf-build.ps1 (300 сек.)"
        except Exception as e:
            err_msg = str(e)

        if (not base_path or base_path == "stub") and not output_file.exists():
            return None, f"Stub-DB сборка не удалась:\n{err_msg[-1000:]}"

    bp = base_path if (base_path and base_path != "stub") else get_default_decompile_base()
    if not bp:
        return None, "Не указана база для сборки и Stub-DB не сработал."

    log_file = build_dir / "build.log"
    cmd = [
        str(v8), "DESIGNER",
        "/F", bp,
        "/N", "",
        "/DisableStartupDialogs",
        "/LoadExternalDataProcessorOrReportFromFiles",
        str(root_xml),
        str(output_file),
        "/Out", str(log_file),
    ]

    try:
        with _designer_lock:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "Таймаут сборки в 1C:Designer (300 сек.)"

    if output_file.exists() and output_file.stat().st_size > 0:
        return output_file, ""

    err_text = "Файл обработки не сформирован."
    if log_file.exists():
        err_text = log_file.read_text("utf-8", errors="replace")[-1500:]
    return None, err_text


# ═══════════════════════════════════════════════════════════════════════
#  Обычные формы: Form.bin ↔ Form.xml + Module.bsl (v8unpack)
# ═══════════════════════════════════════════════════════════════════════

_EXCLUDE_DIRS = {".git", "node_modules", "build", "__pycache__", "original", ".claude"}


def has_ordinary_forms(project_dir: Path) -> bool:
    for form_bin in project_dir.rglob("Form.bin"):
        if not any(part in {".git", "build", "original"} for part in form_bin.parts):
            return True
    return False


def _ascii_tmp(tag: str) -> Path:
    tmp = Path(os.environ.get("TEMP", r"C:\temp")) / f"pp_v8u_{tag}"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def _find_extracted_stage(tmp_path: Path) -> Optional[Path]:
    for candidate in (tmp_path / "decode_stage_0" / "0", tmp_path / "0"):
        if candidate.exists() and (candidate / "form").exists():
            return candidate
    for f in tmp_path.rglob("form"):
        if f.is_file():
            return f.parent
    return None


def unpack_ordinary_forms(project_dir: Path) -> list[str]:
    """Form.bin → Ext/Form/Form.xml + Module.bsl. Список обработанных путей."""
    import tempfile

    unpacked: list[str] = []
    for form_bin in project_dir.rglob("Form.bin"):
        if any(part in _EXCLUDE_DIRS for part in form_bin.parts):
            continue

        form_output_dir = form_bin.parent / "Form"
        form_output_dir.mkdir(exist_ok=True)

        with tempfile.TemporaryDirectory(dir=str(_ascii_tmp("unpack"))) as tmp:
            tmp_path = Path(tmp)
            try:
                try:
                    from v8unpack.container_reader import extract
                    extract(str(form_bin), str(tmp_path))
                except ImportError:
                    subprocess.run(
                        ["v8unpack", "-E", str(form_bin), str(tmp_path),
                         "--temp", str(tmp_path)],
                        check=True, capture_output=True, timeout=60,
                    )

                stage_dir = _find_extracted_stage(tmp_path)
                if not stage_dir:
                    continue

                form_file = stage_dir / "form"
                if form_file.exists():
                    shutil.copy2(form_file, form_output_dir / "Form.xml")

                module_file = stage_dir / "module"
                if module_file.exists():
                    text = module_file.read_bytes().decode("utf-8-sig", errors="replace").strip()
                    if text:
                        (form_output_dir / "Module.bsl").write_text(text, encoding="utf-8-sig")

                unpacked.append(str(form_bin.relative_to(project_dir)))
            except Exception as e:
                logger.error("распаковка %s: %s", form_bin, e)
    return unpacked


def pack_ordinary_forms(project_dir: Path) -> list[str]:
    """Ext/Form/Form.xml (+Module.bsl) → Form.bin. Список упакованных путей."""
    import tempfile

    packed: list[str] = []
    for form_xml in project_dir.rglob("Form.xml"):
        if form_xml.parent.name != "Form":
            continue
        if any(part in _EXCLUDE_DIRS for part in form_xml.parts):
            continue

        ext_dir = form_xml.parent.parent
        bin_path = ext_dir / "Form.bin"
        form_dir = form_xml.parent

        with tempfile.TemporaryDirectory(dir=str(_ascii_tmp("pack"))) as tmp:
            tmp_path = Path(tmp)
            container_dir = tmp_path / "0"
            container_dir.mkdir()
            shutil.copy2(form_xml, container_dir / "form")
            module_bsl = form_dir / "Module.bsl"
            if module_bsl.exists():
                shutil.copy2(module_bsl, container_dir / "module")
            else:
                (container_dir / "module").write_text("", encoding="utf-8-sig")

            try:
                try:
                    from v8unpack.container_writer import build as container_build
                    container_build(str(tmp_path), str(bin_path), nested=True)
                except ImportError:
                    subprocess.run(
                        ["v8unpack", "-B", str(tmp_path), str(bin_path), "--nested"],
                        check=True, capture_output=True, timeout=60,
                    )
                packed.append(str(bin_path.relative_to(project_dir)))
            except Exception as e:
                logger.error("упаковка %s: %s", bin_path, e)
    return packed


# ═══════════════════════════════════════════════════════════════════════
#  Подготовка проекта и интерактивный запуск
# ═══════════════════════════════════════════════════════════════════════

def prepare_project(
    epf_local_path: Path,
    original_filename: str,
    base_path: str | None,
    projects_root: Path,
    progress=None,
) -> tuple[Path, list[str]]:
    """Полная пре-фаза: каталог → декомпиляция → формы → git → окружение.

    progress(step_text) вызывается в начале и конце каждого шага — бот
    показывает их живым статусом. Returns: (project_dir, warnings).
    """
    original_filename = Path(original_filename).name
    name = sanitize_project_name(original_filename)
    project_dir = unique_project_dir(projects_root, name)
    project_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    def report(text: str) -> None:
        if progress:
            progress(text)

    # Оригинал
    report(f"📂 Каталог проекта: {project_dir.name}")
    original_dir = project_dir / "original"
    original_dir.mkdir(exist_ok=True)
    shutil.copy2(epf_local_path, original_dir / original_filename)
    report(f"💾 Сохранён оригинал: {original_filename}")

    # Декомпиляция
    report("🔓 Декомпиляция (1C Designer)...")
    ok, err = decompile_epf(
        original_dir / original_filename, project_dir, base_path
    )
    if not ok:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise EpfError(err)
    report("✅ Декомпиляция завершена")

    # Обычные формы
    if has_ordinary_forms(project_dir):
        report("📦 Распаковка обычных форм (Form.bin)...")
        try:
            unpacked = unpack_ordinary_forms(project_dir)
            if unpacked:
                warnings.append(f"распаковано обычных форм: {len(unpacked)}")
                report(f"✅ Распаковано форм: {len(unpacked)}")
        except Exception as e:
            warnings.append(f"распаковка форм: {e}")

    # Git
    report("🔧 Инициализация Git...")
    if not init_git(project_dir):
        warnings.append("git: не удалось сделать начальный коммит")

    # Окружение
    report("⚙️ Настройка .v8-project.json и cc-1c-skills...")
    setup_v8_project(project_dir, base_path)
    if not link_skills(project_dir):
        warnings.append("не удалось подключить cc-1c-skills")
    (project_dir / "CLAUDE.md").write_text(_CLAUDE_MD_TEMPLATE, encoding="utf-8")

    report("✅ Проект готов")
    return project_dir, warnings


def write_interactive_runner(project_dir: Path, cli: str | None = None) -> None:
    """Runner для интерактивного запуска AI-CLI в каталоге проекта.

    Задача читается из task.txt. Используется кнопкой «открыть в терминале»
    и fallback-запуском, когда агент недоступен напрямую.
    """
    cli = cli or DEFAULT_CLI

    runner_file = project_dir / "interactive_runner.py"
    runner_code = (
        '"""Интерактивный запуск AI-ассистента."""\n'
        'import shutil\n'
        'import subprocess\n'
        'import sys\n'
        'from pathlib import Path\n\n'
        'root = Path(__file__).resolve().parent\n'
        'task_file = root / "task.txt"\n'
        'task = task_file.read_text(encoding="utf-8").strip() if task_file.exists() else ""\n\n'
        f'cli_name = {cli!r}\n'
        'cli_exe = shutil.which(cli_name) or cli_name\n'
        'cmd = [cli_exe]\n'
        'if task:\n'
        '    cmd.extend(["-p", task, "--dangerously-skip-permissions"])\n\n'
        'print("Запуск:", cli_exe)\n'
        'sys.exit(subprocess.run(cmd).returncode)\n'
    )
    runner_file.write_text(runner_code, encoding="utf-8")

    bat_file = project_dir / "start_interactive.bat"
    bat_content = (
        "@echo off\n"
        "chcp 65001 >nul\n"
        f"title PromptPilot 1C - {project_dir.name}\n"
        f"cd /d \"{project_dir}\"\n"
        "echo ===================================================\n"
        "echo Задача для доработки:\n"
        "type task.txt\n"
        "echo.\n"
        "echo ===================================================\n"
        "py -3.11 -X utf8 interactive_runner.py || python -X utf8 interactive_runner.py\n"
        "echo.\n"
        "echo ===================================================\n"
        "echo 💡 Сессия завершена. Сборка: /build или кнопка в PromptPilot.\n"
        "cmd /k\n"
    )
    bat_file.write_text(bat_content, encoding="utf-8")


def open_interactive(project_dir: Path, task: str) -> tuple[bool, str]:
    """Открыть интерактивную сессию AI в новом окне терминала (Windows)."""
    try:
        (project_dir / "task.txt").write_text(task, encoding="utf-8")
        write_interactive_runner(project_dir)
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", str(project_dir / "start_interactive.bat")],
            cwd=str(project_dir), shell=True,
        )
        return True, "Открыто окно терминала"
    except Exception as e:
        return False, str(e)
