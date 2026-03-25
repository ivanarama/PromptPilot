"""Data models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    prompt: str
    working_dir: Optional[str] = None
    priority: int = Field(default=5, ge=1, le=10)
    scheduled_at: Optional[datetime] = None
    max_retries: int = Field(default=5, ge=0, le=50)


class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None
    priority: Optional[int] = Field(default=None, ge=1, le=10)


class TaskInDB(BaseModel):
    id: int
    prompt: str
    working_dir: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 5
    scheduled_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 5
    exit_code: Optional[int] = None


class Stats(BaseModel):
    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    rate_limited: int = 0
    cancelled: int = 0
    total: int = 0
