"""FastAPI web API + static file serving."""

import asyncio
import base64
import secrets
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel

import os
import re as _re

from . import db, epf_tools, workflows
from . import pipeline_insights
from .config import API_TOKEN, DB_DIR, EFFORT_LEVELS, PIPELINE_SNAPSHOT_INTERVAL, get_provider_models, get_skills, load_providers, mask_secret_value, provider_available, PROJECTS_ROOT
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
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
    WorkflowPlanInDB,
    WorkflowPlanReplace,
    WorkflowDispatchResult,
    WorkflowGateDecision,
    WorkflowHistoryImport,
    WorkflowHumanInput,
    WorkflowReviewDecision,
    WorkflowRoundInDB,
    WorkflowRunInDB,
    WorkflowStageInDB,
    WorkflowStartRequest,
    WorkflowStatus,
    WorkflowSetupCheck,
    WorkflowSetupValidationRequest,
    WorkflowSetupValidationResponse,
    WorkflowTaskDispatch,
    WorkflowUpdate,
    WorkflowVersionRequest,
)
from .version import check_for_update


def _sample_pipeline_profiles():
    """Run the complete snapshot read outside the HTTP event loop.

    ``asyncio.to_thread`` only moves evaluation of its callable and arguments.
    Passing ``db.list_series()`` as an argument evaluated the SQLite read on
    the event-loop thread first, so slow storage could freeze every API route
    before the sampler even reached its worker thread.
    """
    return pipeline_insights.sample_active_profiles(db.list_series())


async def _pipeline_sampler():
    """Collect profile-scoped queue history without invoking an LLM."""
    while True:
        await asyncio.sleep(PIPELINE_SNAPSHOT_INTERVAL)
        try:
            await asyncio.to_thread(_sample_pipeline_profiles)
        except Exception as exc:
            print(f"pipeline sampler: {exc}", file=sys.stderr)


@asynccontextmanager
async def _lifespan(application: FastAPI):
    task = None
    if PIPELINE_SNAPSHOT_INTERVAL:
        task = asyncio.create_task(_pipeline_sampler())
        application.state.pipeline_sampler = task
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="PromptPilot", version="0.1.0", lifespan=_lifespan)


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

# PyInstaller one-file builds unpack bundled data into a temporary directory.
# macOS may clean an old extraction directory while a launchd service is still
# running, so opening index.html lazily on every request eventually turns the
# otherwise healthy API into HTTP 500. Keep the small single-file UI in memory
# for the lifetime of the server; a broken package now also fails at startup
# instead of surfacing only when somebody opens the dashboard.
_INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# --- API ---

def _task_with_epf(task: TaskInDB) -> dict:
    """TaskInDB + карточка 1С-доработки (epf_jobs), если задача из /epf."""
    d = task.model_dump()
    job = db.get_epf_job_by_task(task.id)
    if job:
        d["epf"] = {
            "base_label": job["base_label"],
            "status": job["status"],
            "epf_path": job["epf_path"] or "",
        }
    return d


@app.get("/api/tasks")
def api_list_tasks(status: Optional[TaskStatus] = None, limit: int = 50, offset: int = 0):
    return [_task_with_epf(t) for t in db.list_tasks(status=status, limit=limit, offset=offset)]


@app.post("/api/tasks", response_model=TaskInDB, status_code=201)
def api_create_task(task: TaskCreate):
    # Allowlist providers: an unknown name would otherwise be turned into a raw
    # command by build_cmd's fallback (provider='touch x' → run `touch x`). The
    # UI only ever offers registered providers, so this rejects nothing real.
    if task.provider and task.provider not in load_providers():
        raise HTTPException(400, f"Неизвестный провайдер «{task.provider}»")
    return db.create_task(task)


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return _task_with_epf(task)


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


class SeriesUpdate(BaseModel):
    title: Optional[str] = None
    recurrence: Optional[str] = None
    temporary_recurrence: Optional[str] = None
    temporary_until: Optional[datetime] = None
    temporary_empty_limit: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    priority: Optional[int] = None
    task_timeout: Optional[int] = None


class SeriesAction(BaseModel):
    action: str


class PipelinePriorityUpdate(BaseModel):
    level: str
    run_now: bool = False


@app.patch("/api/schedule/{series_id}")
def api_update_series(series_id: int, update: SeriesUpdate):
    fields = {}
    supplied = update.model_fields_set
    if "title" in supplied:
        fields["title"] = (update.title or "").strip() or "Повторяющаяся задача"
    if "recurrence" in supplied:
        recurrence = (update.recurrence or "").strip()
        if not recurrence or db.parse_recurrence(recurrence) is None:
            raise HTTPException(400, "Повтор не разобран: «6h», «90m», «daily@09:00»")
        fields["base_recurrence"] = recurrence
    if "temporary_recurrence" in supplied:
        temporary = (update.temporary_recurrence or "").strip()
        if temporary and db.parse_recurrence(temporary) is None:
            raise HTTPException(400, "Временный интервал не разобран")
        fields["temporary_recurrence"] = temporary or None
    if "temporary_until" in supplied:
        fields["temporary_until"] = update.temporary_until
    if "temporary_empty_limit" in supplied:
        if update.temporary_empty_limit is not None and not 1 <= update.temporary_empty_limit <= 20:
            raise HTTPException(400, "Порог ПУСТО: от 1 до 20")
        fields["temporary_empty_limit"] = update.temporary_empty_limit
    if "provider" in supplied:
        provider = (update.provider or "").strip()
        if provider and provider not in load_providers():
            raise HTTPException(400, f"Провайдер «{provider}» не найден")
        fields["provider"] = provider or None
    if "model" in supplied:
        fields["model"] = (update.model or "").strip() or None
    if "effort" in supplied:
        effort = (update.effort or "").strip().lower()
        if effort and effort not in EFFORT_LEVELS:
            raise HTTPException(400, f"Эффорт: {', '.join(EFFORT_LEVELS)} или пусто")
        fields["effort"] = effort or None
    if "priority" in supplied:
        if update.priority is None or not 1 <= update.priority <= 10:
            raise HTTPException(400, "Приоритет: от 1 до 10")
        fields["priority"] = update.priority
    if "task_timeout" in supplied:
        if update.task_timeout is not None and update.task_timeout < 0:
            raise HTTPException(400, "Таймаут не может быть отрицательным")
        fields["task_timeout"] = update.task_timeout
    if not fields or not db.update_series(series_id, fields):
        raise HTTPException(404, "Активная серия не найдена")
    return db.get_series(series_id)


@app.post("/api/schedule/{series_id}/action")
def api_series_action(series_id: int, body: SeriesAction):
    if body.action not in ("run_now", "pause", "resume", "end"):
        raise HTTPException(400, "Неизвестное действие")
    if not db.series_action(series_id, body.action):
        raise HTTPException(400, "Действие неприменимо к этой серии")
    return {"ok": True, "series": db.get_series(series_id)}


@app.get("/api/pipeline-insights/profiles")
def api_pipeline_profiles():
    return pipeline_insights.list_profiles()


@app.get("/api/pipeline-insights/{profile_id}/report")
def api_pipeline_report(
        profile_id: str,
        hours: int = Query(24),
        refresh: bool = False,
        format: str = Query("json", pattern="^(json|markdown)$")):
    try:
        report = pipeline_insights.build_period_report(
            profile_id, db.list_series(), hours=hours,
            refresh_delivery=refresh)
        if format == "markdown":
            return Response(
                content=pipeline_insights.render_period_report_markdown(report),
                media_type="text/markdown")
        return report
    except KeyError:
        raise HTTPException(404, "Профиль анализа конвейера не найден")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (RuntimeError, OSError) as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/pipeline-insights/{profile_id}")
def api_pipeline_insights(profile_id: str, refresh: bool = False):
    try:
        series = db.list_series()
        if not refresh:
            return pipeline_insights.read_cached(profile_id, series)
        return pipeline_insights.analyze(
            profile_id, series, use_cache=False, refresh_diagnostics=True)
    except KeyError:
        raise HTTPException(404, "Профиль анализа не найден")
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/pipeline-insights/{profile_id}/queues/{queue_id}/items/{kind}/{number}/priority")
def api_pipeline_item_priority(profile_id: str, queue_id: str, kind: str, number: int,
                               body: PipelinePriorityUpdate):
    try:
        return pipeline_insights.set_item_priority(
            profile_id, queue_id, kind, number, body.level.lower(), body.run_now,
            db.list_series(),
        )
    except KeyError:
        raise HTTPException(404, "Профиль анализа не найден")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (RuntimeError, OSError) as exc:
        raise HTTPException(503, str(exc))


@app.delete("/api/tasks/{task_id}", response_model=dict)
def api_delete_task(task_id: int):
    task = db.get_task(task_id)
    if task and task.status.value == "running":
        raise HTTPException(409, "Cancel the running task and wait for it to stop first")
    if not db.delete_task(task_id):
        raise HTTPException(404, "Task not found")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/reset", response_model=dict)
def api_reset_task(task_id: int):
    if not db.reset_task(task_id):
        task = db.get_task(task_id)
        if (task and task.status.value == "running"
                and db.task_has_live_pipeline_target_reservation(task_id)):
            raise HTTPException(
                409,
                "This pipeline task still owns a live provider/target; cancel it "
                "and wait for cleanup before resetting",
            )
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


def _setup_check(code: str, status: str, message: str) -> WorkflowSetupCheck:
    return WorkflowSetupCheck(code=code, status=status, message=message)


def _validate_gate_syntax(command: str) -> tuple[bool, str]:
    """Parse one gate command without executing it."""
    try:
        if os.name == "nt":
            env = os.environ.copy()
            env["PP_GATE_VALIDATE_COMMAND"] = command
            script = (
                "$tokens=$null; $errors=$null; "
                "[System.Management.Automation.Language.Parser]::ParseInput("
                "$env:PP_GATE_VALIDATE_COMMAND,[ref]$tokens,[ref]$errors) | Out-Null; "
                "if($errors.Count){$errors | ForEach-Object {$_.Message}; exit 1}"
            )
            parsed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=5, errors="replace", env=env,
            )
        else:
            parsed = subprocess.run(
                ["/bin/sh", "-n", "-c", command], capture_output=True,
                text=True, timeout=5, errors="replace",
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"не удалось запустить parser: {exc}"
    detail = ((parsed.stdout or "") + (parsed.stderr or "")).strip()
    return parsed.returncode == 0, detail


@app.post(
    "/api/workflows/validate-setup",
    response_model=WorkflowSetupValidationResponse,
)
def api_validate_workflow_setup(request: WorkflowSetupValidationRequest):
    """Validate repository, branch, providers and gate syntax without mutation."""
    checks: list[WorkflowSetupCheck] = []
    repo = Path(request.repository_path).expanduser()
    if not repo.exists():
        checks.append(_setup_check("repository", "error", "Каталог репозитория не найден"))
    elif not repo.is_dir():
        checks.append(_setup_check("repository", "error", "Путь не является каталогом"))
    else:
        try:
            git = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5, errors="replace",
            )
            if git.returncode:
                checks.append(_setup_check(
                    "repository", "error", "Каталог не является доступным Git-репозиторием",
                ))
            else:
                checks.append(_setup_check(
                    "repository", "ok", f"Git-репозиторий: {git.stdout.strip()}",
                ))
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append(_setup_check("repository", "error", f"Git недоступен: {exc}"))

    try:
        branch = subprocess.run(
            ["git", "check-ref-format", "--branch", request.candidate_branch],
            capture_output=True, text=True, timeout=5, errors="replace",
        )
        checks.append(_setup_check(
            "branch", "ok" if branch.returncode == 0 else "error",
            (f"Допустимое имя ветки: {request.candidate_branch}"
             if branch.returncode == 0 else "Недопустимое имя Git-ветки"),
        ))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(_setup_check("branch", "error", f"Git недоступен: {exc}"))

    configured = load_providers()
    for name in dict.fromkeys(request.providers):
        info = configured.get(name)
        if not info:
            checks.append(_setup_check("provider", "error", f"Провайдер «{name}» не настроен"))
        elif not provider_available(info):
            checks.append(_setup_check("provider", "error", f"Провайдер «{name}» недоступен"))
        else:
            checks.append(_setup_check("provider", "ok", f"Провайдер «{name}» доступен"))

    if not request.gate_commands:
        checks.append(_setup_check(
            "gate", "warning", "Gate-команды не заданы: функциональная готовность не проверяется",
        ))
    for index, command in enumerate(request.gate_commands, start=1):
        ok, detail = _validate_gate_syntax(command)
        checks.append(_setup_check(
            "gate", "ok" if ok else "error",
            (f"Gate #{index}: синтаксис корректен"
             if ok else f"Gate #{index}: {detail or 'ошибка синтаксиса'}"),
        ))

    return WorkflowSetupValidationResponse(
        ready=not any(check.status == "error" for check in checks), checks=checks,
    )


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


@app.get(
    "/api/workflows/{workflow_id}/plan",
    response_model=Optional[WorkflowPlanInDB],
)
def api_get_workflow_plan(workflow_id: str):
    if not db.get_workflow(workflow_id):
        raise HTTPException(404, "Workflow not found")
    return db.get_workflow_plan(workflow_id)


@app.get(
    "/api/workflows/{workflow_id}/stages",
    response_model=List[WorkflowStageInDB],
)
def api_list_workflow_stages(workflow_id: str):
    if not db.get_workflow(workflow_id):
        raise HTTPException(404, "Workflow not found")
    return db.list_workflow_stages(workflow_id)


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
    "/api/workflows/{workflow_id}/plan/dispatch",
    response_model=WorkflowPlanInDB,
)
def api_dispatch_workflow_planner(
    workflow_id: str, dispatch: WorkflowPlanDispatch
):
    if dispatch.provider and dispatch.provider not in load_providers():
        raise HTTPException(400, f"Неизвестный провайдер «{dispatch.provider}»")
    return _workflow_action(workflows.dispatch_planner, workflow_id, dispatch)


@app.put(
    "/api/workflows/{workflow_id}/plan",
    response_model=List[WorkflowStageInDB],
)
def api_replace_workflow_plan(
    workflow_id: str, replacement: WorkflowPlanReplace
):
    return _workflow_action(workflows.replace_plan, workflow_id, replacement)


@app.post(
    "/api/workflows/{workflow_id}/plan/approve",
    response_model=WorkflowInDB,
)
def api_approve_workflow_plan(
    workflow_id: str, approval: WorkflowPlanApproval
):
    approved = _workflow_action(workflows.approve_plan, workflow_id, approval)
    return workflows.advance_workflow(approved.id)


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


# --- Доработка обработок 1С (/epf) ---

class EpfSettings(BaseModel):
    exe_path: str = ""
    bases_root: str = ""
    base_path: str = ""
    ut11: str = ""
    bp3: str = ""
    erp: str = ""
    unf: str = ""
    aa: str = ""
    skills_repo: str = ""
    skills_dir: str = ""


@app.get("/api/epf/status")
def api_epf_status():
    v8 = epf_tools.find_v8_exe()
    return {
        "available": v8 is not None,
        "v8_path": str(v8) if v8 else "",
        "bases_count": len(epf_tools.scan_available_bases()),
        "env_keys": epf_tools.env_overridden_keys(),
        "config": epf_tools.load_1c_config(),
    }


@app.post("/api/epf/settings")
def api_epf_save_settings(s: EpfSettings):
    """Записать секцию "1c" в tg_config.json. Переменные PP_1C_* в env
    сильнее — их из UI не перекрыть (см. status.env_keys)."""
    epf_tools.save_1c_config(s.model_dump())
    return api_epf_status()


@app.get("/api/epf/bases")
def api_epf_bases():
    bases = [{"key": "stub", "label": "🧩 Авто (Stub-DB)"}]
    for key, label in epf_tools.PRESET_LABELS.items():
        path, _ = epf_tools.resolve_base(key)
        bases.append({"key": key, "label": label, "missing": path is None})
    for b in epf_tools.scan_available_bases():
        bases.append({"key": b["path"], "label": f"📁 {b['name']}"})
    return bases


@app.post("/api/epf/rework", status_code=201)
async def api_epf_rework(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    base_key: str = Form("stub"),
):
    """Создать доработку обработки 1С: декомпиляция → задача в очереди.

    Декомпиляция тяжёлая (до нескольких минут) — преп-фаза в threadpool.
    Сборка результата происходит в боте по завершении задачи (epf_jobs).
    """
    if not epf_tools.is_available():
        raise HTTPException(409, "1cv8.exe не найден — 1С-доработка недоступна")

    name = Path(file.filename or "").name
    if not name.lower().endswith((".epf", ".erf")):
        raise HTTPException(400, "Нужен файл .epf или .erf")

    base_path, base_label = epf_tools.resolve_base(base_key)
    if base_key != "stub" and not base_path:
        raise HTTPException(400, f"База «{base_key}» не найдена на этой машине")

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{uuid.uuid4().hex}{Path(name).suffix}"
    size = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"«{name}» больше 20 МБ")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    try:
        projects_root = Path(PROJECTS_ROOT) if PROJECTS_ROOT else Path.cwd() / "pp-epf-projects"
        projects_root.mkdir(parents=True, exist_ok=True)
        project_dir, warnings = await run_in_threadpool(
            epf_tools.prepare_project, dest, name, base_path, projects_root,
        )
    except epf_tools.EpfError as e:
        raise HTTPException(422, f"Не удалось декомпилировать: {str(e)[:800]}")
    finally:
        dest.unlink(missing_ok=True)

    task = db.create_task(TaskCreate(
        prompt=prompt.strip(),
        working_dir=str(project_dir),
        skip_permissions=True,  # агенту нужен запуск 1cv8/epf-build и правка файлов
    ))
    db.create_epf_job(
        task_id=task.id, project_dir=str(project_dir), original_name=name,
        base_key=base_key, base_label=base_label, chat_id=None,
    )
    return {"task": task.model_dump(), "base_label": base_label, "warnings": warnings}


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
    return db.worker_runtime_status()


@app.post("/api/worker/pause")
def api_worker_pause():
    db.set_setting("worker_paused", "1")
    pipeline_insights.invalidate_cache()
    return {"ok": True, "paused": True}


@app.post("/api/worker/resume")
def api_worker_resume():
    db.set_setting("worker_paused", "0")
    pipeline_insights.invalidate_cache()
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
            "supports_effort": info.get("supports_effort", False),
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
            proc = _sp.run(
                herdr_argv(args, host), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=20, stdin=_sp.DEVNULL,
            )
            return _json.loads((proc.stdout or "").strip() or "{}")
        except (OSError, TypeError, ValueError, _sp.TimeoutExpired):
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
            "supports_effort": info.get("supports_effort", False),
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
    return Response(content=_INDEX_HTML, media_type="text/html")
