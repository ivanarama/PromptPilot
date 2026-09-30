# OneBase: delivery-first operation

This is an operator deployment/configuration policy, not a replacement for the
canonical OneBase skills, independent review or GitHub protection.

## Release unit

Deploy server, worker and bot from the **same frozen `pp` binary**. A release
manifest records the PromptPilot commit, OneBase procedure commit, health-tool
commit and SHA256 of every artifact. Different repositories necessarily have
different commit IDs; the release ID is their explicit pairing.

`tools/install_onebase_release.py prepare` records immutable artifacts.
`install` requires a paused and idle worker, saves configuration and exact series
prompts, uses transactional prompt CAS, and switches the three LaunchAgents.
It preserves unrelated profiles, manual/automatic series pauses, lease keys,
identity rules and required checks. Pending occurrences receive the same prompt
version. Clean project health checkouts are not patched with overlay files.
Installation leaves the worker paused for verification; the operator resumes it
only after checking the new heartbeat, paired configuration and capabilities.
Installation failures restore touched files/prompts/services, unless a concurrent
edit makes rollback unsafe. Backups stay in the release's `before` directory.
There is no destructive whole-database restore.

Stage prompts bind the canonical `go run ./tools/pipelinehealth -json` check to
the full `health_command` in the installed `pipelinectl-onebase.json`, including
its contract, transport and cache flags. This prevents a stage from silently
running an older checker from its clean working checkout. Global owner/allowlist
checks and local mutation gates remain mandatory.

Targeted fallback leases with base synchronization enabled also sign the
absolute election checkout. The fresh fallback gate synchronizes and runs
health there, even when the provider moved to a detached audit worktree. The
audit checkout is not switched or modified; dirty/base/config/identity and
exact-target fences still fail closed. Older leases retain their previous
behavior until their running task finishes.

Procedures taken from an unmerged PR are an explicitly installed operator hotfix,
**not** evidence that the PR is approved, shipped or merged. Relative skill
references are resolved inside the paired snapshot. Its CLAUDE.md is paired too,
so a new skill cannot silently conflict with an old project-policy clause.

## Intake pressure

Initial intake threshold: **10 active PRs**, including review backlog, reviewed
PRs waiting for ship, merge candidates and PR reworks. Count unique PR numbers;
issues, hold/decision-only items and duplicate diagnostic memberships do not
inflate that count. Existing backlog is neither closed nor rewritten.

Above the threshold, ordinary FIX/PLAN intake waits without launching an agent.
PR rework (`fix_candidates.stage=review`) and critical candidates (`priority=0`)
remain available, but the executor is restricted to those exact candidate
numbers. The candidate still needs fresh canonical eligibility/auth checks.
Disappearing exceptions do not reopen ordinary intake during the same run.
REVIEW and MERGE continue; TRIAGE may classify incoming issues without creating
new implementation PRs.

This is **admission backpressure, not an atomic hard WIP limit**: cached state,
already running tasks and external authors can exceed the threshold. Missing,
stale or incomplete snapshots defer intake. An expired snapshot is refreshed
through the existing shared GitHub scan lease and resource budget, not via a
model or a new unbudgeted API route. The threshold is operator-owned and can be
changed after observing delivery throughput.

## GitHub budget fairness

An aged waiter receives a scheduling opportunity, not an unlimited reservation.
If its own admission still fails because another running task occupies the
required GitHub budget, it yields its aging baton for that scope. Its waiter,
priority and resource floors remain in place; only the fairness age restarts.
The ordinary retry delay is respected and cheaper eligible delivery work can
proceed. A future starvation window provides another opportunity. Priority,
scan-lease and fairness deferrals do not themselves reset the baton.

## Item-level human handoffs

The operator release enables `item_blockers` for TRIAGE, REVIEW and MERGE.
Before launch a fresh complete cached snapshot records the target's Search
metadata (including updated_at) and matching checker witnesses. An explicit,
unambiguous HUMAN handoff parks only measured targets for at most 24 hours.
Unchanged targets are excluded from future admission; changed state, expiry,
Resume or Run now permits a new attempt. New targets remain eligible. A held
integration owner is never skipped to begin another integration: the owner
barrier remains intact. A stale/partial snapshot does not authorize exclusion.
Search timestamps are change hints, not mutation authority; every actual
mutation still requires canonical fresh project checks.

This local ledger does not mutate GitHub labels or resolve a blocker. Holds
without measured state, ambiguous handoffs and execution errors retain the
series-level fallback described below. Turning this opt-in off restores that
fallback. Repeated warnings are not permission to erase checks.

## Repeated human handoffs (fallback)

Two consecutive human-required reports for an explicitly enumerated identical
target set pause only that schedule, preserving its actionable reason. Order
and paraphrasing do not restart the same expensive diagnosis. Multi-target
lists must give a separate diagnosis for every `#N`, separated by semicolons;
ambiguous joint reports remain text-sensitive. Existing single-target and
execution-error behavior is unchanged. This is an admission pause, not a
resolution of the blockers. The sampler resumes only a still-current automatic
pause when a fresh complete snapshot proves new eligible TRIAGE/REVIEW work,
when a stale-ship PR re-enters content REVIEW, or when exact GitHub reads prove
all named targets closed. Closed-target reads are throttled to one check per
ten minutes per paused series. An explicit operator pause clears the automatic
recovery marker and always requires manual Resume. Missing/partial snapshots,
unknown targets and still-open blockers do not resume. Other stages continue;
none of these checks grants `ship`, review proof or mutation authority.

## Outcome and maintenance budget (#1545)

`pipeline_watch` distinguishes agent-reported DONE from GitHub-confirmed merges.
It reads all closed-PR pages needed to cover the observation window, compares
UTC instants rather than timestamp strings, and reports completed runs and
observed uncached-input/output tokens per confirmed merge. These ratios describe
the same observation window: they are **not** per-PR causal attribution, money
spent or subscription quota. Missing usage stays explicitly unmeasured. A failed
GitHub refresh makes delivery ratios unknown instead of pretending the queue
delivered nothing. The legacy `productive_runs` field is retained for consumers;
it is only a DONE-verdict count, not a delivery counter.

Monthly methodology: record confirmed merges, classify each into product,
pipeline maintenance, documentation/planning or unresolved classification;
record queue ages, human interventions and repeated unchanged target/HEAD
handoffs separately. Keep raw evidence links and classification decisions.
Do not infer maintenance from loose title matching or count base-sync churn as
product delivery. A maintenance share **over 25% of classified monthly merges**
is a review signal, not a reason to bypass gates or block urgent infrastructure
fixes. With incomplete classification, publish coverage and do not call the
budget met. Compare run/token trends and actual shipped product outcomes after
one month before making further architectural changes.

The report now separates running work, decisions, diagnostic warnings and
confirmed delivery. Delivery categories use explicit operator-owned
`delivery_classifications` entries keyed by PR number, each with `category`
(`product`, `pipeline`, `docs_plans`) and an `evidence` explanation. Unclassified
PRs remain explicit; titles and DONE verdicts never establish product delivery.
No additional GitHub requests are needed for this presentation. Missing CI
observations remain unknown, not falsely all-green. Closed issues and merged
PRs are different event counts, not independent completed-task counts.

Automatic monthly classification and target/HEAD churn accounting remain
separate work; this document does not claim #1545 is fully implemented.

## Ready integration owners

`ready_owner_merge` is distinct from the historical `base_sync_merge` switch.
It never updates a branch and never infers old ship carry. It admits a CLEAN
owner with canonical reviewed proof of its exact current HEAD, a trusted ship
event in that current epoch, all required CI checks, and an unchanged exact
owner/HEAD/stage/allowlist. These are checked again at completion and after the
cleanup intent before SHA compare-and-merge. Pending CI waits without a model.
Historical carry without current-epoch ship, conflicts and ambiguous recovery
remain in the full canonical lineage procedure — except one proven mechanical
base-sync under the separate opt-in `base_sync_carry` (issue #42; all four carry
conditions re-proven before and after the cleanup intent, see README).
`base_sync_merge` and `base_sync_carry` are not enabled by the installer. Direct completion also recovers a pending cleanup intent only
after the same owner checks.

REVIEW and MERGE now use the existing adaptive cadence (30-minute idle,
10-minute busy, two empty runs before idle, event wake enabled). Replica target
reservations still prevent a second REVIEW from auditing an occupied target.
