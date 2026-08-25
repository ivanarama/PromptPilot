"""FastAPI web API + static file serving."""

import base64
import secrets
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import os
import re as _re

from . import db, workflows
from .config import API_TOKEN, DB_DIR, EFFORT_LEVELS, get_provider_models, get_skills, load_providers, mask_secret_value, provider_available, PROJECTS_ROOT
from .models import (
    CostStats,
    FindingStatus,
    Stats,
    TaskCreate,
    TaskInDB,
    TaskStatus,
    TaskUpdate,
    WorkflowArtifactInDB,
    WorkflowCreate,
    WorkflowEventInDB,
    WorkflowFindingInDB,
    WorkflowInDB,
    WorkflowDispatchResult,
    WorkflowGateDecision,
    WorkflowHistoryImport,
    WorkflowHumanInput,
    WorkflowReviewDecision,
    WorkflowRoundInDB,
    WorkflowRunInDB,
    WorkflowStartRequest,
    WorkflowStatus,
    WorkflowTaskDispatch,
    WorkflowUpdate,
    WorkflowVersionRequest,
)
from .version import check_for_update

app = FastAPI(title="PromptPilot", version="0.1.0")


_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@app.middleware("http")
async def _auth(request, call_next):
    """Optional auth: enabled by PP_API_TOKEN. Accepts Bearer <token> or
    HTTP Basic with the token as password (browser shows a native prompt).

    Regardless of the token, a state-changing request that a browser marks as
    cross-site is refused: without this, a malicious page open in the user's
    browser could POST to the loopback server (no token by default) and queue a
    task that runs with --dangerously-skip-permissions. curl/scripts don't send
    Sec-Fetch-Site, so they're unaffected."""
    if request.method not in _SAFE_METHODS:
        if request.headers.get("sec-fetch-site") == "cross-site":
            return Response(status_code=403, content="cross-site request refused")
    if not API_TOKEN:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    ok = False
    if header.startswith("Bearer "):
        ok = secrets.compare_digest(header[7:].strip(), API_TOKEN)
    elif header.startswith("Basic "):
        try:
            _, _, password = base64.b64decode(header[6:]).decode().partition(":")
            ok = secrets.compare_digest(password, API_TOKEN)
        except Exception:
            ok = False
    if ok:
        return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="PromptPilot"'})

# When frozen by PyInstaller, __file__ points into the temp extraction dir
if getattr(sys, "frozen", False):
    STATIC_DIR = Path(sys._MEIPASS) / "promptpilot" / "static"
else:
    STATIC_DIR = Path(__file__).parent / "static"


# --- API ---

@app.get("/api/tasks", response_model=List[TaskInDB])
def api_list_tasks(status: Optional[TaskStatus] = None, limit: int = 50, offset: int = 0):
    return db.list_tasks(status=status, limit=limit, offset=offset)


@app.post("/api/tasks", response_model=TaskInDB, status_code=201)
def api_create_task(task: TaskCreate):
    # Allowlist providers: an unknown name would otherwise be turned into a raw
    # command by build_cmd's fallback (provider='touch x' → run `touch x`). The
    # UI only ever offers registered providers, so this rejects nothing real.
    if task.provider and task.provider not in load_providers():
        raise HTTPException(400, f"Неизвестный провайдер «{task.provider}»")
    return db.create_task(task)


@app.get("/api/tasks/{task_id}", response_model=TaskInDB)
def api_get_task(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.patch("/api/tasks/{task_id}", response_model=dict)
def api_update_task(task_id: int, update: TaskUpdate):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    if update.status == TaskStatus.CANCELLED:
        # Cancelling settles the task; a priority change alongside it is moot, so
        # don't half-apply both (the old code cancelled, then 400'd on priority).
        if task.status == TaskStatus.RUNNING:
            # running: ask the worker to kill the process (it polls every ~2s)
            if not db.request_cancel(task_id):
                raise HTTPException(400, "Task is no longer running")
        elif not db.cancel_task(task_id):
            raise HTTPException(400, "Can only cancel pending, rate_limited or running tasks")
        return {"ok": True}

    if update.status is not None:
        raise HTTPException(400, "Через API поддерживается только отмена (status=cancelled)")

    # Сначала проверяем ВСЁ, пишем одним запросом: иначе правка «провайдер +
    # приоритет» с опечаткой в провайдере успевала применить приоритет и
    # вернуть 400 — половина применена, а человеку сказано «не вышло».
    fields = {}
    if update.priority is not None:
        fields["priority"] = update.priority
    if update.provider is not None:
        provider = update.provider.strip()
        if provider and provider not in load_providers():
            raise HTTPException(400, f"Провайдер «{provider}» не найден")
        fields["provider"] = provider or None
    if update.model is not None:
        fields["model"] = update.model.strip() or None
    if update.effort is not None:
        effort = update.effort.strip().lower()
        if effort and effort not in EFFORT_LEVELS:
            raise HTTPException(400, f"Эффорт: {', '.join(EFFORT_LEVELS)} или пусто")
        fields["effort"] = effort or None
    if update.recurrence is not None:
        recurrence = update.recurrence.strip()
        if recurrence and db.parse_recurrence(recurrence) is None:
            raise HTTPException(400, "Повтор не разобран: «6h», «90m», «daily@09:00»")
        fields["recurrence"] = recurrence or None
    if update.scheduled_at is not None:
        fields["scheduled_at"] = update.scheduled_at
    if update.working_dir is not None:
        fields["working_dir"] = update.working_dir.strip() or None
    if fields and not db.update_task_fields(task_id, fields):
        raise HTTPException(400, "Править можно только задачу в очереди (pending/rate_limited)")

    return {"ok": True}


@app.get("/api/schedule")
def api_schedule():
    """Повторяющиеся задачи как серии: период, следующий запуск, исход прошлого."""
    return db.list_series()


@app.delete("/api/tasks/{task_id}", response_model=dict)
def api_delete_task(task_id: int):
    if not db.delete_task(task_id):
        raise HTTPException(404, "Task not found")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/reset", response_model=dict)
def api_reset_task(task_id: int):
    if not db.reset_task(task_id):
        raise HTTPException(400, "Task not found or not in running state")
    return {"ok": True}


@app.get("/api/stats", response_model=Stats)
def api_stats():
    return db.get_stats()


@app.get("/api/stats/costs", response_model=CostStats)
def api_cost_stats():
    return db.get_cost_stats()


class NoteBody(BaseModel):
    text: str = ""


@app.post("/api/tasks/{task_id}/note")
def api_set_note(task_id: int, body: NoteBody):
    """Дописать решателю. Пустой текст убирает приписку.

    Живёт при задаче, а не при прогоне: идущий прогон её уже не увидит, зато
    увидит следующий — в том числе повтор после rate limit или срыва среды.
    """
    if not db.set_note(task_id, body.text):
        raise HTTPException(404, "Задача не найдена")
    return {"ok": True, "note": body.text or None}


# --- Workflow orchestrator W0/W1 ------------------------------------------


@app.post("/api/workflows", response_model=WorkflowInDB, status_code=201)
def api_create_workflow(workflow: WorkflowCreate):
    try:
        return db.create_workflow(workflow)
    except db.WorkflowConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/workflows", response_model=List[WorkflowInDB])
def api_list_workflows(
    status: Optional[WorkflowStatus] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return db.list_workflows(
        status=status.value if status else None,
        limit=limit,
        offset=offset,
    )


@app.get("/api/workflows/{workflow_id}", response_model=WorkflowInDB)
def api_get_workflow(workflow_id: str):
    workflow = db.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(404, "Workflow not found")
    return workflow


@app.patch("/api/workflows/{workflow_id}", response_model=WorkflowInDB)
def api_update_workflow(workflow_id: str, update: WorkflowUpdate):
    try:
        updated = db.update_workflow(workflow_id, update)
        return workflows.advance_workflow(workflow_id)
    except db.WorkflowNotFoundError as exc:
        raise HTTPException(404, "Workflow not found") from exc
    except db.WorkflowConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get(
    "/api/workflows/{workflow_id}/rounds",
    response_model=List[WorkflowRoundInDB],
)
def api_list_workflow_rounds(workflow_id: str):
    if not db.get_workflow(workflow_id):
        raise HTTPException(404, "Workflow not found")
    return db.list_workflow_rounds(workflow_id)


@app.get(
    "/api/workflows/{workflow_id}/rounds/{round_id}/runs",
    response_model=List[WorkflowRunInDB],
)
def api_list_workflow_runs(workflow_id: str, round_id: str):
    round_data = db.get_workflow_round(round_id)
    if not round_data or round_data.workflow_id != workflow_id:
        raise HTTPException(404, "Workflow round not found")
    return db.list_workflow_runs(round_id)


@app.get(
    "/api/workflows/{workflow_id}/events",
    response_model=List[WorkflowEventInDB],
)
def api_list_workflow_events(
    workflow_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
):
    if not db.get_workflow(workflow_id):
        raise HTTPException(404, "Workflow not found")
    return db.list_workflow_events(workflow_id, after_seq=after_seq, limit=limit)


@app.get(
    "/api/workflows/{workflow_id}/findings",
    response_model=List[WorkflowFindingInDB],
)
def api_list_workflow_findings(
    workflow_id: str,
    status: Optional[FindingStatus] = None,
):
    if not db.get_workflow(workflow_id):
        raise HTTPException(404, "Workflow not found")
    return db.list_workflow_findings(
        workflow_id, status=status.value if status else None
    )


@app.get(
    "/api/workflows/{workflow_id}/artifacts",
    response_model=List[WorkflowArtifactInDB],
)
def api_list_workflow_artifacts(
    workflow_id: str,
    round_id: Optional[str] = None,
):
    if not db.get_workflow(workflow_id):
        raise HTTPException(404, "Workflow not found")
    if round_id:
        round_data = db.get_workflow_round(round_id)
        if not round_data or round_data.workflow_id != workflow_id:
            raise HTTPException(404, "Workflow round not found")
    return db.list_workflow_artifacts(workflow_id, round_id=round_id)


def _workflow_action(call, *args):
    try:
        return call(*args)
    except db.WorkflowNotFoundError as exc:
        raise HTTPException(404, "Workflow not found") from exc
    except db.WorkflowConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post(
    "/api/workflows/{workflow_id}/start",
    response_model=WorkflowInDB,
)
def api_start_workflow(workflow_id: str, request: WorkflowStartRequest):
    started = _workflow_action(workflows.start_workflow, workflow_id, request)
    return workflows.advance_workflow(started.id)


@app.post(
    "/api/workflows/{workflow_id}/dispatch",
    response_model=WorkflowDispatchResult,
)
def api_dispatch_workflow_task(
    workflow_id: str, dispatch: WorkflowTaskDispatch
):
    if dispatch.provider and dispatch.provider not in load_providers():
        raise HTTPException(400, f"Неизвестный провайдер «{dispatch.provider}»")
    return _workflow_action(workflows.dispatch_task, workflow_id, dispatch)


@app.post(
    "/api/workflows/{workflow_id}/gate",
    response_model=WorkflowInDB,
)
def api_record_workflow_gate(
    workflow_id: str, decision: WorkflowGateDecision
):
    return _workflow_action(workflows.record_gate, workflow_id, decision)


@app.post(
    "/api/workflows/{workflow_id}/review",
    response_model=WorkflowInDB,
)
def api_record_workflow_review(
    workflow_id: str, decision: WorkflowReviewDecision
):
    return _workflow_action(workflows.record_review, workflow_id, decision)


@app.post(
    "/api/workflows/{workflow_id}/human-input",
    response_model=WorkflowInDB,
)
def api_workflow_human_input(
    workflow_id: str, action: WorkflowHumanInput
):
    return _workflow_action(workflows.human_input, workflow_id, action)


@app.post(
    "/api/workflows/{workflow_id}/cancel",
    response_model=WorkflowInDB,
)
def api_cancel_workflow(workflow_id: str, request: WorkflowVersionRequest):
    return _workflow_action(
        workflows.cancel_workflow, workflow_id, request.expected_version
    )


@app.post("/api/workflows/{workflow_id}/sync")
def api_sync_workflow(workflow_id: str):
    if not db.get_workflow(workflow_id):
        raise HTTPException(404, "Workflow not found")
    return {
        "ok": True,
        "runs_synced": workflows.sync_all_tasks(workflow_id),
        "workflow": db.get_workflow(workflow_id),
    }


@app.post(
    "/api/workflows/{workflow_id}/advance",
    response_model=WorkflowInDB,
)
def api_advance_workflow(workflow_id: str):
    return _workflow_action(workflows.advance_workflow, workflow_id)


@app.get("/api/workflows/{workflow_id}/report")
def api_workflow_report(
    workflow_id: str,
    format: str = Query(default="json", pattern="^(json|markdown)$"),
):
    try:
        if format == "markdown":
            return Response(
                content=workflows.workflow_report_markdown(workflow_id),
                media_type="text/markdown; charset=utf-8",
                headers={
                    "Content-Disposition":
                    f'attachment; filename="workflow-{workflow_id}.md"'
                },
            )
        return workflows.workflow_report(workflow_id)
    except db.WorkflowNotFoundError as exc:
        raise HTTPException(404, "Workflow not found") from exc


@app.post(
    "/api/workflows/{workflow_id}/history/import",
    response_model=WorkflowInDB,
)
def api_import_workflow_history(
    workflow_id: str, history: WorkflowHistoryImport
):
    return _workflow_action(workflows.import_history, workflow_id, history)


# --- Вложения к задачам ---

UPLOADS_DIR = DB_DIR / "uploads"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@app.post("/api/upload")
async def api_upload(files: List[UploadFile] = File(...)):
    """Принять вложения (файлы/скриншоты) для будущей задачи.

    Файлы ложатся в ~/.promptpilot/uploads под случайными именами — имя клиента
    в путь не попадает, а наружу каталог не раздаётся: агент читает вложения по
    абсолютному пути, который фронт дописывает в промпт. Отказ по любому файлу
    убирает и уже сохранённые — либо весь набор, либо ничего.
    """
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    try:
        for up in files:
            ext = Path(up.filename or "").suffix
            if not _re.fullmatch(r"[A-Za-z0-9.]{1,10}", ext):
                ext = ""
            dest = UPLOADS_DIR / f"{uuid.uuid4().hex}{ext}"
            size = 0
            try:
                with open(dest, "wb") as out:
                    # потоково: сам FastAPI размер тела не ограничивает
                    while chunk := await up.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise HTTPException(413, f"«{up.filename}» больше 20 МБ")
                        out.write(chunk)
            except HTTPException:
                dest.unlink(missing_ok=True)
                raise
            saved.append({"path": str(dest), "name": up.filename or dest.name, "size": size})
    except HTTPException:
        for s in saved:
            Path(s["path"]).unlink(missing_ok=True)
        raise
    return saved


# --- Экран агента herdr-задачи ---

# РОВНО эти клавиши: подтвердить/выбрать один из четырёх вариантов в диалоге агента. Произвольный
# ввод и обращение по сырому pane_id — сознательно не в V1: только через id
# задачи, чтобы не открывать все панели машины наружу.
HERDR_UI_KEYS = ("enter", "1", "2", "3", "4", "esc")
SCREEN_TAIL_LINES = 30


class KeyBody(BaseModel):
    key: str


def _herdr_task_pane(task_id: int):
    """(pane, host) herdr-задачи; 404, если у задачи нет панели."""
    from .config import load_machines, machine_remote
    task = db.get_task(task_id)
    if not task or not task.herdr_pane:
        raise HTTPException(404, "У задачи нет herdr-панели")
    host = None
    if task.machine:
        m = load_machines().get(task.machine)
        if not m or not m.get("host"):
            raise HTTPException(404, "Машина задачи не найдена в реестре")
        host = machine_remote(m)
    return task.herdr_pane, host


def screen_tail(pane: str, host=None, lines_n: int = SCREEN_TAIL_LINES) -> str:
    """Хвост видимого экрана панели (как 📺 в боте, но для веб-карточки)."""
    from .herdr_exec import _run
    rc, _, raw = _run(["agent", "read", pane, "--source", "visible",
                       "--format", "text"], host=host, timeout=20)
    if rc != 0:
        raise HTTPException(502, f"herdr agent read failed — панель закрыта? ({raw[:200]})")
    lines = [l.rstrip() for l in raw.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines[-lines_n:])


@app.get("/api/tasks/{task_id}/screen")
def api_task_screen(task_id: int):
    from .herdr_exec import HerdrError, _agent_status, _run
    pane, host = _herdr_task_pane(task_id)
    try:
        text = screen_tail(pane, host)
        rc, data, _ = _run(["agent", "get", pane], host=host, timeout=20)
        status = _agent_status(data) if rc == 0 else ""
    except HerdrError as e:
        raise HTTPException(502, str(e))
    return {"text": text, "agent_status": status}


@app.post("/api/tasks/{task_id}/keys")
def api_task_keys(task_id: int, body: KeyBody):
    from .herdr_exec import HerdrError, _run
    key = body.key.strip().lower()
    if key not in HERDR_UI_KEYS:
        raise HTTPException(400, f"Клавиша не поддерживается (можно: {', '.join(HERDR_UI_KEYS)})")
    pane, host = _herdr_task_pane(task_id)
    try:
        rc, data, raw = _run(["agent", "send-keys", pane, key], host=host, timeout=20)
    except HerdrError as e:
        raise HTTPException(502, str(e))
    if rc != 0 or not (data or {}).get("result"):
        raise HTTPException(502, f"Не удалось отправить — агент ещё существует? ({raw[:200]})")
    return {"ok": True, "key": key}


@app.get("/api/stats/usage")
def api_usage(hours: float = 5.0):
    """Расход за окно лимита по ВСЕМ сессиям Claude Code на машине.

    Отдельно от /stats/costs: тот считает по результатам задач и herdr-задач не
    видит вовсе. Разбор транскриптов идёт синхронно, но по mtime отсеиваются все
    файлы вне окна — на сотнях сессий это доли секунды.
    """
    from .usage import summary
    try:
        return summary(hours)
    except Exception as e:  # дашборд не должен падать из-за чужого журнала
        return {"error": f"{type(e).__name__}: {e}", "cost": 0, "sessions": 0}


@app.get("/api/worker/status")
def api_worker_status():
    return {"paused": db.is_paused()}


@app.post("/api/worker/pause")
def api_worker_pause():
    db.set_setting("worker_paused", "1")
    return {"ok": True, "paused": True}


@app.post("/api/worker/resume")
def api_worker_resume():
    db.set_setting("worker_paused", "0")
    return {"ok": True, "paused": False}


@app.get("/api/version")
def api_version():
    return check_for_update()


@app.get("/api/providers")
def api_providers():
    providers = load_providers()
    return {
        name: {
            "description": info.get("description", name),
            "supports_skills": info.get("supports_skills", False),
            # Dynamic discovery for Claude-type providers (cached); falls back to
            # the provider's own list or the sonnet/opus/haiku tiers.
            "models": get_provider_models(name),
            # Эффорт провайдера — дефолт, который мастер показывает как
            # «по умолчанию» и который задача может перекрыть.
            "effort": info.get("effort", ""),
            "available": provider_available(info),
            "hidden": bool(info.get("hidden")),
            "executor": info.get("executor", ""),
            "session_target": bool(info.get("session_target")),
        }
        for name, info in providers.items()
    }


@app.get("/api/herdr/agents")
def api_herdr_agents(machine: str = ""):
    """Live herdr agents for the session-target picker (locally or on a machine)."""
    import json as _json
    import subprocess as _sp
    from .config import load_machines, machine_remote
    from .herdr_exec import herdr_argv

    host = None
    if machine:
        m = load_machines().get(machine)
        if not m or not m.get("host"):
            raise HTTPException(404, "Машина не найдена")
        host = machine_remote(m)

    def _cli_json(*args):
        try:
            proc = _sp.run(herdr_argv(args, host), capture_output=True, text=True,
                           timeout=20, stdin=_sp.DEVNULL)
            return _json.loads(proc.stdout.strip() or "{}")
        except (OSError, ValueError, _sp.TimeoutExpired):
            return {}

    data = _cli_json("agent", "list")
    agents = ((data.get("result") or {}).get("agents")) or []
    ws = _cli_json("workspace", "list")
    ws_labels = {w.get("workspace_id"): w.get("label") or w.get("workspace_id")
                 for w in ((ws.get("result") or {}).get("workspaces")) or []}
    return [
        {
            "target": a.get("name") or a.get("pane_id"),
            "pane_id": a.get("pane_id"),
            "name": a.get("name"),
            "agent": a.get("display_agent") or a.get("agent") or "",
            "status": a.get("agent_status"),
            "cwd": a.get("cwd") or "",
            "title": (a.get("terminal_title_stripped") or "")[:80],
            "workspace": ws_labels.get(a.get("workspace_id"), a.get("workspace_id") or ""),
        }
        for a in agents
        if a.get("pane_id")
    ]


@app.get("/api/machines")
def api_machines():
    from .config import load_machines
    return load_machines()


from pydantic import BaseModel as _PydanticBase


class MachineCreate(_PydanticBase):
    name: str
    host: str


@app.post("/api/machines", status_code=201)
def api_machine_create(m: MachineCreate):
    from .config import probe_machine, save_machine
    import re as __re
    if not __re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", m.name):
        raise HTTPException(400, "Имя: латиница/цифры/-/_/. до 32 символов")
    # A host starting with '-' is read by ssh as an option: '-oProxyCommand=...'
    # would run a local command on the first probe. Enforce [user@]host[:port].
    host = m.host.strip()
    if host.startswith("-") or not __re.fullmatch(r"[A-Za-z0-9._@:\-]{1,255}", host):
        raise HTTPException(400, "Некорректный хост (ожидается [user@]host[:port])")
    m.host = host
    providers, shell = probe_machine(m.host)
    if not shell:
        raise HTTPException(400, "Машина недоступна по ssh (проверь ключи и BatchMode)")
    if not providers:
        raise HTTPException(400, "На машине не найдено ни одного известного CLI")
    save_machine(m.name, m.host, providers, shell)
    return {"ok": True, "providers": providers, "shell": shell}


@app.post("/api/machines/{name}/probe")
def api_machine_probe(name: str):
    from .config import load_machines, probe_machine, save_machine
    machine = load_machines().get(name)
    if not machine:
        raise HTTPException(404, "Машина не найдена")
    providers, shell = probe_machine(machine["host"])
    if not shell:
        raise HTTPException(400, "Машина недоступна по ssh (проверь ключи и BatchMode)")
    save_machine(name, machine["host"], providers, shell)
    return {"ok": True, "providers": providers, "shell": shell}


@app.delete("/api/machines/{name}")
def api_machine_delete(name: str):
    from .config import remove_machine
    if not remove_machine(name):
        raise HTTPException(404, "Машина не найдена")
    return {"ok": True}


from pydantic import BaseModel as _BaseModel


class ProviderCreate(_BaseModel):
    name: str
    source_name: Optional[str] = None  # provider the edit form was opened from
    description: str = ""
    cmd: Optional[str] = None
    executor: Optional[str] = None  # "herdr"
    kind: Optional[str] = None
    keep_pane: bool = False
    env: dict = {}
    models: list = []
    args: list = []
    effort: Optional[str] = None


@app.get("/api/providers/manage")
def api_providers_manage():
    """Full provider info for the settings UI."""
    from .config import load_providers_detailed
    providers = load_providers_detailed()
    return {
        name: {
            "description": info.get("description", ""),
            "cmd": info.get("cmd", ""),
            "executor": info.get("executor", ""),
            "kind": info.get("kind", ""),
            "keep_pane": bool(info.get("keep_pane")),
            "models": info.get("models") or [],
            "args": info.get("args") or [],
            "effort": info.get("effort", ""),
            "supports_skills": info.get("supports_skills", False),
            "env": {k: mask_secret_value(k, v) for k, v in (info.get("env") or {}).items()},
            "available": provider_available(info),
            "hidden": bool(info.get("hidden")),
            "source": info.get("_source", "builtin"),
        }
        for name, info in providers.items()
    }


@app.post("/api/providers", status_code=201)
def api_provider_create(p: ProviderCreate):
    from .config import save_provider
    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", p.name):
        raise HTTPException(400, "Имя: латиница/цифры/-/_, до 32 символов")
    # "***" — оставленный без изменений секрет: берём сохранённое значение
    # (из провайдера-источника при копировании, иначе из одноимённого)
    if p.env:
        stored = load_providers().get(p.source_name or p.name, {}).get("env", {})
        resolved = {}
        for k, v in p.env.items():
            if v == "***":
                if not stored.get(k):
                    raise HTTPException(400, f"Секрет {k} не найден — введите значение вместо ***")
                resolved[k] = stored[k]
            else:
                resolved[k] = v
        p.env = resolved
    if p.executor:
        if p.executor != "herdr":
            raise HTTPException(400, "Поддерживаемый executor: herdr")
        save_provider(p.name, description=p.description, env=p.env or None,
                      executor=p.executor, kind=p.kind, keep_pane=p.keep_pane,
                      models=p.models or None, args=p.args or None, effort=p.effort)
    else:
        if not p.cmd or "{prompt}" not in p.cmd:
            raise HTTPException(400, "cmd обязателен и должен содержать {prompt}")
        # Остальные флаги cmd-провайдера живут в шаблоне, а эффорт — поле:
        # он единственный, который приходится менять от этапа к этапу.
        save_provider(p.name, p.cmd, p.description, env=p.env or None,
                      models=p.models or None, effort=p.effort)
    return {"ok": True}


@app.delete("/api/providers/{name}")
def api_provider_delete(name: str):
    from .config import remove_provider
    if not remove_provider(name):
        raise HTTPException(404, "Провайдер не найден среди кастомных (встроенные можно только скрыть)")
    return {"ok": True}


@app.post("/api/providers/{name}/hide")
def api_provider_hide(name: str):
    from .config import set_provider_hidden
    if not set_provider_hidden(name, True):
        raise HTTPException(404, "Провайдер не найден")
    return {"ok": True}


@app.post("/api/providers/{name}/unhide")
def api_provider_unhide(name: str):
    from .config import set_provider_hidden
    if not set_provider_hidden(name, False):
        raise HTTPException(404, "Провайдер не найден")
    return {"ok": True}


@app.get("/api/skills")
def api_skills(provider: Optional[str] = None, workdir: Optional[str] = None):
    """Return available Claude Code skills. Empty list if provider doesn't support skills."""
    if provider is not None:
        providers = load_providers()
        if not providers.get(provider, {}).get("supports_skills", False):
            return []
    return get_skills(working_dir=workdir)


@app.get("/api/projects")
def api_projects():
    """Return sorted list of {name, path, git} for subdirs under PP_PROJECTS_ROOT.

    `git` tells the UI whether "own worktree" is even offerable for that project.
    """
    if not PROJECTS_ROOT:
        return []
    try:
        entries = []
        for d in sorted(os.listdir(PROJECTS_ROOT)):
            full = os.path.join(PROJECTS_ROOT, d)
            if os.path.isdir(full) and not d.startswith("."):
                entries.append({"name": d, "path": full,
                                "git": os.path.exists(os.path.join(full, ".git"))})
        return entries
    except OSError:
        return []


# --- Frontend ---

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
