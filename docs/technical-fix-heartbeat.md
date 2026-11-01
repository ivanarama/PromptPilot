# Technical FIX producer heartbeat

The opt-in pipeline path uses a separate `review-routing-v1-fix-heartbeat`
purpose. `prepare-fix-heartbeat fix` accepts an old START as a lookup only;
`renew-fix-heartbeat fix` never grants launch, push, settlement or slot refund.
Both require the scheduler's exact still-running task, strict local reservation,
canonical started receipt, complete peer/checker/readers, actor and policy.

The worker delegates `PP_TECHNICAL_EXECUTION_NONCE` only to its control command
while it retains the original owned process boundary. The nonce stays in worker
memory; only its scope/generation/PID-bound hash is persisted. It is excluded
from the provider prompt/environment, output and durable receipt files. Losing
the original producer does not let a later worker recover or mint this proof.
This mechanism is not an OS sandbox against an agent with host filesystem access.

Renewal keeps the same canonical owner within the final five minutes of its
30-minute lease, before expiry. It cannot take over or revive a lease. The
controller timebox is the smaller of the positive task timeout and configured
`fix_lease_seconds` (default 7200, permitted 300..14400), measured from the
actual scheduler task start. It does not extend indefinitely with heartbeats.

Publication uses the existing write-ahead transport and one-use final guard
after journal writer waits. An uncertain POST requires authenticated exact
readback; absence of a visible comment cannot authorize a repeated POST.

This source component remains unpublished and production execution remains OFF
until the complete worker, CAS, settlement and paired OneBase acceptance pass.
