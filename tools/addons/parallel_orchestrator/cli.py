"""Command line entry point for the PromptPilot parallel add-on."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .orchestrator import (
    ParallelOrchestrator,
    Plan,
    PlanError,
    PromptPilotClient,
    RunState,
    StateStore,
    CompositeAdjudicator,
    make_jev_from_env,
    scaffold_plan,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promptpilot-parallel",
        description="Opt-in dependency-aware scheduling on top of the PromptPilot API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a plan and print its dependency waves")
    validate.add_argument("plan", type=Path)

    run = sub.add_parser("run", help="run one plan against a PromptPilot server")
    run.add_argument("plan", type=Path)
    run.add_argument("--base-url", default=os.environ.get("PROMPTPILOT_URL", "http://127.0.0.1:8420"))
    run.add_argument("--state-file", type=Path)
    run.add_argument("--run-id")
    run.add_argument("--poll-seconds", type=float, default=2.0)
    run.add_argument("--max-ticks", type=int)
    run.add_argument("--once", action="store_true", help="submit/poll one tick and exit")
    run.add_argument("--dry-run", action="store_true", help="show ready payloads without calling the API")
    run.add_argument("--jev-url")
    run.add_argument("--jev-key-file", type=Path)

    status = sub.add_parser("status", help="print a saved add-on state file")
    status.add_argument("state_file", type=Path)

    resolve = sub.add_parser(
        "resolve",
        help="explicitly resolve one human_required node in an add-on state file",
    )
    resolve.add_argument("state_file", type=Path)
    resolve.add_argument("node")
    resolve.add_argument("action", choices=("retry", "accept", "abort"))

    scaffold = sub.add_parser(
        "scaffold",
        help="emit a deterministic fan-out/synthesis/implementation/review plan for one large task",
    )
    scaffold.add_argument("--prompt", required=True)
    scaffold.add_argument("--working-dir", required=True)
    scaffold.add_argument("--analysis-lanes", default="architecture,backend,ui,tests")
    scaffold.add_argument("--implementation-lanes", default="backend,ui,tests")
    scaffold.add_argument("--max-parallel", type=int, default=4)
    scaffold.add_argument("--analysis-provider")
    scaffold.add_argument("--planner-provider")
    scaffold.add_argument("--implementation-provider")
    scaffold.add_argument("--review-provider")
    scaffold.add_argument("--output", type=Path)

    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _validate(path: Path) -> int:
    plan = Plan.from_file(path)
    waves = plan.topological_waves()
    print(f"plan: {plan.name}")
    print(f"nodes: {len(plan.nodes)}")
    print(f"max_parallel: {plan.max_parallel}")
    for index, wave in enumerate(waves, 1):
        print(f"wave {index}: {', '.join(wave)}")
    return 0


def _dry_run(plan: Plan, state_file: Path | None, run_id: str | None) -> int:
    store = StateStore(state_file)
    state = store.load(plan) if store.path.exists() else RunState.create(plan, run_id=run_id)
    ready = [node_id for node_id in plan.topological_waves()[0]] if state.status == "pending" else []
    print(f"run_id: {state.run_id}")
    print(f"max_parallel: {plan.max_parallel}")
    print("ready_nodes: " + (", ".join(ready) if ready else "(none)"))
    for node_id in ready[: plan.max_parallel]:
        node = plan.node_map()[node_id]
        print(json.dumps({"node": node_id, "prompt": node.prompt, "mode": node.mode}, ensure_ascii=False))
    return 0


def _run(args: argparse.Namespace) -> int:
    plan = Plan.from_file(args.plan)
    if args.dry_run:
        return _dry_run(plan, args.state_file, args.run_id)
    store = StateStore(args.state_file)
    client = PromptPilotClient(args.base_url)
    jev = make_jev_from_env(args.jev_url, str(args.jev_key_file) if args.jev_key_file else None)
    orchestrator = ParallelOrchestrator(
        plan,
        client,
        store=store,
        run_id=args.run_id,
        adjudicator=CompositeAdjudicator(jev),
    )
    if args.once:
        state = orchestrator.tick()
    else:
        state = orchestrator.run_until_terminal(args.poll_seconds, args.max_ticks)
    _print_json(state.to_dict())
    return 0 if state.status in {"completed", "running", "pending"} else 2


def _status(path: Path) -> int:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    _print_json(value)
    return 0


def _resolve(path: Path, node_id: str, action: str) -> int:
    store = StateStore(path)
    if not path.exists():
        print(f"state file does not exist: {path}", file=sys.stderr)
        return 2
    try:
        state = RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    runtime = state.nodes.get(node_id)
    if runtime is None:
        print(f"unknown node: {node_id}", file=sys.stderr)
        return 2
    if runtime.status != "human_required":
        print(f"node {node_id} is {runtime.status}; resolve only human_required nodes", file=sys.stderr)
        return 2
    runtime.task_id = None
    if action == "retry":
        runtime.status = "pending"
        runtime.phase = "review" if runtime.last_decision.get("action") == "REPAIR" else "task"
        runtime.reason = "explicitly returned to scheduler by operator"
    elif action == "accept":
        runtime.status = "completed"
        runtime.phase = "done"
        runtime.reason = "explicitly accepted by operator"
    else:
        runtime.status = "failed"
        runtime.phase = "done"
        runtime.reason = "explicitly aborted by operator"
    runtime.touch()
    state.event("operator_resolution", node=node_id, action=action)
    statuses = [item.status for item in state.nodes.values()]
    if all(item == "completed" for item in statuses):
        state.status = "completed"
    elif any(item == "human_required" for item in statuses):
        state.status = "human_required"
    elif any(item in {"failed", "blocked", "cancelled"} for item in statuses):
        state.status = "failed"
    else:
        state.status = "running"
    store.save(state)
    _print_json(state.to_dict())
    return 0


def _scaffold(args: argparse.Namespace) -> int:
    plan = scaffold_plan(
        args.prompt,
        args.working_dir,
        analysis_lanes=[item.strip() for item in args.analysis_lanes.split(",")],
        implementation_lanes=[item.strip() for item in args.implementation_lanes.split(",")],
        max_parallel=args.max_parallel,
        analysis_provider=args.analysis_provider,
        planner_provider=args.planner_provider,
        implementation_provider=args.implementation_provider,
        review_provider=args.review_provider,
    )
    payload = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(payload, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _validate(args.plan)
        if args.command == "run":
            return _run(args)
        if args.command == "status":
            return _status(args.state_file)
        if args.command == "resolve":
            return _resolve(args.state_file, args.node, args.action)
        if args.command == "scaffold":
            return _scaffold(args)
        raise AssertionError(args.command)
    except PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
