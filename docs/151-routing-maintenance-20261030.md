# A151 author bridge: partial maintenance attempt log

This document records the historical author attempts and their validation
limits. It is not the live branch acceptance status. Any later exact-commit
manual verification, independent review and CI are recorded separately.

Branch: `feature/151-review-routing-20261029`. The required base
`1564adab99ee165a96644fe458dc146fe9ec1098` was merged locally without conflicts;
it is an ancestor of this branch. The author attempts made no GitHub writes,
production changes or flag activation. This slice does **not** complete or
accept A151.

## Windows CI repair follow-up

Read-only inspection of run `40985371723`, on exact commit
`13644cec40ca05980061c0584575968fb6445b74`, found two failed executable-peer
tests on Windows/Python 3.10: REVIEW completion and claimed author handoff both
reported `routing record is not canonical UTF-8 JSON`. Windows/Python 3.12 was
cancelled; the two Linux test jobs and lint passed on that earlier commit.

The test-only C# launcher forwarded pipes through .NET console text encodings
and launched Python without explicit UTF-8 mode. With inherited Python UTF-8
mode disabled, the old launcher reproducibly changed a Unicode JSON body on
this Windows host. The repaired launcher starts its fake peer with `-X utf8`
and forwards stdin/stdout/stderr as bytes, draining stdout and stderr together.
The fixture also pins Python pipe encoding. Production wire validation and
legacy/OFF formats are unchanged.

A new finite executable regression checks exact Cyrillic/CJK/emoji bytes on
both output streams, CRLF, exit status, and text JSON round-trip with inherited
UTF-8 mode disabled. The existing public REVIEW test now also checks the full
report and canonical bytes returned by the fake server. The original launcher
failed the disabled-UTF-8 JSON round-trip in a separate controlled reproduction;
the repaired launcher passed. All children use hidden windows and no shell.

The four executable fake-peer CLI tests and the final encoding regression pass
locally, as do ruff and `git diff --check`. A focused run with Python UTF-8 mode
disabled (`python -X utf8=0 -m pytest -q -W error`, selecting CLI peer, FIX,
settlement, execution and policy tests) passed: **135 passed, 2 skipped**.
Its TEMP/TMP and basetemp were private directories inside the checkout.
The first full-suite run passed: **2463 passed, 22 skipped in 818.99 seconds**.
The final UTF-8-mode assertion was added during that run, so it is not presented
as a full check of the final test tree. The final encoding regression then
passed separately. A fresh full-suite follow-up with TEMP/TMP and basetemp inside
the checkout hit its finite **1200-second timeout** and was terminated by its
runner. It produced no final pytest summary; it is **not PASS**. Fresh complete
verification of the final test tree remained open at the end of that attempt.
Only Python 3.12 was installed
here; disabling UTF-8 mode is a regression scenario, not a claim that the Python
3.10 CI job was rerun.
The bridge implementation and the exact remaining gaps below are unchanged
by this test-harness repair.

Automatic approval review rejected recursive cleanup of the private test
artifacts with `blocked by policy`, including a second attempt with an exact
verified literal path. They remain untracked under
`tools/a151-validation-6c843596/` and `tools/a151-full-final-6c843596/` and are
not part of the commit. Plain ruff passed before those artifacts accumulated;
a later repeat also scanned an intentionally invalid generated `app.py` fixture
containing only `reviewed`. Source lint passes with just these two untracked
directories excluded:
`python -m ruff check promptpilot tests tools main.py --extend-exclude tools/a151-full-final-6c843596,tools/a151-validation-6c843596`.
No repository lint rule, CI file or policy was changed. `git diff --check` passes.

## Implemented

- A separate source-bound author admission before legacy FIX handoff, using the
  already claimed task, its exact occurrence and reservation. No substitute task
  is enqueued. Legacy parallel FIX still requires at least two replicas.
- Canonical attempt receipts through the existing write-ahead comment journal;
  immutable lifetime budget, source, executor and canonical owner checks.
- One attempted additive assignee API operation and authenticated full PR REST
  readback by author node ID. A separate strict-schema SQLite receipt preserves
  ambiguity and prevents retry after crash, ignored delivery or manual removal.
- Verified assigned/handoff-done phases. The actual worker completes this
  administrative route with zero providers and a separate verdict that does not
  wake REVIEW or fabricate started/fix-done.
- Fresh checker/identity/actor/lease after database waits, followed by an atomic
  local task/cancel/reservation read, final config/readers and expiry checks.
  Expired reservations are refused rather than resurrected.
- Trusted settlement wire/reader/reducer and separate canonical vectors:
  exact attempt/source/context/barrier/start references, explicit verified old
  executor outcome, engineering parking, one new-HEAD resume and no budget refund.
  The public REVIEW reader can consume this alternate path without fake fix-done.
- OFF/removed-flag read-only fences for existing trusted v1 history. Ordinary
  no-v1 legacy serialization and the original wire vectors remain unchanged.
- Windows executable fake GH peers and subprocess CLI tests; GH/checker children
  use CREATE_NO_WINDOW. Actual-worker tests cover author/fork routes, envelope
  and purpose mismatches, unsupported transports, late holds/actors, task ABA,
  cancellation, reservation loss/expiry, crash and two private scheduler DBs.

The additional `claimed-fix-v1` author capability is required in the fake peer
contract; merely setting the flag cannot activate this path against the accepted
OneBase checker. No actual accepted OneBase checker binary was available in this
checkout, so its public refusal test and paired acceptance have not been run.

## Earlier author attempt verification

Local environment: Windows, Python 3.12.10, pytest 9.1.1, ruff 0.16.9.

- `python -m ruff check promptpilot tests tools main.py`: passed.
- `git diff --check`: passed.
- Focused composed-base run: 271 passed, including executable fake GH/CLI,
  author worker, settlement reader, legacy FIX election and replica tests.
- Further routing/proof/fallback run: 227 passed after updating legacy read-only
  peers for the new OFF timeline check. The last boundary/worker/executable CLI/
  OneUseStart/fallback run on the final implementation had 213 passed.
- The initial complete `python -m pytest -q -W error` run had 2359 passed,
  24 skipped and 8 failed. Three depended on inherited GH_EXE/GO_EXE; five still
  selected the default Codex provider for narrow rights after the base merge.
  Tests now isolate those environment variables and select the supported Claude
  confinement adapter. The related rerun had 56 passed.
- The complete follow-up run overlapped final edits and is not a valid final-tree
  check: 2426 passed, 22 skipped, 31 failed (8 legacy peers before their OFF-read
  fixture correction; 23 calls mixing the newly loaded guard with an older
  in-memory request module lacking verify_local). Fresh targeted runs above
  verified those final modules together. A fresh complete validation of all 114
  test files was then started in four independent pytest processes, each with
  private TEMP/TMP and basetemp inside the checkout. All four fresh partitions
  reached their finite 360-second subprocess timeout and were terminated by the
  runner. This complete validation is unfinished, **not PASS**. The 45-minute
  task limit prevents another complete run; mandatory full-suite verification
  remains open. The overlapping run is not represented as PASS either.

These historical local checks are not independent exact-commit review or the
five GitHub CI jobs. Neither was claimed or performed by these author attempts.

## Exact remaining gap

1. Operational progress/start issuer and policy-bound pipeline coordinator with
   exact canonical started/task/UUID. Progress is a reserved schema; start is
   explicitly refused. Pipeline execution configuration is refused honestly.
2. Durable OneUseStart integration at the real provider factory, technical lease
   heartbeat and the positive one-launch/recovery/rate-limit/ambiguous-spawn
   public worker matrix. The existing internal component is still unconnected.
3. Actual CAS-push/new-HEAD transition/source-trailer proof and fix-done for both
   author and pipeline routes. Administrative handoff cannot synthesize success.
4. Public trusted settlement/resume writers that independently verify the old
   executor/delivery outcome and current HEAD. Reader fixtures supply trusted
   server records; no automatic settlement is inferred from exit code, expiry,
   absent PID or agent text. Unknown old execution remains a barrier.
5. Actual accepted OneBase capability refusal, four real peer readers and paired
   public end-to-end acceptance. Compatible fake peers are not that evidence.

Flags must stay OFF pending that work and separate exact-commit review/all CI.
The parent issue must not be closed on the basis of this partial slice.

## Manual execution continuation, not acceptance

The still-private OneUseStart component now records an established-store marker
in the scheduler settings transaction when it creates its receipt table. A later
table loss cannot be mistaken for first initialization by a fresh component
instance. Missing/corrupt markers, unmarked existing tables and unknown schemas
fail closed; no existing receipt is erased or silently migrated. Complete loss
of trusted history still cannot establish a safe launch. The eventual public
coordinator must independently retain its canonical started/source barriers.

A finite real scheduler regression reproduced a second factory call after table
loss on the old code. The repaired marker, concurrent consumption, ordinary
single-launch and unknown-schema boundaries passed eight focused Windows cases.
An earlier broad component invocation exceeded its180-second bound and is not a
full-suite PASS. This change does not connect the provider factory, enable a
public start/progress issuer, deploy source or accept the five remaining gaps.
