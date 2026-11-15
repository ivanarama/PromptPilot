# Native external-client inbox

This router hands an explicitly selected native Workflow external stage to an MCP client. It creates no queue task, executor process, repository checkout, Git checkpoint or second business workflow. A claimed attempt and its result remain in the Workflow's own history; receiving an external result does not accept the stage.

Mount `external_inbox.router` in the existing API application, under its normal authentication, Host and Origin middleware. Do not publish this router in an unauthenticated public application. Client IDs route assignments; they do not replace API authentication or provide separate operator permissions.

## Select an external executor

Create/approve a normal Workflow containing an `external` execution stage, with this additional configuration:

```json
{
  "external_executor": {
    "client_id": "glm",
    "advance_after_submit": true
  }
}
```

The inbox selects only that client's currently `awaiting_external` executor run, with no queue task attached. It freezes the native workflow version, run ID and input SHA when claimed. It neither opens an external stage nor changes the approved plan. Workflows carrying any non-null `workorder_guard` are excluded, including when a guard appears after a claim. Other execution modes, roles, clients, cancelled runs and stale versions cannot be submitted through this adapter.

`advance_after_submit` is optional and defaults to false. When true, a proven accepted submission requests `advance_workflow` for this exact workflow once. That engine still observes the workflow's automation, gate, reviewer and revision settings. A failed/uncertain advance is never replayed by a duplicate result submission; inspect the native workflow and use normal workflow controls. Without the option the workflow stays in `gating` until normal workflow advancement.

## MCP compatibility and session ownership

Endpoint: `POST /api/external-clients/{client_id}/mcp`.

The four legacy tool names and input schemas are preserved: `pilot_next_task`, `pilot_status`, `pilot_read_input`, `pilot_submit_result`. Claimed responses include the familiar `job_id`, `claim_token`, `task.instructions` and `task.inputs`; idle has `task: null`. Input reads include `next_offset` and `eof`. Results use the legacy receipt/result digest fields with additional native workflow/run references.

The transport supports the 2025-06-18 and 2025-03-26 MCP versions. `initialize` returns an opaque `Mcp-Session-Id` header, which the client must preserve on subsequent tool requests. This follows the [2025 Streamable HTTP session contract](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-03-26/basic/transports.mdx). It does not claim compatibility with later sessionless protocol versions. Initialization, ping, tools/list and notifications never claim an assignment or advance a Workflow. JSON-RPC notifications cannot execute tools.

Only hashes of session secrets are persisted. A claim is owned by that client and session, not by IP. Its claim token is derived from the session secret and stable claim identity. A second session cannot steal it; elapsed time, missing heartbeats and reconnects do not trigger reassignment. Keep the session header to reconcile a lost response. Losing it requires an explicit operator decision through normal native lifecycle controls, not an automatic lease expiry.

`pilot_read_input` reads only the issued virtual `assignment.txt` (UTF-8, at most 128 KiB), in character ranges of at most 16,384 characters. There is no arbitrary repository, home directory or broker-store read. Supply necessary task context in the native assignment itself.

## Submission, unknown outcomes and artifacts

A submission contains an honest `completed`, `blocked` or `failed` outcome, a nonblank summary (at most 65,536 UTF-8 bytes), and at most 64 files totalling 2 MiB. Request bodies are bounded to 4 MiB. Paths must be finite relative POSIX names; traversal, absolute/Windows paths, reserved device names, `.git`, case collisions and file/directory collisions are refused. Files are UTF-8 text or strict base64, never both.

Files are immutable SQLite blobs in this adapter's namespace. They are never applied to the repository. Authenticated downloads use:

`GET /api/external-clients/{client_id}/jobs/{job_id}/artifacts/{path}`

Native output includes the declared outcome, summary, exact file hashes/sizes, bounded full text files when they fit, and the authenticated artifact route for the rest. Binary or large omitted files require explicit retrieval by the reviewer/operator; their presence is not an acceptance claim. Artifacts remain untrusted data. The report explicitly says `UNREVIEWED EXTERNAL RESULT`; the core external-result function's executor verdict is transport completion, not gate or audit approval.

Before the non-idempotent native submit function runs, one transaction durably stores `submitting`, the canonical payload digest, exact frozen native submission, expected canonical native output hash, immutable artifacts and a receipt ID. The native function then enforces its own version/run/input and lifecycle fences.

An exact retry first inspects the native run's completed output and canonical hash, including external performer/comment metadata. A lost native response can therefore return the same receipt without resubmitting. A changed retry is refused. If acceptance cannot be proven, state remains `submitting` or `unknown`, owned by the original session; neither the adapter nor a new client blindly calls native submit again. Inspect `pilot_status` and native history. An exception alone is never rollback evidence. A stale/cancelled/reassigned tuple is never refreshed to a new assignment by retrying an old result.

## Read-only feed integration

`client_activity(workflow_id=None)` returns a redacted projection suitable for the single Workflow card: client, public session and claim identities, job/run references, state, last signal, receipt/digest and outcome. It creates no tables, updates no heartbeat, and returns an empty list before first initialization. No session secret, claim token, raw assignment, configuration or artifact body appears in this projection. `execution_observed` is always false: polling or claiming proves a client signal, not active coding or executor termination.

`client_result(workflow_id)` is another literal read-only projection for the operator card. It returns the latest submitted report only after verifying the canonical native output hash and the frozen report itself. It includes the actual run ID, outcome, receipt, an explicitly unreviewed text preview (at most 150,000 characters, with a truncation flag), and immutable artifact names, hashes, sizes and authenticated relative download URLs. A newer revision may still be pending: the historical run ID is never presented as that new attempt. Missing databases return no report without creating a directory or database.

Storage tables are `external_inbox_sessions_v1`, `external_inbox_claims_v1`, and `external_inbox_artifacts_v1`, created only by explicit MCP POST handling. They supplement native attempts with transport ownership and receipts; they are not a second task scheduling queue.

## Existing broker and stopped clients

Adding this router does not migrate, cancel, claim or submit any legacy broker job, change the existing port/authentication, reconnect ZCode or modify production workers. A legacy proxy needs an explicit, separately reviewed forwarding adapter that preserves the native session header and prevents dual queue ownership. Its queued legacy work and existing uncertain receipts must be reconciled before switching clients. A read-only task list federation alone does not provide that execution handoff.