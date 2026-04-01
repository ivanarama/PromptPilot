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
    provider: Optional[str] = None  # e.g. "claude", "claude-z", or raw command
    priority: int = Field(default=5, ge=1, le=10)
    scheduled_at: Optional[datetime] = None
    max_retries: int = Field(default=5, ge=0, le=50)
    skip_permissions: bool = False
    model: Optional[str] = None  # e.g. "sonnet", "opus", "haiku"
    session_id: Optional[str] = None  # Claude session to resume (--resume)
    parent_task_id: Optional[int] = None  # Task this is a reply to
    tg_chat_id: Optional[int] = None  # Telegram chat to notify on completion
    recurrence: Optional[str] = None  # e.g. "6h", "daily@09:00"


class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None
    priority: Optional[int] = Field(default=None, ge=1, le=10)


class TaskInDB(BaseModel):
    id: int
    prompt: str
    working_dir: Optional[str] = None
    provider: Optional[str] = None
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
    model_used: Optional[str] = None
    skip_permissions: bool = False
    model: Optional[str] = None
    session_id: Optional[str] = None
    parent_task_id: Optional[int] = None
    tg_chat_id: Optional[int] = None
    notified_at: Optional[datetime] = None
    recurrence: Optional[str] = None


class Stats(BaseModel):
    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    rate_limited: int = 0
    cancelled: int = 0
    total: int = 0


class CostStats(BaseModel):
    today: float = 0.0
    week: float = 0.0
    total: float = 0.0
    by_provider: dict = {}
