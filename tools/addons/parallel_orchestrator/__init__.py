"""Additive DAG orchestration for PromptPilot.

The add-on talks to the existing PromptPilot HTTP API.  It does not modify the
PromptPilot core, spawn a second worker, or write to the PromptPilot database.
"""

from .orchestrator import (
    Decision,
    NodeRuntime,
    NodeSpec,
    ParallelOrchestrator,
    Plan,
    PlanError,
    PromptPilotClient,
    RuleAdjudicator,
    RunState,
    StateStore,
    TypeSafeJevAdjudicator,
    scaffold_plan,
)

__all__ = [
    "Decision",
    "NodeRuntime",
    "NodeSpec",
    "ParallelOrchestrator",
    "Plan",
    "PlanError",
    "PromptPilotClient",
    "RuleAdjudicator",
    "RunState",
    "StateStore",
    "TypeSafeJevAdjudicator",
    "scaffold_plan",
]
