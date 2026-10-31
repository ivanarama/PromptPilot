# Bound native questions

An executor or reviewer can end a native report with exactly these final lines:

```text
ELICIT_JSON: {"schema_version":1,"kind":"information","question":"Which target?","options":["staging","production"]}
ИТОГ: НУЖЕН ЧЕЛОВЕК (operator answer required)
```

A `permission` question additionally describes its requested `action` as text.
This description is never executed. Quotes/code fences, trailing instructions,
unknown fields, duplicate JSON keys, invalid versions and ambiguous reports do
not create a structured request. The ordinary human-wait fallback remains.
Planner/external assignments retain their existing question path; this protocol
binds a native `workflow_run`, not a plan or an outside performer's identity.

The server records the question with its current workflow version, round, run,
role, run attempt, native task/retry/start, terminal output and actual rights.
A read endpoint returns only a still-current awaiting decision. An answer needs
its exact request ID, binding digest and expected version. Output, retry, rights,
configuration, round or lifecycle drift invalidates that response. A completed
answer cannot be replayed or resurrected by reconciliation of the old task.
The existing event journal is authoritative; no second mutable request store is
introduced. Capture and lifecycle pause share one transaction; recording an
answer and its operator note share another.

While the question is current, deterministic reconciliation waits and ordinary
resume/review/dispatch cannot bypass the answer. Cancellation remains available.
Saving the answer keeps the workflow paused and grants no permissions, executes
no command, creates no provider task and does not apply a gate or review PASS.
The operator can then continue through the existing workflow controls; the
answer is rendered into the next native executor/reviewer prompt as an operator
note. The actual rights policy remains authoritative. Permission changes require
the existing explicit configuration path; answering 'yes' does not change them.

The UI reads the current bound question separately from the capped event tail,
escapes its untrusted text, and submits the form's exact workflow/version/request
binding. This is a question/answer contract, not OS sandboxing or a command
approval broker. Historical/external fingerprints remain compatible; new native
rights prompt fingerprints carry policy v2.

This is a separate slice of #145. Pair deployment, other autonomous handoff work
and production flags are unaffected by this source change.
