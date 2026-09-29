# Parallel FIX: fenced two-lane rollout

Tracking: [#140](https://github.com/ivanarama/PromptPilot/issues/140).

The existing `replicas > 1` path is REVIEW-only. The FIX queue must remain
single-lane until all gates below are implemented and tested. Increasing
`capacity`, cloning the series, or allowing `execution.mode=skill` to fall back
is not a parallel FIX implementation.

## Target and ownership

1. A FIX target is either an open PR requiring owner-authorized rework or an
   open approved/ready-fix issue. PR rework has precedence, as in the canonical
   `fix-approved` procedure. Pipeline health must publish both candidate kinds
   as structured data; prose and the first N unpaginated PRs cannot elect them.
2. Election reserves `(repository, number)` atomically in the scheduler DB for
   one task attempt. A PR carries its exact HEAD and review handoff proof; an
   issue carries a versioned digest of its full eligibility snapshot. The
   reservation has an opaque token, an owner attempt, and a renewable lease.
   Different targets may run concurrently; the same target may not.
3. Re-election by the same attempt is idempotent only for the exact target and
   revision. An expiry never transfers a target while its provider still runs.
   Recovery must prove provider termination before releasing ownership.
4. Each lane has a persistent, distinct working directory and its own git
   worktree/branch. Dynamic shared worktrees and remote execution are rejected.

## Mutation gate

The preflight is a scheduling decision, not permission to mutate GitHub. The
worker receives a signed exact-target envelope, then calls a gate immediately
before every comment, label change, push, and PR creation. The gate renews the
same reservation and re-reads the live PR/issue state, HEAD/base, labels,
handoff proof and applicable canonical `fix-approved` checks. A changed target,
revision, ownership, or queue priority fails closed. No generic skill fallback
is allowed for a replicated task; one envelope never switches targets.

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

Base-sync under [#42](https://github.com/ivanarama/PromptPilot/issues/42) can
be developed concurrently, but a failing base-sync pilot blocks enabling a
second experiment on the working Mac. Moving verdicts to Checks or a new
database is not a prerequisite.
