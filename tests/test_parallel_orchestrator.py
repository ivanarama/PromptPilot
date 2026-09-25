from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.addons.parallel_orchestrator.orchestrator import (
    ParallelOrchestrator,
    Plan,
    PlanError,
    RuleAdjudicator,
    StateStore,
    scaffold_plan,
)


class FakeClient:
    def __init__(self) -> None:
        self.next_id = 1
        self.tasks: dict[int, dict] = {}
        self.created: list[dict] = []

    def create_task(self, payload):
        task_id = self.next_id
        self.next_id += 1
        task = {"id": task_id, "status": "queued", **payload}
        self.tasks[task_id] = task
        self.created.append(task)
        return dict(task)

    def get_task(self, task_id):
        return dict(self.tasks[int(task_id)])

    def finish(self, task_id: int, output: str = "", status: str = "completed", **extra):
        self.tasks[task_id].update({"status": status, "output": output, **extra})


def _plan(nodes, max_parallel=4):
    return Plan.from_dict({"name": "test", "max_parallel": max_parallel, "nodes": nodes})


def _node(node_id, prompt=None, depends_on=None, **kwargs):
    value = {
        "id": node_id,
        "prompt": prompt or node_id,
        "working_dir": "C:/repo",
        "mode": "worktree",
        "depends_on": depends_on or [],
    }
    value.update(kwargs)
    return value


def test_plan_reports_waves_and_rejects_cycles():
    plan = _plan(
        [
            _node("a"),
            _node("b"),
            _node("c", depends_on=["a", "b"]),
        ],
        max_parallel=3,
    )
    assert plan.topological_waves() == [["a", "b"], ["c"]]

    with pytest.raises(PlanError, match="cycle"):
        _plan([_node("a", depends_on=["b"]), _node("b", depends_on=["a"])])


def test_tick_fans_out_then_unlocks_dependent(tmp_path: Path):
    plan = _plan(
        [
            _node("a"),
            _node("b"),
            _node("c"),
            _node("join", depends_on=["a", "b", "c"]),
        ],
        max_parallel=3,
    )
    client = FakeClient()
    scheduler = ParallelOrchestrator(plan, client, StateStore(tmp_path / "state.json"))

    scheduler.tick()
    assert [task["prompt"] for task in client.created] == ["a", "b", "c"]
    assert scheduler.state.status == "running"
    assert scheduler.ready_nodes() == []

    for task in client.created[:3]:
        client.finish(task["id"], output=f"result-{task['id']}")
    scheduler.tick()

    assert [task["prompt"] for task in client.created[:3]] == ["a", "b", "c"]
    assert client.created[-1]["prompt"].startswith("join\n\nCompleted dependency context:")
    assert scheduler.state.nodes["a"].status == "completed"
    assert scheduler.state.nodes["join"].status == "queued"
    assert "result-" in client.created[-1]["prompt"]


def test_modes_map_to_promptpilot_worktree_flag(tmp_path: Path):
    plan = _plan(
        [
            _node("isolated", mode="worktree"),
            {
                "id": "serial",
                "prompt": "serial",
                "working_dir": "C:/repo",
                "mode": "shared_serial",
            },
        ],
        max_parallel=2,
    )
    client = FakeClient()
    scheduler = ParallelOrchestrator(plan, client, StateStore(tmp_path / "state.json"))
    scheduler.tick()
    by_prompt = {task["prompt"]: task for task in client.created}
    assert by_prompt["isolated"]["worktree"] is True
    assert by_prompt["serial"]["worktree"] is False


def test_shared_serial_same_directory_uses_one_scheduler_slot(tmp_path: Path):
    plan = _plan(
        [
            {
                "id": "serial-a",
                "prompt": "serial-a",
                "working_dir": "C:/repo",
                "mode": "shared_serial",
            },
            {
                "id": "serial-b",
                "prompt": "serial-b",
                "working_dir": "C:/repo",
                "mode": "shared_serial",
            },
        ],
        max_parallel=2,
    )
    client = FakeClient()
    scheduler = ParallelOrchestrator(plan, client, StateStore(tmp_path / "state.json"))
    scheduler.tick()
    assert [task["prompt"] for task in client.created] == ["serial-a"]
    client.finish(client.created[0]["id"])
    scheduler.tick()
    assert [task["prompt"] for task in client.created] == ["serial-a", "serial-b"]


def test_review_checkpoint_repairs_in_same_worktree_then_rechecks(tmp_path: Path):
    plan = _plan(
        [
            _node(
                "review",
                kind="review",
                review=True,
                autofix=True,
                max_repairs=1,
            )
        ],
        max_parallel=1,
    )
    client = FakeClient()
    scheduler = ParallelOrchestrator(plan, client, StateStore(tmp_path / "state.json"))

    scheduler.tick()
    review_task = client.created[0]
    client.finish(
        review_task["id"],
        output=(
            "AUDIT_VERDICT: REVISION_REQUIRED\n"
            'AUDIT_FINDINGS_JSON: [{"file": "reader.py", "message": "missing guard"}]'
        ),
        worktree_path="C:/repo/.worktrees/reader-review",
    )
    scheduler.tick()

    repair_task = client.created[-1]
    assert scheduler.state.nodes["review"].phase == "repair"
    assert repair_task["worktree"] is False
    assert repair_task["working_dir"] == "C:/repo/.worktrees/reader-review"
    assert repair_task["parent_task_id"] == review_task["id"]

    client.finish(repair_task["id"], output="fixed")
    scheduler.tick()
    assert scheduler.state.nodes["review"].status == "queued"
    assert scheduler.state.nodes["review"].phase == "review"

    second_review = client.created[-1]
    client.finish(second_review["id"], output="AUDIT_VERDICT: PASS")
    scheduler.tick()

    assert scheduler.state.status == "completed"
    assert scheduler.state.nodes["review"].repair_count == 1


def test_revision_without_repair_budget_becomes_human_required(tmp_path: Path):
    plan = _plan([_node("review", kind="review", review=True, autofix=True, max_repairs=0)])
    client = FakeClient()
    scheduler = ParallelOrchestrator(plan, client, StateStore(tmp_path / "state.json"))
    scheduler.tick()
    client.finish(client.created[0]["id"], output="AUDIT_VERDICT: REVISION_REQUIRED")
    scheduler.tick()
    assert scheduler.state.status == "human_required"
    assert scheduler.state.nodes["review"].status == "human_required"


def test_state_is_atomic_and_plan_bound(tmp_path: Path):
    plan = _plan([_node("a")])
    store = StateStore(tmp_path / "state.json")
    state = ParallelOrchestrator(plan, FakeClient(), store).state
    store.save(state)
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert raw["plan_fingerprint"] == plan.fingerprint()
    assert store.load(plan).run_id == state.run_id
    with pytest.raises(PlanError, match="different plan"):
        store.load(_plan([_node("different")]))


def test_rule_adjudicator_fails_closed_on_missing_verdict():
    node = Plan.from_dict({"name": "x", "nodes": [_node("review", kind="review", review=True)]}).nodes[0]
    decision = RuleAdjudicator().evaluate(node, {"status": "completed", "output": "looks fine"})
    assert decision.action == "HUMAN"


def test_scaffold_creates_explicit_fanout_and_review():
    plan = scaffold_plan(
        "Improve reader flow",
        "C:/repo",
        analysis_lanes=("api", "ui"),
        implementation_lanes=("backend", "frontend"),
        max_parallel=2,
    )
    assert plan.max_parallel == 2
    assert plan.topological_waves() == [
        ["analysis-api", "analysis-ui"],
        ["plan-synthesis"],
        ["implementation-backend", "implementation-frontend"],
        ["integration-review"],
    ]
    review = plan.node_map()["integration-review"]
    assert review.review is True
    assert review.autofix is True
    assert review.max_repairs == 2
