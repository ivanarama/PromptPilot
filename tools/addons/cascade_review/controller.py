"""Run the optional ``review_chain`` as a durable additive controller.

PromptPilot's core workflow has one reviewer slot.  A configured review chain
needs a small coordinator around that slot: the coordinator selects the next
reviewer, records intermediate PASS decisions without closing the workflow,
and routes a revision to the configured fixer.  The core worker still owns all
agent tasks, gates, retries, and ordinary workflow transitions.

The controller deliberately uses the existing SQLite workflow primitives rather
than inventing a second task queue.  All mutations are short transactions and
are represented by ``cascade.*`` events, so a restart can safely resume from
the last durable phase.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from promptpilot import db, workflows
from promptpilot.models import (
    FindingSeverity,
    FindingStatus,
    ReviewVerdict,
    WorkflowEventCreate,
    WorkflowHumanInput,
    WorkflowInDB,
    WorkflowRole,
    WorkflowStatus,
    WorkflowReviewDecision,
)


STATE_VERSION = 2
DEFAULT_POLL_SECONDS = 15
DEFAULT_LOG_PATH = Path.home() / ".promptpilot" / "cascade-review.log"
DEFAULT_STATE_PATH = Path.home() / ".promptpilot" / "cascade-review-state.json"

# A review-chain slot may use an explicit budget, or the additive controller
# can derive a conservative budget from the severity of the open findings.
# The derived values are deliberately small: the workflow-level round budget
# remains the final safety cap.
DEFAULT_AUTO_ATTEMPT_POLICY = {
    "mode": "manual",
    "default": 2,
    "max": 3,
    "by_severity": {
        "low": 1,
        "medium": 2,
        "high": 3,
        "blocker": 3,
    },
}
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "blocker": 4}


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _minutes(value: str) -> int:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return max(0, min(23, hour)) * 60 + max(0, min(59, minute))
    except (TypeError, ValueError):
        return 0


def in_window(slot: dict[str, Any], now_utc: datetime | None = None) -> bool:
    """Return whether a configured slot may run now.

    The UI stores Moscow as ``tz_offset_hours=3``.  A named ``tz`` is accepted
    for hand-written configs and currently maps the supported Moscow value to
    the same fixed offset.
    """

    window = slot.get("window") or {}
    if not window:
        return True
    now_utc = now_utc or datetime.now(timezone.utc)
    offset = window.get("tz_offset_hours")
    if offset is None:
        offset = 3 if str(window.get("tz", "")).lower() in {
            "europe/moscow", "msk", "мск"
        } else 0
    try:
        local = now_utc + timedelta(hours=float(offset))
    except (TypeError, ValueError):
        local = now_utc
    current = local.hour * 60 + local.minute
    start = _minutes(window.get("from", "22:00"))
    end = _minutes(window.get("to", "04:00"))
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state is not an object")
    except (OSError, ValueError, json.JSONDecodeError):
        raw = {}
    # Older prototype state used top-level chain_pos/slot_rounds.  Do not
    # silently reinterpret it as a new state machine; durable DB events rebuild
    # the current slot on the first pass.
    if not isinstance(raw.get("workflows"), dict):
        raw = {"version": STATE_VERSION, "workflows": {}}
    raw["version"] = STATE_VERSION
    return raw


def save_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class CascadeController:
    """One-process controller for all workflows with ``review_chain``."""

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        log_path: Path = DEFAULT_LOG_PATH,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.state = load_state(self.state_path)
        self._external_logger = logger

    def log(self, message: str) -> None:
        line = f"{_now_iso()} {message}"
        if self._external_logger:
            self._external_logger(line)
        else:
            print(line, flush=True)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def _chain(workflow: WorkflowInDB) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        chain = workflow.config.get("review_chain") or {}
        if not isinstance(chain, dict) or not chain.get("enabled"):
            return {}, []
        steps = [
            _json_copy(step)
            for step in (chain.get("steps") or [])
            if isinstance(step, dict) and str(step.get("provider") or "").strip()
        ]
        for index, step in enumerate(steps):
            step["slot"] = index + 1
            step.setdefault("fixer", "executor")
            step.setdefault("blocking", True)
            step.setdefault("max_rounds", 2)
            step.setdefault("on_exhaust", "human")
        return chain, steps

    @staticmethod
    def _attempt_policy(chain: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded attempt policy without changing the stored config."""
        raw = chain.get("attempt_policy")
        raw = raw if isinstance(raw, dict) else {}
        policy = _json_copy(DEFAULT_AUTO_ATTEMPT_POLICY)
        policy.update({key: value for key, value in raw.items()
                       if key in {"mode", "default", "max", "by_severity"}})
        try:
            policy["default"] = max(1, min(10, int(policy["default"])))
        except (TypeError, ValueError):
            policy["default"] = DEFAULT_AUTO_ATTEMPT_POLICY["default"]
        try:
            policy["max"] = max(1, min(10, int(policy["max"])))
        except (TypeError, ValueError):
            policy["max"] = DEFAULT_AUTO_ATTEMPT_POLICY["max"]
        policy["max"] = max(policy["max"], policy["default"])
        if not isinstance(policy.get("by_severity"), dict):
            policy["by_severity"] = {}
        cleaned = {}
        for severity, fallback in DEFAULT_AUTO_ATTEMPT_POLICY["by_severity"].items():
            value = policy["by_severity"].get(severity, fallback)
            try:
                cleaned[severity] = max(1, min(policy["max"], int(value)))
            except (TypeError, ValueError):
                cleaned[severity] = min(policy["max"], fallback)
        policy["by_severity"] = cleaned
        policy["mode"] = "auto" if str(policy.get("mode", "manual")).lower() == "auto" else "manual"
        return policy

    @staticmethod
    def _finding_value(finding: Any, name: str) -> str:
        value = getattr(finding, name, "")
        return str(getattr(value, "value", value) or "").lower()

    def _max_rounds_for_slot(
        self,
        workflow: WorkflowInDB,
        chain: dict[str, Any],
        slot: dict[str, Any],
        decision: WorkflowReviewDecision | None = None,
    ) -> tuple[int, str]:
        """Resolve the retry budget and expose why that value was selected."""
        policy = self._attempt_policy(chain)
        mode = str(slot.get("attempts_mode") or policy["mode"]).lower()
        if mode != "auto":
            try:
                value = max(1, min(10, int(slot.get("max_rounds") or 2)))
            except (TypeError, ValueError):
                value = 2
            return value, "manual"

        findings: list[Any] = []
        try:
            current_round_no = int(workflow.current_round or 0)
            findings.extend(
                finding for finding in db.list_workflow_findings(workflow.id)
                if int(getattr(finding, "last_seen_round", 0) or 0) == current_round_no
            )
        except Exception:  # noqa: BLE001 - a policy fallback must not stop routing
            pass
        if decision is not None:
            findings.extend(decision.findings)
        open_severities = {
            self._finding_value(item, "severity")
            for item in findings
            if self._finding_value(item, "status") in {"open", "reopened"}
        }
        selected = max(open_severities, key=lambda item: _SEVERITY_RANK.get(item, 0), default="")
        value = policy["by_severity"].get(selected, policy["default"])
        value = max(1, min(policy["max"], int(value)))
        return value, f"auto:{selected or 'default'}"

    @staticmethod
    def _current_round(workflow: WorkflowInDB):
        rounds = db.list_workflow_rounds(workflow.id)
        return next(
            (item for item in rounds if item.round_no == workflow.current_round),
            None,
        )

    @staticmethod
    def _completed_reviewer(workflow: WorkflowInDB):
        current = CascadeController._current_round(workflow)
        if not current:
            return None
        runs = [
            item for item in db.list_workflow_runs(current.id)
            if item.role is WorkflowRole.REVIEWER and item.status.value == "completed"
        ]
        return max(runs, key=lambda item: item.attempt_no) if runs else None

    @staticmethod
    def _pending_reviewer(workflow: WorkflowInDB) -> bool:
        current = CascadeController._current_round(workflow)
        if not current:
            return False
        return any(
            item.role is WorkflowRole.REVIEWER
            and item.status.value in {"pending", "running"}
            for item in db.list_workflow_runs(current.id)
        )

    @staticmethod
    def _events(workflow_id: str):
        return db.list_workflow_events(workflow_id, limit=5000)

    @staticmethod
    def _event_for_run(events, event_type: str | None, run_id: str) -> bool:
        for event in events:
            if event_type and event.event_type != event_type:
                continue
            if not event.event_type.startswith("cascade."):
                continue
            if event.run_id == run_id or event.payload.get("run_id") == run_id:
                return True
        return False

    def _workflow_state(
        self,
        workflow: WorkflowInDB,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        records = self.state.setdefault("workflows", {})
        current_stage = workflow.current_stage_id or "planless"
        existing = records.get(workflow.id)
        if not isinstance(existing, dict) or existing.get("stage_id") != current_stage:
            slot = 0
            # Rebuild after a controller restart from durable cascade events.
            for event in reversed(self._events(workflow.id)):
                if event.payload.get("stage_id", current_stage) != current_stage:
                    continue
                if event.event_type in {"cascade.slot_passed", "cascade.slot_skipped"}:
                    slot = int(event.payload.get("next_slot", slot))
                    break
            existing = {
                "stage_id": current_stage,
                "slot": max(0, min(slot, len(steps))),
                "attempts": {},
                "last_run_id": None,
            }
            records[workflow.id] = existing
        existing["slot"] = max(0, min(int(existing.get("slot", 0)), len(steps)))
        existing.setdefault("attempts", {})
        existing.setdefault("last_run_id", None)

        # The JSON state is only a cache.  Reconcile the fields that affect
        # routing from SQLite events on every pass so a process termination
        # between two controller transactions cannot send the next task to an
        # old reviewer slot or reset its configured retry count.
        stage_events = [
            event for event in self._events(workflow.id)
            if event.payload.get("stage_id", current_stage) == current_stage
        ]
        latest_slot_boundary = max(
            (
                event.seq for event in stage_events
                if event.event_type in {"cascade.slot_passed", "cascade.slot_skipped"}
            ),
            default=0,
        )
        for event in stage_events:
            if event.seq < latest_slot_boundary:
                continue
            if event.event_type != "cascade.revision_requested":
                continue
            slot_index = max(0, int(event.payload.get("slot", 1)) - 1)
            existing["slot"] = min(slot_index, len(steps))
            attempt = int(event.payload.get("attempt", 0) or 0)
            existing["attempts"][str(slot_index)] = max(
                int(existing["attempts"].get(str(slot_index), 0) or 0),
                attempt,
            )
            existing["last_run_id"] = event.payload.get("run_id") or existing.get(
                "last_run_id"
            )
        existing["round"] = workflow.current_round
        return existing

    @staticmethod
    def _base_executor(chain: dict[str, Any], workflow: WorkflowInDB) -> str | None:
        value = chain.get("base_executor_provider")
        if value is not None:
            return value or None
        return (workflow.config.get("roles", {}).get("executor", {}) or {}).get("provider")

    @staticmethod
    def _resolve_fixer(
        slot: dict[str, Any],
        chain: dict[str, Any],
        workflow: WorkflowInDB,
    ) -> str | None:
        fixer = str(slot.get("fixer") or "executor")
        if fixer == "executor":
            return CascadeController._base_executor(chain, workflow)
        if fixer == "self":
            return str(slot.get("provider") or "") or None
        return fixer or None

    @staticmethod
    def _review_context(workflow: WorkflowInDB, slot_index: int) -> str:
        current = CascadeController._current_round(workflow)
        if not current or slot_index <= 0:
            return "(это первая ступень каскадного ревью)"
        reports = []
        for run in db.list_workflow_runs(current.id):
            if run.role is not WorkflowRole.REVIEWER or run.status.value != "completed":
                continue
            result = (run.output or {}).get("result") or ""
            if result:
                reports.append(f"Ревьюер task #{run.task_id}:\n{result}")
        return "\n\n".join(reports[-3:])[-14000:] or "(предыдущих отчётов нет)"

    def _update_runtime_config(
        self,
        workflow: WorkflowInDB,
        *,
        slot: dict[str, Any] | None = None,
        executor_provider: str | None = None,
        reviewer_provider: str | None = None,
        reason: str,
    ) -> WorkflowInDB:
        """Update only additive runtime routing metadata, without auto-advance."""

        with db._connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            if not row:
                raise db.WorkflowNotFoundError(workflow.id)
            config = db._json_load(row["config_json"])
            before = db._json_dump(config)
            chain = config.setdefault("review_chain", {})
            roles = config.setdefault("roles", {})
            executor = roles.setdefault("executor", {})
            reviewer = roles.setdefault("reviewer", {})
            automation = config.setdefault("automation", {})

            if "base_executor_provider" not in chain:
                chain["base_executor_provider"] = executor.get("provider")
            if "base_reviewer_provider" not in chain:
                chain["base_reviewer_provider"] = reviewer.get("provider")
            if "base_reviewer_prompt_template" not in chain:
                chain["base_reviewer_prompt_template"] = reviewer.get(
                    "prompt_template", ""
                )

            # The add-on is the owner of reviewer dispatch, review application,
            # and revision resume while a chain is enabled.
            automation["auto_dispatch_reviewer"] = False
            automation["auto_apply_review"] = False
            automation["auto_resume_revision"] = False

            if executor_provider is not None:
                executor["provider"] = executor_provider
            if reviewer_provider is not None:
                reviewer["provider"] = reviewer_provider
            if slot is not None:
                slot_index = int(slot.get("slot", 1)) - 1
                context = self._review_context(workflow, slot_index)
                fixer = self._resolve_fixer(slot, chain, workflow)
                base_prompt = chain.get("base_reviewer_prompt_template") or ""
                if not base_prompt:
                    base_prompt = workflows.DEFAULT_REVIEWER_PROMPT
                reviewer["prompt_template"] = (
                    f"{base_prompt.rstrip()}\n\n"
                    "[КОНТЕКСТ КАСКАДНОГО РЕВЬЮ]\n"
                    f"Ты работаешь на ступени {slot_index + 1}. "
                    f"Твой провайдер: {slot.get('provider')}. "
                    f"При REVISION_REQUIRED исправление будет передано: "
                    f"{fixer or 'не настроено'}.\n"
                    "Не исправляй код в этой задаче ревью; верни машинные строки "
                    "AUDIT_FINDINGS_JSON и AUDIT_VERDICT.\n"
                    "Предыдущие отчёты каскада:\n"
                    f"{context}"
                )

            after = db._json_dump(config)
            if before == after:
                return db._row_to_workflow(row)
            version = int(row["state_version"]) + 1
            now = db._now()
            conn.execute(
                """UPDATE workflows SET config_json=?, state_version=?, updated_at=?
                   WHERE id=? AND state_version=?""",
                (after, version, now, workflow.id, row["state_version"]),
            )
            db._append_workflow_event(conn, WorkflowEventCreate(
                workflow_id=workflow.id,
                event_type="cascade.runtime_configured",
                idempotency_key=f"cascade.runtime_configured:{workflow.id}:v{version}",
                payload={
                    "reason": reason,
                    "slot": slot.get("slot") if slot else None,
                    "executor_provider": executor_provider,
                    "reviewer_provider": reviewer_provider,
                    "automation": {
                        "auto_dispatch_reviewer": False,
                        "auto_apply_review": False,
                        "auto_resume_revision": False,
                    },
                },
            ))
            updated = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            return db._row_to_workflow(updated)

    def _transition_to_reviewing(
        self,
        workflow: WorkflowInDB,
        *,
        slot: dict[str, Any],
        next_slot: int,
        previous_run_id: int,
        findings: list[Any],
    ) -> WorkflowInDB:
        with db._connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            current = WorkflowStatus(row["status"])
            if current is WorkflowStatus.REVIEWING:
                return db._row_to_workflow(row)
            if current is not WorkflowStatus.AWAITING_HUMAN:
                raise db.WorkflowConflictError(
                    f"cascade expected awaiting_human, got {current.value}"
                )
            round_row = workflows._current_round_row(conn, row)
            for finding in findings:
                workflows._upsert_review_finding(
                    conn, workflow.id, round_row["round_no"], finding
                )
            workflow_row = workflows._transition(
                conn,
                row,
                WorkflowStatus.REVIEWING,
                "cascade.slot_passed",
                {
                    "stage_id": row["current_stage_id"] or "planless",
                    "slot": int(slot.get("slot", 1)),
                    "next_slot": next_slot,
                    "run_id": previous_run_id,
                    "provider": slot.get("provider"),
                    "finding_count": len(findings),
                },
                round_id=round_row["id"],
            )
            return db._row_to_workflow(workflow_row)

    def _advance_to_next_slot(
        self,
        workflow: WorkflowInDB,
        state: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> WorkflowInDB | None:
        next_index = int(state["slot"]) + 1
        state["slot"] = next_index
        state["attempts"] = {}
        state["last_run_id"] = None
        if next_index >= len(steps):
            return None
        return workflow

    def _append_marker(
        self,
        workflow_id: str,
        *,
        event_type: str,
        run_id: str | None,
        round_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        key_run = run_id or "none"
        key_round = round_id or "none"
        db.append_workflow_event(WorkflowEventCreate(
            workflow_id=workflow_id,
            round_id=round_id,
            run_id=run_id,
            event_type=event_type,
            idempotency_key=f"{event_type}:{workflow_id}:{key_round}:{key_run}",
            payload={"run_id": run_id, **payload},
        ))

    def _record_review_once(
        self,
        workflow: WorkflowInDB,
        decision: WorkflowReviewDecision,
    ) -> WorkflowInDB:
        """Record a verdict, reusing a durable core verdict after a takeover.

        The core may have parsed the reviewer output just before the cascade
        controller disabled the core review flags. Replaying the same report
        then collides on the strict finding idempotency key. If the durable
        round already contains the same revision verdict, continue from that
        projection instead of treating the collision as a new failure.
        """
        try:
            return workflows.record_review(
                workflow.id,
                decision.model_copy(update={"expected_version": workflow.state_version}),
            )
        except db.WorkflowConflictError as exc:
            if "idempotency key" not in str(exc):
                raise
            if decision.verdict is not ReviewVerdict.REVISION_REQUIRED:
                raise
            refreshed = db.get_workflow(workflow.id)
            if not refreshed:
                raise
            current = self._current_round(refreshed)
            if not current:
                raise
            already_applied = any(
                event.event_type == "review.revision_required"
                and event.round_id == current.id
                for event in self._events(refreshed.id)
            )
            if not already_applied:
                raise
            self.log(
                f"{workflow.slug}: повторный verdict уже записан core; "
                "продолжаю каскад без повторной записи findings"
            )
            return refreshed

    def _hold_for_human(
        self,
        workflow: WorkflowInDB,
        *,
        slot: dict[str, Any],
        event_type: str,
        reason: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowInDB:
        with db._connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            state = WorkflowStatus(row["status"])
            round_row = workflows._current_round_row(conn, row)
            payload = {
                "stage_id": row["current_stage_id"] or "planless",
                "slot": int(slot.get("slot", 1)),
                "reason": reason,
            }
            if metadata:
                payload.update(metadata)
            if state in {WorkflowStatus.REVISION_REQUIRED, WorkflowStatus.REVIEWING}:
                row = workflows._transition(
                    conn, row, WorkflowStatus.AWAITING_HUMAN,
                    event_type, payload, round_id=round_row["id"],
                )
            elif state is WorkflowStatus.AWAITING_HUMAN:
                row = workflows._touch(
                    conn, row, event_type, payload, round_id=round_row["id"],
                )
            if run_id:
                db._append_workflow_event(conn, WorkflowEventCreate(
                    workflow_id=workflow.id,
                    round_id=round_row["id"],
                    run_id=run_id,
                    event_type="cascade.review_processed",
                    idempotency_key=(
                        f"cascade.review_processed:{workflow.id}:"
                        f"{round_row['id']}:{run_id}"
                    ),
                    payload={
                        "run_id": run_id,
                        "slot": int(slot.get("slot", 1)),
                        "reason": reason,
                    },
                ))
            return db._row_to_workflow(row)

    def _resume_chain_revision(
        self,
        workflow: WorkflowInDB,
        *,
        chain: dict[str, Any],
        slot: dict[str, Any],
        attempt: int,
        max_rounds: int | None,
        budget_source: str | None,
        fixer: str,
        text: str,
    ) -> WorkflowInDB:
        """Create the next executor round for a chain revision.

        ``workflows.human_input(resume=True)`` also applies the core's
        per-stage revision budget.  That budget is a single-reviewer policy;
        applying it here would make a visually configured cascade stop before
        its later slots or retries.  The add-on owns the slot budget, while
        the core still owns the global workflow round budget and lifecycle
        transitions.
        """

        with db._connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            if not row:
                raise db.WorkflowNotFoundError(workflow.id)
            state = WorkflowStatus(row["status"])
            if state is WorkflowStatus.QUEUED:
                return db._row_to_workflow(row)
            round_row = workflows._current_round_row(conn, row)
            if state not in {
                WorkflowStatus.REVISION_REQUIRED,
                WorkflowStatus.AWAITING_HUMAN,
            } or round_row["status"] != "revision_required":
                raise db.WorkflowConflictError(
                    "cascade revision expected revision_required, "
                    f"got workflow={state.value}, round={round_row['status']}"
                )
            if max_rounds is None:
                max_rounds, budget_source = self._max_rounds_for_slot(
                    workflow, chain, slot
                )
            next_round = int(row["current_round"]) + 1
            if not workflows._check_round_budget(conn, row, next_round):
                payload = {
                    "stage_id": row["current_stage_id"] or "planless",
                    "slot": int(slot.get("slot", 1)),
                    "attempt": attempt,
                    "max_rounds": workflows._max_automated_rounds(row),
                    "refused_round": next_round,
                    "text": text,
                    "fixer": fixer,
                    "attempt_budget": max_rounds,
                    "attempt_budget_source": budget_source,
                }
                if state is WorkflowStatus.AWAITING_HUMAN:
                    row = workflows._touch(
                        conn,
                        row,
                        "cascade.max_rounds",
                        payload,
                        round_id=round_row["id"],
                    )
                else:
                    row = workflows._transition(
                        conn,
                        row,
                        WorkflowStatus.AWAITING_HUMAN,
                        "cascade.max_rounds",
                        payload,
                        round_id=round_row["id"],
                    )
                return db._row_to_workflow(row)

            new_round = workflows._insert_round(
                conn,
                workflow.id,
                next_round,
                base_sha=round_row["candidate_sha"],
                stage_id=row["current_stage_id"],
            )
            row = workflows._workflow_row(conn, workflow.id)
            row = workflows._transition(
                conn,
                row,
                WorkflowStatus.QUEUED,
                "cascade.revision_resumed",
                {
                    "stage_id": row["current_stage_id"] or "planless",
                    "slot": int(slot.get("slot", 1)),
                    "attempt": attempt,
                    "max_rounds": max_rounds,
                    "attempt_budget": max_rounds,
                    "attempt_budget_source": budget_source,
                    "fixer": fixer,
                    "text": text,
                    "previous_round": round_row["round_no"],
                    "new_round": next_round,
                    "stage_revision_budget": "owned_by_cascade",
                },
                round_id=new_round["id"],
            )
            return db._row_to_workflow(row)

    def _resume_recorded_revision(
        self,
        workflow: WorkflowInDB,
        *,
        chain: dict[str, Any],
        state: dict[str, Any],
        slot: dict[str, Any],
        attempt: int,
        fixer: str,
        reason: str,
        max_rounds: int | None = None,
        budget_source: str | None = None,
    ) -> WorkflowInDB:
        if max_rounds is None:
            max_rounds, budget_source = self._max_rounds_for_slot(
                workflow, chain, slot
            )
        index = int(slot.get("slot", 1)) - 1
        resume_text = (
            f"Каскад: исправить замечания ревью-ступени {index + 1} "
            f"провайдером {fixer}. Раунд {attempt}/{max_rounds} "
            f"({budget_source or 'manual'})."
        )
        configured = self._update_runtime_config(
            workflow,
            slot=slot,
            executor_provider=fixer,
            reviewer_provider=str(slot["provider"]),
            reason=reason,
        )
        resumed = self._resume_chain_revision(
            configured,
            chain=chain,
            slot=slot,
            attempt=attempt,
            max_rounds=max_rounds,
            budget_source=budget_source,
            fixer=fixer,
            text=resume_text,
        )
        if resumed.status is WorkflowStatus.AWAITING_HUMAN:
            save_state(self.state, self.state_path)
            return resumed
        advanced = workflows.advance_workflow(resumed.id)
        self.log(
            f"{workflow.slug}: ступень {index + 1} REVISION_REQUIRED → "
            f"исправление provider={fixer}, раунд {attempt}/{max_rounds} "
            f"({budget_source or 'manual'})"
        )
        return advanced

    def _dispatch_reviewer(
        self,
        workflow: WorkflowInDB,
        state: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> WorkflowInDB:
        index = int(state["slot"])
        if index >= len(steps):
            return workflow
        slot = steps[index]
        if not in_window(slot):
            window = slot.get("window") or {}
            outside_policy = str(window.get("outside") or "wait").lower()
            if outside_policy == "skip":
                self.log(
                    f"{workflow.slug}: ступень {index + 1} вне окна — пропускаю"
                )
                with db._connect(immediate=True) as conn:
                    row = conn.execute(
                        "SELECT * FROM workflows WHERE id = ?", (workflow.id,)
                    ).fetchone()
                    round_row = workflows._current_round_row(conn, row)
                    next_slot = index + 1
                    row = workflows._touch(
                        conn, row, "cascade.slot_skipped",
                        {
                            "stage_id": row["current_stage_id"] or "planless",
                            "slot": index + 1,
                            "next_slot": next_slot,
                            "reason": "outside_window",
                            "provider": slot.get("provider"),
                        },
                        round_id=round_row["id"],
                    )
                    workflow = db._row_to_workflow(row)
                state["slot"] = next_slot
                state["attempts"] = {}
                state["last_run_id"] = None
                if next_slot >= len(steps):
                    return self._hold_for_human(
                        workflow,
                        slot=slot,
                        event_type="cascade.all_slots_skipped",
                        reason="Все ступени каскада сейчас вне разрешённого окна.",
                    )
                return self._dispatch_reviewer(workflow, state, steps)

            # A checked "only in window" option means wait for the next poll;
            # silently dispatching now would contradict the UI, while
            # stopping for a human would defeat the unattended pipeline.
            if state.get("window_wait_slot") != index:
                self.log(
                    f"{workflow.slug}: ступень {index + 1} вне окна — жду "
                    f"{window.get('from', '22:00')}–{window.get('to', '04:00')}"
                )
                state["window_wait_slot"] = index
            return workflow
        state.pop("window_wait_slot", None)

        workflow = self._update_runtime_config(
            workflow,
            slot=slot,
            reviewer_provider=str(slot["provider"]),
            reason="select reviewer slot",
        )
        current = db.get_workflow(workflow.id)
        if not current or current.status is not WorkflowStatus.REVIEWING:
            return current or workflow
        if self._pending_reviewer(current):
            return current
        result = workflows._dispatch_configured_role(current, WorkflowRole.REVIEWER)
        task_id = result.task.id
        state["last_run_id"] = result.run.id
        self.log(
            f"{current.slug}: запущен ревью-слот {index + 1}/{len(steps)} "
            f"provider={slot['provider']} task=#{task_id}"
        )
        return result.workflow

    def _gate_is_green(self, workflow: WorkflowInDB) -> bool:
        current = self._current_round(workflow)
        if not current:
            return False
        return any(
            event.event_type == "gate.passed"
            and event.round_id == current.id
            for event in self._events(workflow.id)
        )

    def _apply_revision(
        self,
        workflow: WorkflowInDB,
        state: dict[str, Any],
        slot: dict[str, Any],
        decision: WorkflowReviewDecision,
        chain: dict[str, Any],
    ) -> WorkflowInDB:
        index = int(state["slot"])
        attempts = int(state["attempts"].get(str(index), 0)) + 1
        state["attempts"][str(index)] = attempts
        max_rounds, budget_source = self._max_rounds_for_slot(
            workflow, chain, slot, decision
        )
        if attempts > max_rounds:
            policy = str(slot.get("on_exhaust") or "human")
            if policy == "accept_if_gate_green" and self._gate_is_green(workflow):
                if index + 1 < len(self._chain(workflow)[1]):
                    passed = decision.model_copy(update={"verdict": ReviewVerdict.PASS})
                    return self._apply_pass(workflow, state, passed, chain)
                try:
                    return self._record_review_once(
                        workflow,
                        decision.model_copy(update={"verdict": ReviewVerdict.PASS}),
                    )
                except db.WorkflowConflictError:
                    pass
            return self._hold_for_human(
                workflow,
                slot=slot,
                event_type="cascade.exhausted",
                reason=(
                    f"Лимит попыток исчерпан ({attempts - 1}/{max_rounds}); "
                    f"бюджет {budget_source}; политика: {policy}."
                ),
                run_id=str(state.get("last_run_id") or "") or None,
                metadata={
                    "attempt": attempts - 1,
                    "attempt_budget": max_rounds,
                    "attempt_budget_source": budget_source,
                    "on_exhaust": policy,
                },
            )

        recorded = self._record_review_once(workflow, decision)
        current_round = self._current_round(recorded)
        self._append_marker(
            recorded.id,
            event_type="cascade.revision_requested",
            run_id=str(state.get("last_run_id") or "") or None,
            round_id=current_round.id if current_round else None,
            payload={
                "stage_id": recorded.current_stage_id or "planless",
                "slot": index + 1,
                "attempt": attempts,
                "attempt_budget": max_rounds,
                "attempt_budget_source": budget_source,
            },
        )
        fixer = self._resolve_fixer(slot, chain, recorded)
        if not fixer:
            return self._hold_for_human(
                recorded,
                slot=slot,
                event_type="cascade.no_fixer",
                reason="Для ступени не выбран провайдер исправления.",
                run_id=str(state.get("last_run_id") or "") or None,
            )
        return self._resume_recorded_revision(
            recorded,
            chain=chain,
            state=state,
            slot=slot,
            attempt=attempts,
            fixer=fixer,
            reason="route revision to configured fixer",
            max_rounds=max_rounds,
            budget_source=budget_source,
        )

    def _apply_pass(
        self,
        workflow: WorkflowInDB,
        state: dict[str, Any],
        decision: WorkflowReviewDecision,
        chain: dict[str, Any],
    ) -> WorkflowInDB:
        index = int(state["slot"])
        steps = self._chain(workflow)[1]
        slot = steps[index]
        reviewer = self._completed_reviewer(workflow)
        if not reviewer:
            return workflow
        if index + 1 < len(steps):
            if any(
                finding.severity in {FindingSeverity.BLOCKER, FindingSeverity.HIGH}
                and finding.status in {FindingStatus.OPEN, FindingStatus.REOPENED}
                for finding in decision.findings
            ):
                return self._hold_for_human(
                    workflow,
                    slot=slot,
                    event_type="cascade.open_blocker",
                    reason="Ревью вернул PASS, но оставил открытый blocker/high finding.",
                )
            transitioned = self._transition_to_reviewing(
                workflow,
                slot=slot,
                next_slot=index + 2,
                previous_run_id=reviewer.id,
                findings=decision.findings,
            )
            state["slot"] = index + 1
            state["attempts"] = {}
            state["last_run_id"] = None
            state["round"] = transitioned.current_round
            return self._dispatch_reviewer(transitioned, state, steps)

        # The final slot is allowed to use the core's normal PASS transition:
        # it closes the current stage or advances to the next one atomically.
        base_executor = self._base_executor(chain, workflow)
        configured = self._update_runtime_config(
            workflow,
            executor_provider=base_executor,
            reviewer_provider=str(slot["provider"]),
            reason="restore executor after final review slot",
        )
        completed = self._record_review_once(configured, decision)
        state["slot"] = 0
        state["attempts"] = {}
        state["last_run_id"] = None
        if completed.status is WorkflowStatus.QUEUED:
            completed = workflows.advance_workflow(completed.id)
        self.log(f"{workflow.slug}: каскад завершён — финальная ступень PASS")
        return completed

    def _process_completed_review(
        self,
        workflow: WorkflowInDB,
        state: dict[str, Any],
        chain: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> WorkflowInDB:
        reviewer = self._completed_reviewer(workflow)
        if not reviewer:
            return workflow
        events = self._events(workflow.id)
        if self._event_for_run(events, None, str(reviewer.id)):
            return workflow
        index = int(state["slot"])
        slot = steps[index]
        report = (reviewer.output or {}).get("result") or ""
        decision = workflows.parse_reviewer_report(report)
        if not decision:
            # Let the existing Jev/verdict watcher repair invalid output, but
            # leave the chain paused instead of guessing a PASS.
            state["last_run_id"] = reviewer.id
            return self._hold_for_human(
                workflow,
                slot=slot,
                event_type="cascade.invalid_review_output",
                reason="Ревьюер не вернул валидные AUDIT_FINDINGS_JSON/AUDIT_VERDICT.",
                run_id=str(reviewer.id),
            )
        state["last_run_id"] = reviewer.id
        if decision.verdict is ReviewVerdict.HUMAN_REQUIRED:
            recorded = self._record_review_once(workflow, decision)
            return self._hold_for_human(
                recorded,
                slot=slot,
                event_type="cascade.human_required",
                reason="Ревьюер явно запросил решение человека.",
                run_id=str(reviewer.id),
            )
        if decision.verdict is ReviewVerdict.REVISION_REQUIRED:
            if slot.get("blocking", True) is False:
                advisory = decision.model_copy(update={"verdict": ReviewVerdict.PASS})
                return self._apply_pass(workflow, state, advisory, chain)
            current = self._current_round(workflow)
            already_recorded = any(
                event.event_type == "review.revision_required"
                and event.round_id == (current.id if current else None)
                for event in self._events(workflow.id)
            )
            if already_recorded:
                # Core automation may have projected an old chain review to
                # ``awaiting_human`` before this controller took ownership.
                # Do not call record_review a second time: its finding event
                # is already durable and would collide on the idempotency key.
                attempts = int(state["attempts"].get(str(state["slot"]), 0) or 0) + 1
                state["attempts"][str(state["slot"])] = attempts
                self._append_marker(
                    workflow.id,
                    event_type="cascade.revision_requested",
                    run_id=str(reviewer.id),
                    round_id=current.id if current else None,
                    payload={
                        "stage_id": workflow.current_stage_id or "planless",
                        "slot": int(state["slot"]) + 1,
                        "attempt": attempts,
                        "recovered_existing_review": True,
                    },
                )
                fixer = self._resolve_fixer(slot, chain, workflow)
                if not fixer:
                    return self._hold_for_human(
                        workflow,
                        slot=slot,
                        event_type="cascade.no_fixer",
                        reason="Для ступени не выбран провайдер исправления.",
                        run_id=str(reviewer.id),
                    )
                return self._resume_recorded_revision(
                    workflow,
                    chain=chain,
                    state=state,
                    slot=slot,
                    attempt=attempts,
                    fixer=fixer,
                    reason="recover review already applied by core automation",
                )
            return self._apply_revision(workflow, state, slot, decision, chain)
        return self._apply_pass(workflow, state, decision, chain)

    def process_workflow(self, workflow_id: str) -> WorkflowInDB | None:
        workflow = db.get_workflow(workflow_id)
        if not workflow:
            return None
        chain, steps = self._chain(workflow)
        if not steps or workflow.status in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }:
            return workflow

        automation = workflow.config.get("automation") or {}
        if (
            automation.get("auto_dispatch_reviewer", True)
            or automation.get("auto_apply_review", True)
            or automation.get("auto_resume_revision", True)
        ):
            # Repair configs written by the prototype or by an older UI before
            # the next executor reaches the reviewer boundary.  This is an
            # additive config normalization, not a core state transition.
            workflow = self._update_runtime_config(
                workflow,
                reason="enable cascade controller ownership",
            )

        # Reconcile a task that finished between worker ticks before deciding
        # which slot owns the next action.
        try:
            workflows.sync_all_tasks(workflow.id)
        except Exception as exc:  # noqa: BLE001
            self.log(f"{workflow.slug}: sync перед каскадом: {type(exc).__name__}: {exc}")
        workflow = db.get_workflow(workflow.id) or workflow
        state = self._workflow_state(workflow, steps)
        index = int(state["slot"])
        if index >= len(steps):
            return workflow

        if workflow.status is WorkflowStatus.REVIEWING:
            if self._pending_reviewer(workflow):
                return workflow
            if self._completed_reviewer(workflow):
                # The worker normally projects this to awaiting_human.  Do not
                # dispatch another reviewer until that projection is durable.
                return workflow
            result = self._dispatch_reviewer(workflow, state, steps)
            save_state(self.state, self.state_path)
            return result

        if workflow.status is WorkflowStatus.AWAITING_HUMAN:
            result = self._process_completed_review(workflow, state, chain, steps)
            save_state(self.state, self.state_path)
            return result

        if workflow.status is WorkflowStatus.REVISION_REQUIRED:
            current = self._current_round(workflow)
            events = self._events(workflow.id)
            stage_id = workflow.current_stage_id or "planless"

            # Normal path after a review decision.  If the controller was
            # restarted after ``record_review`` but before the next round was
            # created, this durable marker lets it continue with the same
            # configured fixer instead of leaving the workflow stuck.
            marker = next(
                (
                    event for event in reversed(events)
                    if event.event_type == "cascade.revision_requested"
                    and event.round_id == (current.id if current else None)
                    and event.payload.get("stage_id", stage_id) == stage_id
                ),
                None,
            )
            if marker:
                slot_index = max(0, int(marker.payload.get("slot", 1)) - 1)
                if slot_index < len(steps):
                    state["slot"] = slot_index
                    attempt = max(
                        int(marker.payload.get("attempt", 1) or 1),
                        int(state["attempts"].get(str(slot_index), 0) or 0),
                    )
                    state["attempts"][str(slot_index)] = attempt
                    slot = steps[slot_index]
                    fixer = self._resolve_fixer(slot, chain, workflow)
                    if fixer:
                        result = self._resume_recorded_revision(
                            workflow,
                            chain=chain,
                            state=state,
                            slot=slot,
                            attempt=attempt,
                            fixer=fixer,
                            reason="recover cascade revision after controller restart",
                        )
                        save_state(self.state, self.state_path)
                        return result

            # A crash can occur in the small gap after core
            # ``record_review`` commits and before the marker above is saved.
            # A reviewer revision is still identifiable from the core event
            # and its completed report; deterministic gate revisions have no
            # reviewer run and therefore cannot enter this branch.
            review_event = next(
                (
                    event for event in reversed(events)
                    if event.event_type == "review.revision_required"
                    and event.round_id == (current.id if current else None)
                ),
                None,
            )
            reviewer = self._completed_reviewer(workflow)
            reviewer_decision = workflows.parse_reviewer_report(
                ((reviewer.output or {}).get("result") or "")
            ) if reviewer else None
            if (
                review_event
                and reviewer
                and reviewer_decision
                and reviewer_decision.verdict is ReviewVerdict.REVISION_REQUIRED
            ):
                slot = steps[index]
                attempt = int(state["attempts"].get(str(index), 0) or 0) + 1
                state["attempts"][str(index)] = attempt
                self._append_marker(
                    workflow.id,
                    event_type="cascade.revision_requested",
                    run_id=str(reviewer.id),
                    round_id=current.id if current else None,
                    payload={
                        "stage_id": stage_id,
                        "slot": index + 1,
                        "attempt": attempt,
                        "recovered_without_marker": True,
                    },
                )
                fixer = self._resolve_fixer(slot, chain, workflow)
                if fixer:
                    result = self._resume_recorded_revision(
                        workflow,
                        chain=chain,
                        state=state,
                        slot=slot,
                        attempt=attempt,
                        fixer=fixer,
                        reason="recover markerless cascade revision",
                    )
                    save_state(self.state, self.state_path)
                    return result

            # Deterministic gate failure: the chain does not invent a review
            # verdict, but it can resume the base executor through the core's
            # ordinary gate-retry path.
            recent = events[-1:] if workflow.id else []
            if recent and recent[0].event_type == "gate.failed":
                base = self._base_executor(chain, workflow)
                configured = self._update_runtime_config(
                    workflow,
                    executor_provider=base,
                    reviewer_provider=str(steps[index]["provider"]),
                    reason="resume deterministic gate revision",
                )
                resumed = workflows.human_input(
                    configured.id,
                    WorkflowHumanInput(
                        expected_version=configured.state_version,
                        text="Каскад: повторить этап после неуспешного deterministic gate.",
                        resume=True,
                    ),
                )
                result = workflows.advance_workflow(resumed.id)
                save_state(self.state, self.state_path)
                return result
        return workflow

    def process_all(self) -> int:
        count = 0
        for workflow in db.list_workflows(limit=500):
            chain, steps = self._chain(workflow)
            if not steps:
                continue
            count += 1
            try:
                self.process_workflow(workflow.id)
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"{workflow.slug}: цикл каскада: {type(exc).__name__}: {exc}"
                )
        save_state(self.state, self.state_path)
        return count

    def run_forever(self, *, workflow_id: str | None = None, poll_seconds: int = DEFAULT_POLL_SECONDS) -> None:
        self.log(
            "cascade-review запущен "
            f"({'workflow ' + workflow_id if workflow_id else 'все review_chain-воркфлоу'})"
        )
        while True:
            try:
                if workflow_id:
                    self.process_workflow(workflow_id)
                else:
                    self.process_all()
            except Exception as exc:  # noqa: BLE001
                self.log(f"цикл: {type(exc).__name__}: {exc}")
            time.sleep(max(1, poll_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PromptPilot additive review-chain controller")
    parser.add_argument("workflow_id", nargs="?", help="workflow id/slug, or all workflows")
    parser.add_argument("--once", action="store_true", help="process once and exit")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_PATH)
    args = parser.parse_args(argv)
    controller = CascadeController(state_path=args.state_file, log_path=args.log_file)
    if args.once:
        if args.workflow_id:
            controller.process_workflow(args.workflow_id)
        else:
            controller.process_all()
        return 0
    controller.run_forever(
        workflow_id=args.workflow_id,
        poll_seconds=args.poll_seconds,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
