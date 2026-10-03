> Historical recovery checkpoint. The final state and remaining limits are documented in [the closure report](foundational-loop-closure-report.md).

# Foundational loop continuation checkpoint — 2026-09-24

This is an incomplete verification checkpoint, not final closure. Resume from
this exact working tree. Do not discard or restart implementation.

## Repository

Actual checkout: `/home/lace/Documents/Doug/IdentityOS`.
Branch: `feat/aster-culture-commons-interop`; base HEAD `0470a0b`.
All interrupted closure changes and progress changes remain uncommitted.
Nothing discarded. Nothing pushed. Only permitted push target is
`fork/feat/aster-culture-commons-interop`; never origin, never merge.
Initial `/home/lace/Documents/IdentityOS` is a different checkout.

## Preserved closure work

Runtime expression rendering/strict output contracts, timeout initialization,
no replay after tool attempts, negotiated specifications before jobs, narrow
Engineer `capability.develop` catalog, local free/paid acceptance/settlement tests,
classified gaps and existing test changes are intact. Old checkpoint/report is
`docs/foundational-loop-closure-report.md`; its test status is superseded here.

## New progress behavior

`core/operations/progress.py` persists blockers in existing Need metadata and
structured cycle outcomes in append-only provenance. Authority/configuration
failures are suppressed until prerequisite digests change; provider waits are
bounded. Principal deferrals without an executor wait for real prerequisites.
Project fingerprint excludes observation time; change events retain fact diffs.
Commons reads check actual local counters before calling, reserve 20%, wait for
UTC reset, and adapt observation intervals. Waiting needs cannot become outreach
needs. Activity adds default cycle summary, last progress, waiting, eligibility,
principal requirements and scoped resource counts; detailed provenance remains.
Historical readiness evidence is persisted without probing; evidence-write
failure after an effect logs but cannot turn success into a replayable failure.
See `docs/architecture/progress-aware-operations.md` for boundaries/limitations.

## Verification

- First progress/closure/Control/engine/IdentityBench/Commons group: 165 passed.
- Follow-up progress/closure/engine/Control: 98 passed.
- Latest progress/Operations group: 53 passed.
- Provider/config/progress group: 64 passed.
- Full suite: **1346 passed, 43 skipped, 2 failed**, 240.98 seconds.
  `/tmp/identityos-progress-broad.log`.
- Both failures were presence waiting status overwritten by the new cycle
  summary (daily outreach budget, escalation). Fixed preserving established
  policy/degraded status. Latest presence/progress/closure group:
  **68 passed**, `/tmp/identityos-progress-presencefix.log`.
- The old full-suite IdentityBench MagicMock serialization failures were fixed
  by constraining structured-output description to strings.
- `git diff --check` passed. New progress tests: `tests/test_progress_autonomy.py`.
- A final full suite after those fixes still needs its result checked.

## Live evidence and remaining work

Same Aster, same authority and standing. No production test jobs, relationships,
opportunities or grants created. All three services active after restart.
At 14:19:38 UTC first actual daemon cycle recorded five waits: web authority,
MCP configuration, A2A configuration, Commons exhausted budget, principal no
executor. Zero Commons observation calls and zero principal model requests.
It recorded genuinely changed project facts (new engineering files/tests), not
completed outreach. This does not prove productive outreach autonomy.

Daemon interval remains 300 seconds. Bounded read-only capture script
`/tmp/identityos-progress-live-capture.py` is collecting three cycles into ignored
mode-0600 `.identity_store/.identityos/progress-live.json` and summary log
`/tmp/identityos-progress-live.log`. Inspect the result; do not claim three cycles
until observed. It exits after three cycles or eleven minutes.

Control HTTP Activity projection was read successfully. Browser visual check was
blocked by the browser client (`ERR_BLOCKED_BY_CLIENT`); no bypass attempted.
First-screen visual acceptance remains unverified.

Live expression replay found another edge case: generation deadline error did
not trigger fallback. Added exhaustion marker and parametrized regression.
Second replay showed Ollama deadline -> Groq model output, but expression guard
still chose truthful runtime fallback. Evidence is in ignored mode-0600
`.identity_store/.identityos/closure-expression-replay.json`, log
`/tmp/identityos-expression-final2.log`. Inspect guard reasons; successful model
contract compliance is NOT established. No new principal message was submitted.

## Finish next

1. Inspect HEAD/status, checkpoint, latest tests and live capture. Preserve all.
2. Check final full-suite result and resolve genuine regressions.
3. Inspect three live cycles and confirm unchanged-state suppression. Later
   process restart must reload blockers; fixtures already cover this.
4. Inspect latest expression guard rejection reasons and configured fallback
   attempts. Avoid repeating live calls without a new reason.
5. Finish readiness/UI review, scoped resource-counter honesty, actual local
   objective eligibility, failure/restart continuity, and final required report.
6. Commit coherent tested work, push only fork branch, verify cleanliness.
7. Report PARTIALLY PROVEN until safety AND productive autonomy are supported.

Resource checkpoint: five-hour 92% consumed; weekly 78%. Paused to preserve
usage, per principal instruction. Leave Aster, Engineer and Control running.

## Confirmed on continuation

Final broad suite completed: **1350 passed, 43 skipped**, 90.76 seconds
(`/tmp/identityos-progress-finalbroad.log`). Three real daemon cycles were
observed at 14:19:38, 14:24:39, and 14:29:39 UTC. First cycle recorded changed
project information; subsequent two recorded PRINCIPAL_REQUIRED, each suppressing
all five known operations, with zero Commons observation calls and zero principal
model requests. No productive outreach is claimed. All three services remain
active. HEAD and uncommitted work remain intact. Usage on continuation was 98%
five-hour, 79% weekly; substantive work remains paused to avoid exhaustion.
