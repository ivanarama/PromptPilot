"""Usage from structured completion counters, never result text or global logs.

pricing.json maps a reported model (or provider) to {"in": $/1M, "out": $/1M}.
Unknown coverage/prices yield cost=null; known_cost is the priced subtotal.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from . import db
from .config import DB_DIR


def load_pricing() -> dict:
    try:
        data = json.loads((DB_DIR / "pricing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean = {}
    for model, rates in data.items():
        if not isinstance(rates, dict):
            continue
        values = [rates.get("in"), rates.get("out")]
        try:
            valid = all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values)
        except OverflowError:
            valid = False
        if valid:
            clean[model] = dict(zip(("in", "out"), values))
    return clean


def _bucket():
    return dict(tasks=0, measured_tasks=0, priced_tasks=0, input=0, output=0,
                cached=0, total=0, known_cost=0.0, cost=None, measured=False)


def summary(now=None) -> dict:
    """Group compact counters in SQLite; polling never reads result blobs."""
    pricing = load_pricing()
    today = (now or datetime.now().astimezone()).date()
    first_day = (today - timedelta(days=6)).isoformat()
    today = today.isoformat()
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT COALESCE(t.provider, '?') AS provider,
                   date(t.completed_at, 'localtime') AS day, u.model,
                   COUNT(*) AS tasks, COUNT(u.task_id) AS measured,
                   TOTAL(u.input_tokens) AS inp, TOTAL(u.output_tokens) AS out,
                   TOTAL(u.cached_input_tokens) AS cached
            FROM tasks AS t LEFT JOIN task_token_usage AS u ON u.task_id=t.id
            WHERE t.status='completed' AND t.completed_at IS NOT NULL
            GROUP BY provider, day, u.model
        """).fetchall()
    totals, daily, weekly = _bucket(), _bucket(), _bucket()
    providers, days, models = {}, {}, {}
    for row in rows:
        provider, day, model = row["provider"], row["day"], row["model"]
        bucket = providers.setdefault(provider, _bucket())
        targets = [totals, bucket]
        if day:
            targets.append(days.setdefault(day, _bucket()))
        if day == today:
            targets.append(daily)
        if day and first_day <= day <= today:
            targets.append(weekly)
        rates = pricing.get(model) or pricing.get(provider)
        cost = ((row["inp"] * rates["in"] + row["out"] * rates["out"]) / 1e6
                if rates else None)
        if cost is not None and not math.isfinite(cost):
            cost = None
        for target in targets:
            target["tasks"] += row["tasks"]
            target["measured_tasks"] += row["measured"]
            target["priced_tasks"] += row["measured"] if cost is not None else 0
            for key, column in (("input", "inp"), ("output", "out"), ("cached", "cached")):
                target[key] += int(row[column])
            target["known_cost"] += cost or 0.0
        if model and row["measured"]:
            models.setdefault(provider, set()).add(model)

    def finish(value):
        value["total"] = value["input"] + value["output"]
        value["measured"] = value["measured_tasks"] > 0
        value["known_cost"] = round(value["known_cost"], 8)
        if value["tasks"] and value["priced_tasks"] == value["tasks"]:
            value["cost"] = value["known_cost"]
        return value

    return {
        "today": finish(daily), "week": finish(weekly), "total": finish(totals),
        "by_provider": {
            p: {**finish(v), "model": next(iter(models[p])) if len(models.get(p, ())) == 1 else None}
            for p, v in sorted(providers.items())},
        "by_day": [{"date": day, **finish(v)} for day, v in sorted(days.items())][-14:],
        "pricing_configured": bool(pricing),
        "sources": {"structured_completion": True, "legacy_text": False,
                    "machine_journals": False},
    }
