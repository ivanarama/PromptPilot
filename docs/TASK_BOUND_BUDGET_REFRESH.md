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
