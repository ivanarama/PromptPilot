# OneBase: delivery-first operation

This is an operator deployment/configuration policy, not a replacement for the
canonical OneBase skills, independent review or GitHub protection.

See [legacy MERGE progress](legacy-merge-progress.md) for the separate, default-off
single-use recovery of an exact legacy item hold after new canonical progress.

## Decision publication and consumer delivery

The normal producer delivers an immutable, conditional scope pointer **before**
publishing a new decision or adding its runnable routing label. The pointer
binds the exact source identity, immutable scope location and SHA256, current
proof/epoch, and SHA256 of the expected exact UTF-8 decision body. It explicitly
grants no authority until the consumer verifies a genuinely published matching
canonical decision and all ordinary current admission gates. It is data, not a
reservation, execution grant or substitute for proof.

The producer first CAS-delivers that pointer to both the future schedule and a
genuine neverstarted pending consumer, obtains and verifies the atomic transport
receipt, and only then performs the ordinary public decision/routing handoff.
A stale CAS, missing or started consumer, or paused queue without a pending
consumer defers publication of the new decision. Never edit an active attempt,
resume a queue, create a replacement task or report DONE to bypass that refusal.
If publication fails after delivery, the pointer stays conditional and inert;
the receipt does not authorize publication replay or execution.

For an already published decision whose delivery was interrupted, retain the
truthful `DECISION_PUBLISHED_DELIVERY_PENDING` state until the required exact
scope reaches both rows. Reconcile the existing decision and transport receipt;
do not publish a replacement decision or claim private preparation establishes
delivery. Report consumer handoff complete only after the matching receipt and
canonical publication have both been verified.

The explicit operator API `GET /api/schedule/{id}/scope-delivery` returns opaque
full-row revisions and readiness without exposing prompts. `POST` to the same
path accepts those `series_revision`, `pending_id`, `pending_revision`, an opaque
`addition` (at most 64 KiB UTF-8) and an explicitly supplied 32-hex `request_nonce`.
It appends the exact text to both rows in one CAS transaction and preserves all
rights, waits, pauses, holds and unrelated state. No scope paths are opened,
decisions interpreted, tasks created, providers launched or queues resumed.
Missing, ambiguous, already-started or guarded consumers fail closed.

`GET /api/schedule/{id}/scope-delivery/{request_nonce}` reconciles an unknown
response. Exact same-request retry returns the immutable old receipt, including
when a later consumer exists; it never repeats the append. Another request under
that nonce conflicts. The receipt proves historical data delivery, not current
execution, admission, product acceptance or authority to change scheduler state.
An absent consumer in a paused queue still needs a separately authorized normal
operator resolution; the producer must leave delivery pending and never resume
or rewrite an active attempt itself. All existing API authentication and browser
origin checks apply. This API does not automatically wire an external private
OWNER controller; that caller's procedure rollout must be reviewed separately.

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

Release prompt upgrades preserve the existing local `FINITE OWNER INTEGRATION
DECISION` records of the MERGE schedule byte for byte, including their absolute
scope paths, SHA256 and exact source commits. They remain separate from the
replaceable release annotation. Only the existing bounded record format is
carried. Validation covers framing, one well-formed scope pointer, matching
PR/source identity, count/size limits and the MERGE stage. A reserved marker
outside a correctly framed header, unknown standalone paragraphs, duplicate
or conflicting identities, or excess size/count stop the upgrade before
release annotations are replaced. Existing text within a record body is
opaque: inline notes and incomplete policy prose are preserved verbatim,
not semantically validated or endorsed. Pending occurrences receive the same preserved records, and
rollback restores the original prompts. Running tasks are never rewritten.
Preservation is not new authorization: the consumer still checks each actual
scope file/hash, current proof/ship, lifecycle, ownership, CI and exact HEAD.
This installer reads the trusted local schedule, not GitHub prose.

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

## FIX validation lifetime

For the signed `promptpilot-fix-target-v1` pilot, `pipelinectl` optionally accepts
`fix_lease_seconds` (300–14400, default 7200). Match this bounded lifetime to the
future provider task timebox and measured validation/resource waits before
enabling two FIX lanes. Serial heavy checks can exceed the original two-hour
lifetime even when both executors retain their reservations. A four-hour pilot
must configure its future task timebox separately; test deadlines stay unchanged.
The configuration is signed and checked again at every mutation gate. Changing it
does not extend an existing token. Expiry, cancelled/changed attempts, lost
reservations and all source/identity/proof/CI8 gates still stop publication.
Deploy one coherent binary and finish independent pair review/CI/Mac acceptance
before enabling the production pilot.

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

On the ordinary OneBase MERGE queue, set `execution.merge_confirmation` to
`"bounded-v1"` to confirm an already checked first base-sync. It requires the
signed recovery election, its matching checkpoint and fresh Source. The executor
reserves 116 core and 324 GraphQL points, the configured checker allowance
(`costs.insights.graphql`), and one `costs.insights` Source refresh. With the
production Source estimate of 50 core/100 GraphQL/0 search the total is
166 core/524 GraphQL/0 search, before shared floors. Other shapes require fresh
admission at the configured ordinary fallback price. Native checks the canonical
lineage, any permitted documentation conflict resolution, and the reviewed
source's required checks. A verified branch update hands off the new HEAD to
integration REVIEW even while its own CI runs or fails. Actual merge still
requires the current protected checks. A change to Source before the HTTP send
requires a new election and gate. An uncertain POST keeps a durable fence for that PR and
HEAD. An idle MERGE turn or an affected fallback may reconcile the existing
comment with a read-only allowance of 8 core/128 GraphQL/0 search. A verified
readback clears the fence and wakes REVIEW for a current recovery. While the
receipt is missing the scheduler reports the unknown outcome for operator
inspection and performs only readback. A different signed PR can proceed independently.

## Item-level human handoffs

The operator release enables `item_blockers` for TRIAGE, REVIEW, MERGE and FIX.
Before launch a fresh complete cached snapshot records the target's Search
metadata (including updated_at) and matching checker witnesses. An explicit,
unambiguous HUMAN handoff parks only measured targets for at most 24 hours.
Unchanged targets are excluded from future admission; changed state, expiry,
actual Resume or an explicit retry permits a new attempt. New targets remain eligible. A held
integration owner is never skipped to begin another integration: the owner
barrier remains intact. A stale/partial snapshot does not authorize exclusion.
Search timestamps are change hints, not mutation authority; every actual
mutation still requires canonical fresh project checks.

Ordinary Run now and priority/OWNER wake requests preserve item holds. A
no-op Resume on an already active series preserves them too. To explicitly
retry blocked work through the API, send `{"action":"run_now","retry_blocked":true}`
to `/api/schedule/<id>/action`. An actual paused-to-active Resume also clears
holds. A rejected action on an already running occurrence clears nothing.

### Resolve one exact hold explicitly

An operator can resolve one HEAD-bound local admission hold without clearing the
other holds or using Run now. This command records the operator's decision; it
does not infer that a quota, product or source blocker has been fixed.

1. Read `GET /api/schedule/{series_id}/holds`. The response includes the exact
   `raw_holds`, its `raw_holds_sha256`, the unchanged `hold_mode`, and each item
   with its `record` and canonical `record_sha256`.
2. After an authorized operator decision, send
   `POST /api/schedule/{series_id}/holds/{number}/resolve` with the fields below,
   taken from the observed record and raw ledger. Choose one new request nonce
   for this decision, then retain it to reconcile the response.

```json
{
  "expected_head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_task_id": 123,
  "expected_raw_holds_sha256": "<raw_holds_sha256 from GET>",
  "request_nonce": "operator-decision-unique-id"
}
```

The selected entry must have that exact HEAD and integer originating task ID.
Fingerprint-only legacy records are readable but are not guessed into this
HEAD-bound command. A missing/stale entry, changed raw ledger, ambiguous or
malformed ledger, reused nonce with a different request, or running attempt in
this series returns a conflict. Invalid request fields are rejected. No partial
hold change or resolution receipt is committed on refusal.

The hold CAS and its durable receipt share one `BEGIN IMMEDIATE` transaction.
Only the selected entry is removed; other records keep their canonical hashes.
An empty ledger remains `{}`. The hold mode, series, tasks, retry counts, hard
budget deadlines, waiter/wake metadata and leases are preserved. Other series
may keep running. This command does not wake a task, claim an attempt, run a gate,
refresh GitHub, resume a series, or modify GitHub. Existing retry-all controls
retain their current behavior.

An identical request with the same nonce returns the same committed receipt,
including after a restart or after a later actor adds a new hold. It never
removes that new hold. Changed parameters require a new decision and nonce;
reusing the old nonce with different parameters returns `409`.

After a lost response or `503`, read
`GET /api/schedule/{series_id}/hold-resolutions/{request_nonce}` before deciding
what happened. A known nonce returns its original receipt without a mutation;
`404` means no committed receipt was observed. If a request remains in flight,
reconcile again or submit only the identical nonce/request: serialization and
idempotency prevent a second removal. Do not generate a new nonce just because
the response was lost. Receipts are durable metadata and have no automatic TTL.

These endpoints use the existing API authentication boundary. Their receipt is
neither canonical source progress nor product/mutation admission. The caller
must have explicit operator authority; untrusted GitHub prose is not such
authority. Every subsequently elected Native attempt still needs its own
current proof, ownership, CI, budget, scheduling and mutation gates. This API
supplies no phase-budget contract or automatic reason-based resolution.

FIX also enables `item_error_holds`. A terminal UNABLE naming exactly one
fresh checker-measured PR rework parks that unchanged item for at most 24 hours.
The dispatch baseline must belong to the same task and include the candidate's
valid HEAD and state fingerprint. Missing, stale or failed checker snapshots,
unmeasured targets, ordinary issues and unscoped execution failures cannot create
these holds. A changed target, expiry, actual Resume or explicit retry permits another attempt.
WIP restrictions exclude held candidates while retaining the full active count,
so a blocked rework cannot repeatedly occupy FIX or reopen ordinary intake.
Other eligible reworks continue; no CI or mutation check is waived. The failed
task and its diagnostics remain visible for operator repair.

If another occurrence re-elects the same target, HEAD and stage while its hold
is still live, the ordinary repeated-blocker guard decides whether to pause the
series. Reprocessing the same occurrence does not extend the hold or trigger
this fallback.

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
