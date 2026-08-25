"""Data models."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

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
    effort: Optional[str] = None  # Claude --effort: low|medium|high|xhigh|max; None = provider default
    session_id: Optional[str] = None  # Claude session to resume (--resume)
    parent_task_id: Optional[int] = None  # Task this is a reply to
    tg_chat_id: Optional[int] = None  # Telegram chat to notify on completion
    recurrence: Optional[str] = None  # e.g. "6h", "daily@09:00"
    task_timeout: Optional[int] = None  # per-task timeout in seconds; None = use global TASK_TIMEOUT; 0 = no limit
    detached: bool = False  # if True: start process and mark completed immediately (for servers/bots)
    keep_pane: bool = True  # herdr executor: keep the live session open after success
    herdr_target: Optional[str] = None  # herdr: send the prompt into an EXISTING session (agent name or pane id)
    machine: Optional[str] = None  # run on a registered remote machine (machines.json) over ssh
    worktree: bool = False  # run in a fresh git worktree of working_dir (branch pp/t<id>)


class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None
    priority: Optional[int] = Field(default=None, ge=1, le=10)
    # Правка ещё не начавшейся задачи. Пустая строка — «снять значение»
    # (например, убрать повтор), None — «не трогать это поле».
    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    recurrence: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    working_dir: Optional[str] = None


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
    effort: Optional[str] = None
    session_id: Optional[str] = None
    parent_task_id: Optional[int] = None
    tg_chat_id: Optional[int] = None
    notified_at: Optional[datetime] = None
    recurrence: Optional[str] = None
    task_timeout: Optional[int] = None
    detached: bool = False
    keep_pane: bool = True
    herdr_target: Optional[str] = None
    machine: Optional[str] = None
    worktree: bool = False
    worktree_path: Optional[str] = None  # filled in once the checkout exists
    worktree_branch: Optional[str] = None
    herdr_pane: Optional[str] = None  # pane of a herdr-executor run (📺 in the bot)
    note: Optional[str] = None  # the human's late word, injected into the next attempt
    verdict: Optional[str] = None  # ГОТОВО | УЖЕ СДЕЛАНО | НУЖЕН ЧЕЛОВЕК | НЕ СМОГ | ПУСТО (тихий: без TG-уведомления)


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


# --- Workflow orchestrator -------------------------------------------------


class WorkflowStatus(str, Enum):
    """Durable lifecycle states for an orchestrated engineering workflow."""

    DRAFT = "draft"
    QUEUED = "queued"
    EXECUTING = "executing"
    GATING = "gating"
    REVIEWING = "reviewing"
    REVISION_REQUIRED = "revision_required"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowRoundStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    GATING = "gating"
    REVIEWING = "reviewing"
    REVISION_REQUIRED = "revision_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowRole(str, Enum):
    EXECUTOR = "executor"
    GATE = "gate"
    REVIEWER = "reviewer"
    ARCHIVER = "archiver"


class FindingSeverity(str, Enum):
    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    REOPENED = "reopened"
    ACCEPTED_RISK = "accepted_risk"


class WorkflowCreate(BaseModel):
    slug: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    objective: str = Field(min_length=1)
    repository_path: str = Field(min_length=1)
    candidate_branch: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowUpdate(BaseModel):
    """Editable W0 metadata with optimistic concurrency control.

    Lifecycle transitions are deliberately not exposed here; W1 owns the state
    machine. ``expected_version`` prevents two API clients from silently
    overwriting each other's draft configuration.
    """

    objective: Optional[str] = Field(default=None, min_length=1)
    repository_path: Optional[str] = Field(default=None, min_length=1)
    candidate_branch: Optional[str] = Field(default=None, min_length=1)
    config: Optional[dict[str, Any]] = None
    expected_version: int = Field(ge=0)


class WorkflowInDB(BaseModel):
    id: str
    slug: str
    objective: str
    repository_path: str
    candidate_branch: str
    status: WorkflowStatus
    current_round: int = 0
    state_version: int = 0
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class WorkflowRoundCreate(BaseModel):
    workflow_id: str
    round_no: int = Field(ge=1)
    base_sha: Optional[str] = None


class WorkflowRoundInDB(BaseModel):
    id: str
    workflow_id: str
    round_no: int
    status: WorkflowRoundStatus
    base_sha: Optional[str] = None
    candidate_sha: Optional[str] = None
    audit_sha: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    summary: Optional[dict[str, Any]] = None


class WorkflowRunCreate(BaseModel):
    workflow_id: str
    round_id: str
    role: WorkflowRole
    attempt_no: int = Field(default=1, ge=1)
    input_sha256: str = Field(min_length=1)
    task_id: Optional[int] = None


class WorkflowRunInDB(BaseModel):
    id: str
    workflow_id: str
    round_id: str
    role: WorkflowRole
    attempt_no: int
    task_id: Optional[int] = None
    status: WorkflowRunStatus
    input_sha256: str
    output_sha256: Optional[str] = None
    output: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class WorkflowFindingUpsert(BaseModel):
    workflow_id: str
    fingerprint: str = Field(min_length=1)
    severity: FindingSeverity
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: FindingStatus = FindingStatus.OPEN
    round_no: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowFindingInDB(BaseModel):
    id: str
    workflow_id: str
    fingerprint: str
    severity: FindingSeverity
    category: str
    title: str
    status: FindingStatus
    first_seen_round: int
    last_seen_round: int
    reopen_count: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowArtifactCreate(BaseModel):
    workflow_id: str
    round_id: str
    kind: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    run_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowArtifactInDB(BaseModel):
    id: str
    workflow_id: str
    round_id: str
    run_id: Optional[str] = None
    kind: str
    path: str
    sha256: str
    size_bytes: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowEventCreate(BaseModel):
    workflow_id: str
    event_type: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    round_id: Optional[str] = None
    run_id: Optional[str] = None


class WorkflowEventInDB(BaseModel):
    seq: int
    workflow_id: str
    event_type: str
    idempotency_key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    round_id: Optional[str] = None
    run_id: Optional[str] = None


class WorkflowStartRequest(BaseModel):
    expected_version: int = Field(ge=0)
    base_sha: Optional[str] = None


class WorkflowVersionRequest(BaseModel):
    expected_version: int = Field(ge=0)


class WorkflowTaskDispatch(BaseModel):
    expected_version: int = Field(ge=0)
    role: WorkflowRole
    prompt: str = Field(min_length=1)
    provider: Optional[str] = None
    priority: int = Field(default=5, ge=1, le=10)
    max_retries: int = Field(default=5, ge=0, le=50)
    skip_permissions: bool = False
    model: Optional[str] = None
    working_dir: Optional[str] = None
    worktree: bool = False
    task_timeout: Optional[int] = Field(default=None, ge=0)


class WorkflowDispatchResult(BaseModel):
    workflow: WorkflowInDB
    round: WorkflowRoundInDB
    run: WorkflowRunInDB
    task: TaskInDB


class GateVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class WorkflowGateDecision(BaseModel):
    verdict: GateVerdict
    expected_version: int = Field(ge=0)
    gate_id: str = Field(default="manual-gate", min_length=1)
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)


class ReviewVerdict(str, Enum):
    PASS = "PASS"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class ReviewFindingInput(BaseModel):
    fingerprint: str = Field(min_length=1)
    severity: FindingSeverity
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: FindingStatus = FindingStatus.OPEN
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowReviewDecision(BaseModel):
    verdict: ReviewVerdict
    expected_version: int = Field(ge=0)
    summary: str = ""
    findings: list[ReviewFindingInput] = Field(default_factory=list)


class WorkflowHumanInput(BaseModel):
    text: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    resume: bool = False


class HistoricalFactStatus(str, Enum):
    CLAIMED = "CLAIMED"
    VERIFIED = "VERIFIED"
    DISPROVED = "DISPROVED"
    PARTIAL = "PARTIAL"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class HistoricalFactImport(BaseModel):
    claim: str = Field(min_length=1)
    status: HistoricalFactStatus
    source: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    notes: str = ""


class HistoricalRoundImport(BaseModel):
    round_no: int = Field(ge=1)
    status: WorkflowRoundStatus = WorkflowRoundStatus.COMPLETED
    base_sha: Optional[str] = None
    candidate_sha: Optional[str] = None
    audit_sha: Optional[str] = None
    summary: str = ""
    facts: list[HistoricalFactImport] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class WorkflowHistoryImport(BaseModel):
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1)
    source: str = Field(min_length=1)
    rounds: list[HistoricalRoundImport] = Field(min_length=1)
    notes: str = ""
