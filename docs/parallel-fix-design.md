# Parallel FIX: fenced two-lane rollout

Tracking: [#140](https://github.com/ivanarama/PromptPilot/issues/140).

The default configuration remains single-lane. The opt-in FIX route uses the
same local replica lifetime/work-directory fences as REVIEW, plus its own
signed target and paired repository consumer. Increasing
`capacity`, cloning the series, or allowing `execution.mode=skill` to fall back
is not a parallel FIX implementation.

## Target and ownership

1. A FIX target is either an open PR requiring owner-authorized rework or an
   open approved/ready-fix issue. PR rework has precedence, as in the canonical
   `fix-approved` procedure. Pipeline health must publish both candidate kinds
   as structured data; prose and the first N unpaginated PRs cannot elect them.
2. Election reserves `(repository, number)` atomically in the scheduler DB for
   one task attempt. A PR carries its exact HEAD and review handoff proof; an
   issue carries the health producer's versioned eligibility digest (the
   40-hex `head` field in a normalized FIX target is not a Git commit). The
   reservation has an opaque token, an owner attempt, and a renewable lease.
   Different targets may run concurrently; the same target may not.
3. Re-election by the same attempt is idempotent only for the exact target and
   revision. An expiry never transfers a target while its provider still runs.
   Recovery must prove provider termination before releasing ownership.
4. Each lane has a persistent, distinct working directory and its own git
   worktree/branch. Dynamic shared worktrees and remote execution are rejected.

## Mutation gate

The preflight is a scheduling decision, not permission to mutate GitHub.
`next fix` requires `parallel_fix_enabled: true`, at least two replicas and a
trusted local `fix-approved` procedure with the exact separate-line marker
`<!-- pp:fix-target-consumer-v1 -->`. Its bytes are hashed into the lease.
`fix_consumer_skill` can select that procedure; the default is
`.claude/skills/fix-approved/SKILL.md`, relative to the election checkout.
Missing/changed consumer, unknown target kind, CLI failure and unsigned or
contradictory handoff block dispatch, rather than falling back to queue choice.

Before its first external mutation the worker runs `gate-fallback fix`: fresh
full health must still elect the exact revision, with all earlier eligible
targets reserved by other live attempts. This renews the same reservation and
returns an `ownership_lease` with a distinct signed purpose. Before **every**
later mutation it runs `gate-fix-owner` with that progress token. This rechecks
the full health, actor, pinned consumer/config, replica count and exact live
reservation/task attempt. Neither token grants GitHub mutation authority.
Both commands return `mutation_authorized: false`.

After each successful scheduling gate, the canonical repository consumer must
independently repeat all its live state/HEAD/base/labels, trusted handoff/epoch,
branch-claim, delivery-proof and CAS checks immediately before the single write.
This combined gate is required for comments, labels, branch creation, push and
PR creation. A changed owner, human hold or invalid project proof stops writes.
The initial election revision never changes: after an own push/PR creation,
only the project's narrowly permitted delivery finalization may proceed.
Requiring initial queue membership again then would strand that finalization,
since the successful delivery itself changes HEAD or removes the issue from
the queue. New higher-priority work does not preempt an admitted live owner.
There is no re-election or automatic post-delivery retargeting.

The election checkout is signed into both tokens. Health continues there if
the agent later enters its isolated target worktree. Scheduler-owned negative
admission exclusions are carried as exclusions only; they never elect a target
or grant authority. MERGE remains a separate single-flight route.

The scheduler supplies one shared DB path and lease key to both lanes. GitHub
rate admission, active-PR WIP and token quotas remain global rather than
per-replica.

## Delivery slices

1. Add structured FIX PR-rework candidates and a versioned issue eligibility
   fingerprint to pipeline health. Include pagination and negative tests for
   edited/deleted review markers, labels, base and HEAD changes.
2. Add exact-target FIX election, reservation, signed handoff and pre-mutation
   gate in PromptPilot. Test same-target race, independent targets, lease loss,
   provider crash/recovery, CLI outage, unsigned handoff and GitHub 422. Keep
   the new route disabled by default.
3. Add two isolated Mac series under a flag. Run a limited pilot; compare
   ready-fix-to-PR latency, duplicate PR count, fallback, GitHub API and token
   use with the single-lane baseline. MERGE stays single-flight. Disable the
   flag on ambiguous ownership or worsening throughput.

Code/tests for a disabled route are not evidence of a completed production
pilot. The paired repository consumer, all CI and independent review must be
accepted before enabling any working profile or adding a second FIX series.
Source tests use fake GitHub transport and isolated scheduler databases; they
do not exercise paid providers or mutate the working Mac's pipeline.

Base-sync under [#42](https://github.com/ivanarama/PromptPilot/issues/42) can
be developed concurrently, but a failing base-sync pilot blocks enabling a
second experiment on the working Mac. Moving verdicts to Checks or a new
database is not a prerequisite.
