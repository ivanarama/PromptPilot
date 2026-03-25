"""FastAPI web API + static file serving."""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import load_providers
from .models import Stats, TaskCreate, TaskInDB, TaskStatus, TaskUpdate

app = FastAPI(title="PromptPilot", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"


# --- API ---

@app.get("/api/tasks", response_model=list[TaskInDB])
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
        if not db.cancel_task(task_id):
            raise HTTPException(400, "Can only cancel pending or rate_limited tasks")

    if update.priority is not None:
        if not db.update_priority(task_id, update.priority):
            raise HTTPException(400, "Can only reprioritize pending or rate_limited tasks")

    return {"ok": True}


@app.delete("/api/tasks/{task_id}", response_model=dict)
def api_delete_task(task_id: int):
    if not db.delete_task(task_id):
        raise HTTPException(404, "Task not found")
    return {"ok": True}


@app.get("/api/stats", response_model=Stats)
def api_stats():
    return db.get_stats()


@app.get("/api/providers")
def api_providers():
    providers = load_providers()
    return {name: info.get("description", name) for name, info in providers.items()}


# --- Frontend ---

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
