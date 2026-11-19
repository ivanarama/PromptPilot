# Task-bound budget observation

An already admitted local Native task can refresh its GitHub rate observation:

```text
pp pipelinectl budget-refresh --profile <profile-id>
```

Use the task's original worker environment. The command accepts no manual task,
start, route or cost argument. It checks the actual running task, start,
session/pane, series, priority, execution inputs, lifetime, pause/cancellation
and its unique existing `skill` or `fallback_targeted` reservation. The existing
reservation must match the profile/queue and that route's canonical cost.
Provider hints cannot select another route. A missing identity fails before
Native data is initialized.

The command uses the same configured `insights` admission estimate, actual task
priority, shared floors, fairness and account-wide scan lease as a Source
refresh. Its existing execution promise is counted once; no new task, provider
or Skill reservation is created. Other reservations and foreign leases are
preserved. The estimate is deliberately unchanged even though the command
stops after the canonical REST and GraphQL rate observations.

The rate cache write checks the exact scan lease and Native task/session,
execution and admission guard inside the SQLite write transaction. A lost
identity, changed execution input, changed reservation or lost lease cannot
publish the new observation. Partial rate responses or a mismatched trusted
GitHub account are refused. No queue Search, checker, Source collection,
Source cache publication, Source refresh-status publication or trend/history
update runs. The command does not change the Source epoch or freshness token.

On success, exit0 and `state:"refreshed"`, `rate_snapshot_refreshed:true` and
`admitted_observation:true` mean that this command observed and durably stored
the rates under its actual admission. `rate_snapshot` contains the genuine
`observed_at` and canonical resources. A separate budget checkpoint must still
apply its existing TTL, reset, policy, cost, floor and reservation checks.
Freshness is not transferable to a later action and its TTL is not extended.

All results explicitly set `source_refreshed`, `source_admission_granted`,
`native_source_admission_granted`, `native_admission_granted`,
`mutable_admission_granted`, `granted` and `product_mutation_authorized` to false.
The scope is `Native-budget-observation-only`. Every independent Source,
ownership, proof, CI, publication and mutation gate remains mandatory.

Ordinary budget/lease contention returns exit3 and `state:"wait"`; invalid
identity, failed observation or lost fencing returns exit2 and
`state:"refused"`. Neither permits a retry loop or a mutation. The canonical
admission may have stored valid rates **before** deciding that the configured
scan estimate does not fit. Therefore a failed command reports
`admitted_observation:false` and `rate_cache_write_not_excluded:true`; it does
not claim that the rate cache stayed unchanged. Source data and history remain
unchanged in either case. Preserve the typed result and follow the task's
existing continuation/defer procedure.

## Final Source check for MERGE

A costed `merge` queue reserves one additional `insights` scan when it admits a
`skill` or `fallback_targeted` attempt. Use the following command when that
attempt needs a fresh Source immediately before its publication gate:

```text
pp pipelinectl source-refresh --profile <profile-id> --final
```

The check spends the reservation once, after it obtains the shared scan lease
and verifies the live resource floors. Other tasks keep their position in the
queue. Earlier Source observations and `budget-refresh` use ordinary admission.
For an attempt with an unused reservation, an admitted final scan reports
`budget.final_source_credit_used: true`. A retry after that scan, including a
retry after a failed observation, goes through ordinary admission. Older
attempts without a reservation also use ordinary admission.


## Resuming a confirmed branch update in MERGE

A targeted MERGE using the ordinary signed fallback and item blockers can
store its confirmed branch update before its final Source observation. Use the
Native executable from the worker environment and the absolute gate lease file
from that occurrence's handoff.

```sh
pp pipelinectl merge-checkpoint record --profile "$profile" --number "$pr" \
  --from "$old" --base "$second_parent" --to "$new" --tree "$tree" \
  --intent "$comment_id" --lease-file "$gate_file"
pp pipelinectl merge-checkpoint read --profile "$profile" --number "$pr" \
  --to "$new" --lease-file "$recovery_gate_file"
```

Record only after the repository procedure verifies the trusted immutable
intent, the updated ref, both actual parents in order and the tree. The intent
ID is the positive numeric GitHub issue-comment ID. Repeating the same record
returns the original facts. A conflicting record is refused. Record and read
require a running task, its original start marker, targeted Native admission
and the exact gate file elected for this occurrence. A recovery election on
the new HEAD can reconstruct a missing record from independently verified
GitHub evidence after a crash.

A signed recovery waiting for Source saves an observation hint for the same task.
On its next claim, Native compares the remaining snapshot TTL with the profile's
refresh-ahead interval. If needed, it runs the ordinary budgeted Source scan
before acquiring the election lease. A matching fresh snapshot from the sampler
can satisfy this check. The new signed election and Source limit the handoff to the canonical post-update
checks and confirmation. If a one-use admission hold was already claimed, the
admitted recovery keeps its normal Source refresh path. A changed target,
configuration, or conflicting graph follows the repository recovery procedure.

The commands print JSON and exit 0 on `recorded`, `found`, or `missing`.
Exit 1 with `state: "refused"` reports a runtime refusal. Every result sets
`product_mutation_authorized` to false. Only the repository procedure can
validate the original intent, determine whether done already exists, and
authorize its publication for the exact current HEAD.
