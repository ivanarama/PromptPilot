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

A separate read-only post-push evidence component checks a complete bounded
forward graph of at most100 commits from the exact source, unique terminal
attempt/source/context trailers, and exact current PR/ref/repository/HEAD REST
identity after network reads. Sixteen finite metadata-peer cases cover divergent
or truncated graphs, copied/duplicate/nonterminal trailers including CRLF, and
late identity drift. It observes an author's push; it does not attest CAS or
issue completion/launch permissions. Public progress/coordinator wiring and the
pipeline's actual expected-HEAD CAS writer are still required.

The private progress-request schema binds a separate progress_version and exact
new_head inside the existing version1 signed lease envelope. Old admission and
reserved progress decoders reject this closed field set. Twelve focused scope
cases verify valid HMAC round-trip, wrong-purpose/source/version refusal and
configuration drift. This serializer is not a public issuer or mutation permit.


## Private author-progress continuation

The public next/complete FIX CLI and actual worker now have a separately gated
administrative author-progress prototype. A complete explicit peer capability
must advertise both author-handoff and author-progress. The source, author node,
assigned executor, canonical attempt and lifetime budget are retained. The new
signed purpose also binds the exact new HEAD. Every write revalidates checker,
all four reader hashes, full stable server history, local claimed task, exact
reservation, cancellation, current canonical lease and clock after writer waits.
A new local owner cannot take over an unexpired canonical owner lease.

The observer reads the authenticated head repository for same-repository and
fork commit graphs, then confirms the exact PR identity in the base repository.
It publishes fix-done only for independently checked graph/trailers/current HEAD.
The actual worker verifies the signed action/target/protocol, performs only this
administrative completion, and loads no provider. A duplicate readback does not
spend another attempt, assign again or publish a second fix-done.

This remains unpublished development, with compatible private peers. It does
not attest the author's CAS push, implement the pipeline execution factory,
heartbeat, trusted settlement writers or actual accepted OneBase pairing.
All five original acceptance gaps still require their remaining work; production
flags and Windows scheduled maintenance stay OFF.


The private pipeline START schema now binds a distinct signed purpose to the
canonical attempt and started IDs, source, full same-repository PR identity,
trusted executor, task occurrence, attempt UUID, reservation, four reader hashes,
configuration digest, replica count and finite expiry. Its explicit executor
policy refuses fork execution. Sixteen focused serialization/policy cases pass,
including signed purpose/source/owner/identity changes, replica drift and OFF.

This is serialization and policy scaffolding only. It neither verifies server
canonicality by itself nor connects a provider. The future public coordinator
must freshly verify actual started history, ownership, source and checker after
all waits and use OneUseStart at the actual factory. The complete execution peer
contract is deliberately unavailable from all current public routes. No pipeline
execution, heartbeat, CAS writer or settlement completion is claimed here.


A private canonical START guard now composes the real history reducer, exact
started/task/UUID binding, complete checker allowlist, actual four reader hashes,
explicit same-repository executor policy, current actor/source/author and live
canonical lease. A final local consumed-scope read checks cancellation, task
occurrence, exact reservation and strict lifetime immediately before one factory.
The token consumer cannot create a missing receipt table or reconstruct lost
local issuance from an existing server START. The future trusted issuer must
persist its own new issuance and prepare the exact scope; an old token cannot.

Eight focused scheduler/peer/owned-process and base one-use cases passed43.46s.
Two further strict-lifetime/positive cases passed11.85s; two fresh changed-checker
started/task cases passed11.34s. The positive seam launches an actual harmless
owned Python child once; ambiguous factory failure stays unknown without retry.
These are private integration seams with fixture-produced trusted records.
The actual public pipeline admission/START issuer, worker wiring, technical lease
heartbeat, CAS writer, settlement/resume writers and real paired acceptance are
still required. This does not enable a production capability or close #151.


The private pipeline-admission purpose is separate from author admission and
START. Both codecs share closed structural/local configuration and live strict
reservation checks; the public author decoder still rejects the pipeline
purpose, and pipeline admission rejects author execution and fork identities.
Forty combined pure scope and actual public author/fork progress cases passed
35.79s after that extraction, with Ruff. No public pipeline issuer or capability
was added by this serialization/refactoring slice.


The private pipeline issuer now composes source-bound admission, canonical
attempt/assigned/handoff/started receipts and prepared local one-use scope. Its
final write ticket repeats source/actor/checker/lease after SQLite renewal waits,
then checks the actual local task/cancel/reservation and readers/config/clock.
It preserves one spent lifetime slot, derives one task UUID from the immutable
admission nonce and refuses retargeting a claimed task. An existing server START
cannot rebuild a missing local issuance. An ambiguous absent POST stays a barrier;
an authenticated exact readback of a newly sent START can recover response loss.

Six private scheduler/authenticated-peer/actual owned-child tests passed56.53s
with Ruff. They include positive one-launch, response loss, repeated pre-factory
readback, lost local issuance, unknown absent POST, late actor/cancellation and
zero author assignment operations. This is a private issuer integration API;
actual pipelinectl dispatch and installed worker, technical heartbeat, CAS push,
settlement/resume writers and accepted OneBase pairing remain unconnected.


## Unpublished public START metadata CLI

The development source now exposes source-bound `next fix` pipeline admission,
`prepare-fix-start fix` and read-only `gate-fix-start fix` under the explicit
complete paired policy. The gate requires an already consumed matching local
START row; possession of a token alone is refused. Three actual CLI/legacy gate
cases passed24.95s. Three additional public fork/default-author and existing
same-repository/fork author-progress cases passed37.45s: switching on a configured
pipeline executor never rewrites an existing canonical author attempt.

This is an unpublished CLI metadata slice. The installed worker still refuses
these new pipeline purposes before any generic provider fallback. Its actual
factory/workspace wiring, technical heartbeat, CAS push, trusted settlement/resume
writers and accepted checker/four readers/paired qualification remain required.
No production configuration, installed source or scheduler flag changed.


The private workspace component creates a deterministic detached worktree for the
bound source commit and writes its exact scope/common-repository receipt outside
the checkout. It requires a single credential-free origin URL for the expected
GitHub repository, disables hooks/fsmonitor/replacement objects for its Git
operations and never checks out/resets the base directory. Existing partial
creation or drift is preserved for engineering inspection; no automatic deletion.
Seven actual zero-network Git cases passed57.08s with Ruff: clean/idempotent source,
base dirty files/index retained, bytes/HEAD/scope/remote drift refused, missing
receipt preserved and a real base post-checkout hook not executed. This remains
a private component, not worker wiring, OS sandboxing or paired acceptance.


The unpublished public START gate now derives the exact workspace from the
scope UUID and actual scheduler source directory, verifies its receipt/common
repository/clean source before and after fresh remote gates, then repeats the
strict consumed local task fence and lease clock. It returns the exact path and
receipt digest to the trusted coordinator. Real Git source fixtures exercise
unconsumed-token refusal after preparing a valid checkout and reject modified
bytes/receipt before any factory; three cases passed57.20s. The initial combined
source/default-author check had two cases passed28.96s. Metadata-only launch
composition now lives solely in test helpers; its positive case passed21.47s.
Installed worker wiring, heartbeat/CAS/settlement and real pairing remain open.
