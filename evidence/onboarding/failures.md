# Observed failures and recovery

## Baseline failures

- The initial adapter/SDK/secrets/chat/portability selection reported 90 passed,
  1 skipped, and 1 OpenAI transport subprocess timeout.
- A separate public API selection reported 20 passed and 1 failure because the
  exported constitution was empty.
- Direct probing showed imported memory and goals available only in the current
  process, then absent from a fresh destination runtime.

These observations predate this implementation and are recorded in
`docs/adoption/BASELINE_AUDIT.md`.

## Local inference timeout

The first genuine Ollama attempt used the runtime's broad default context and a
45-second provider timeout. It failed with `APITimeoutError`; the runtime
reported generation unavailable and the acceptance driver rejected it. No
success evidence was written for that attempt.

Investigation showed that a small CPU-only local model was spending most of the
time evaluating onboarding context and could also encounter unrelated
capability-evolution work. The acceptance verification path was changed to one
bounded identity context and one genuine provider request, while leaving normal
runtime behavior intact. The later clean-artifact run succeeded with observed
provider provenance. Its latency remains reported as a bottleneck.

## Controlled failures in every clean run

Each committed clean run required `idos import /tmp/missing.identityos.json` to
exit nonzero. A subsequent `idos doctor --json` recovery command succeeded.
The expected failures appear as successful expected-failure checks in
`commands.jsonl`; they are not omitted from the command history.

## Final full-suite timing failure

The final all-tests run reported `1789 passed, 43 skipped, 1 failed` in 638.89
seconds. The sole failure was the baseline OpenAI stalled-socket subprocess
exceeding its outer 30-second process limit under load. The exact test passed
when immediately rerun alone (`1 passed in 24.21s`). No adoption code handles
that transport path, and the same timing failure was present in the baseline;
it remains reported rather than converted into a green full-suite claim.

## Installer evidence-wrapper failures

Two additional clean installer containers completed package installation but
were not accepted as successful evidence runs. The first wrapper ended with a
shell quoting error while formatting its final JSON. The second proved delayed
dashboard status was `running: true`, then ended on a typo in the wrapper's JSON
assertion. A third new container completed installation, initialization,
doctor, delayed status, a real HTTP request to onboarding, and shutdown. Its
accepted record is `final-public-facing-installer.json`. These two failures are
included in the aggregate attempt count.

## Remaining untested failures

Genuine cloud invalid-key, provider rate-limit, provider timeout, and no-network
responses were not exercised because no cloud credential was available.
Deterministic tests cover credential omission and false-success prevention, but
they are not labeled real provider tests. Machine power loss, macOS, and WSL2
remain untested.
