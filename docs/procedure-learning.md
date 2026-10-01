# Procedure Learning

Procedure Learning turns an observed, successful Executive task into a
parameterized step template. It is deliberately separate from episodic memory:
a conversation saying that a workflow succeeded cannot create a procedure.

## Admission rules

A source task must:

- belong to the same identity;
- be durably `completed`;
- contain at least two completed steps;
- use only actions declared replay-safe by the Executive; and
- contain successful runtime evidence on every learned step.

Concrete training values are replaced by typed parameter markers. Bindings
that do not occur in the task are rejected, and sensitive fields such as
tokens, passwords, secrets, and credentials cannot remain as literals in the
stored template.

## Held-out promotion

Held-out suites are registered through the trusted host API, not a model-facing
skill. Suite identifiers are immutable and their contents are SHA-256 bound.
The learner freezes and digests a candidate before loading the suite. The
stored evaluation contains only example IDs, pass/fail outcomes, and expected
and observed digests—not the held-out answers.

Every candidate version remains in history. A candidate becomes champion only
when it reaches the required score and strictly beats the current champion.
Ties and regressions are retained as rejected evidence and never replace the
reusable baseline.

The marketplace capability exposes:

- `procedure_learning.list` for current promotion state; and
- `procedure_learning.learn` for learning from a completed task and an
  already-registered suite.

Learning requires the explicit `procedure:learn` permission. Compilation
validates all bindings against the champion's parameter schema before emitting
an Executive-compatible step graph.
