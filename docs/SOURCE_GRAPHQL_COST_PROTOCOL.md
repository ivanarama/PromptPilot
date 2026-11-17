# Successful Source cost receipts

`source-graphql-cost-v1` is an optional project-checker protocol. It does not
authorize Source publication or any product mutation on its own.

New successful-observation pricing requires the explicit per-profile
`github_budget.source_cost_protocol: "source-graphql-cost-v1"`, enabled budgeting
and configured finite route costs. Absence preserves the accepted strict
counter-window pricing and existing checker commands, including their legacy
exit/JSON behavior. Unknown declared modes refuse before RPCs; a modern failure
never falls back to legacy pricing. A checker cost declaration without this
pricing opt-in is invalid. Legacy accounting is labelled separately and never
claims the new successful-cost proof.

The profile opts in with `health_check.source_graphql_cost.protocol` and
`executable_sha256` and explicit `contract_sha256`; command0 is a new direct
immutable executable with one explicit `-contract <file>` argument. PromptPilot
verifies that executable before and after the checker. It appends
`-source-graphql-budget N -source-graphql-nonce <32 lowercase hex>` and requires
`source_graphql_usage` in the normal full checker report:

```json
{"protocol":"source-graphql-cost-v1","nonce":"...","allowance":99,
 "charged_upper_bound":42,"observed_actual_cost":42,"physical_attempts":12,
 "preview_attempts":6,"data_attempts":6,"unproved_attempts":0,
 "uncertain_cost":false,"complete":true,"repo":"owner/repository",
 "trusted_account":"account","contract_sha256":"..."}
```

Every physical request, retry and churn attempt shares the same meter. Unknown
pricing reserves the entire remaining allowance. Only a clean, correctly bound
server `rateLimit.cost` receipt settles that escrow. Preview receipts charge
their reported cost and quote the following data request. Data reserves the
quote before sending and must return an actual cost no larger than that quote.
Any error, timeout, missing receipt or cost above escrow poisons the observation;
an internal retry cannot repair its successful-cost proof.

`charged_upper_bound` names retained local accounting. **For a failed or
uncertain request it is not a guarantee about the actual server charge:** timed
out requests may incur additional points. Only a complete, certain, full report
can prove a successful observation within its allowance. Red product findings
can be a complete report; a technical failure cannot.
Only normal checker exits0/1 can support that receipt. Successful v1 receipts
have exactly the declared fields, positive physical attempts and equal positive
preview/data counts; an all-zero fresh checker receipt is invalid.

Exit75 has one narrow meaning under this opted-in protocol: a usage-only JSON
report with the exact current nonce/cap/artifact/input bindings, reason
`allowance_exhausted`, `complete:false`, `uncertain_cost:false`, no unproved
attempts, equal known charged/observed costs within the cap, and consistent
positive physical counters. It is a typed budget wait, never a Source proof.
Every malformed or foreign exit75, other technical exit, timeout and bad JSON
remains a checker failure. Duplicate JSON keys are rejected for this protocol;
legacy unbudgeted checker decoding and exit behavior remain unchanged.

PromptPilot's one whole-operation meter includes bootstrap metadata, the checker
receipt once, every own metadata/preview/data request and final metadata. A
separate non-replenishing account envelope preserves the unchanged full route
estimate, floors and all foreign/own reservations. Raw canonical rate snapshots
are stored unchanged. Regional counter/reset variations never create credit or
serve as proof of own request cost.
V1-opted-in budgeted profiles with an unmetered checker fail configuration validation
before spawning it. Unbudgeted profiles retain their existing checker protocol.

Cached diagnostics may be reused only from a complete publication under the
same new profile/artifact with a successful checker receipt and the original
health TTL. No new checker RPC means current checker cost0; the old receipt SHA
and age remain reuse provenance, without recharging old cost or matching its old
nonce to this new operation. Old unmetered diagnostics are not that proof.
