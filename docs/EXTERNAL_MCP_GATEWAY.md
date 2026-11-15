# Legacy MCP gateway to a native external Workflow

`tools/external_mcp_gateway.py` is a fixed-backend HTTP compatibility gateway for one legacy client credential. It keeps the existing frontend URL and bearer credential while forwarding the four existing tools to the native external-client inbox. It does not create an alternative work queue, execute an agent, apply submitted files, or claim product acceptance.

The gateway is deployed on POSIX hosts so it can verify owner UID, private permissions and `flock`. The intended existing deployment host is the Mac; importing the module for portable isolated tests does not attest Windows ACL or locking behavior.

## Private configuration

Run the foreground entry point with a private configuration file:

```sh
python tools/external_mcp_gateway.py --config /absolute/private/gateway.json
```

Example structure (paths and port are examples, not production values):

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 38452,
  "client_id": "glm",
  "upstream_url": "http://127.0.0.1:18520/api/external-clients/glm/mcp",
  "frontend_token_file": "/absolute/private/frontend-token",
  "backend_token_file": "/absolute/private/backend-token",
  "state_file": "/absolute/private/native-session.json",
  "io_timeout": 30
}
```

The seven identity/path fields are required; `io_timeout` is optional and bounded to 1–120 seconds. Credentials are separate files, not command-line parameters or inline config values. Files must belong to the current UID, be regular nonsymlink files, and have no group/other permissions. Their immediate parent directories must be owned private directories, and resolved paths must match the configured absolute paths. Prepare directories with mode `0700` and files with mode `0600`. A state file may initially be absent. Gateway-created lock, temporary and state files use `0600`; atomic state writes fsync the file and directory.

The backend URL must be the exact `/api/external-clients/{client_id}/mcp` endpoint. HTTP backends are loopback-only. A remote backend requires HTTPS and normal certificate/hostname verification. User-info, query strings, fragments and caller-selected endpoints are refused. Direct `http.client` connections never inherit environment proxies, follow redirects, or retry a POST automatically. The frontend listener is explicitly configured; it has no public discovery or additional unauthenticated route. Frontend requests require the configured bearer before JSON parsing or backend contact. Keep this listener on the intended controlled interface; client IDs and shared tokens are operator identities, not proof of a physical computer.

## One owner, one native session

Use **one poller per frontend credential**. Stateless copies sharing a credential cannot be distinguished. Give another computer its own frontend credential and native client ID instead of sharing ownership implicitly.

Supported frontend methods are `initialize`, `ping`, `tools/list` and `tools/call` with only `pilot_next_task`, `pilot_status`, `pilot_read_input`, and `pilot_submit_result`. JSON-RPC notifications return 202 without reaching the backend. Requests and replies are bounded to 4 MiB; nonfinite or excessively nested JSON is refused. The upstream call has a total caller deadline, including drip-fed HTTP headers. A blocked system resolver may outlive the caller in a daemon thread, but cancellation and a deadline check after connect prevent that thread from sending a late POST. One bounded I/O slot remains owned until that thread finishes, so explicit requests cannot accumulate additional blocked resolver threads. Already-sent requests remain UNKNOWN and require reconciliation. The frontend body has an absolute 15-second read deadline; its header socket uses a 15-second inactivity timeout. Logs never include bodies, credentials or the native session.

Before the first backend initialization POST, the gateway durably writes an initialization intent. On success it privately persists `Mcp-Session-Id` and the initialization result. Later legacy `initialize` requests, including after gateway restart, reuse that same result with only the JSON-RPC request ID changed. They create no new native session. The backend session is supplied in subsequent upstream headers but never echoed as a frontend header or response field.

Private state is bound to the exact backend URL, client ID and hashes of both credentials. An identity mismatch refuses the request without changing state or creating a new session. Changes to the URL/credentials are a separately reviewed reconciliation/migration operation, not a reason to delete the state file. Locking serializes one upstream operation; concurrent calls fail closed as busy. State writes also compare the previous raw state hash before replacing it, so an operator must not edit live state behind the lock.

## Unknown outcomes

Each upstream tool request gets a durable intent before its single POST. ACK/UNKNOWN records retain the request digest, tool, public job identity and nonce, not the raw arguments, claim token or report. A crash with an outstanding intent is treated as an unknown outcome on the next request. Histories are retained; once the 4 MiB private-state bound is reached, the gateway stops before another send until an operator performs a separately reviewed archival operation preserving the native session and unresolved records.

An **unknown initialization** is a hard stop. No restart or frontend reconnect creates another native session. Reconcile privately with the backend; do not reset/delete the state file.

An **unknown `pilot_submit_result`** blocks further submit and next-task calls. Explicit `pilot_status` is still allowed in the same owned native session. Only a successful status reporting `submitted`, a receipt and result digest for the **same pending job** resolves that fence. A status timeout or another job's status preserves the original uncertainty. The unknown record remains in history. The gateway itself does not retry submission, advance a Workflow, or replay a gate. Once status proves receipt, an explicit result retry reaches the native adapter's exact-payload idempotency checks; changed content is still refused there.

Unknown next-task and input-read calls do not permanently deadlock the client: a new explicit frontend request may resume the same native owned claim or read the same declared chunk. The gateway makes one upstream POST per request and zero implicit retries. It does not rotate sessions, expire ownership, reset attempts or bypass native claim/version/input fences.

A native result endpoint may spend time in the normal configured gate/reviewer handoff after durably accepting the report. The gateway timeout can therefore be UNKNOWN while the native report already exists. `pilot_status` can prove its receipt without replaying that handoff. A heartbeat, claim or receipt still does not prove that a model is actively coding or that the product passed independent review.

## Existing broker and activation boundary

This source does not replace the existing listener, touch any launchd service, start ZCode, read/claim/cancel the old broker store, or fall back to the old queue. Before an actual switch, the operator must explicitly reconcile or preserve the legacy outstanding job and ensure it cannot be executed twice. A new native assignment is an explicitly selected Workflow external stage, not an implicit migration of a queued legacy payload.

Qualify the gateway against an isolated native candidate first, with separate frontend/backend credentials and an approved native external assignment. Keep the legacy queued record and uncertain receipts intact. Replacing a live listener and connecting the actual GUI require a concrete reviewed operation; running the tests is not that operation. Automatic upstream discovery, production API exposure, legacy cancellation and client restarts are outside this tool.

## Validation

Tests cover authentication before contact, initialization intent/persistence/restart, unknown initialization, one-request/one-send behavior, lost result ACK and same-job receipt reconciliation, explicit same-session next/read retries, serialization, identity mismatch, malformed JSON/body bounds, direct loopback HTTP, no environment proxy, no redirects and secret/header non-disclosure. Windows test runs mock only the POSIX metadata, flock and directory-fsync primitives; real atomic state files and real loopback HTTP remain exercised. A POSIX qualification run uses the actual ownership, lock and fsync implementation. No test contacts the deployed broker or a production Native API.