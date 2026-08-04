"""FastAPI web API + static file serving."""

import base64
import secrets
import sys
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import os

from . import db
from .config import API_TOKEN, get_skills, load_providers, provider_available, PROJECTS_ROOT
from .models import CostStats, Stats, TaskCreate, TaskInDB, TaskStatus, TaskUpdate
from .version import check_for_update

app = FastAPI(title="PromptPilot", version="0.1.0")


@app.middleware("http")
async def _auth(request, call_next):
    """Optional auth: enabled by PP_API_TOKEN. Accepts Bearer <token> or
    HTTP Basic with the token as password (browser shows a native prompt)."""
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
        if task.status == TaskStatus.RUNNING:
            # running: ask the worker to kill the process (it polls every ~2s)
            if not db.request_cancel(task_id):
                raise HTTPException(400, "Task is no longer running")
        elif not db.cancel_task(task_id):
            raise HTTPException(400, "Can only cancel pending, rate_limited or running tasks")

    if update.priority is not None:
        if not db.update_priority(task_id, update.priority):
            raise HTTPException(400, "Can only reprioritize pending or rate_limited tasks")

    return {"ok": True}


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
    default_models = ["sonnet", "opus", "haiku"]
    return {
        name: {
            "description": info.get("description", name),
            "supports_skills": info.get("supports_skills", False),
            "models": info.get("models", default_models if info.get("supports_skills") else []),
            "available": provider_available(info),
            "hidden": bool(info.get("hidden")),
        }
        for name, info in providers.items()
    }


import re as _re

from pydantic import BaseModel as _BaseModel


class ProviderCreate(_BaseModel):
    name: str
    description: str = ""
    cmd: Optional[str] = None
    executor: Optional[str] = None  # "herdr"
    kind: Optional[str] = None
    keep_pane: bool = False
    env: dict = {}
    models: list = []
    args: list = []


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
            "supports_skills": info.get("supports_skills", False),
            "env": {k: ("***" if any(s in k.upper() for s in ("TOKEN", "KEY", "SECRET")) else v)
                    for k, v in (info.get("env") or {}).items()},
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
    if p.executor:
        if p.executor != "herdr":
            raise HTTPException(400, "Поддерживаемый executor: herdr")
        save_provider(p.name, description=p.description, env=p.env or None,
                      executor=p.executor, kind=p.kind, keep_pane=p.keep_pane,
                      models=p.models or None, args=p.args or None)
    else:
        if not p.cmd or "{prompt}" not in p.cmd:
            raise HTTPException(400, "cmd обязателен и должен содержать {prompt}")
        save_provider(p.name, p.cmd, p.description, env=p.env or None,
                      models=p.models or None)  # for cmd providers flags live in cmd
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
    """Return sorted list of {name, path} for subdirs under PP_PROJECTS_ROOT."""
    if not PROJECTS_ROOT:
        return []
    try:
        entries = []
        for d in sorted(os.listdir(PROJECTS_ROOT)):
            full = os.path.join(PROJECTS_ROOT, d)
            if os.path.isdir(full) and not d.startswith("."):
                entries.append({"name": d, "path": full})
        return entries
    except OSError:
        return []


# --- Frontend ---

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
