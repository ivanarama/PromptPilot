# Technical FIX administrative settlement (development source)

`next fix` can select the separate `review-routing-v1-fix-settlement` purpose
only from a fresh complete checker `technical_fix_settlement_candidates`
allowlist. Each closed target contains `stage`, PR `number`, current `head`,
the original committed `source` and `attempt_id`. The current HEAD must differ
from that source; lifetime PR/repository/ref identity and author node ID remain
bound. All four consumer hashes and the full paired execution contract are
mandatory. Absent configuration/capability cannot silently enable this route.

The scheduler reserves the exact current-head target for its claimed headless
task. It cannot retarget a previously reserved task. A fresh canonical owner
waits for the previous 30-minute technical lease to expire, then takes over
through the normal immutable lease protocol. The expired lease is scheduling
eligibility only; it is never evidence that the old executor stopped. An
engineering barrier on one target does not preclude selecting other targets.

`complete-fix-settlement fix` independently checks the complete canonical
source/attempt/history and old executor:

- Pipeline: the original exact START/UUID/task binding, trusted original owned
  cleanup or known pre-factory refusal, terminal old coordinator and durable
  publication history are required. Unknown launch stays blocked. Unknown push
  first needs independent authenticated graph/trailer/full-identity readback;
  no push is repeated. Retirement permanently seals old launch/publication.
- Author: the original verified delivery receipt and terminal old coordinator
  are required. A controlled, journaled DELETE revokes only the bound author's
  assignment. Exact authenticated node-ID readback confirms revocation; an
  absent assignee alone cannot substitute for the original delivery/intent.
  Lost responses use reads only. An ambiguous DELETE is not repeated, and
  reassignment after confirmation is preserved as a manual change.

Only after that confirmation can the controller publish the canonical
`settlement` and one exact-current-HEAD `resume`. It never fabricates `fix-done`,
refunds a slot, changes source, launches a provider or permits MERGE. The
existing REVIEW admission/publication path consumes the resume once and keeps
the spent lifetime budget. Product/safety/hold/ship/manual/override gates remain
effective, including changes after local writer waits.

The actual worker validates the signed purpose against the outer action, exact
task, transport, replica count and target before the administrative command. Its
completion is deterministic with zero model/provider calls. Metadata control
commands own their entire process tree, including descendants after timeout;
failed OS cleanup retains the original handle and task quarantine until a
same-handle retry succeeds. Unknown process outcomes cannot release ownership.

The public source path is tested with real scheduler/journal/worker APIs and
synthetic authenticated GitHub/checker peers. The pipeline recovery case also
uses an actual private Git checkout and local bare-remote CAS. It includes a
complete new-HEAD REVIEW publication and rejection of a second resume use.
This is not live OneBase compatibility or production enablement. The actual
OneBase checker/four-reader pair and full native/CI/independent review gates
are still required for full #151 acceptance.
