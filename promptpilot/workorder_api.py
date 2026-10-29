"""Idempotent integration endpoints without changing interactive workflow defaults."""

import hashlib
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db
from .models import WorkflowCreate, WorkflowInDB

router = APIRouter(prefix="/api/integrations/workorders")


class EnsureWorkflow(BaseModel):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    workflow: WorkflowCreate


@router.post("/ensure", response_model=WorkflowInDB)
def ensure_workflow(data: EnsureWorkflow):
    """A lost response can be retried; same key and different input is a conflict."""
    if data.workflow.plan is not None:
        raise HTTPException(422, "Workorders use their separately approved website plan")
    if data.workflow.config.get("workorder_guard"):
        from .workorder_private import protect_directory

        protect_directory(db.DB_DIR)
    inputs = data.workflow.model_dump(mode="json", exclude={"plan"})
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    hashed = hashlib.sha256(canonical.encode()).hexdigest()
    workflow_id = "wo_" + hashlib.sha256(data.request_id.encode()).hexdigest()[:40]
    # create_workflow owns its transaction. The deterministic ID is the atomic uniqueness boundary.
    existing = db.get_workflow(workflow_id)
    if not existing:
        try:
            existing = db.create_workflow(data.workflow, workflow_id=workflow_id)
        except db.WorkflowConflictError:
            existing = db.get_workflow(workflow_id)
            if not existing:
                raise HTTPException(409, "Workflow slug belongs to another request")
    events = db.list_workflow_events(workflow_id)
    created = next((event for event in events if event.event_type == "workflow.created"), None)
    if not created:
        raise HTTPException(409, "Missing original workflow data")
    original = {field: created.payload[field] for field in inputs}
    original_hash = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if original_hash != hashed:
        raise HTTPException(409, "Request key reused with different inputs")
    return existing
