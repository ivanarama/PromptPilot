# Owned technical FIX worker (development source)

The opt-in pipeline executor uses the actual public `next fix`, source-bound
`prepare-fix-start fix` and `gate-fix-start fix` commands. Before issuing START,
the scheduler restores the exact running task's priority and reserves its
provider-spanning GitHub admission budget. The worker validates the signed
purpose against the outer action, complete source/PR identity, task occurrence,
replica count and headless transport. Invalid input cannot enter a generic
provider fallback.

The provider runs once in a deterministic detached private source checkout.
Its public START response carries the original canonical blocker report. Before
any provider is built, the worker validates the report's exact committed hash,
HEAD and all-technical classification. The prompt includes that report, full
PR identity and source/attempt binding as data; it cannot substitute another
target or treat report text as controller commands.
New audit bindings optionally retain `audit-history-v1`: the exact original
header and an ordered cursor/node-ID manifest, bound by the existing complete
snapshot hash and publication context. Runtime REVIEW retains this material.
Historical verification uses the retained header and the unchanged original
nodes, so later labels, lifecycle state and backdated commit insertions cannot
erase a completed receipt. It rejects missing, reordered or changed original
nodes and unexplained independent events. This is historical evidence only;
live writes still require the strict full-prefix, current HEAD and owner gate.
Older development records without retained material require an available exact
original projection. The existing 60,000-byte complete-comment bound remains:
oversized history is an explicit engineering refusal, never truncated evidence.

The one-use receipt is consumed before the final gate and factory. The worker
atomically keeps separate permanent attempt/source issuance tombstones. Losing
the mutable START row cannot authorize an overlapping issuer to recreate it.
The checkout verifier hashes regular working files as raw Git blobs, independently
of status, index concealment and clean/smudge transformations. Private checkout
uses LF with automatic line conversion disabled; files whose working bytes still
differ from their tree, linked files and submodules require engineering handling.
The finite raw check permits at most128 MiB per file and512 MiB for the tree.
The physical file/directory manifest must match the tracked tree and its local
Git pointer; ignored uncommitted files, including instructions, are refused.
Build/test/cache artifacts belong in the owned sibling `../artifacts` directory.
Both initial metadata selection and configured availability probes use retained
owned process boundaries, including cleanup quarantine after an OS error.
The worker retains the original kernel boundary before audit I/O, registers it and records
its memory-bound producer proof. The task's rights/provider snapshot remain in
force. START, heartbeat and publication tokens and the producer nonce are kept
out of the provider prompt/environment. The agent receives only the intended
source correction, repository procedure and exact non-secret commit trailers.
This is an owned-process protocol, not an OS filesystem/network sandbox.

The worker keeps the strict local target alive and renews its same canonical
technical owner within the finite timebox described in
[technical-fix-heartbeat.md](technical-fix-heartbeat.md). It never calls the
generic spawn retry, rate-limit requeue or verdict-repair model. Provider errors,
cancellation, deadline expiry and ambiguous launch cannot authorize a second
provider. Cancellation and stop close/wait on the original boundary; completed
stdout/stderr/input readers are required before publication.

`prepare-fix-publication fix` uses an expired START only as a lookup. It requires
the original committed cleanup receipt, actual still-claimed task and strict
reservation, fresh complete paired capability, canonical source/started owner
and clean forward output. It issues the separate
`review-routing-v1-fix-publication` purpose, binding the exact new commit/tree.

The publisher journals one expected-HEAD CAS before its final gates. It checks
the exact source ancestry (at most100 actual commit objects before the source), terminal source/context/attempt
trailers and clean private checkout. The push names one explicit trusted GitHub
branch with `--force-with-lease=<ref>:<reviewed-source>`, after independently
proving the update moves forward. Hooks, URL rewrites, remotes named like the
literal destination URL (including their pushURL), implicit tags and
submodule pushes cannot widen that operation. There is no second push after an
attempted or ambiguous send. Independent authenticated graph/commit/full-identity
readback can confirm the result even when the push response was lost.
Ancestry is reconstructed from hashed raw commit parents with grafts and object
replacement disabled; filtered local reachability cannot authorize a rewrite.

Only the confirmed local CAS receipt plus independent new-HEAD/trailer evidence
can publish the canonical `fix-done`. Its write-ahead transport repeats source,
actor, checker, all four readers, configuration, task, ownership and expiry gates
after SQLite waits. A provider success declaration alone never completes FIX or
wakes REVIEW. Unknown receipt/table loss preserves an engineering barrier.

The retirement observer independently requires the old canonical START, exact
durable original factory cleanup (or a known pre-factory refusal), terminal old
coordinator and known publication outcome. It cannot settle from task status,
lease expiry, process absence or agent text. An unknown push first needs exact
readback. A permanent local retirement seal prevents a stale worker from starting
or publishing again, including a concurrent publisher waiting for its DB writer.
The public administrative continuation is documented in
[technical-fix-settlement.md](technical-fix-settlement.md).

Focused Windows evidence includes real owned subprocesses, real private Git
checkouts and actual CAS against a local bare remote, with authenticated synthetic
GitHub/checker peers. Public OneBase checker/four-reader pairing, complete native
qualification, independent exact-HEAD review and five
mandatory CI remain required before accepting the whole #151 change. Production
configuration, installed source and paused maintenance tasks remain unchanged.

The opt-in `tests/test_review_routing_actual_onebase_pair.py` suite runs actual
Go `pipelinehealth` online against a private executable GraphQL peer. Its output
is consumed unchanged by public PromptPilot REVIEW/FIX commands and the owned
worker. The fixture copies the complete actual REVIEW/FIX/MERGE/TAIL procedures
and the existing TRIAGE/PLAN encoding contracts; it does not synthesize reader
markers or capability dictionaries. A harmless finite Python child produces the
correction, and the exact CAS arguments target a private bare Git repository.

The paired-build operator must explicitly supply all three test inputs:
`PP_ONEBASE_PAIR_CHECKER` (absolute compiled binary path),
`PP_ONEBASE_PAIR_CHECKER_SHA256` (the exact binary digest), and
`PP_ONEBASE_PAIR_SKILLS` (the actual OneBase `.claude/skills` source directory).
The suite verifies the binary and all four source tree hashes before running.
Absent inputs produce an explicit ordinary-CI skip; partial or incorrect inputs
fail. Run `python -m pytest -q tests/test_review_routing_actual_onebase_pair.py`
and preserve every `actual-pair-receipt.json`. The three scenarios cover normal
completion, a lost push response and reader drift after the publication writer
wait. They require one provider and at most one CAS, no second technical slot,
and either the exact fix-done/new-HEAD REVIEW or a retained failure barrier.
This local pair qualification does not supply production metrics or activate
the paused maintenance service. Repeat it for the exact accepted source pair.
