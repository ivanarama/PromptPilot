# Legacy MERGE holds and new canonical progress

The project `pipelinectl` configuration may explicitly set
`"legacy_merge_progress_migration": true`. This boolean defaults to off. Deploy
the matching worker and read-only `inspect-merge-progress` producer before
enabling it. Without the option, the original v1 facts and normalized config
digest remain unchanged. An old producer supplies no intent timestamp and cannot
release a legacy hold; an old consumer rejects the extended facts.

The option covers one narrow case: an exact legacy MERGE item hold has no old
`canonical_progress` baseline, but a later genuine execution published new
canonical progress for that same source. It does not classify provider result
text such as “budget” or “priority_waiter” as authority to retry.

Before observing a held target, the scheduler requires the known legacy record
shape and its actual completed task in the same series, with verdict `НЕ СМОГ`
and valid, ordered start/completion/hold timestamps. Explicit HUMAN, operator
metadata, unknown task/verdict, changed HEAD or ambiguous identity stays held.

The bounded read-only producer still verifies the complete stable current
source/proof/epoch, trusted unedited review and ship, stopped labels, graph,
current main and all required successful exact-HEAD CI. It accepts exactly one
pending base-sync intent, without done/cleanup/ambiguous protocol history. Only
when opted in, it signs that existing server row's `createdAt` in addition to
the existing intent identity. This consumes no extra GitHub reads.

The intent must be strictly newer than **both** the blocked task's completion
and the original hold timestamp. An intent from before or during failed or
partial acceptance does not qualify. A fresh runtime snapshot or quota reset
alone is not progress, and no old source baseline is fabricated.

Dispatch may observe availability without consuming it. Before `next`, a real
running scheduler attempt atomically rechecks the exact old hold and completed
task, then records the single-use `progress_attempt`. It retains the original
hold's reason, age, task and HEAD. Another occurrence cannot spend that intent;
crash, wrong election, unknown outcome or another failure does not refund it.
The normal canonical source/election/ownership/admission and per-mutation gates
remain independent. This is neither product authority nor permission to repeat
failed tests or an unknown public send.

This change does not alter live configuration, pauses, manual holds, budgets,
floors, TTLs, acceptance requirements or executor count. Explicit operator
resolution remains available through the existing exact-hold CAS API when
separate remediation has actually been established. A local admission exclusion
is reported as a local hold, rather than being described as a human decision.
