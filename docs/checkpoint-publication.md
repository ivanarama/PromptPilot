# Checkpoint publication and cleanup readiness

A reviewed checkpoint is published through its durable source-bound Git intent.
Preparation does not hold the SQLite writer. Admission rechecks the exact pending
checkpoint and workflow version under the writer, index lock and prepared Git
ref transaction. These Git fences remain held while the checkpoint and stage
advancement events commit. An unlocked readback cannot replace those fences.

That acceptance does not make the next stage runnable. The same transaction
records a versioned cleanup barrier and keeps the workflow awaiting human input.
Only successful completion of the original owned processes and readers releases
readiness. Readers finish before their buffered pipes close. A cleanup failure
returns the current blocked workflow, never a misleading stale-version refusal
while secretly queueing the next stage. Accepted source events remain durable
and are never repeated merely to retry cleanup.

Readiness release checks the exact barrier generation, stage, round and version
under a new SQLite writer, and confirms the accepted commit, branch, Git intent,
index and reviewed bytes. Changed context stays blocked for engineering review.
Cancellation remains available and cannot be reversed by a later cleanup result.
If cleanup failed before source acceptance, its barrier must first be released;
the existing pending-checkpoint protocol then recovers publication exactly once.

A retry can use only the retained original resource handles. Automatic retry is
bounded to one attempt, after those handles report readiness. Explicit resume can
retry that same owned cleanup. Missing ownership after process restart, malformed
history or unknown cleanup outcome preserves the barrier and requires engineering
verification. PID absence, elapsed time, an agent report or a newly launched
process never supplies a cleanup acknowledgement. No scheduler, migration or
provider is started by cleanup recovery. This is not OS sandboxing or a guarantee
that arbitrary scripts are idempotent.


Restarting a pending checkpoint uses the same readiness barrier. Revoking the
old PASS and invalidating its checkpoint may commit under the existing Git
fences, but the workflow stays awaiting until those original resources close.
Only then does it become revision_required. Restart cleanup failure retains the
same handles and returns the blocked state, with no new configured executor.

A matching DONE idempotency key is not an acknowledgement by itself. Completion
must have the exact event type, closed JSON schema, protocol, generation, typed
terminal outcome, lifecycle version and ordered reference to the exact release
event. Invalid JSON, duplicate keys, mismatched fields or ordering preserve an
engineering barrier. Unconfirmed cleanup cannot be acknowledged by metadata
that merely resembles a completion receipt.
