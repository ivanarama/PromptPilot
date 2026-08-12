"""Сколько сожжено — по всем сессиям Claude Code на машине, а не только по нашим.

Дашборд стоимости считал деньги, выдирая регуляркой строку `Cost: $X` из текста
результата задачи. herdr-задачи такой строки не дают вообще — интерактивная
сессия не отдаёт stream-json, — поэтому чем больше работы уходит в herdr, тем
слепее дашборд. Второй источник закрывает дыру: Claude Code пишет транскрипт
каждой сессии в ``~/.claude/projects/<путь>/<session>.jsonl``, в том числе для
панелей herdr.

Считать по этим файлам можно именно потому, что там каждое сообщение записано
целиком и с полным usage. (В потоковом журнале headless-прогона наоборот: одно
сообщение разбито на куски с частичным usage, и суммирование занижает выход в
сотню раз — поэтому для headless-задач мы по-прежнему берём готовый итог из
события `result`.) Цены итога в транскрипте нет — считаем по прайсу.

Лимит у Claude общий на человека, поэтому живая переписка ест то же окно, что и
задачи pp: чтобы ответить «сколько осталось», надо смотреть на оба источника.
Задачи узнаются по session_id из базы, а herdr-задачи — по рабочему каталогу:
свой worktree у задачи уникален, и это единственная зацепка, пока herdr-сессии
не отдают session_id.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# $ за миллион токенов, из справочника Anthropic (сверено 2026-08-12).
# Ключ — префикс id модели; ищется самое длинное совпадение, поэтому датированные
# варианты (claude-haiku-4-5-20251001) попадают в свою строку сами.
PRICES = {
    "claude-fable-5":   (10.0, 50.0),
    "claude-mythos-5":  (10.0, 50.0),
    "claude-opus-5":    (5.0, 25.0),
    "claude-opus-4":    (5.0, 25.0),
    "claude-opus":      (5.0, 25.0),
    "claude-sonnet-5":  (3.0, 15.0),
    "claude-sonnet-4":  (3.0, 15.0),
    "claude-sonnet":    (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku":     (1.0, 5.0),
}
DEFAULT_PRICE = (5.0, 25.0)  # неизвестная модель — считаем по Opus, чтобы не занижать

# Чтение кэша ~0.1x от входа, запись ~1.25x (5-минутный TTL — то, что использует
# Claude Code). Без этих множителей счёт мимо на порядок: кэш-чтений на порядок
# больше, чем обычного входа.
CACHE_READ_RATE = 0.1
CACHE_WRITE_RATE = 1.25

WINDOW_HOURS = 5  # окно лимита Claude
SESSIONS_DIR = Path.home() / ".claude" / "projects"


def price_for(model: str):
    """($/1M вход, $/1M выход) для модели — по самому длинному совпавшему префиксу."""
    model = (model or "").strip()
    best = ""
    for prefix in PRICES:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return PRICES[best] if best else DEFAULT_PRICE


def cost_of(model: str, usage: dict) -> float:
    """Во что обошлось одно сообщение, по прайсу и множителям кэша."""
    inp, out = price_for(model)
    return (
        (usage.get("input_tokens") or 0) * inp
        + (usage.get("output_tokens") or 0) * out
        + (usage.get("cache_creation_input_tokens") or 0) * inp * CACHE_WRITE_RATE
        + (usage.get("cache_read_input_tokens") or 0) * inp * CACHE_READ_RATE
    ) / 1_000_000


def _parse_ts(value) -> float:
    """ISO-8601 с Z → unix seconds; 0, если разобрать не вышло."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _scan_file(path: Path, since: float) -> dict:
    """Свод по одной сессии: только сообщения новее `since`.

    Файл читается построчно и почти целиком пропускается по подстроке: транскрипт
    активной сессии — это десятки мегабайт, а json.loads на каждой строке
    превращает опрос дашборда в секунды.
    """
    total = {"cost": 0.0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
             "messages": 0, "first": 0.0, "last": 0.0, "model": "", "cwd": "", "session": ""}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"assistant"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") != "assistant":
                    continue
                msg = ev.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                ts = _parse_ts(ev.get("timestamp"))
                if ts < since:
                    continue
                model = msg.get("model") or ""
                if model.startswith("<"):  # <synthetic> — не настоящий вызов модели
                    continue
                total["cost"] += cost_of(model, usage)
                total["input"] += usage.get("input_tokens") or 0
                total["output"] += usage.get("output_tokens") or 0
                total["cache_read"] += usage.get("cache_read_input_tokens") or 0
                total["cache_write"] += usage.get("cache_creation_input_tokens") or 0
                total["messages"] += 1
                total["first"] = min(total["first"] or ts, ts)
                total["last"] = max(total["last"], ts)
                total["model"] = model or total["model"]
                total["cwd"] = ev.get("cwd") or total["cwd"]
                total["session"] = ev.get("sessionId") or total["session"]
    except OSError:
        return {}
    return total if total["messages"] else {}


def scan(hours: float = WINDOW_HOURS, sessions_dir: Path = None) -> list:
    """Расход по сессиям за последние `hours` часов, дороже — первыми.

    Файлы, которых не касались в окне, отсеиваются по mtime: сессий на машине
    сотни, а в окно попадают единицы.
    """
    root = Path(sessions_dir or SESSIONS_DIR)
    since = time.time() - hours * 3600
    out = []
    try:
        files = list(root.glob("*/*.jsonl"))
    except OSError:
        return out
    for path in files:
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        rec = _scan_file(path, since)
        if rec:
            rec["path"] = str(path)
            out.append(rec)
    out.sort(key=lambda r: r["cost"], reverse=True)
    return out


def _pp_sessions() -> tuple:
    """(session_id задач pp, чекауты задач pp) — по ним сессия узнаётся как наша.

    Каталоги берём ТОЛЬКО из worktree_path. Обычный working_dir задача делит с
    человеком — это ровно тот каталог, в котором он сам сидит, — и по нему живая
    переписка засчиталась бы задаче. Чекаут же принадлежит одной задаче целиком.
    """
    from . import db
    ids, dirs = set(), set()
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, worktree_path FROM tasks "
                "WHERE started_at IS NOT NULL ORDER BY id DESC LIMIT 500"
            ).fetchall()
    except Exception:
        return ids, dirs
    for row in rows:
        if row["session_id"]:
            ids.add(row["session_id"])
        if row["worktree_path"]:
            dirs.add(os.path.normpath(row["worktree_path"]))
    return ids, dirs


def summary(hours: float = WINDOW_HOURS) -> dict:
    """Свод для дашборда: сколько за окно, чьё это и что дороже всего.

    Окно — скользящее: API не отдаёт «сколько осталось», а точку сброса называет
    только событием лимита, которого в транскриптах нет. Поэтому здесь честные
    «последние 5 часов», а не доля выбранной нормы.

    Деньги — оценка по прайсу API, а не выставленный счёт: на подписке они не
    списываются вовсе. Полезны как мера того, куда уходит окно лимита.
    """
    sessions = scan(hours)
    ids, dirs = _pp_sessions()

    def is_pp(rec):
        return rec.get("session") in ids or os.path.normpath(rec.get("cwd") or "") in dirs

    pp = [r for r in sessions if is_pp(r)]
    other = [r for r in sessions if not is_pp(r)]
    total = sum(r["cost"] for r in sessions)
    return {
        "window_hours": hours,
        "cost": round(total, 4),
        "cost_tasks": round(sum(r["cost"] for r in pp), 4),
        "cost_other": round(sum(r["cost"] for r in other), 4),
        "tokens": sum(r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
                      for r in sessions),
        "sessions": len(sessions),
        "top": [
            {"cost": round(r["cost"], 4), "model": r["model"], "messages": r["messages"],
             "dir": r["cwd"], "task": is_pp(r),
             "last": datetime.fromtimestamp(r["last"], timezone.utc).isoformat()}
            for r in sessions[:10]
        ],
    }
