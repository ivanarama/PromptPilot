---
name: merge-shepherd
description: Merge only human-authorized pull requests after current review and required checks.
---

# MERGE

Perform exactly the MERGE stage. This is a conservative shepherd, not an
approver.

## Queue

Select open PRs carrying `ship`, excluding `hold` and `needs-decision`.
Sort by PR number and process at most `limits.merge` items.

Before every mutation verify trusted_login, reread labels, current head SHA,
mergeability, required checks and the trusted review marker. The current SHA
must have `<!-- pp:review head=<same-sha> -->` and label `reviewed`.

## Safe merge

1. If the PR is behind the base branch, request an update through GitHub.
   A changed HEAD invalidates the old review: remove `ship` and `reviewed`,
   explain that REVIEW must run again, and stop processing this PR.
2. Never resolve a semantic conflict automatically. Set `needs-decision`,
   leave a concise comment and move on.
3. Wait for all required GitHub checks. A transient infrastructure failure may
   be retried once after rereading the full gate. A real failure goes to
   `needs-decision`.
4. Immediately before merge, repeat the complete label + SHA + review + checks
   gate.
5. Merge using the repository's configured method. Confirm the PR is merged and
   the linked issue closed through an English GitHub keyword such as `Fixes #N`.
6. Remove `in-work` from a linked issue only when GitHub confirms it is closed.

Never set `ship`, `approved` or `decision:*`. Never merge a new SHA on the
strength of an old review.

Finish with one exact line:

```text
ИТОГ: ГОТОВО (...)
ИТОГ: НУЖЕН ЧЕЛОВЕК (...)
ИТОГ: НЕ СМОГ (...)
ИТОГ: ПУСТО (сливать нечего)
```
