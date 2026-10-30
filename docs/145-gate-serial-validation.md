# #145 serial gate execution: author evidence

Work unit: `145-gate-serial`, branch `fix/145-serial-gate-20261030`.
Required base: `8cd8628c7c5cb00578a7203bee52ecfb86d44101`; it was already HEAD
and an ancestor at the start. No merge or conflict resolution was needed.
The checkout had no Git author identity configured; the local commit uses
invocation-only `Codex <codex@local.invalid>` without changing Git settings.

This is author evidence for this bounded change, not independent review or CI
evidence, and does not close the remaining parent issue.

## Implementation

- One stable auxiliary SQLite file per workflow under the trusted data root,
  with a nonblocking writer transaction. Application code never unlinks it.
  A missing established file, corruption or open error fails closed.
- Primary `STARTED` and the frozen scope commit before any process factory.
  The primary writer is released during shell execution. Scope includes the
  exact completed executor identity and input/output digests, round/stage,
  repository/cwd, effective ordered/deduplicated commands, configuration and argv.
- `SETTLED` follows successful owned lifetime cleanup, or definite no-factory
  validation. Projection, gate event and `APPLIED` share one primary transaction.
  A persisted settled result replays without executing shell commands.
- An abandoned `STARTED` or uncertain factory/cleanup becomes one `UNKNOWN`
  event. Versions, new rounds, disabled automation, elapsed time and PID data
  cannot remove that barrier. Cancellation retains its state and history.
- Manual gate/review, ordinary input/resume/checkpoint restart, configuration,
  dispatch and round/run creation cannot bypass unresolved ownership. Execution
  history is retained and has no cascading deletion.
- Separate engineering recovery requires exact execution ID, one-use nonce,
  workflow version, nonblank reason and a strict boolean acknowledgement. It
  records the operator's claim with `cleanup_confirmed=false`, preserves the
  unknown event and returns to the same round's `GATING` without an executor
  retry. Terminal workflows are never resurrected.

API and CLI recovery examples are in `WORKFLOW_AUTOMATION_GUIDE.md`.

## Actual local checks

Environment: Windows, Python 3.12, pytest 9.1.1, Ruff 0.16.9; Node is available.
Tests use disposable databases and the repository's isolated pytest home.

- Focused compatibility command: `python -m pytest -q -W error
  tests/test_workflow_automation.py tests/test_workflow_gate_failures.py
  tests/test_workflow_gate_process_lifetime.py tests/test_workflow_report_boundary.py
  tests/test_workflow_w1.py tests/test_workflow_db.py`: **49 passed**.
- `python -m pytest -q -W error tests/test_workflow_gate_serial.py`:
  **34 passed in 50.54 seconds**, including interrupted auxiliary acquisition
  cleanup and cancellation preventing remaining commands from starting.
- With the default-provider override cleared in the child environment,
  `python -m pytest -q -W error tests/test_workorder_integration.py`:
  **7 passed in 22.36 seconds**.
- `ruff check promptpilot tests tools main.py`: **passed**.
- `git diff --check`: **passed**.
- Full mandatory `python -m pytest -q -W error` run on the final source:
  **2029 passed, 20 skipped in 591.52 seconds (9:51)**, exit code 0.
  Its child test environment cleared `GH_EXE`, `GO_EXE` and `PP_DEFAULT_CLI`
  to exercise the repository defaults. No source changes followed this run.

An earlier full-suite attempt stopped at an unrelated ambient-environment
failure in `test_provider_env_exposes_configured_pipeline_tools_on_path`
(269 passed, 14 skipped): inherited `GH_EXE` overrode the fixture's fake path.
The provider implementation is unchanged. A subsequent full run completed with
**5 failed, 2023 passed, 20 skipped** in 605.87 seconds: all five failures were
existing workorder fixtures using the ambient default Codex provider, which
cannot satisfy their required narrow write rights. The repository's default
provider is Claude. Clearing `GH_EXE`, `GO_EXE` and `PP_DEFAULT_CLI` only in the
child test environment produced the successful final full run above. No
permanent environment or provider/rights policy changes were made.

The first interruption probe used `KeyboardInterrupt` through ASGI and exposed
an AnyIO shutdown hang after pytest printed its summary. Those exact owned
test sessions were stopped; that summary is not counted as a successful run.
The corrected test injects a custom `BaseException` through the public Python
coordinator, then verifies the barrier through the public ASGI endpoint; its
test process exits successfully.

Final inspection also added explicit connection cleanup if auxiliary lock
acquisition is interrupted before yielding, plus a regression for that public
coordinator path. The partial full run was stopped and restarted after that
source change; the final successful run includes it.

## Regression coverage and limits

The serial suite calls real ASGI endpoints in two separate Python processes,
executes finite harmless commands, and checks one append effect while another
coordinator returns promptly. It also exercises another workflow and an
unrelated primary writer, same-process separate connections, stale admission,
six process-crash windows (including a live command), factory/cleanup/timeout
ambiguity, interruption, all three transaction boundaries with rollback and
lost commit acknowledgement, old scope rejection, cancellation, manual/API
bypasses and exact one-use recovery. Fixture cleanup uses owned handles only.

The strict UNKNOWN path does not implement automatic handle recovery after an
ambiguous cleanup. Engineering acknowledgement is not machine cleanup proof or
rollback of external effects. The scope is a snapshot of known inputs, not a
promise that arbitrary shell commands or outside files are idempotent.
No distributed lock, ELICIT, policy/CI changes, production actions, Telegram
messages or feature activation are included. Only the local Windows/Python
3.12 checks are executable here; the four-platform/version CI matrix and fresh
independent exact-commit review remain external release gates. No GitHub writes
or CI/review/merge success are claimed.
