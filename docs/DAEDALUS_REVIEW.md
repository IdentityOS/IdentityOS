# Daedalus pull request review

Daedalus combines deterministic repository analysis with advisory model review.
Its merge-readiness status follows runtime-observed evidence, not an assertion
made by a language model.

## Evidence and decision rules

The privileged review job runs the reviewer from the trusted base revision. It
fetches the proposed revision only to produce an inert diff and never executes
pull request code with a write-capable token.

Before reviewing code, Daedalus compares its parsed file, addition, and deletion
counts with GitHub's pull request metadata. A mismatch produces an
`INCONCLUSIVE` result with the exact discrepancy. It does not produce a code
quality verdict from incomplete input.

Independent checks run outside the privileged review job. Daedalus requires the
`benchmark` check to register and waits for every registered independent check
to finish before recording their conclusions. Merely observing another quick
check is not sufficient evidence that the queued provider benchmark is absent.
The wait budget is 60 minutes so a benchmark queued behind a main-branch run can
complete without producing a false timeout. The readiness states are:

* `READY`: no deterministic blocker or observed failed check was found.
* `NEEDS_WORK`: a deterministic warning requires a change.
* `NOT_READY`: a deterministic blocker or observed failed check requires a
  change.
* `INCONCLUSIVE`: review inputs could not be verified or checks did not finish.

Model-written interpretations of source code are shown as unverified advisory
observations. Naming a changed file proves only that the file is in scope; it
does not prove the model's interpretation is correct. Model output can support a
readiness downgrade only by naming an independent check that the recorded GitHub
evidence shows actually failed.

Advisory suggestions are normalized into a readable title, unverified model
rationale, suggested change, and suggested verification. Structured model
objects are never dumped directly into the review, and their self-described
severity or evidence source does not change readiness.

## Actionable failure reports

Every readiness blocker identifies the observed evidence, why it matters, the
required recovery action, and how to verify the recovery. Pending checks explain
that no readiness conclusion is available yet and tell the contributor to wait
for completion. Diff-integrity failures report expected and observed counts so
the automation defect can be diagnosed without blaming pull request code.

## Provider serialization

Provider-backed IdentityBench and Daedalus jobs share the
`identityos-provider` concurrency group with cancellation disabled. This avoids
competing requests and prevents a newer run from erasing evidence from an older
one. The review starts only after independent checks have reached terminal
states.
