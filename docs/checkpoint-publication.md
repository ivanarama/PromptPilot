# Checkpoint publication and lifecycle acceptance

A reviewed checkpoint is first published through the existing source-bound Git
intent. Object preparation does not hold the SQLite writer. The publication
phase rechecks its exact workflow version and pending identity while holding
the writer, protects the index and symbolic branch, and uses an expected-HEAD
Git ref transaction. The final verification fence covers that admission commit.

The returned immutable commit is a receipt. It does not advance the workflow
until all owned Git-process and reader cleanup has finished successfully. The
reader must finish before buffered pipes are closed. A stuck reader or reported
owned-boundary cleanup failure leaves the workflow awaiting its pending
checkpoint; it cannot commit stage.checkpoint/stage.advanced and then report a
stale-version error. A published Git commit is preserved, not rolled back or
inferred from an agent's report.

After cleanup, a second SQLite writer admission checks the exact workflow and
pending checkpoint version again. It freshly verifies the receipt's repository,
symbolic branch, HEAD, immutable commit parent/tree, index digest and reviewed
working bytes/modes after any writer wait. Only then are the checkpoint event
and stage advancement projected together. Cancellation between these phases
wins. This projects the already fenced, source-bound receipt; it does not accept
a different HEAD or silently reuse PASS after a context change.

If publication succeeded but cleanup or projection did not, the durable Git
intent and unchanged pending workflow allow a later explicit resume to recover
that same reviewed commit. No duplicate commit or stage advancement is created.
Unknown Git/index state and changed working bytes still require engineering
inspection or the existing explicit restart/review cycle. No arbitrary-script
idempotence or OS sandboxing is implied.
