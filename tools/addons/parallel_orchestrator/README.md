# PromptPilot parallel orchestrator

This is an opt-in add-on for PromptPilot. It keeps the core package and its
SQLite database unchanged and uses only the existing HTTP task API. One
PromptPilot worker remains responsible for execution; this add-on schedules
ordinary tasks and remembers the dependency graph in a separate JSON file.

## What it provides

- DAG plans with explicit `depends_on` edges and cycle validation.
- A bounded fan-out (`max_parallel`, from 1 to 16). A plan can start three or
  four independent analysis tasks in the same wave while waiting for no
  unrelated task.
- Worktree isolation for parallel coding tasks. A `shared_serial` node is
  available when a plan intentionally targets one checkout; it is still
  subject to the global slot limit.
- Dependency context in downstream prompts, including the preceding task IDs,
  summaries, and worktree paths returned by PromptPilot.
- Review/checkpoint nodes with strict `AUDIT_VERDICT: PASS`,
  `AUDIT_VERDICT: REVISION_REQUIRED`, or `AUDIT_VERDICT: HUMAN_REQUIRED` output.
- Optional TypeSafe Jev adjudication. A low-confidence, unknown, or unavailable
  answer fails closed to `human_required`. Automatic repair is bounded by
  `max_repairs` and reuses the completed task worktree when the API reports it.
- Atomic state in `%USERPROFILE%\\.promptpilot\\parallel-orchestrator\\run.json`
  (or an explicit `--state-file`). The state contains no API key.

## Safety boundaries

The add-on does **not** start or stop the PromptPilot server, launch another
worker, write the PromptPilot DB, merge branches, push to GitHub, or silently
resume an existing `awaiting_human` workflow. Human-required nodes are left in
that state for an explicit operator decision. Use one orchestrator instance
per plan/state file; two instances would race on the same task graph.

The existing `verdict-repair-watcher.py` and `cascade-review.py` remain useful
for their current workflow contracts. Do not run two automation controllers
against the same workflow unless their ownership and event scopes are explicit.

If a checkpoint reaches `human_required`, inspect its saved state and resolve
only that node explicitly. `retry` sends it through the scheduler again,
`accept` records an operator acceptance, and `abort` fails the run:

```powershell
python -m tools.addons.parallel_orchestrator resolve .\reader-state.json integration-review retry
```

## Quick start

```powershell
$env:BOOKAPP_DIR = 'C:\Users\Nachfin\Desktop\Projets\BookApp'
python -m tools.addons.parallel_orchestrator validate `
  tools\addons\parallel_orchestrator\examples\bookapp-reader.json

# Inspect the first wave without submitting anything
python -m tools.addons.parallel_orchestrator run `
  tools\addons\parallel_orchestrator\examples\bookapp-reader.json `
  --dry-run

# The PromptPilot API and its single worker must already be running.
python -m tools.addons.parallel_orchestrator run `
  tools\addons\parallel_orchestrator\examples\bookapp-reader.json `
  --state-file "$env:USERPROFILE\.promptpilot\parallel-orchestrator\bookapp-reader.json" `
  --poll-seconds 3
```

For a new large task, `scaffold` emits a deterministic plan with four
independent analysis lanes, a synthesis node, independent implementation
worktrees, and an integration review. Inspect and edit the JSON before the
first run:

```powershell
python -m tools.addons.parallel_orchestrator scaffold `
  --prompt 'Improve the BookApp reader flow' `
  --working-dir "$env:BOOKAPP_DIR" `
  --output .\reader-plan.json
```

The scaffold deliberately asks for explicit file ownership. It does not let a
planner model invent hidden dependencies, because an unreviewed dependency
graph can create unsafe concurrent edits.

To enable Jev for review nodes, set `TYPESAFE_API_KEY` in the process
environment or pass `--jev-key-file` to the command. The key is only used for
the HTTPS request and is never persisted.

The PowerShell wrapper `PromptPilot-Parallel.ps1` invokes this add-on and
exports `PP_CONCURRENCY` for child processes. If the PromptPilot worker is
already running, its concurrency was fixed when that worker started; set
`PP_CONCURRENCY=4` before starting the existing worker to let it execute the
four submitted tasks concurrently. The wrapper does not alter any existing
launcher or service.

## Plan shape

```json
{
  "name": "reader-change",
  "max_parallel": 4,
  "nodes": [
    {
      "id": "api-analysis",
      "prompt": "Inspect the API surface and write an analysis artifact.",
      "working_dir": "${BOOKAPP_DIR}",
      "mode": "worktree",
      "max_retries": 1
    },
    {
      "id": "integration-review",
      "kind": "review",
      "review": true,
      "autofix": true,
      "max_repairs": 2,
      "depends_on": ["api-analysis"],
      "prompt": "Review the dependency result and emit an AUDIT_VERDICT.",
      "working_dir": "${BOOKAPP_DIR}",
      "mode": "worktree"
    }
  ]
}
```

An implementation node can be parallel with another implementation node only
when their worktrees or file ownership are independent. A node that consumes
their combined changes belongs after both dependencies and should be the
single integration point.
