# -*-coding: utf-8 -*-
"""Лестница смен исполнителей (квота-фейловер v2).

Универсальная надстройка: пользователь настраивает ЛЕСТНИЦУ смен — у каждой
свои исполнитель и (опционально) своя схема ревью. Исчерпание квоты текущей
смены определяется двумя способами:

  1) штатная проверка квоты (если у провайдера есть команда, напр. MiniMax);
  2) по ошибкам задач: повторные «429 / quota / limit / high demand» в ошибках
     исполнителя = исчерпание (работает для ЛЮБЫХ провайдеров).

Квота восстановилась (у своей или нижней смены) — конвейер возвращается на
лучшую доступную смену МЕЖДУ задачами (меняется только конфиг — применяется
к ближайшему диспетчу, текущая задача не рвётся).

Конфиг (правится руками или из UI ⚙ Настройки → 🔁 Смены):
  ~/.promptpilot/quota-failover.json
Состояние: ~/.promptpilot/quota-failover-state.json
Журнал:    ~/.promptpilot/quota-failover.log
Запуск:    pythonw quota-failover-watcher.py (или --once для одного цикла).
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
PP_DIR = os.path.join(HOME, ".promptpilot")
CONFIG_PATH = os.path.join(PP_DIR, "quota-failover.json")
STATE_PATH = os.path.join(PP_DIR, "quota-failover-state.json")
LOG_PATH = os.path.join(PP_DIR, "quota-failover.log")
DB_PATH = os.path.join(PP_DIR, "promptpilot.db")
BASE_URL = "http://127.0.0.1:8420"
MAX_ERR_STREAK = 5

# Встроенные схемы ревью для смен (используются и UI-пресетами).
REVIEW_TEMPLATES = {
    "keep": None,  # не менять текущую схему воркфлоу
    "standard": {"enabled": True, "steps": [
        {"slot": 1, "provider": "goose-zai", "fixer": "self",
         "blocking": True, "max_rounds": 2, "on_exhaust": "arbitrate_planner"}]},
    "strict": {"enabled": True, "steps": [
        {"slot": 1, "provider": "goose-zai", "fixer": "self",
         "blocking": True, "max_rounds": 2, "on_exhaust": "arbitrate_planner"},
        {"slot": 2, "provider": "qwen-review", "fixer": "goose-zai",
         "blocking": True, "max_rounds": 3, "on_exhaust": "arbitrate_planner",
         "window": {"from": "22:00", "to": "04:00", "tz_offset_hours": 3}}]},
    "qwen_main": {"enabled": True, "steps": [
        {"slot": 1, "provider": "qwen-review", "fixer": "self",
         "blocking": True, "max_rounds": 2, "on_exhaust": "arbitrate_planner"}]},
    "none": {"enabled": False, "steps": []},
}

DEFAULT_CONFIG = {
    "enabled": True,
    "workflow_slug": "reader",
    "poll_seconds": 300,
    "failover_below_pct": 10,
    "recover_above_pct": 50,
    "recover_weekly_min_pct": 20,
    "error_patterns": ["429", "quota", "usage limit", "limit reached",
                       "high demand", "rate limit", "token plan",
                       "exceeded your current quota"],
    "error_window_minutes": 30,   # окно учёта ошибок
    "error_strikes": 2,           # сколько ошибок в окне = исчерпание
    "ladder": [
        {"name": "Основная смена", "executor": "mmx-m3",
         "quota": {"cmd": "mmx quota show --output json", "model": "general"},
         "review": "keep"},
        {"name": "Смена 2", "executor": "goose-flash",
         "quota": None, "review": "qwen_main"},
        {"name": "Смена 3", "executor": "codex",
         "quota": None, "review": "keep"},
    ],
}


def log(message):
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        BASE_URL + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def normalize_config(raw):
    """Миграция v1 (failover-пара) -> v2 (лестница) + дефолты полей."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(raw, dict):
        for k in ("enabled", "workflow_slug", "poll_seconds", "failover_below_pct",
                  "recover_above_pct", "recover_weekly_min_pct",
                  "error_patterns", "error_window_minutes", "error_strokes",
                  "error_strikes", "ladder"):
            if k in raw and raw[k] is not None:
                cfg[k] = raw[k]
        if "error_strokes" in raw:  # опечатко-устойчивость
            cfg["error_strikes"] = raw["error_strokes"]
    if not cfg.get("ladder"):
        cfg["ladder"] = DEFAULT_CONFIG["ladder"]
    if isinstance(raw, dict) and "failover" in raw and "ladder" not in raw:
        # v1: пара основной/фейловер -> лестница из двух ступеней
        old_primary_exec = "mmx-m3"
        try:
            wfs = api("/api/workflows")
            wf = next(w for w in wfs if w["slug"] == cfg["workflow_slug"])
            full = api(f"/api/workflows/{wf['id']}")
            chain = full["config"].get("review_chain")
            # сохраняем исходную схему в state-подобный снапшот прямо в конфиг
            cfg["ladder"][0]["snapshot_chain"] = chain
        except Exception:
            pass
        cfg["ladder"] = [
            {"name": "Основная смена", "executor": old_primary_exec,
             "quota": {"cmd": raw.get("quota_cmd", DEFAULT_CONFIG["ladder"][0]["quota"]["cmd"]),
                       "model": raw.get("quota_model", "general")},
             "review": "keep",
             "snapshot_chain": cfg["ladder"][0].get("snapshot_chain")},
            {"name": "Смена 2", "executor": raw["failover"].get("executor", "goose-flash"),
             "quota": None, "review": "qwen_main"},
        ]
    return cfg


# ── детекторы исчерпания ──────────────────────────────────────────────

def native_quota(quota_cfg):
    """Штатная проверка: (interval_pct, weekly_pct) или None."""
    if not quota_cfg:
        return None
    try:
        proc = subprocess.run(
            quota_cfg["cmd"], shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        for entry in data.get("model_remains", []):
            if entry.get("model_name") == quota_cfg.get("model", "general"):
                return (entry.get("current_interval_remaining_percent"),
                        entry.get("current_weekly_remaining_percent"))
    except Exception:
        pass
    return None


def error_strikes(cfg, provider):
    """Ошибки «квота/лимит» у провайдера за окно: список (id, error)."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA busy_timeout = 4000")
        since = (datetime.now(timezone.utc) - timedelta(
            minutes=int(cfg.get("error_window_minutes", 30)))).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "SELECT id, error FROM tasks "
            "WHERE status='failed' AND completed_at > ? AND provider = ? "
            "ORDER BY id DESC LIMIT 20", (since, provider)).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    patterns = [re.compile(p, re.I) for p in cfg.get("error_patterns", [])]
    hits = []
    for tid, err in rows:
        if err and any(p.search(err) for p in patterns):
            hits.append((tid, err[:120]))
    return hits


# ── применение смены ─────────────────────────────────────────────────

_SENTINEL = object()


def patch_workflow(cfg, executor=None, review_chain=_SENTINEL):
    for attempt in range(4):
        wfs = api("/api/workflows")
        wf = next((w for w in wfs if w["slug"] == cfg["workflow_slug"]), None)
        if wf is None:
            raise RuntimeError(f"воркфлоу {cfg['workflow_slug']!r} не найден")
        full = api(f"/api/workflows/{wf['id']}")
        new_cfg = json.loads(json.dumps(full["config"]))
        if executor is not None:
            new_cfg.setdefault("roles", {}).setdefault("executor", {})
            new_cfg["roles"]["executor"]["provider"] = executor
        if review_chain is not _SENTINEL:
            new_cfg["review_chain"] = review_chain
        try:
            api(f"/api/workflows/{wf['id']}", method="PATCH", body={
                "config": new_cfg, "expected_version": full["state_version"]})
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 409 and attempt < 3:
                time.sleep(2)
                continue
            raise
    return False


def current_wf_executor(cfg):
    wfs = api("/api/workflows")
    wf = next((w for w in wfs if w["slug"] == cfg["workflow_slug"]), None)
    full = api(f"/api/workflows/{wf['id']}")
    return (full["config"].get("roles", {}).get("executor", {}).get("provider"),
            full["config"].get("review_chain"))


def apply_rung(cfg, state, idx, reason):
    """Переключить воркфлоу на ступень idx (между задачами)."""
    ladder = cfg["ladder"]
    rung = ladder[idx]
    chain = REVIEW_TEMPLATES.get(rung.get("review", "keep"))
    if rung.get("review") == "keep" and idx == 0 and state.get("rung0_snapshot_chain") is None:
        # первый уход с основной смены: запоминаем её схему для возврата
        _, cur_chain = current_wf_executor(cfg)
        state["rung0_snapshot_chain"] = cur_chain
    if rung.get("review") == "keep" and idx == 0:
        chain = state.get("rung0_snapshot_chain") or rung.get("snapshot_chain")
    patch_workflow(cfg, executor=rung["executor"], review_chain=chain)
    state["rung"] = idx
    state["switched_at"] = datetime.now().isoformat(timespec="seconds")
    state["switch_reason"] = reason
    log(f">>> СМЕНА {idx} «{rung.get('name', rung['executor'])}»: исполнитель "
        f"{rung['executor']}; причина: {reason}")


# ── цикл ──────────────────────────────────────────────────────────────

def rung_state(cfg, state, idx):
    """(exhausted: bool, info: str) для ступени — по нативной квоте и ошибкам."""
    rung = cfg["ladder"][idx]
    q = native_quota(rung.get("quota"))
    if q is not None:
        interval_pct, weekly_pct = q
        low = ((interval_pct is not None and interval_pct <= cfg["failover_below_pct"]) or
               (weekly_pct is not None and weekly_pct <= cfg["failover_below_pct"]))
        if low:
            return True, f"квота окно {interval_pct}%/неделя {weekly_pct}%"
    strikes = error_strikes(cfg, rung["executor"])
    if len(strikes) >= int(cfg.get("error_strikes", 2)):
        return True, f"{len(strikes)} ошибок квоты за {cfg.get('error_window_minutes', 30)} мин (#{strikes[0][0]}…)"
    if q is not None:
        return False, f"квота окно {q[0]}%/неделя {q[1]}%"
    return False, f"ошибок квоты: {len(strikes)}"


def cycle(cfg, state):
    ladder = cfg["ladder"]
    # миграция состояния v1 (mode: failover) -> v2 (rung)
    if "rung" not in state and state.get("mode") == "failover":
        state["rung"] = 1
        snap = (state.get("snapshot") or {}).get("review_chain")
        if snap is not None:
            state["rung0_snapshot_chain"] = snap
    cur = state.get("rung", 0)
    state["rung"] = cur
    if cur >= len(ladder):
        cur = state["rung"] = 0
    # рассинхрон: фактический исполнитель воркфлоу vs ступень состояния
    actual_exec, _ = current_wf_executor(cfg)
    if actual_exec != ladder[cur]["executor"]:
        match = next((i for i, r in enumerate(ladder)
                      if r["executor"] == actual_exec), None)
        if match is not None:
            state["rung"] = cur = match
            log(f"рассинхрон: исполнитель {actual_exec} — считаем ступенью {cur}")
        else:
            log(f"рассинхрон: исполнитель {actual_exec} вне лестницы — применяю ступень {cur}")
            apply_rung(cfg, state, cur, "рассинхрон с конфигурацией")
    try:
        exhausted, info = rung_state(cfg, state, cur)
    except Exception as exc:
        state["err_streak"] = state.get("err_streak", 0) + 1
        log(f"цикл: {type(exc).__name__}: {exc}")
        return state
    state["err_streak"] = 0
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    state["current_info"] = info

    if exhausted:
        if cur + 1 < len(ladder):
            apply_rung(cfg, state, cur + 1, info)
        else:
            log(f"!!! Смена {cur} исчерпана ({info}), но лестница закончилась — "
                f"остаёмся; следите за квотами")
            state["ladder_end"] = datetime.now().isoformat(timespec="seconds")
        return state

    # возврат на лучшую доступную ступень (ниже текущей), между задачами
    if cur > 0:
        for idx in range(0, cur):
            try:
                lower_exhausted, lower_info = rung_state(cfg, state, idx)
            except Exception:
                continue
            if not lower_exhausted:
                apply_rung(cfg, state, idx, f"ступень {idx} доступна ({lower_info})")
                break
    else:
        log(f"режим: смена {cur} «{ladder[cur].get('name', '')}» — {info}")
    return state


def main():
    once = "--once" in sys.argv
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        log(f"создан конфиг по умолчанию: {CONFIG_PATH}")
    while True:
        raw = load_json(CONFIG_PATH, DEFAULT_CONFIG)
        cfg = normalize_config(raw)
        state = load_json(STATE_PATH, {"rung": 0, "err_streak": 0})
        if cfg.get("enabled", True):
            try:
                state = cycle(cfg, state)
            except Exception as exc:
                log(f"цикл: {type(exc).__name__}: {exc}")
            history = state.setdefault("history", [])
            history.append({"at": state.get("last_check"), "rung": state.get("rung"),
                            "info": state.get("current_info")})
            del history[:-30]
            try:
                save_json(STATE_PATH, state)
            except OSError as exc:
                log(f"состояние не сохранено: {exc}")
        else:
            log("выключен в конфиге — пропуск цикла")
        if once:
            break
        time.sleep(max(30, int(cfg.get("poll_seconds", 300))))


if __name__ == "__main__":
    main()
