# Website workorder integration

`POST /api/integrations/workorders/ensure` creates a manual workflow from a website-authorized order. The request contains a stable `request_id` and a `WorkflowCreate` object. A retry with the same inputs returns the existing workflow; changed inputs with the same key return HTTP 409. The separately approved website plan is expressed through its objective and requirements; supplying PromptPilot's optional staged `plan` is rejected by this endpoint.

The website bridge sets `config.workorder_guard` with `site_url`, `run_id`, `worker_key`, and `lease_token`. Keep the PromptPilot data directory private. Configure both API and worker instances with the same data directory, and set the worker's `WORKORDERS_SITE_URL` and `WORKORDERS_WORKER_TOKEN` outside Git. Bind the PromptPilot API to loopback and configure `PP_API_TOKEN`.

Guarded tasks require an owned local headless provider, a pinned working directory, zero automatic task retries, a finite timeout, and normal provider permission checks. Remote targets, worktrees, custom executors, and permission bypass flags are refused. Use a read-only reviewer profile.

Before each actual provider spawn, including a resource-related spawn retry, the worker checks the website lease. A stage reservation counts the task ID and its attempt start time; repeated authorization checks are idempotent. A watchdog cancels the owned provider tree on lease loss. Website and PromptPilot service secrets are removed from the guarded agent's environment.

Authorization refusal is handled separately from process ownership failures. The latter retain PromptPilot's normal quarantine and cleanup handling. Unguarded tasks keep the existing workflow behavior.

This integration does not provision an isolated 1C execution host. The external bridge must provide the approved environment, perform build/runtime checks, collect evidence, and keep result release under human control.

Validation: run `python -m pytest tests` and the repository's Ruff check. When nesting this checkout inside another Python project, explicitly select this `pyproject.toml` and set pytest's `--confcutdir` to this checkout, including for subprocess tests.
