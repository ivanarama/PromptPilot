"""A small, API-only DAG orchestrator for PromptPilot.

The core PromptPilot worker remains the only worker process.  This module is
an external scheduler: it creates ordinary PromptPilot tasks, remembers their
dependency graph in its own JSON state file, and limits the number of active
tasks.  All state transitions are persisted atomically so a stopped scheduler
can be resumed without rebuilding the plan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


ACTIVE_TASK_STATUSES = {"pending", "queued", "running", "rate_limited"}
TERMINAL_NODE_STATUSES = {
    "completed",
    "failed",
    "blocked",
    "human_required",
    "cancelled",
}
TASK_SUCCESS = "completed"


class PlanError(ValueError):
    """Raised when a plan is unsafe or internally inconsistent."""


class ApiError(RuntimeError):
    """Raised when the PromptPilot API cannot satisfy a request."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clip(value: Any, limit: int = 8000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = repr(value)
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _task_status(task: Mapping[str, Any]) -> str:
    return str(task.get("status") or task.get("state") or "").lower()


@dataclass(frozen=True)
class NodeSpec:
    """One task in a dependency graph.

    ``mode=worktree`` asks the existing PromptPilot API to create a task
    worktree.  ``shared_serial`` is useful only when the plan deliberately
    targets one working directory; the scheduler still limits it to one
    active task for that directory.  ``read_only`` is a policy label for
    analysis tasks and does not grant permissions by itself.
    """

    id: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    kind: str = "task"
    mode: str = "worktree"
    working_dir: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    priority: int = 0
    max_retries: int = 0
    max_repairs: int = 0
    autofix: bool = False
    review: bool = False
    include_dependency_context: bool = True
    skip_permissions: bool = False
    timeout: int | None = None
    keep_pane: bool = False
    herdr_target: str | None = None
    machine: str | None = None
    detached: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NodeSpec":
        node_id = str(raw.get("id") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not node_id:
            raise PlanError("node id is required")
        if not prompt:
            raise PlanError(f"node {node_id!r} has an empty prompt")

        kind = str(raw.get("kind") or ("review" if raw.get("review") else "task")).lower()
        if kind not in {"task", "review"}:
            raise PlanError(f"node {node_id!r}: kind must be task or review")
        mode = str(raw.get("mode") or ("worktree" if raw.get("worktree", True) else "shared_serial")).lower()
        if mode not in {"worktree", "shared_serial", "read_only"}:
            raise PlanError(f"node {node_id!r}: unsupported mode {mode!r}")
        working_dir = raw.get("working_dir")
        if working_dir is not None:
            working_dir = str(working_dir)
        if mode in {"worktree", "shared_serial"} and not working_dir:
            raise PlanError(f"node {node_id!r}: {mode} requires working_dir")

        depends = raw.get("depends_on", raw.get("dependencies", [])) or []
        if isinstance(depends, str):
            depends = [depends]
        try:
            dependencies = tuple(str(item).strip() for item in depends if str(item).strip())
        except TypeError as exc:
            raise PlanError(f"node {node_id!r}: depends_on must be a list") from exc
        if len(set(dependencies)) != len(dependencies):
            raise PlanError(f"node {node_id!r}: duplicate dependency")

        def bounded_int(name: str, default: int, minimum: int = 0, maximum: int = 100) -> int:
            try:
                result = int(raw.get(name, default))
            except (TypeError, ValueError) as exc:
                raise PlanError(f"node {node_id!r}: {name} must be an integer") from exc
            if result < minimum or result > maximum:
                raise PlanError(f"node {node_id!r}: {name} must be between {minimum} and {maximum}")
            return result

        timeout = raw.get("timeout")
        if timeout is not None:
            try:
                timeout = int(timeout)
            except (TypeError, ValueError) as exc:
                raise PlanError(f"node {node_id!r}: timeout must be an integer") from exc
            if timeout <= 0:
                raise PlanError(f"node {node_id!r}: timeout must be positive")

        return cls(
            id=node_id,
            prompt=prompt,
            depends_on=dependencies,
            kind=kind,
            mode=mode,
            working_dir=working_dir,
            provider=(str(raw["provider"]) if raw.get("provider") is not None else None),
            model=(str(raw["model"]) if raw.get("model") is not None else None),
            effort=(str(raw["effort"]) if raw.get("effort") is not None else None),
            priority=bounded_int("priority", 0, -1000, 1000),
            max_retries=bounded_int("max_retries", 0, 0, 10),
            max_repairs=bounded_int("max_repairs", 0, 0, 10),
            autofix=_as_bool(raw.get("autofix"), False),
            review=_as_bool(raw.get("review"), kind == "review"),
            include_dependency_context=_as_bool(raw.get("include_dependency_context"), True),
            skip_permissions=_as_bool(raw.get("skip_permissions"), False),
            timeout=timeout,
            keep_pane=_as_bool(raw.get("keep_pane"), False),
            herdr_target=(str(raw["herdr_target"]) if raw.get("herdr_target") is not None else None),
            machine=(str(raw["machine"]) if raw.get("machine") is not None else None),
            detached=_as_bool(raw.get("detached"), False),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["depends_on"] = list(self.depends_on)
        return result


@dataclass(frozen=True)
class Plan:
    name: str
    nodes: tuple[NodeSpec, ...]
    max_parallel: int = 1
    version: int = 1
    description: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Plan":
        expanded = _expand(dict(raw))
        try:
            max_parallel = int(expanded.get("max_parallel", 1))
        except (TypeError, ValueError) as exc:
            raise PlanError("max_parallel must be an integer") from exc
        if max_parallel < 1 or max_parallel > 16:
            raise PlanError("max_parallel must be between 1 and 16")
        try:
            version = int(expanded.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise PlanError("version must be an integer") from exc
        nodes_raw = expanded.get("nodes")
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise PlanError("plan must contain a non-empty nodes list")
        nodes = tuple(NodeSpec.from_dict(item) for item in nodes_raw)
        ids = [node.id for node in nodes]
        if len(set(ids)) != len(ids):
            raise PlanError("node ids must be unique")
        known = set(ids)
        for node in nodes:
            unknown = set(node.depends_on) - known
            if unknown:
                raise PlanError(f"node {node.id!r}: unknown dependencies: {sorted(unknown)}")
            if node.id in node.depends_on:
                raise PlanError(f"node {node.id!r}: cannot depend on itself")
        plan = cls(
            name=str(expanded.get("name") or "parallel-run"),
            nodes=nodes,
            max_parallel=max_parallel,
            version=version,
            description=str(expanded.get("description") or ""),
        )
        plan.topological_waves()  # validate cycles
        return plan

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "Plan":
        plan_path = Path(path)
        try:
            raw = json.loads(plan_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PlanError(f"cannot read plan {plan_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PlanError(f"invalid JSON in plan {plan_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise PlanError("plan JSON root must be an object")
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "max_parallel": self.max_parallel,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    def node_map(self) -> dict[str, NodeSpec]:
        return {node.id: node for node in self.nodes}

    def topological_waves(self) -> list[list[str]]:
        remaining = {node.id: set(node.depends_on) for node in self.nodes}
        waves: list[list[str]] = []
        while remaining:
            ready = sorted(node_id for node_id, deps in remaining.items() if not deps)
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise PlanError(f"dependency cycle detected near: {cycle}")
            waves.append(ready)
            for node_id in ready:
                remaining.pop(node_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return waves

    def fingerprint(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass
class NodeRuntime:
    status: str = "pending"
    phase: str = "task"
    task_id: int | str | None = None
    attempt: int = 0
    repair_count: int = 0
    last_task: dict[str, Any] = field(default_factory=dict)
    last_decision: dict[str, Any] = field(default_factory=dict)
    repair_history: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NodeRuntime":
        values = dict(raw)
        values["repair_history"] = list(values.get("repair_history") or [])
        values["last_task"] = dict(values.get("last_task") or {})
        values["last_decision"] = dict(values.get("last_decision") or {})
        return cls(**{key: values[key] for key in cls.__dataclass_fields__ if key in values})

    def touch(self) -> None:
        self.updated_at = _utc_now()


@dataclass
class RunState:
    run_id: str
    plan_name: str
    plan_fingerprint: str
    status: str = "pending"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    nodes: dict[str, NodeRuntime] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, plan: Plan, run_id: str | None = None) -> "RunState":
        return cls(
            run_id=run_id or uuid.uuid4().hex,
            plan_name=plan.name,
            plan_fingerprint=plan.fingerprint(),
            nodes={node.id: NodeRuntime() for node in plan.nodes},
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RunState":
        nodes = {key: NodeRuntime.from_dict(value) for key, value in (raw.get("nodes") or {}).items()}
        return cls(
            run_id=str(raw.get("run_id") or ""),
            plan_name=str(raw.get("plan_name") or ""),
            plan_fingerprint=str(raw.get("plan_fingerprint") or ""),
            status=str(raw.get("status") or "pending"),
            created_at=str(raw.get("created_at") or _utc_now()),
            updated_at=str(raw.get("updated_at") or _utc_now()),
            nodes=nodes,
            events=list(raw.get("events") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "plan_name": self.plan_name,
            "plan_fingerprint": self.plan_fingerprint,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": {key: asdict(value) for key, value in self.nodes.items()},
            "events": self.events[-500:],
        }

    def event(self, kind: str, **data: Any) -> None:
        self.events.append({"at": _utc_now(), "kind": kind, **data})
        self.updated_at = _utc_now()


class StateStore:
    """Atomic JSON persistence kept outside the PromptPilot database."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        default_dir = Path.home() / ".promptpilot" / "parallel-orchestrator"
        self.path = Path(path) if path else default_dir / "run.json"

    def load(self, plan: Plan) -> RunState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = RunState.from_dict(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PlanError(f"cannot load state {self.path}: {exc}") from exc
        if state.plan_fingerprint != plan.fingerprint():
            raise PlanError(
                "state belongs to a different plan; choose a new --state-file or remove the old add-on state"
            )
        if set(state.nodes) != {node.id for node in plan.nodes}:
            raise PlanError("state node set does not match the plan")
        return state

    def save(self, state: RunState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


class PromptPilotClient:
    """Minimal stdlib client for the public PromptPilot task API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8420", token: str | None = None, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("PROMPTPILOT_API_TOKEN")
        self.timeout = timeout

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        url = path if path.startswith("http://") or path.startswith("https://") else self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"PromptPilot API {exc.code} {method} {path}: {_clip(detail, 2000)}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"PromptPilot API unavailable for {method} {path}: {exc.reason}") from exc
        if not content:
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ApiError(f"PromptPilot API returned non-JSON for {method} {path}") from exc

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self.request("POST", "/api/tasks", payload)
        if not isinstance(result, dict):
            raise ApiError("create task response is not an object")
        return result

    def get_task(self, task_id: int | str) -> dict[str, Any]:
        result = self.request("GET", f"/api/tasks/{task_id}")
        if not isinstance(result, dict):
            raise ApiError("get task response is not an object")
        return result


@dataclass(frozen=True)
class Decision:
    action: str
    confidence: float = 1.0
    reason: str = ""
    repair_prompt: str = ""
    findings: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["findings"] = list(self.findings)
        return result


def _extract_report(task: Mapping[str, Any]) -> str:
    for key in ("result", "output", "stdout", "report", "summary", "error"):
        if task.get(key):
            return _clip(task[key])
    return ""


def _parse_findings(report: str) -> tuple[dict[str, Any], ...]:
    match = re.search(r"AUDIT_FINDINGS_JSON\s*:\s*(\[.*?\])", report, re.IGNORECASE | re.DOTALL)
    if not match:
        return ()
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, dict))


class RuleAdjudicator:
    """Deterministic checkpoint policy used when Jev is unavailable."""

    def evaluate(self, node: NodeSpec, task: Mapping[str, Any], context: str = "") -> Decision:
        status = _task_status(task)
        if status != TASK_SUCCESS:
            if status in {"failed", "cancelled"}:
                return Decision("RETRY", reason=f"task ended with {status}", raw=dict(task))
            return Decision("HUMAN", reason=f"unexpected task status {status or 'unknown'}", raw=dict(task))
        if not node.review:
            return Decision("PASS", reason="task completed", raw=dict(task))

        report = _extract_report(task)
        verdict_match = re.search(r"AUDIT_VERDICT\s*:\s*([A-Z_]+)", report, re.IGNORECASE)
        verdict = verdict_match.group(1).upper() if verdict_match else ""
        findings = _parse_findings(report)
        if verdict == "PASS":
            return Decision("PASS", reason="reviewer reported PASS", findings=findings, raw=dict(task))
        if verdict == "REVISION_REQUIRED":
            details = json.dumps(list(findings), ensure_ascii=False, indent=2) if findings else _clip(report, 4000)
            repair_prompt = (
                "Repair the implementation reviewed by this checkpoint. Address every finding below, "
                "run the relevant checks, and report the changed files and evidence.\n\n"
                f"Findings:\n{details}"
            )
            return Decision(
                "REPAIR",
                reason="reviewer requested revision",
                repair_prompt=repair_prompt,
                findings=findings,
                raw=dict(task),
            )
        if verdict == "HUMAN_REQUIRED":
            return Decision("HUMAN", reason="reviewer explicitly requested human review", findings=findings, raw=dict(task))
        return Decision(
            "HUMAN",
            reason="review output lacks a strict AUDIT_VERDICT",
            findings=findings,
            raw=dict(task),
        )


class TypeSafeJevAdjudicator:
    """Optional TypeSafe Jev checkpoint with confidence and fail-closed gates.

    The key is supplied by the caller or environment; it is never written to
    the run state or logs.  A network failure, an unknown choice, or a low
    confidence answer becomes HUMAN rather than an automatic resume.
    """

    def __init__(
        self,
        url: str = "https://api.typesafe.ai/v1/systemone",
        api_key: str | None = None,
        min_confidence: float = 0.85,
        min_done: float = 0.70,
        timeout: int = 60,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.min_confidence = min_confidence
        self.min_done = min_done
        self.timeout = timeout

    def evaluate(self, node: NodeSpec, task: Mapping[str, Any], context: str = "") -> Decision:
        if not self.api_key:
            return Decision("HUMAN", reason="Jev is not configured")
        report = _extract_report(task)
        payload = {
            "state": _clip((context + "\n\n" + report).strip(), 12000),
            "model": "jev-latest",
            "questions": {
                "verdict": {
                    "type": "choice",
                    "choices": ["valid_pass", "valid_revision", "human_required", "invalid"],
                    "question": "Is the checkpoint result valid, needs revision, or requires a human?",
                },
                "done": {
                    "type": "noul",
                    "question": "Is the proposed action complete and safe to automate?",
                },
            },
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            return Decision("HUMAN", reason=f"Jev unavailable: {type(exc).__name__}")
        answers = raw.get("answers") if isinstance(raw, dict) else None
        verdict = answers.get("verdict") if isinstance(answers, dict) else None
        done = answers.get("done") if isinstance(answers, dict) else None
        choice = str(verdict.get("choice") or "").lower() if isinstance(verdict, dict) else ""
        confidence = _as_float(verdict.get("confidence")) if isinstance(verdict, dict) else 0.0
        done_score = (
            _as_float(done.get("noul") or done.get("confidence")) if isinstance(done, dict) else 0.0
        )
        if confidence < self.min_confidence or done_score < self.min_done:
            return Decision(
                "HUMAN",
                confidence=confidence,
                reason=f"Jev confidence gate failed ({confidence:.2f}/{done_score:.2f})",
                raw=raw if isinstance(raw, dict) else {},
            )
        findings = _parse_findings(report)
        if choice in {"valid_pass", "pass", "done", "already"}:
            return Decision("PASS", confidence=confidence, reason="Jev validated checkpoint", findings=findings, raw=raw)
        if choice in {"valid_revision", "revision", "revision_required"}:
            return Decision(
                "REPAIR",
                confidence=confidence,
                reason="Jev validated an automatic repair",
                repair_prompt=(
                    "Apply the review findings and verify the result.\n\n"
                    + (_clip(json.dumps(list(findings), ensure_ascii=False, indent=2), 6000) or report)
                ),
                findings=findings,
                raw=raw,
            )
        return Decision("HUMAN", confidence=confidence, reason=f"Jev returned unsupported choice {choice!r}", raw=raw)


class CompositeAdjudicator:
    def __init__(self, jev: TypeSafeJevAdjudicator | None = None) -> None:
        self.jev = jev
        self.rules = RuleAdjudicator()

    def evaluate(self, node: NodeSpec, task: Mapping[str, Any], context: str = "") -> Decision:
        if node.review and self.jev is not None:
            decision = self.jev.evaluate(node, task, context)
            if decision.action != "HUMAN" or decision.reason != "Jev is not configured":
                return decision
        return self.rules.evaluate(node, task, context)


class ParallelOrchestrator:
    """Persistent, bounded scheduler for one plan and one PromptPilot worker."""

    def __init__(
        self,
        plan: Plan,
        client: PromptPilotClient,
        store: StateStore | None = None,
        run_id: str | None = None,
        adjudicator: Any | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.plan = plan
        self.client = client
        self.store = store or StateStore()
        self.state = self.store.load(plan) if self.store.path.exists() else None
        if self.state is None:
            self.state = RunState.create(plan, run_id=run_id)
        elif run_id and self.state.run_id != run_id:
            raise PlanError(f"state run id is {self.state.run_id}, requested {run_id}")
        self.adjudicator = adjudicator or CompositeAdjudicator()
        self.clock = clock

    def ready_nodes(self) -> list[str]:
        ready: list[str] = []
        for node in self.plan.nodes:
            runtime = self.state.nodes[node.id]
            if runtime.status != "pending":
                continue
            if all(self.state.nodes[dep].status == "completed" for dep in node.depends_on):
                ready.append(node.id)
        return ready

    def is_terminal(self) -> bool:
        return self.state.status in {"completed", "failed", "human_required"}

    def tick(self) -> RunState:
        self._refresh_active_tasks()
        self._mark_blocked_nodes()
        self._launch_ready_nodes()
        self._update_run_status()
        self.store.save(self.state)
        return self.state

    def run_until_terminal(self, poll_seconds: float = 2.0, max_ticks: int | None = None) -> RunState:
        ticks = 0
        while True:
            self.tick()
            ticks += 1
            if self.is_terminal() or (max_ticks is not None and ticks >= max_ticks):
                return self.state
            time.sleep(max(0.0, poll_seconds))

    def _refresh_active_tasks(self) -> None:
        for node in self.plan.nodes:
            runtime = self.state.nodes[node.id]
            if not runtime.task_id or runtime.status not in {"queued", "running", "pending", "rate_limited"}:
                continue
            try:
                task = self.client.get_task(runtime.task_id)
            except Exception as exc:  # keep the run resumable; do not invent a failure
                runtime.reason = f"poll failed: {type(exc).__name__}: {exc}"
                runtime.touch()
                self.state.event("poll_error", node=node.id, error=runtime.reason)
                continue
            runtime.last_task = dict(task)
            status = _task_status(task)
            if status in ACTIVE_TASK_STATUSES:
                runtime.status = "running" if status == "running" else "queued"
                runtime.touch()
                continue
            if runtime.phase == "repair":
                self._finish_repair_task(node, runtime, task, status)
                continue
            if status == TASK_SUCCESS:
                context = self._dependency_context(node)
                decision = self.adjudicator.evaluate(node, task, context)
                self._apply_decision(node, runtime, task, decision)
            elif status in {"failed", "cancelled"}:
                decision = self.adjudicator.evaluate(node, task, self._dependency_context(node))
                self._apply_decision(node, runtime, task, decision)
            else:
                runtime.status = "human_required"
                runtime.reason = f"unrecognised terminal status {status or 'empty'}"
                runtime.touch()

    def _finish_repair_task(self, node: NodeSpec, runtime: NodeRuntime, task: Mapping[str, Any], status: str) -> None:
        if status == TASK_SUCCESS:
            runtime.phase = "review"
            runtime.status = "pending"
            runtime.task_id = None
            runtime.reason = "repair completed; re-running review checkpoint"
            runtime.touch()
            self.state.event("repair_completed", node=node.id, task_id=task.get("id"))
            return
        runtime.status = "human_required"
        runtime.reason = f"repair task ended with {status or 'unknown'}"
        runtime.touch()
        self.state.event("repair_failed", node=node.id, task_id=task.get("id"), status=status)

    def _apply_decision(
        self,
        node: NodeSpec,
        runtime: NodeRuntime,
        task: Mapping[str, Any],
        decision: Decision,
    ) -> None:
        runtime.last_decision = decision.to_dict()
        runtime.last_task = dict(task)
        action = decision.action.upper()
        if action == "PASS":
            runtime.status = "completed"
            runtime.phase = "done"
            runtime.task_id = None
            runtime.reason = decision.reason
            runtime.touch()
            self.state.event("node_completed", node=node.id, task_id=task.get("id"), decision=decision.reason)
            return
        if action == "REPAIR" and node.autofix and runtime.repair_count < node.max_repairs:
            repair_payload = self._payload_for(node, decision.repair_prompt or node.prompt, parent_task=task, repair=True)
            try:
                repair_task = self.client.create_task(repair_payload)
            except Exception as exc:
                runtime.status = "human_required"
                runtime.reason = f"could not start repair: {type(exc).__name__}: {exc}"
                runtime.touch()
                self.state.event("repair_start_error", node=node.id, error=runtime.reason)
                return
            runtime.repair_count += 1
            runtime.phase = "repair"
            runtime.status = "queued"
            runtime.task_id = repair_task.get("id")
            runtime.repair_history.append(
                {
                    "task_id": repair_task.get("id"),
                    "findings": list(decision.findings),
                    "reason": decision.reason,
                    "at": _utc_now(),
                }
            )
            runtime.reason = "automatic repair started"
            runtime.touch()
            self.state.event("repair_started", node=node.id, task_id=repair_task.get("id"))
            return
        if action == "RETRY":
            if runtime.attempt <= node.max_retries:
                runtime.status = "pending"
                runtime.phase = "task"
                runtime.task_id = None
                runtime.reason = decision.reason or "retry requested"
                runtime.touch()
                self.state.event("node_retry", node=node.id, attempt=runtime.attempt, reason=runtime.reason)
                return
            runtime.status = "failed"
            runtime.task_id = None
            runtime.reason = (decision.reason or "retry requested") + "; retry budget exhausted"
            runtime.touch()
            self.state.event("node_failed", node=node.id, reason=runtime.reason)
            return
        if action in {"HUMAN", "ABORT"} or action == "REPAIR":
            runtime.status = "human_required" if action == "HUMAN" or action == "REPAIR" else "failed"
            runtime.task_id = None
            runtime.reason = decision.reason or action.lower()
            runtime.touch()
            self.state.event("node_attention", node=node.id, action=action, reason=runtime.reason)
            return
        runtime.status = "failed"
        runtime.task_id = None
        runtime.reason = f"unsupported decision {decision.action!r}"
        runtime.touch()
        self.state.event("node_failed", node=node.id, reason=runtime.reason)

    def _launch_ready_nodes(self) -> None:
        active = sum(
            1
            for runtime in self.state.nodes.values()
            if runtime.task_id is not None and runtime.status in {"queued", "running", "rate_limited"}
        )
        available = max(0, self.plan.max_parallel - active)
        if available == 0:
            return
        active_shared_dirs = {
            self._normalise_dir(other.working_dir)
            for other in self.plan.nodes
            if other.mode == "shared_serial"
            and self.state.nodes[other.id].task_id is not None
            and self.state.nodes[other.id].status in {"queued", "running", "rate_limited"}
            and other.working_dir
        }
        for node in self.plan.nodes:
            if available <= 0:
                break
            runtime = self.state.nodes[node.id]
            if runtime.status != "pending" or not all(
                self.state.nodes[dep].status == "completed" for dep in node.depends_on
            ):
                continue
            if node.mode == "shared_serial" and self._normalise_dir(node.working_dir) in active_shared_dirs:
                continue
            payload = self._payload_for(node, node.prompt)
            try:
                task = self.client.create_task(payload)
            except Exception as exc:
                runtime.status = "human_required"
                runtime.reason = f"could not create task: {type(exc).__name__}: {exc}"
                runtime.touch()
                self.state.event("create_error", node=node.id, error=runtime.reason)
                continue
            runtime.task_id = task.get("id")
            runtime.attempt += 1
            runtime.status = "queued"
            runtime.phase = "task" if runtime.phase != "review" else "review"
            runtime.last_task = dict(task)
            runtime.reason = "task submitted"
            runtime.touch()
            self.state.event("node_submitted", node=node.id, task_id=task.get("id"), attempt=runtime.attempt)
            available -= 1
            if node.mode == "shared_serial" and node.working_dir:
                active_shared_dirs.add(self._normalise_dir(node.working_dir))

    def _payload_for(
        self,
        node: NodeSpec,
        prompt: str,
        parent_task: Mapping[str, Any] | None = None,
        repair: bool = False,
    ) -> dict[str, Any]:
        working_dir = node.working_dir
        worktree = node.mode == "worktree"
        if repair and parent_task:
            existing_worktree = parent_task.get("worktree_path") or parent_task.get("worktree")
            if isinstance(existing_worktree, (str, os.PathLike)) and str(existing_worktree).strip():
                working_dir = str(existing_worktree)
                worktree = False
        if working_dir:
            working_dir = os.path.expandvars(os.path.expanduser(working_dir))
        full_prompt = prompt
        if node.include_dependency_context and node.depends_on and not repair:
            full_prompt += "\n\nCompleted dependency context:\n" + self._dependency_context(node)
        payload: dict[str, Any] = {
            "prompt": full_prompt,
            "priority": node.priority,
            "skip_permissions": node.skip_permissions,
            "detached": node.detached,
            "worktree": worktree,
        }
        if working_dir:
            payload["working_dir"] = working_dir
        if node.provider:
            payload["provider"] = node.provider
        if node.model:
            payload["model"] = node.model
        if node.effort:
            payload["effort"] = node.effort
        if node.timeout is not None:
            payload["timeout"] = node.timeout
        if node.keep_pane:
            payload["keep_pane"] = True
        if node.herdr_target:
            payload["herdr_target"] = node.herdr_target
        if node.machine:
            payload["machine"] = node.machine
        if parent_task and parent_task.get("id") is not None:
            payload["parent_task_id"] = parent_task["id"]
        return payload

    def _dependency_context(self, node: NodeSpec) -> str:
        if not node.depends_on:
            return "(no dependencies)"
        chunks: list[str] = []
        for dep in node.depends_on:
            runtime = self.state.nodes[dep]
            task = runtime.last_task
            evidence = {
                "result": task.get("result") or task.get("output") or task.get("summary") or runtime.reason,
                "worktree_path": task.get("worktree_path"),
                "branch": task.get("branch") or task.get("branch_name"),
                "working_dir": task.get("working_dir"),
            }
            evidence = {key: value for key, value in evidence.items() if value not in (None, "")}
            chunks.append(
                f"[{dep}] status={runtime.status} task_id={task.get('id') or runtime.task_id}\n"
                f"evidence={_clip(evidence, 5000)}"
            )
        return "\n\n".join(chunks)

    @staticmethod
    def _normalise_dir(path: str | None) -> str:
        if not path:
            return ""
        return os.path.normcase(os.path.normpath(os.path.expandvars(os.path.expanduser(path))))

    def _mark_blocked_nodes(self) -> None:
        for node in self.plan.nodes:
            runtime = self.state.nodes[node.id]
            if runtime.status != "pending":
                continue
            blockers = [
                dep
                for dep in node.depends_on
                if self.state.nodes[dep].status in {"failed", "blocked", "human_required", "cancelled"}
            ]
            if blockers:
                runtime.status = "blocked"
                runtime.reason = "blocked by: " + ", ".join(blockers)
                runtime.touch()
                self.state.event("node_blocked", node=node.id, blockers=blockers)

    def _update_run_status(self) -> None:
        statuses = [runtime.status for runtime in self.state.nodes.values()]
        if statuses and all(status == "completed" for status in statuses):
            self.state.status = "completed"
        elif any(status == "human_required" for status in statuses):
            self.state.status = "human_required"
        elif any(status in {"failed", "blocked", "cancelled"} for status in statuses):
            self.state.status = "failed"
        else:
            self.state.status = "running"
        self.state.updated_at = self.clock()


def make_jev_from_env(url: str | None = None, key_file: str | None = None) -> TypeSafeJevAdjudicator | None:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if key_file and not api_key:
        try:
            api_key = Path(key_file).read_text(encoding="utf-8").strip()
        except OSError:
            api_key = None
    if not api_key:
        return None
    return TypeSafeJevAdjudicator(url=url or "https://api.typesafe.ai/v1/systemone", api_key=api_key)


def scaffold_plan(
    task_prompt: str,
    working_dir: str,
    analysis_lanes: Sequence[str] = ("architecture", "backend", "ui", "tests"),
    implementation_lanes: Sequence[str] = ("backend", "ui", "tests"),
    max_parallel: int = 4,
    analysis_provider: str | None = None,
    planner_provider: str | None = None,
    implementation_provider: str | None = None,
    review_provider: str | None = None,
) -> Plan:
    """Build a reviewable fan-out/synthesis/fan-in plan for one large task.

    This is intentionally deterministic. It gives the operator a concrete
    plan to inspect before any task is submitted; it does not ask an LLM to
    invent dependencies behind the operator's back. File ownership and any
    additional serial edges can be edited in the emitted JSON.
    """

    prompt = str(task_prompt).strip()
    if not prompt:
        raise PlanError("task_prompt is required")
    root = str(working_dir).strip()
    if not root:
        raise PlanError("working_dir is required")

    def lane_ids(values: Sequence[str], label: str) -> list[str]:
        result = []
        for value in values:
            slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
            if not slug:
                raise PlanError(f"{label} contains an empty lane")
            result.append(slug)
        if not result or len(set(result)) != len(result):
            raise PlanError(f"{label} must contain unique non-empty lanes")
        return result

    analyses = lane_ids(analysis_lanes, "analysis_lanes")
    implementations = lane_ids(implementation_lanes, "implementation_lanes")
    nodes: list[dict[str, Any]] = []
    for lane in analyses:
        node: dict[str, Any] = {
            "id": f"analysis-{lane}",
            "prompt": (
                f"Analyze the {lane} aspect of this task without editing production files. "
                f"Record concrete files, risks, assumptions, and acceptance evidence.\n\nTask:\n{prompt}"
            ),
            "working_dir": root,
            "mode": "worktree",
            "max_retries": 1,
        }
        if analysis_provider:
            node["provider"] = analysis_provider
        nodes.append(node)

    analysis_ids = [f"analysis-{lane}" for lane in analyses]
    synthesis: dict[str, Any] = {
        "id": "plan-synthesis",
        "prompt": (
            "Synthesize the completed analysis artifacts into one implementation plan. "
            "Resolve contradictions, assign file ownership, and state which implementation lanes "
            "must remain serial.\n\nOriginal task:\n" + prompt
        ),
        "depends_on": analysis_ids,
        "working_dir": root,
        "mode": "worktree",
        "max_retries": 1,
    }
    if planner_provider:
        synthesis["provider"] = planner_provider
    nodes.append(synthesis)

    implementation_ids: list[str] = []
    for lane in implementations:
        node = {
            "id": f"implementation-{lane}",
            "prompt": (
                f"Implement only the {lane} portion of the synthesized plan for the task below. "
                "Respect the file ownership boundaries, run focused checks, and report evidence.\n\n"
                f"Task:\n{prompt}"
            ),
            "depends_on": ["plan-synthesis"],
            "working_dir": root,
            "mode": "worktree",
            "max_retries": 1,
        }
        if implementation_provider:
            node["provider"] = implementation_provider
        nodes.append(node)
        implementation_ids.append(node["id"])

    review: dict[str, Any] = {
        "id": "integration-review",
        "kind": "review",
        "review": True,
        "autofix": True,
        "max_repairs": 2,
        "prompt": (
            "Integrate the implementation worktrees, run the focused acceptance checks, and review "
            "all preceding evidence. Finish with exactly one AUDIT_VERDICT: PASS, "
            "AUDIT_VERDICT: REVISION_REQUIRED, or AUDIT_VERDICT: HUMAN_REQUIRED.\n\n"
            f"Task:\n{prompt}"
        ),
        "depends_on": implementation_ids,
        "working_dir": root,
        "mode": "worktree",
        "max_retries": 1,
    }
    if review_provider:
        review["provider"] = review_provider
    nodes.append(review)
    return Plan.from_dict(
        {
            "name": "parallel-task",
            "description": "Deterministic fan-out, synthesis, implementation fan-out, and review checkpoint.",
            "max_parallel": max_parallel,
            "nodes": nodes,
        }
    )
