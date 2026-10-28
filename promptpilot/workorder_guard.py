"""Fail-closed execution lease for website orders. No effect on other tasks."""

import contextlib
import json
import os
import threading
import urllib.request
from urllib.parse import urlparse

from . import db


class WorkorderAuthorizationError(RuntimeError):
    """Execution was refused by the website authorization boundary."""


def configuration(task_id):
    with db._connect() as connection:
        row = connection.execute(
            "SELECT w.config_json FROM workflows w JOIN workflow_runs r "
            "ON r.workflow_id=w.id WHERE r.task_id=?", (task_id,)
        ).fetchone()
    return json.loads(row["config_json"]).get("workorder_guard") if row else None


def check_task(task_id, reserve=False):
    config = configuration(task_id)
    if not config:
        return
    token = os.environ.get("WORKORDERS_WORKER_TOKEN", "")
    allowed_site = os.environ.get("WORKORDERS_SITE_URL", "").rstrip("/")
    if not token or not allowed_site or config["site_url"].rstrip("/") != allowed_site:
        raise WorkorderAuthorizationError("Workorder guard is not configured")
    parsed = urlparse(allowed_site)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise WorkorderAuthorizationError("HTTPS required")
    operation = "reserve" if reserve else "check"
    url = f"{allowed_site}/internal/workorders/v1/runs/{config['run_id']}/{operation}/"
    task = db.get_task(task_id)
    attempt = task.started_at.isoformat() if task and task.started_at else "pending"
    request = urllib.request.Request(url, method="POST", data=json.dumps({"stage_key": f"task:{task_id}:{attempt}"}).encode(), headers={
        "Authorization": f"Bearer {token}", "X-Worker-Key": config["worker_key"],
        "X-Lease-Token": config["lease_token"], "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read(65536))
    except (OSError, ValueError) as exc:
        raise WorkorderAuthorizationError("Workorder authorization unavailable") from exc
    if result.get("approved" if reserve else "continue") is not True:
        raise WorkorderAuthorizationError("Workorder authorization refused")


@contextlib.contextmanager
def watch_task(task_id, stop_tree):
    if not configuration(task_id):
        yield
        return
    check_task(task_id, reserve=True)
    done = threading.Event()

    def watch():
        while not done.wait(10):
            try:
                check_task(task_id)
            except Exception:
                db.mark_cancelled(task_id, "Workorder execution lease lost")
                stop_tree(task_id)
                return

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=10)
