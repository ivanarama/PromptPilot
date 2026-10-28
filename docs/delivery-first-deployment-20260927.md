# Delivery-first deployment, 2026-09-27

Recorded at approximately 14:12 UTC / 17:12 Moscow time.

## Installed

- Release: `~/PromptPilotBuild/delivery-first-20260927-v2`.
- PromptPilot source: `0506c23f3645d9de153fe5b29c1492cdee0b47a1`.
- Procedure/project-policy snapshot: OneBase `2cbc452421b97c263851ed7b7c7516c67d65ef7c`
  from PR #1730, paired under the preceding immutable release directory.
- Health binary: built/tested from OneBase main
  `a4ecf8e85f58528dafa0ba66cce2a118b663a5b9`, with explicit `-contract`
  pointing to the paired REVIEW skill, not the checkout's older copy.
- Server, worker, bot and profile execution/probe commands use the same v2
  frozen binary. Artifact hashes and all seven schedule prompts verified.
- Backups in the release's `before/`; previous binaries/snapshots retained.
  No running agent was interrupted. Worker PID 97893 online and unpaused.

The first installation (`d15015b`) exposed an expired-cache refresh detail:
`analyze(use_cache=True)` is intentionally a cache-only read. The final v2 uses
budgeted `use_cache=False` after checking the cached state first, fails closed
on refresh errors, and upgrades explicit frozen commands on subsequent releases.
Regression tests cover both details. Every deployment used a fresh immutable
directory rather than overwriting a running binary.

REVIEW series 3/13 were subsequently promoted from priority 2 to priority 1 via
the public schedule API. They now share the delivery priority class with MERGE,
so REVIEW is no longer charged the extra headroom reserved for primary work.
Real resource contention can still defer REVIEW; this is not unlimited admission.
All resource safety minima, reservations and scan leases remain active;
existing series pause flags were not changed.

## Verification and observation

624 focused tests passed, including CLI install/rollback and delivery of a WIP
exception's exact-number restriction to the provider boundary. Mac Go tests for
`tools/pipelinehealth` and `internal/pipelinecontract` passed. Public frozen
`pipelinectl capabilities` retained target-v1 completion/fallback contracts.

Real FIX dispatch preflight returned `defer` without a provider when a fresh
complete snapshot was unavailable under occupied API budget. This proves the
safe missing-data branch, **not** yet the live above-threshold branch. MERGE
task 2149 continued through the protected full-skill base-sync-owner fallback;
that path still legitimately uses a model. Do not call the whole pipeline
model-free or claim a delivery speedup from installation checks alone.

Read-only watch LaunchAgent:
`com.promptpilot.onebase-watch-delivery-first-20260927`.
Started 14:06 UTC for 24 hours; no automatic GitHub mutations or agent launches.
Report: `~/.promptpilot/pipeline-watch-delivery-first-20260927.json`.
At 14:11 UTC it was updating, worker online/unpaused, no alerts or current read
errors. No merge had yet been confirmed in this short observation window;
zero-denominator cost/run ratios were correctly unknown. A transient API outage
during the service switch cleared on the next successful observation.

Code review: https://github.com/ivanarama/PromptPilot/pull/113.
OneBase PR #1730 has green CI but still awaits independent review; no self-ship
or self-approval was performed. Monthly maintenance/churn automation remains
unfinished as described in `pipeline-operation-policy.md`.
