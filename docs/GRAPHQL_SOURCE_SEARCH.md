# Full Source search and the REST Search window

Full queue snapshots use GitHub GraphQL `search(type: ISSUE)` with the original
`repo:<repository> <query>` search string. GitHub defines this search union to
contain both `Issue` and `PullRequest`; the query does not approximate search by
listing repository issues or interpreting search qualifiers locally. Cursor
pagination retains the 1,000-result search window. A larger result set is
explicitly incomplete and cannot supply full Source admission.

This avoids spending the separate REST Search allowance while preserving the
configured conservative admission estimates. In particular, a task with its own
Search12 reservation and hard execution floor12 still needs24 remaining Search
requests. A REST Source scan that consumes8 of a30-request window leaves22 and
correctly fails that continuation gate. The GraphQL transport does not consume
that REST window. This changes actual API use; it does not reduce either floor,
the promised skill cost, the configured `insights` estimate, or foreign reserves.

The same account lease, actual task/start/session/execution/reservation fences,
priority policy, pause barrier, Source profile/epoch CAS and complete checker
publication still apply. A data-plus-errors response, unknown node type, foreign
repository, missing field, count drift, repeated cursor or duplicate member
fails the observation. Label connections are also fully paginated; their item
ID/type/updatedAt and total count must remain stable. No REST fallback hides a
GraphQL denial or partial result.

Each queue's `search_usage` reports the server-returned data-query `rateLimit.cost`,
data request count, cost-preview count and observed returned nodes. Costed scans
use `rateLimit(dryRun: true)` on the exact document and variables before each
search or label request. This calculates cost without evaluating its data fields.
A query above the unchanged configured GraphQL `insights` estimate is refused.
Before both preview and data calls, the ordinary full scan estimate is checked
again against fresh canonical rate observations and all live reservations under
the existing lease/task fence. A quota loss returns a typed non-PASS budget wait;
it cannot publish a partial Source snapshot. Rate metadata collection follows
the same producer/fence as initial scan admission. One configured GraphQL
allowance covers the whole leased Source operation, including its initial and
final rate observations, health checker, every queue, preview and data page.
Known request costs and observed quota reductions are charged conservatively;
the next request is refused before it can exceed that unchanged allowance.

A search request statically asks for at most
100 item nodes,100 repository nodes and10,000 label nodes; a label continuation
asks for at most one item and100 labels. These bounds are below GitHub's
500,000-node request limit. **These metrics are observations, not admission or
proof grants.** Data-query cost excludes cost-preview and rate-metadata overhead
and the external health checker. These values do not establish that a particular configured GraphQL cost
estimate covers every queue size and external health checker. Before activation,
qualify the measured query costs plus the checker on the largest supported queue;
an inadequate estimate requires its ordinary reviewed configuration change.
Do not infer live query cost from test fixtures.

All producers need the accepted implementation: API sampler, worker, and any
separately installed `pipelinectl source-refresh` package. Updating only the CLI
leaves older background samplers consuming REST Search. Source installation
continues through the usual reviewed, qualified cutover; no running provider or
Native identity is created by this transport.

Primary references:

- [GitHub GraphQL search: syntax, union and 1,000-result window](https://docs.github.com/en/graphql/reference/search)
- [GitHub quota resource families: REST Search and GraphQL](https://docs.github.com/en/rest/rate-limit/rate-limit)
- [GraphQL cost and node limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- [Server cost calculation without evaluating data](https://docs.github.com/en/graphql/reference/meta#ratelimit)
