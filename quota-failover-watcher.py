# -*- coding: utf-8 -*-
"""Квота-фейловер: штатная проверка квоты исполнителя и автоматическое
переключение ролей конвейера при исчерпании.

Схема (настройка пользователя, 2026-09-26):
  основной режим : исполнитель МиниМакс (mmx-m3), ревью-каскад по конфигу
  фейловер       : исполнитель ГЛМ-Быстрый (goose-flash), главный ревьюер
                   Квен (qwen-review, без ночного окна), финально работу
                   принимает планер (on_exhaust: arbitrate_planner)

Квота проверяется штатной командой провайдера (MiniMax: `mmx quota show`).
Гистерезис против дёрганья: уход в фейловер при остатке <= failover_below_pct,
возврат при восстановлении >= recover_above_pct (окно) и >= recover_weekly_min_pct (неделя).

Файлы:
  ~/.promptpilot/quota-failover.json        — настройки (можно править руками)
  ~/.promptpilot/quota-failover-state.json  — состояние + снапшот ролей для отката
  ~/.promptpilot/quota-failover.log         — журнал
Запуск: pythonw quota-failover-watcher.py (или --once для одного цикла).
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
PP_DIR = os.path.join(HOME, ".promptpilot")
CONFIG_PATH = os.path.join(PP_DIR, "quota-failover.json")
STATE_PATH = os.path.join(PP_DIR, "quota-failover-state.json")
LOG_PATH = os.path.join(PP_DIR, "quota-failover.log")
BASE_URL = "http://127.0.0.1:8420"
MAX_ERR_STREAK = 5  # подряд неудачных замеров — проще пропустить цикл

DEFAULT_CONFIG = {
    "enabled": True,
    "workflow_slug": "reader",
    "poll_seconds": 300,
    "quota_cmd": "mmx quota show --output json",
    "quota_model": "general",
    "failover_below_pct": 10,     # остаток окна/недели <= 10% -> фейловер
    "recover_above_pct": 50,      # остаток окна >= 50% и недели >= 20% -> возврат
    "recover_weekly_min_pct": 20,
    "failover": {
        "executor": "goose-flash",
        "review_chain": {
            "enabled": True,
            "steps": [
                {
                    "slot": 1,
                    "provider": "qwen-review",
                    "fixer": "self",
                    "blocking": True,
                    "max_rounds": 2,
                    "on_exhaust": "arbitrate_planner",
                }
            ],
        },
    },
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


def measure_quota(cfg):
    """Запускает штатную команду квоты, возвращает (interval_pct, weekly_pct)."""
    proc = subprocess.run(
        cfg["quota_cmd"], shell=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"quota cmd rc={proc.returncode}: {(proc.stderr or '')[:200]}")
    data = json.loads(proc.stdout)
    for entry in data.get("model_remains", []):
        if entry.get("model_name") == cfg["quota_model"]:
            return (entry.get("current_interval_remaining_percent"),
                    entry.get("current_weekly_remaining_percent"))
    raise RuntimeError(f"модель {cfg['quota_model']!r} не найдена в ответе квоты")


_SENTINEL = object()  # «аргумент не передан» (None — законное значение цепочки)


def patch_workflow(cfg, executor=None, review_chain=_SENTINEL):
    """PATCH конфига воркфлоу с повтором при конфликте версий."""
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
                "config": new_cfg,
                "expected_version": full["state_version"],
            })
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 409 and attempt < 3:
                time.sleep(2)
                continue
            raise
    return False


def cycle(cfg, state):
    """Один цикл: замер -> решение. Возвращает обновлённое состояние."""
    try:
        interval_pct, weekly_pct = measure_quota(cfg)
    except Exception as exc:
        state["err_streak"] = state.get("err_streak", 0) + 1
        log(f"замер не удался ({state['err_streak']}/{MAX_ERR_STREAK}): {exc}")
        if state["err_streak"] >= MAX_ERR_STREAK:
            log("  слишком много ошибок подряд — решения на таком замере не принимаются")
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        return state
    state["err_streak"] = 0
    state.update({
        "last_check": datetime.now().isoformat(timespec="seconds"),
        "interval_pct": interval_pct,
        "weekly_pct": weekly_pct,
    })
    mode = state.get("mode", "primary")

    low = (interval_pct is not None and interval_pct <= cfg["failover_below_pct"]) or \
          (weekly_pct is not None and weekly_pct <= cfg["failover_below_pct"])
    ok = (interval_pct is not None and interval_pct >= cfg["recover_above_pct"] and
          weekly_pct is not None and weekly_pct >= cfg["recover_weekly_min_pct"])

    if mode == "primary" and low:
        # Снапшотим текущие роли, чтобы вернуть их при восстановлении квоты.
        wfs = api("/api/workflows")
        wf = next((w for w in wfs if w["slug"] == cfg["workflow_slug"]), None)
        full = api(f"/api/workflows/{wf['id']}") if wf else None
        if full:
            state["snapshot"] = {
                "executor": full["config"].get("roles", {}).get("executor", {}).get("provider"),
                "review_chain": full["config"].get("review_chain"),
            }
        fo = cfg["failover"]
        patch_workflow(cfg, executor=fo["executor"], review_chain=fo["review_chain"])
        state["mode"] = "failover"
        state["switched_at"] = datetime.now().isoformat(timespec="seconds")
        log(f">>> ФЕЙЛОВЕР: квота {cfg['quota_model']} окно {interval_pct}%/неделя {weekly_pct}% — "
            f"исполнитель -> {fo['executor']}, ревью -> " +
            ", ".join(s["provider"] for s in fo["review_chain"]["steps"]))
    elif mode == "failover" and ok:
        snap = state.get("snapshot") or {}
        patch_workflow(cfg,
                       executor=snap.get("executor", "mmx-m3"),
                       review_chain=snap.get("review_chain"))
        state["mode"] = "primary"
        state["switched_at"] = datetime.now().isoformat(timespec="seconds")
        log(f"<<< ВОЗВРАТ: квота окно {interval_pct}%/неделя {weekly_pct}% — "
            f"исполнитель -> {snap.get('executor', 'mmx-m3')}, каскад восстановлен из снапшота")
    else:
        log(f"режим {mode}: окно {interval_pct}%, неделя {weekly_pct}% (пороги "
            f"фейловер<={cfg['failover_below_pct']}%, возврат>={cfg['recover_above_pct']}%)")
    return state


def main():
    once = "--once" in sys.argv
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        log(f"создан конфиг по умолчанию: {CONFIG_PATH}")
    while True:
        cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
        state = load_json(STATE_PATH, {"mode": "primary", "err_streak": 0})
        if cfg.get("enabled", True):
            try:
                state = cycle(cfg, state)
            except Exception as exc:
                log(f"цикл: {type(exc).__name__}: {exc}")
            history = state.setdefault("history", [])
            history.append({
                "at": state.get("last_check"),
                "mode": state.get("mode"),
                "interval_pct": state.get("interval_pct"),
                "weekly_pct": state.get("weekly_pct"),
            })
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
