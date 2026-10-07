"""Токены конвейера: агрегация из всех источников, которые реально есть.

Провайдеры конвейера — CLI, и юзаж остаётся в разных местах:
  * codex (и любые stream-json CLI) — воркер кладёт «Tokens: X in / Y out»
    в Meta-блок результата задачи, источник = БД задач;
  * qwen — CLI ведёт собственный журнал
    ~/.qwen/usage/token-usage-YYYY-MM.jsonl (построчные записи с localDate);
  * goose / mmx — одноразовые запуски без сессий и журналов: честные нули,
    источник помечается как недоступный (без выдумывания оценок).

Доллары считаются только там, где известна цена: pricing.json в DATA_DIR
вида {"glm-5.3": {"in": 0.6, "out": 2.2}, ...} ($ за млн токенов, in/out
без кэш-скидки). Файл редактирует владелец под свои тарифы; без файла —
только токены.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DB_DIR

_META_TOKENS = re.compile(r"Tokens:\s*(\d+)\s*in\s*/\s*(\d+)\s*out")
_META_CACHED = re.compile(r"Cached input:\s*(\d+)")
_META_MODEL = re.compile(r"Model:\s*(\S+)")


def _local_date(iso_utc: str) -> str:
    """completed_at (UTC ISO) -> локальная дата панели."""
    try:
        parsed = datetime.fromisoformat(iso_utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().date().isoformat()
    except ValueError:
        return ""


def _task_tokens_from_db() -> list[dict]:
    """Meta-блоки завершённых задач: провайдер, дата, токены, модель."""
    from . import db

    rows: list[dict] = []
    with db._connect() as conn:
        for provider, result, completed in conn.execute(
            "SELECT provider, result, completed_at FROM tasks "
            "WHERE status='completed' AND result LIKE '%--- Meta ---%' "
            "AND completed_at IS NOT NULL"
        ).fetchall():
            meta = result.split("--- Meta ---")[-1]
            tokens = _META_TOKENS.search(meta)
            if not tokens:
                continue
            cached = _META_CACHED.search(meta)
            model = _META_MODEL.search(meta)
            rows.append({
                "provider": provider or "?",
                "date": _local_date(completed),
                "input": int(tokens.group(1)),
                "output": int(tokens.group(2)),
                "cached": int(cached.group(1)) if cached else 0,
                "model": model.group(1) if model else None,
            })
    return rows


def _qwen_records(home: Path | None = None) -> list[dict]:
    """Журнал qwen: token-usage-YYYY-MM.jsonl, по строке на вызов."""
    base = (home or Path.home()) / ".qwen" / "usage"
    if not base.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(base.glob("token-usage-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in lines.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not item.get("totalTokens"):
                continue
            records.append({
                "provider": "qwen-review",
                "date": str(item.get("localDate") or "")[:10],
                "input": int(item.get("inputTokens") or 0),
                "output": int(item.get("outputTokens") or 0),
                "cached": int(item.get("cachedTokens") or 0),
                "model": item.get("model"),
            })
    return records


def _task_counts() -> dict[str, int]:
    """Число завершённых задач по провайдерам — видно и без юзажа."""
    from . import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT provider, COUNT(*) FROM tasks "
            "WHERE status='completed' GROUP BY provider").fetchall()
    return {provider or "?": count for provider, count in rows}


def load_pricing() -> dict:
    """pricing.json в DATA_DIR: {"модель": {"in": $/1M, "out": $/1M}}."""
    path = DB_DIR / "pricing.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_cost(rec: dict, pricing: dict) -> float:
    rates = pricing.get(rec.get("model") or "") or pricing.get(rec.get("provider") or "")
    if not rates:
        return 0.0
    return (rec["input"] * float(rates.get("in", 0))
            + rec["output"] * float(rates.get("out", 0))) / 1e6


def summary() -> dict:
    """Сводка токенов конвейера для панели."""
    records = _task_tokens_from_db() + _qwen_records()
    pricing = load_pricing()
    today = datetime.now().astimezone().date().isoformat()
    week_ago = (datetime.now().astimezone() - timedelta(days=7)).date().isoformat()

    by_provider: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    totals = {"input": 0, "output": 0, "cached": 0, "total": 0, "cost": 0.0}
    periods = {"today": {"total": 0, "cost": 0.0},
               "week": {"total": 0, "cost": 0.0}}

    counts = _task_counts()
    for provider in counts:
        by_provider.setdefault(provider, {
            "tasks": 0, "input": 0, "output": 0, "cached": 0,
            "total": 0, "cost": 0.0, "measured": False, "model": None,
        })
        by_provider[provider]["tasks"] = counts[provider]

    model_hits: dict[str, dict[str, int]] = {}
    for rec in records:
        bucket = by_provider.setdefault(rec["provider"], {
            "tasks": 0, "input": 0, "output": 0, "cached": 0,
            "total": 0, "cost": 0.0, "measured": False, "model": None,
        })
        if rec.get("model"):
            hits = model_hits.setdefault(rec["provider"], {})
            hits[rec["model"]] = hits.get(rec["model"], 0) + 1
        cost = _record_cost(rec, pricing)
        total = rec["input"] + rec["output"]
        for target in (bucket, totals):
            target["input"] += rec["input"]
            target["output"] += rec["output"]
            target["cached"] += rec["cached"]
            target["total"] += total
        bucket["cost"] += cost
        totals["cost"] += cost
        bucket["measured"] = True
        day = by_day.setdefault(rec["date"], {"total": 0, "cost": 0.0})
        day["total"] += total
        day["cost"] += cost
        if rec["date"] == today:
            periods["today"]["total"] += total
            periods["today"]["cost"] += cost
        if rec["date"] >= week_ago:
            periods["week"]["total"] += total
            periods["week"]["cost"] += cost

    return {
        "today": periods["today"],
        "week": periods["week"],
        "total": {"input": totals["input"], "output": totals["output"],
                  "cached": totals["cached"], "total": totals["total"],
                  "cost": round(totals["cost"], 4)},
        "by_provider": {
            p: {**v,
                "model": max(model_hits.get(p, {}),
                             key=model_hits.get(p, {}).get, default=None),
                "cost": round(v["cost"], 4)}
            for p, v in sorted(by_provider.items(),
                               key=lambda kv: -kv[1]["total"])
        },
        "by_day": [
            {"date": d, **v} for d, v in sorted(by_day.items())
        ][-14:],
        "pricing_configured": bool(pricing),
        "sources": {
            "codex": "meta", "qwen": "journal", "goose": "нет",
            "mmx": "нет",
        },
    }
