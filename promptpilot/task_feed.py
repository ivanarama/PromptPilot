"""Read-only business task projection, independent of execution and federation.

Only an unambiguous, structurally valid workflow link hides a task. All links
are resolved before business filtering/pagination. Configuration, provider
inputs, credentials and event payloads are deliberately not public read models.
"""
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
from typing import Literal

from . import config, db


STATUSES = frozenset({
    "queued", "active", "blocked", "needs_attention", "completed", "cancelled",
})
FeedStatus = Literal["queued", "active", "blocked", "needs_attention", "completed", "cancelled"]
FeedKind = Literal["workflow", "task"]
ROLE_LABELS = {
    "planner": "Планировщик", "executor": "Исполнитель",
    "reviewer": "Проверяющий", "gate": "Проверки", "archiver": "Архивирование",
}
WORKFLOW_STATES = {
    "draft": ("queued", "Черновик", "Процесс создан, выполнение ещё не начато."),
    "queued": ("queued", "В очереди", "Процесс ожидает исполнения."),
    "planning": ("active", "Планирование", "Процесс находится на этапе планирования."),
    "executing": ("active", "Выполняется", "Процесс находится на этапе исполнения."),
    "gating": ("active", "Проверки", "Процесс находится на этапе проверок."),
    "reviewing": ("active", "Проверяется", "Результат передан проверяющему."),
    "awaiting_external": (
        "blocked", "Ожидает внешнего результата",
        "Задание выдано внешнему исполнителю; его фактический запуск не подтверждён.",
    ),
    "awaiting_plan_approval": (
        "needs_attention", "Нужно утвердить план", "План ожидает решения человека.",
    ),
    "awaiting_human": (
        "needs_attention", "Требуется решение", "Продолжение требует решения человека.",
    ),
    "revision_required": (
        "needs_attention", "Нужна доработка", "Замечания требуют исправления и нового ревью.",
    ),
    "failed": ("needs_attention", "Ошибка", "Процесс завершился ошибкой."),
    "completed": ("completed", "Завершено", "Процесс завершён."),
    "cancelled": ("cancelled", "Отменено", "Процесс отменён."),
}
TASK_STATES = {
    "pending": ("queued", "В очереди", "Задача ожидает допуска и запуска worker."),
    "running": (
        "active", "Выполняется", "Задача запущена worker; возможна подготовка исполнителя.",
    ),
    "rate_limited": (
        "blocked", "Ожидает лимита", "Задача отложена из-за лимита, а не выполняется.",
    ),
    "failed": ("needs_attention", "Ошибка", "Задача завершилась ошибкой."),
    "completed": ("completed", "Завершено", "Задача завершена."),
    "cancelled": ("cancelled", "Отменено", "Задача отменена."),
}
UNKNOWN_STATE = (
    "needs_attention", "Неизвестное состояние", "Состояние требует уточнения.",
)


class ItemNotFound(LookupError):
    """No such local business item."""


class FeedUnavailable(RuntimeError):
    """The database cannot be read without creating or migrating it."""


def _text(value, limit=500):
    return value[:limit] if isinstance(value, str) else ""


def _object(value):
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def local_source():
    label = _text(getattr(config, "MACHINE_NAME", None), 120).strip()
    return {"id": "local", "label": label or "Локальный PromptPilot",
            "status": "online", "readonly": False}


def _executor(row):
    row = _object(row)
    return {name: _text(row.get(name), 200) or None
            for name in ("provider", "model", "machine")}


def project_workflow(row, *, source_id="local", source_label=None,
                     readonly=False, executor=None):
    """Pure projection of a DB row or an authorized legacy API workflow dict."""
    row = dict(row)
    state, label, reason = WORKFLOW_STATES.get(_text(row.get("status"), 80), UNKNOWN_STATE)
    configuration = _object(row.get("config", row.get("config_json")))
    roles = _object(configuration.get("roles"))
    token = _object(configuration.get("workorder_guard")).get("lease_token")
    def display(value, limit):
        text = value if isinstance(value, str) else ""
        return (text.replace(token, "[redacted]") if isinstance(token, str) and token else text)[:limit]
    item = {
        "kind": "workflow", "object_id": _text(row.get("id"), 128),
        "title": display(row.get("slug"), 300),
        "objective": display(row.get("objective"), 1500),
        "status": state, "status_label": label, "status_reason": reason,
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "executor": _executor(executor if executor is not None else roles.get("executor")),
        "workflow": {k: row.get(k) for k in (
            "current_round", "current_stage_id", "state_version")},
    }
    result = namespace_item(item, source_id, source_label or local_source()["label"], readonly)
    external = _object(configuration.get("external_executor"))
    client_id = external.get("client_id")
    if (row.get("status") == "awaiting_external" and configuration.get("workorder_guard") is None
            and isinstance(client_id, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", client_id)):
        result["external_execution"] = {
            "client_id": client_id, "client_label": _text(external.get("label"), 120) or client_id,
            "machine": _text(external.get("machine"), 200) or None,
            "state": "awaiting_connection", "execution_observed": False, "last_seen_at": None,
        }
    return result


def project_task(row, *, source_id="local", source_label=None, readonly=False,
                 link_problem=None, paused=False):
    row = dict(row)
    first_line = _text(row.get("prompt"), 500).splitlines()
    title = _text(row.get("title"), 300) or (first_line[0][:300] if first_line else "Задача")
    state, label, reason = TASK_STATES.get(_text(row.get("status"), 80), UNKNOWN_STATE)
    if row.get("status") == "pending" and paused:
        state, label, reason = "blocked", "На паузе", "Запуск задачи приостановлен."
    elif row.get("status") == "pending" and row.get("budget_wait_scope"):
        state, label, reason = "blocked", "Ожидает допуска", "Задача ожидает допуска конвейера."
    if link_problem:
        # Keep the original task discoverable instead of guessing its owner.
        reason = "Связь с процессом неоднозначна или повреждена; задача показана отдельно."
    item = {
        "kind": "task", "object_id": row["id"], "title": title, "objective": title,
        "status": state, "status_label": label, "status_reason": reason,
        "created_at": row.get("created_at"),
        "updated_at": row.get("completed_at") or row.get("started_at") or row.get("created_at"),
        "executor": _executor({**row, "model": row.get("model_used") or row.get("model")}),
        "task": {k: row.get(k) for k in (
            "status", "series_id", "scheduled_at", "next_run_at", "priority",
            "retry_count", "max_retries")},
    }
    return namespace_item(item, source_id, source_label or local_source()["label"], readonly)


def namespace_item(item, source_id, label, readonly=True):
    """Whitelist a projected peer item and replace only display/source identity.

    This helper does not discover sources, contact peers or accept a URL as an
    identity. Remote configuration/extra fields never survive this boundary.
    Peer external-client activity is omitted in this first federation version.
    """
    if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source_id):
        raise ValueError("Invalid task-feed source identity")
    kind = item.get("kind")
    if kind not in {"workflow", "task"}:
        raise ValueError("Unknown task-feed kind")
    object_id = item.get("object_id")
    if kind == "workflow":
        if not isinstance(object_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", object_id):
            raise ValueError("Invalid workflow identity")
    elif type(object_id) is not int or object_id <= 0:
        raise ValueError("Invalid task identity")
    result = {k: _text(item.get(k), 1500 if k == "objective" else 500)
              for k in ("title", "objective", "status_label", "status_reason")}
    result.update(
        id=f"{source_id}:{kind}:{object_id}", kind=kind, object_id=object_id,
        source_id=source_id, source_label=_text(label, 120), readonly=bool(readonly),
        status=item.get("status") if _text(item.get("status"), 80) in STATUSES else "needs_attention",
        created_at=item.get("created_at") if isinstance(item.get("created_at"), str) else None,
        updated_at=item.get("updated_at") if isinstance(item.get("updated_at"), str) else None,
        executor=_executor(item.get("executor")),
    )
    if kind == "workflow":
        summary = _object(item.get("workflow"))
        result["workflow"] = {
            "current_round": summary.get("current_round")
            if type(summary.get("current_round")) is int else None,
            "current_stage_id": _text(summary.get("current_stage_id"), 128) or None,
            "state_version": summary.get("state_version")
            if type(summary.get("state_version")) is int else None,
        }
    else:
        summary = _object(item.get("task"))
        result["task"] = {k: summary.get(k) for k in (
            "status", "series_id", "scheduled_at", "next_run_at", "priority",
            "retry_count", "max_retries") if isinstance(summary.get(k), (str, int, type(None)))}
    return result


def project_run(row, *, round_no=None):
    row = dict(row)
    role = _text(row.get("role"), 40)
    number = round_no if round_no is not None else row.get("round_no")
    return {"id": _text(row.get("id"), 128), "role": role,
            "role_label": ROLE_LABELS.get(role, "Запуск"),
            "status": _text(row.get("status"), 80),
            "attempt_no": row.get("attempt_no") if type(row.get("attempt_no")) is int else None,
            "round_no": number if type(number) is int else None,
            "task_id": row.get("task_id") if type(row.get("task_id")) is int else None,
            "started_at": _text(row.get("started_at"), 80) or None,
            "completed_at": _text(row.get("completed_at"), 80) or None}


def project_event(row):
    row = dict(row)
    return {k: row.get(k) for k in (
        "seq", "event_type", "created_at", "round_id", "run_id")
        if isinstance(row.get(k), (str, int, type(None)))}


def project_artifact(row):
    row = dict(row)
    return {k: row.get(k) for k in (
        "id", "kind", "path", "sha256", "size_bytes", "round_id", "run_id")
        if isinstance(row.get(k), (str, int, type(None)))}


def project_history(runs, events, rounds=()):
    round_numbers = {r["id"]: r.get("round_no") for r in rounds}
    safe_runs = [project_run(r, round_no=round_numbers.get(r.get("round_id"))) for r in runs]
    groups = defaultdict(list)
    for run in safe_runs:
        groups[run["role"]].append(run)
    return {"runs": safe_runs,
            "role_groups": [{"role": role, "label": ROLE_LABELS.get(role, "Запуск"),
                             "runs": values} for role, values in groups.items()],
            "events": [project_event(r) for r in events]}


@contextmanager
def _snapshot():
    """One coherent read transaction; never create/migrate/sync the database."""
    try:
        connection = sqlite3.connect(db.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.Error as exc:
        raise FeedUnavailable("Task feed is temporarily unavailable") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in {
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_TRANSACTION,
        } else sqlite3.SQLITE_DENY)
        connection.execute("BEGIN")
        yield connection
    except sqlite3.Error as exc:
        raise FeedUnavailable("Task feed is temporarily unavailable") from exc
    finally:
        connection.close()


def _rows(connection, query, args=()):
    return [dict(row) for row in connection.execute(query, args)]


def _planner_events(connection, workflow_id=None):
    """Append-only dispatch identities survive replacement of workflow_plans."""
    result = []
    for row in _rows(connection, """
        SELECT e.workflow_id,e.payload_json,w.id AS existing_workflow
        FROM workflow_events e LEFT JOIN workflows w ON w.id=e.workflow_id
        WHERE e.event_type='planner.dispatched'
    """ + (" AND e.workflow_id=?" if workflow_id is not None else "") + " ORDER BY e.seq",
        (workflow_id,) if workflow_id is not None else ()):
        task_id = _object(row["payload_json"]).get("task_id")
        if type(task_id) is int and 0 < task_id <= 2**63 - 1:
            result.append({**row, "task_id": task_id})
    return result


def _links(connection, task_id=None):
    links = defaultdict(list)
    rows = _rows(connection, """
        SELECT r.task_id, r.workflow_id, r.role, r.round_id,
               w.id AS existing_workflow, q.workflow_id AS round_workflow
        FROM workflow_runs r
        LEFT JOIN workflows w ON w.id=r.workflow_id
        LEFT JOIN workflow_rounds q ON q.id=r.round_id
        WHERE r.task_id IS NOT NULL
    """ + (" AND r.task_id=?" if task_id is not None else ""),
        (task_id,) if task_id is not None else ())
    for row in rows:
        valid = (row["existing_workflow"] is not None
                 and row["round_workflow"] == row["workflow_id"]
                 and row["role"] in ROLE_LABELS)
        links[row["task_id"]].append((row["workflow_id"], row["role"], row["round_id"], valid))
    for row in _rows(connection, """
        SELECT p.planner_task_id, p.workflow_id, w.id AS existing_workflow
        FROM workflow_plans p LEFT JOIN workflows w ON w.id=p.workflow_id
        WHERE p.planner_task_id IS NOT NULL
    """ + (" AND p.planner_task_id=?" if task_id is not None else ""),
        (task_id,) if task_id is not None else ()):
        links[row["planner_task_id"]].append(
            (row["workflow_id"], "planner", "plan", row["existing_workflow"] is not None))
    for row in _planner_events(connection):
        if task_id is None or task_id == row["task_id"]:
            links[row["task_id"]].append(
                (row["workflow_id"], "planner", "event", row["existing_workflow"] is not None))
    return links


def _business_items(connection, *, kind=None, object_id=None):
    source = local_source()
    workflow_rows = [] if kind == "task" else _rows(
        connection, "SELECT * FROM workflows" + (" WHERE id=?" if kind else ""),
        (object_id,) if kind else ())
    items = [project_workflow(row, source_label=source["label"]) for row in workflow_rows]
    if kind == "workflow":
        return items
    links = _links(connection, object_id if kind == "task" else None)
    paused_row = connection.execute("SELECT value FROM settings WHERE key='worker_paused'").fetchone()
    global_paused = paused_row is not None and paused_row[0] == "1"
    for row in _rows(connection, """
        SELECT t.id, substr(t.prompt,1,500) AS prompt, t.status, t.provider,
               t.model, t.model_used, t.machine, t.created_at, t.started_at,
               t.completed_at, t.series_id, t.scheduled_at, t.next_run_at,
               t.priority, t.retry_count, t.max_retries, t.budget_wait_scope,
               s.paused AS series_paused
        FROM tasks t LEFT JOIN task_series s ON s.id=t.series_id
    """ + (" WHERE t.id=?" if kind == "task" else ""),
        (object_id,) if kind == "task" else ()):
        owned = links.get(row["id"], [])
        if owned and all(link[3] for link in owned) and len({link[:2] for link in owned}) == 1:
            continue
        items.append(project_task(row, source_label=source["label"],
                                  link_problem=bool(owned),
                                  paused=global_paused or bool(row["series_paused"])))
    return items


def paginate_items(items, *, limit=50, offset=0, status=None, kind=None, q=None):
    """Pure business filtering/sorting, reusable after authorized federation."""
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("Invalid task-feed pagination")
    if status is not None and status not in STATUSES:
        raise ValueError("Invalid task-feed status")
    if kind is not None and kind not in {"workflow", "task"}:
        raise ValueError("Invalid task-feed kind")
    query = q.casefold().strip() if isinstance(q, str) else ""
    selected = [r for r in items if (status is None or r["status"] == status)
                and (kind is None or r["kind"] == kind)
                and (not query or query in (r["title"] + " " + r["objective"]).casefold())]
    selected.sort(key=lambda r: (r.get("updated_at") or r.get("created_at") or "", r["id"]), reverse=True)
    return {"items": selected[offset:offset + limit], "total": len(selected),
            "limit": limit, "offset": offset}


def list_local_items():
    """All local business items, without pagination, from one read snapshot.

    Federation may combine this with explicitly registered source items before
    applying business filters and pagination exactly once.
    """
    with _snapshot() as connection:
        return _business_items(connection)


def list_feed(*, limit=50, offset=0, status=None, kind=None, q=None):
    items = list_local_items()
    return {**paginate_items(items, limit=limit, offset=offset, status=status, kind=kind, q=q),
            "sources": [local_source()]}


def get_detail(kind, object_id):
    if kind not in {"workflow", "task"}:
        raise ItemNotFound("Task-feed item not found")
    if kind == "task":
        if isinstance(object_id, str):
            if not 1 <= len(object_id) <= 19 or not object_id.isascii() or not object_id.isdecimal():
                raise ItemNotFound("Task-feed item not found")
            object_id = int(object_id)
        if type(object_id) is not int or not 1 <= object_id <= 2**63 - 1:
            raise ItemNotFound("Task-feed item not found")
    with _snapshot() as connection:
        item = next((r for r in _business_items(connection, kind=kind, object_id=object_id)
                     if r["kind"] == kind and r["object_id"] == object_id), None)
        if item is None:
            raise ItemNotFound("Task-feed item not found")
        if kind == "workflow":
            runs = _rows(connection, """
                SELECT r.id,r.round_id,r.role,r.attempt_no,r.task_id,r.status,
                       r.started_at,r.completed_at,r.output_sha256,
                       length(r.output_json) AS output_characters
                FROM workflow_runs r WHERE r.workflow_id=?
                ORDER BY r.rowid
            """, (object_id,))
            rounds = _rows(connection, "SELECT id,round_no FROM workflow_rounds WHERE workflow_id=?", (object_id,))
            events = _rows(connection, """
                SELECT seq,event_type,created_at,round_id,run_id
                FROM workflow_events WHERE workflow_id=? ORDER BY seq
            """, (object_id,))
            artifacts = _rows(connection, """
                SELECT id,kind,path,sha256,size_bytes,round_id,run_id
                FROM workflow_artifacts WHERE workflow_id=? ORDER BY rowid
            """, (object_id,))
            # Current plan metadata plus append-only historical dispatch IDs.
            planner_rows = _rows(connection, """
                SELECT p.planner_task_id AS task_id,t.status,t.started_at,t.completed_at,
                       p.output_sha256,length(p.output_json) AS output_characters
                FROM workflow_plans p LEFT JOIN tasks t ON t.id=p.planner_task_id
                WHERE p.workflow_id=? AND p.planner_task_id IS NOT NULL
            """, (object_id,))
            known_planners = {row["task_id"]: row for row in planner_rows}
            planner_ids = [row["task_id"] for row in _planner_events(connection, object_id)]
            planner_ids.extend(known_planners)
            planner_history = []
            for task_id in dict.fromkeys(planner_ids):
                row = known_planners.get(task_id)
                if row is None:
                    actual = connection.execute(
                        "SELECT id AS task_id,status,started_at,completed_at FROM tasks WHERE id=?",
                        (task_id,)).fetchone()
                    if actual is None:
                        continue
                    row = dict(actual)
                planner_history.append({**row, "id": "planner-task:" + str(task_id),
                                        "role": "planner", "attempt_no": None})
            runs[0:0] = planner_history
            completed = [r for r in runs if r.get("output_sha256")]
            latest = completed[-1] if completed else {}
            result = {"available": bool(completed), "run_id": latest.get("id"),
                      "sha256": latest.get("output_sha256"),
                      "characters": latest.get("output_characters"),
                      "completed_at": latest.get("completed_at")}
            return {"item": item, "history": project_history(runs, events, rounds),
                    "result": result, "artifacts": [project_artifact(r) for r in artifacts]}
        row = connection.execute("SELECT result,completed_at,exit_code,verdict FROM tasks WHERE id=?", (object_id,)).fetchone()
        text = row["result"]
        result = {"available": text is not None, "characters": len(text) if text is not None else 0,
                  "sha256": hashlib.sha256(text.encode()).hexdigest() if text is not None else None,
                  "completed_at": row["completed_at"], "exit_code": row["exit_code"],
                  "verdict": row["verdict"]}
        return {"item": item, "history": {"runs": [], "role_groups": [], "events": []},
                "result": result, "artifacts": []}
