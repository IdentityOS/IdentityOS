# Reflex execution

Reflex execution turns a promoted learned procedure into a fast, exact-match
command path. It does not ask a model to reconstruct or re-plan the procedure.
The compiled steps still run as a durable Executive task and produce ordinary
step evidence.

## Registration and dispatch

A reflex binds three immutable records:

- a trigger template such as `write {content} to {path}`;
- the current promoted procedure version and its content digest; and
- the procedure's scalar parameter schema.

Every parameter must appear exactly once in the trigger. Adjacent parameters,
format expressions, complex object or array parameters, and secret-bearing
parameter names are rejected. Literal trigger text is regex-escaped and the
entire normalized utterance must match. If no unique match exists, the runtime
uses its normal conversation path.

Before a task is queued, the engine recompiles the current procedure and checks
that each step maps to an installed, currently authorized capability skill. A
changed champion version or digest makes an old reflex stale; it must be
registered under a new reflex ID. This prevents a trigger from silently gaining
new behavior.

Install the `reflex` capability and grant `reflex:manage` to register a binding
and `task:execute` to run it. `reflex.list` reports whether each binding is still
ready. `reflex.reconcile` compares the dispatched plan digest with the durable
task and requires a completed task with wholly successful evidence before it
reports verified execution.

## Latency and correctness evidence

`ReflexEngine.benchmark_planning` is a trusted host API. It measures both the
reflex compiler and a supplied planner callback using a monotonic clock, hashes
both emitted plans, and reports speedup only alongside plan equivalence. It
does not accept caller-provided latency or correctness claims. Execution
correctness remains a separate, stronger check performed by reconciliation
against runtime evidence.

Reflexes reduce repeated planning latency; they do not bypass capability
permissions, parameter validation, Executive persistence, crash recovery, or
behavioral verification.
