# Website workorder integration

`POST /api/integrations/workorders/ensure` creates a manual workflow from a website-authorized order. The request contains a stable `request_id` and a `WorkflowCreate` object. A retry with the same inputs returns the existing workflow; changed inputs with the same key return HTTP 409. The separately approved website plan is expressed through its objective and requirements; supplying PromptPilot's optional staged `plan` is rejected by this endpoint.

The website performs its own order approval before this handoff. The optional guard must be supplied at workflow creation to require subsequent website authorization. A workflow created without it remains an ordinary manual workflow; the endpoint name and request key alone are not proof of execution authorization. The API token is an operator credential, not a restricted website-user role.

The website bridge sets `config.workorder_guard` with `site_url`, `run_id`, `worker_key`, and `lease_token`. Keep the PromptPilot data directory private. Configure both API and worker instances with the same data directory, and set the worker's `WORKORDERS_SITE_URL` and `WORKORDERS_WORKER_TOKEN` outside Git. Bind the PromptPilot API to loopback and configure `PP_API_TOKEN`.

Guarded tasks require an owned local headless provider with a supported narrow-rights adapter, a pinned absolute working directory, zero automatic task retries and a finite timeout of at most one hour. Omitted executor rights become `write`; omitted reviewer rights become `read`. Full rights, remote targets, worktrees, external execution and custom executors are refused. Unsupported narrow adapters fail closed rather than falling back to full access.

Guard presence and repository are bound to the immutable workflow creation event. Effective provider, execution settings and its policy digest are recorded in the append-only dispatch event and checked against the actual task before any worker launch branch and at the owned factory. Guard edits/removal are refused; edits to a bound task or provider execution policy cannot silently widen the run. Ordinary workflows without a guard at creation retain their existing behavior.

Guarded runs are one-shot: recurrence edits are refused, and restart, recurrence recovery and retention cleanup cannot turn a guarded legacy series into unguarded successors. The actual command and environment are built from one validated copy of the bound provider policy; separate settings reloads cannot replace those prepared buffers.

Before each actual provider spawn, including a resource-related spawn retry, the worker checks the website lease. A stage reservation counts the task ID and its attempt start time; repeated authorization checks are idempotent. Authorization RPCs refuse redirects, bound response size and require the configured HTTPS origin (HTTP is allowed only for loopback). The owning worker polls authorization during execution and checks it again before accepting the result. Refusal stays latched; process stop must be confirmed before terminal state, otherwise the existing ownership quarantine retains the run. No background thread can settle it independently. Website and PromptPilot service secrets are removed from the guarded agent's environment.

Guarded runs do not start the separate, unowned verdict-repair subprocess. Missing verdicts follow the normal workflow review/decision path. Filesystem ACL/mode hygiene restricts other OS accounts; it does not isolate a child sharing the worker account. Untrusted execution requires an externally enforced identity or sandbox that denies access to worker-private files. This integration does not provision or attest that boundary.

Authorization refusal is handled separately from process ownership failures. The latter retain PromptPilot's normal quarantine and cleanup handling. Unguarded tasks keep the existing workflow behavior.

This integration does not provision an isolated 1C execution host. The external bridge must provide the approved environment, perform build/runtime checks, collect evidence, and keep result release under human control.

Validation: run `python -m pytest tests` and the repository's Ruff check. When nesting this checkout inside another Python project, explicitly select this `pyproject.toml` and set pytest's `--confcutdir` to this checkout, including for subprocess tests.
