#!/usr/bin/env python3
"""PromptPilot MCP server — управление пайплайном через Model Context Protocol.

Транспорт: stdio (newline-delimited JSON-RPC 2.0), чистый stdlib — никаких
зависимостей. Оборачивает REST API PromptPilot (http://127.0.0.1:8420).

Инструменты:
  pipeline_status     — статус воркфлоу + сердцебиение воркера + экзекьютор.
  pipeline_events     — последние события (почему остановился).
  pipeline_stop_reason — классификация остановки: ROUTINE (можно продолжить
                        автоматически) / CRITICAL (нужно решение человека) /
                        RUNNING / UNKNOWN.
  pipeline_continue   — умное «Продолжить»: awaiting_human → resume,
                        queued → sync-пинок воркеру; при 409 сам перечитывает
                        версию; возвращает итоговый статус.
  notify_user         — всплывающее уведомление Windows (toast) с текстом.

Запуск (для MCP-клиента):
  python "C:\\Users\\Nachfin\\Desktop\\Projets\\Other\\PromptPilot\\promptpilot-mcp.py"
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8420"
DEFAULT_WORKFLOW = "wf_1e0bd7a5cfb14d13a60c596e06c85fd9"  # BookApp reader
RESUME_TEXT = "Продолжить работу с учётом сохранённого состояния"


# ── REST helper ──────────────────────────────────────────────────────────────

def _api(path: str, method: str = "GET", body: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(
        BASE_URL + path,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"PromptPilot не отвечает ({BASE_URL}): {e.reason}. "
            "Запусти PromptPilot-Start.bat."
        ) from None


def _get_workflow(wf_id: str) -> dict:
    return _api(f"/api/workflows/{wf_id}")


def _events_ascending(items: list) -> list:
    """API может отдать события новыми-первыми — нормализуем к хронологии."""
    if len(items) >= 2:
        seqs = [e.get("seq") for e in items]
        if all(isinstance(s, int) for s in seqs):
            return sorted(items, key=lambda e: e["seq"])
        if items[0].get("created_at", "") > items[-1].get("created_at", ""):
            return list(reversed(items))
    return items


def _event_payload(ev: dict) -> dict:
    p = ev.get("payload")
    if isinstance(p, dict):
        return p
    try:
        return json.loads(ev.get("payload_json") or "{}")
    except Exception:
        return {}


def _fetch_recent_events(wf_id: str) -> list:
    """Последние события воркфлоу по хронологии.

    Эндпоинт отдаёт максимум 200 событий С НАЧАЛА (после after_seq), поэтому
    листаем страницы, пока не дойдём до конца журнала (защитный лимит 15 страниц).
    """
    all_items: list = []
    after = 0
    for _ in range(15):
        batch = _api(f"/api/workflows/{wf_id}/events?after_seq={after}", timeout=15)
        items = batch if isinstance(batch, list) else batch.get("items", [])
        if not items:
            break
        all_items = items  # страницы идут подряд, достаточно держать последнюю
        seqs = [e.get("seq") for e in items if isinstance(e.get("seq"), int)]
        if not seqs or len(items) < 200:
            break
        after = max(seqs)
    return _events_ascending(all_items)


# ── Инструменты ──────────────────────────────────────────────────────────────

def tool_status(wf_id: str | None = None) -> str:
    wf = _get_workflow(wf_id or DEFAULT_WORKFLOW)
    try:
        wk = _api("/api/worker/status", timeout=8)
        worker = f"{wk.get('state')} (pid {wk.get('pid')}, heartbeat {wk.get('age_seconds')}s назад)"
    except Exception as e:
        worker = f"ОШИБКА ({e})"
    executor = (wf.get("config", {}).get("roles", {}).get("executor", {}) or {})
    reviewer = (wf.get("config", {}).get("roles", {}).get("reviewer", {}) or {})
    stage = wf.get("current_stage_code") or wf.get("stage_code") or "?"
    return json.dumps({
        "slug": wf.get("slug"),
        "status": wf.get("status"),
        "round": wf.get("current_round"),
        "stage": stage,
        "executor_provider": executor.get("provider"),
        "executor_model": executor.get("model"),
        "reviewer_provider": reviewer.get("provider"),
        "state_version": wf.get("state_version"),
        "worker": worker,
    }, ensure_ascii=False)


def tool_events(limit: int = 8, wf_id: str | None = None) -> str:
    wf_id = wf_id or DEFAULT_WORKFLOW
    items = _fetch_recent_events(wf_id)
    tail = items[-max(1, min(limit, 30)):]
    out = []
    for ev in tail:
        payload = _event_payload(ev)
        interesting = {k: payload[k] for k in
                       ("reason", "from", "to", "task_id", "text", "code", "verdict")
                       if k in payload}
        out.append({
            "at": (ev.get("created_at") or "")[5:19],
            "type": ev.get("event_type"),
            **interesting,
        })
    return json.dumps(out, ensure_ascii=False, indent=1)


def tool_stop_reason(wf_id: str | None = None) -> str:
    """Классификация текущей остановки по последним событиям.

    ROUTINE   — известная рутинная причина; можно вызывать pipeline_continue.
    CRITICAL  — нужно решение человека (план на утверждении, HUMAN_REQUIRED,
                исчерпан лимит ревизий — лечится правкой лимита в БД).
    RUNNING   — ничего не встало, продолжать не надо.
    """
    wf_id = wf_id or DEFAULT_WORKFLOW
    wf = _get_workflow(wf_id)
    status = wf.get("status")
    items = _fetch_recent_events(wf_id)
    recent = [e.get("event_type", "") for e in items[-8:]]

    if status in ("executing", "gating", "reviewing"):
        return json.dumps({"verdict": "RUNNING",
                           "status": status,
                           "advice": "Пайплайн работает — ничего делать не нужно."}, ensure_ascii=False)

    if status == "queued":
        return json.dumps({"verdict": "ROUTINE", "status": status,
                           "reason": "queued — воркер, вероятно, не заметил задачу",
                           "advice": "Вызови pipeline_continue (он пошлёт sync-пинок)."}, ensure_ascii=False)

    if status == "awaiting_plan_approval" or (status == "awaiting_human" and "planner.completed" in recent):
        return json.dumps({"verdict": "CRITICAL", "status": status,
                           "reason": "план воркфлоу ждёт утверждения человеком",
                           "advice": "Открой UI и утверди план (или позови пользователя)."}, ensure_ascii=False)

    if "executor.invalid_output" in recent:
        return json.dumps({"verdict": "ROUTINE", "status": status,
                           "reason": "исполнитель потерял вердикт ИТОГ (контракт формата)",
                           "advice": "Вызови pipeline_continue — раунд перезапустится."}, ensure_ascii=False)

    if any(t in recent for t in ("task.failed", "run.failed", "provider.error")):
        return json.dumps({"verdict": "ROUTINE", "status": status,
                           "reason": "сетевая/провайдерная ошибка задачи",
                           "advice": "Подожди пару минут и вызови pipeline_continue; "
                                     "при повторе — смени провайдера в UI."}, ensure_ascii=False)

    if "gate.failed" in recent:
        return json.dumps({"verdict": "ROUTINE", "status": status,
                           "reason": "гейт упал (таймаут/сборка)",
                           "advice": "Вызови pipeline_continue; при повторе таймаута "
                                     "перезапусти воркер Stop/Start.bat."}, ensure_ascii=False)

    if any(t in recent for t in ("reviewer.invalid_output", "automation.paused")):
        return json.dumps({"verdict": "ROUTINE", "status": status,
                           "reason": "ревьюер нарушил формат отчёта / автоматика встала",
                           "advice": "Вызови pipeline_continue; при повторе посмотри "
                                     "pipeline_events."}, ensure_ascii=False)

    if "executor.human_required" in recent:
        return json.dumps({"verdict": "CRITICAL", "status": status,
                           "reason": "исполнитель явно запросил человека",
                           "advice": "Прочитай последний отчёт исполнителя и реши сам."}, ensure_ascii=False)

    return json.dumps({"verdict": "UNKNOWN", "status": status,
                       "recent_events": recent,
                       "advice": "Посмотри pipeline_events и реши по ситуации; "
                                 "чаще всего помогает pipeline_continue."}, ensure_ascii=False)


def tool_continue(wf_id: str | None = None) -> str:
    """Умное «Продолжить»: resume при awaiting_human, sync при queued."""
    wf_id = wf_id or DEFAULT_WORKFLOW
    wf = _get_workflow(wf_id)
    status, version = wf.get("status"), wf.get("state_version")

    if status in ("executing", "gating", "reviewing"):
        return json.dumps({"action": "none", "status": status,
                           "note": "Пайплайн уже работает."}, ensure_ascii=False)

    if status == "awaiting_human":
        for attempt in (1, 2):
            try:
                _api(f"/api/workflows/{wf_id}/human-input", "POST",
                     {"expected_version": version, "text": RESUME_TEXT, "resume": True})
                break
            except RuntimeError as e:
                if "409" in str(e) and attempt == 1:
                    wf = _get_workflow(wf_id)  # версия уехала — перечитали
                    version = wf.get("state_version")
                    continue
                return json.dumps({"action": "resume", "error": str(e),
                                   "status": _get_workflow(wf_id).get("status")}, ensure_ascii=False)
    elif status == "queued":
        for attempt in (1, 2):
            try:
                _api(f"/api/workflows/{wf_id}/sync", "POST", {"expected_version": version})
                break
            except RuntimeError as e:
                if "409" in str(e) and attempt == 1:
                    wf = _get_workflow(wf_id)
                    version = wf.get("state_version")
                    continue
                # sync при отсутствии работы иногда 4xx — не страшно, идём смотреть статус
                break
    else:
        return json.dumps({"action": "none", "status": status,
                           "note": "Статус не требует продолжения — смотри stop_reason."}, ensure_ascii=False)

    time.sleep(8)
    final = _get_workflow(wf_id)
    result = {"action": "resume+sync", "status_before": status, "status_after": final.get("status"),
              "round": final.get("current_round")}
    if final.get("status") == "queued":
        result["note"] = ("Всё ещё queued — подожди минуту и вызови pipeline_continue ещё раз; "
                          "при повторе перезапусти воркер Stop/Start.bat.")
    return json.dumps(result, ensure_ascii=False)


def tool_notify_user(text: str, title: str = "PromptPilot") -> str:
    """Всплывающее уведомление Windows (toast; фолбэк — окно msg)."""
    safe_text = text.replace('"', "'").replace("\n", " ")[:250]
    safe_title = title.replace('"', "'")[:60]
    ps = (
        "$t=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$x=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
        "$x.LoadXml('"
        "<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{safe_title}</text><text>{safe_text}</text>"
        "</binding></visual></toast>');"
        "$APP='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($APP).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=20, creationflags=0x08000000)
        return json.dumps({"delivered": "toast", "text": safe_text}, ensure_ascii=False)
    except Exception:
        try:
            subprocess.run(["msg", "*", f"{safe_title}: {safe_text}"],
                           capture_output=True, timeout=20)
            return json.dumps({"delivered": "msg-box", "text": safe_text}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"delivered": "failed", "error": str(e)}, ensure_ascii=False)


# ── MCP protocol (stdio, JSON-RPC 2.0) ──────────────────────────────────────

TOOLS = [
    {
        "name": "pipeline_status",
        "description": "Статус пайплайна PromptPilot (воркфлоу reader): status/раунд/этап/"
                       "экзекьютор + сердцебиение воркера. Начинай мониторинг отсюда.",
        "inputSchema": {"type": "object", "properties": {
            "wf_id": {"type": "string", "description": "ID воркфлоу (по умолчанию BookApp reader)"}},
            "required": []},
    },
    {
        "name": "pipeline_events",
        "description": "Последние события воркфлоу — почему остановился (вердикты, ошибки, "
                       "переходы статусов).",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Сколько событий (1-30, по умолчанию 8)"},
            "wf_id": {"type": "string"}}, "required": []},
    },
    {
        "name": "pipeline_stop_reason",
        "description": "Классификация остановки пайплайна: ROUTINE (безопасно продолжить — "
                       "вызови pipeline_continue), CRITICAL (нужно решение человека — "
                       "уведоми notify_user), RUNNING (не трогать), UNKNOWN (смотри events).",
        "inputSchema": {"type": "object", "properties": {
            "wf_id": {"type": "string"}}, "required": []},
    },
    {
        "name": "pipeline_continue",
        "description": "Умное «Продолжить»: awaiting_human → resume, queued → sync-пинок "
                       "воркеру. Идемпотентно: при работающем пайплайне ничего не делает.",
        "inputSchema": {"type": "object", "properties": {
            "wf_id": {"type": "string"}}, "required": []},
    },
    {
        "name": "notify_user",
        "description": "Показать пользователю всплывающее уведомление Windows. Используй "
                       "при CRITICAL-остановках или когда нужно его решение.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Текст уведомления"},
            "title": {"type": "string", "description": "Заголовок (по умолчанию PromptPilot)"}},
            "required": ["text"]},
    },
]

HANDLERS = {
    "pipeline_status": lambda a: tool_status(a.get("wf_id")),
    "pipeline_events": lambda a: tool_events(int(a.get("limit", 8)), a.get("wf_id")),
    "pipeline_stop_reason": lambda a: tool_stop_reason(a.get("wf_id")),
    "pipeline_continue": lambda a: tool_continue(a.get("wf_id")),
    "notify_user": lambda a: tool_notify_user(a["text"], a.get("title", "PromptPilot")),
}


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req: dict) -> None:
    method = req.get("method", "")
    req_id = req.get("id")

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "promptpilot", "version": "1.0.0"},
        }})
    elif method == "notifications/initialized" or method.startswith("notifications/"):
        pass  # уведомления не требуют ответа
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = req.get("params", {}).get("name", "")
        args = req.get("params", {}).get("arguments", {}) or {}
        handler = HANDLERS.get(name)
        if handler is None:
            send({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32602, "message": f"Unknown tool: {name}"}})
            return
        try:
            text = handler(args)
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": text}]}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": f"ОШИБКА: {e}"}], "isError": True}})
    elif req_id is not None:
        send({"jsonrpc": "2.0", "id": req_id, "error": {
            "code": -32601, "message": f"Method not found: {method}"}})


def main() -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            continue  # мусор в потоке пропускаем


if __name__ == "__main__":
    main()
