"""Owner-only configuration; protected by the ordinary API authentication."""
from fastapi import APIRouter, HTTPException

from . import agent_control as control

router = APIRouter(prefix="/api/agent-control")


def _call(function, *args):
    try:
        return function(*args)
    except control.AgentControlError as exc:
        status = {"forbidden": 403, "conflict": 409, "unavailable": 503}.get(exc.code, 422)
        raise HTTPException(status, str(exc)) from exc


@router.get("/projects")
def projects():
    return {"projects": _call(control.list_projects)}


@router.post("/projects")
def save_project(project: control.ProjectCreate):
    return {"project": _call(control.upsert_project, project)}


@router.delete("/projects/{project_id}")
def disable_project(project_id: str):
    if not _call(control.delete_project, project_id):
        raise HTTPException(404, "Agent project not found")
    return {"ok": True}


@router.get("/connections")
def connections():
    return {"connections": _call(control.list_connections)}


@router.post("/connections", status_code=201)
def create_connection(connection: control.ConnectionCreate):
    return _call(control.issue_connection, connection)


@router.delete("/connections/{connection_id}")
def revoke_connection(connection_id: str):
    if not _call(control.revoke_connection, connection_id):
        raise HTTPException(404, "Agent connection not found")
    return {"ok": True}
