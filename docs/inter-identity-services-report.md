# IDENTITYOS INTER-IDENTITY SERVICES

Checkpoint: 2026-09-23. Continued from `4799dfc`; that commit is preserved.
This is an implementation and usage checkpoint, not a claim that the entire broad
milestone is finished. See `architecture/inter-identity-services.md` for boundaries.

## DISCOVERY FROM WEB.SEARCH

Capability present: yes, persisted `web` installation.
Required authority: `network` for `web.search`.
Actual invocation: prior real registry invocation returned `permission_denied`.
Classification: AUTHORITY_GAP.
Acquisition attempted: no after `4799dfc`.
Engineer delegation attempted: no; negative tests verify this.
Principal escalation: existing permission-required notification path retained.
Security conclusion: implementation possession does not establish authority.
Aster's identity specification, capability installation list and grants compare
identically to pre-deployment hashes.

## GENERAL ARCHITECTURE

Identity messaging: durable SQL queue with runtime-bound sender identity, receipt,
reply correlation and truthful delivery states.
Service registry/discovery: persistent advertisements and deterministic selection
from multiple providers using observable completion evidence and fixed price.
Delegation/jobs: structured immutable agreements, quotes, deduplication and states.
Engineer identity: actual IdentitySpec and persisted snapshot, not a helper persona.
Engineering workspace: isolated per-job files; bounded local artifact grammar.
Artifacts: canonical JSON fingerprints, source retention and invocation validation.
Acceptance: provider and requester invoke independently through CapabilityRegistry.
Relationships: existing Operations records projected only after actual interaction.
Reputation: derived completed/failed jobs, acceptance events, rework and repeat customers.
Credits: internal, non-redeemable IDC; zero initial balances.
Ledger: double-entry bootstrap/settlement, reservations, atomic completion, replay protection.
Pricing: first completed job free, then inspectable fixed quotes (initially 10 IDC).
First-favor policy: failures, cancellations and authority denials do not consume it.
Provenance: append-only SQL events; input/output hashes rather than raw test data.

## ENGINEER

Identity ID: `engineer`.
Persistent: IdentitySpec and snapshot in the existing identity store.
Services: capability.develop, capability.repair, capability.test, integration.debug,
runtime.diagnose; execution support is limited to local-transform-v1 artifacts.
Availability: local supervised worker, cheap queue polling, no idle model calls.
Authority: no new sensitive grants, no credential accessor or arbitrary execution.
Current work: none.
Relationships: none fabricated.
Reputation: zero completed live jobs; no invented score.
Credit balance: 0 IDC.

## INTEGRATION PROOF

Requester: isolated `test_requester` identity, never presented as live Aster.
Missing capability: `lines.canonicalize`, absent before the isolated job.
Engineer discovered: through advertised capability.develop service.
Job: real isolated persisted agreement, executed by the Executive service step.
Price: 0 IDC for first completion.
Build/acquisition: construct bounded line-sorting/deduplication artifact, later reuse.
Engineer tests: real invocation under Engineer's registry identity.
Delivery: exact fingerprint retained in agreement, artifact store and message.
Requester acceptance: real registry invocation as test_requester, independently checked.
Completion: only after requester success.
Relationship/reputation: existing relationship projection and observable completion events.
Persistence: separate writer process exits; new interpreter reloads and invokes artifact.

## ECONOMY PROOF

First successful job: completed at 0 IDC; favor then consumed.
Second job: fixed quote 10 IDC, accepted within explicit isolated principal limit.
Settlement: exactly once on successful acceptance.
Requester balance change: 30 to 20 IDC.
Engineer balance change: 0 to 10 IDC.
Duplicate settlement test: rejected; concurrent acceptance has one winner.
Failure/cancellation/insufficient balance and negative allocation tests passed.
No production credit allocation or transfer was performed.

## AUTHORITY-LAUNDERING TEST

Scenario: installed web.search denied; request framed as development, diagnosis or testing.
Expected: deny before service request, discovery side effects or payment.
Actual: AUTHORITY_GAP/PermissionError and no acquisition/delegation callbacks.
Engineer job created: no.
Economic effect: none.
Result: regression passed. Unsupported network, secret-read, permission-change and
arbitrary-code operations also fail closed in the artifact grammar.

## LIVE PRODUCTION DEMAND

Inspected Aster's actual installed capabilities, Operations needs and recorded gaps.
Objective: IdentityOS resource and collaboration outreach.
Installed: email, operations, web, mcp, a2a, culture_commons, notification.
Recorded capability gaps: web.search and email.send, both installed_permission_missing.
Open needs include funding, compute, collaborators and adoption; these are not by
themselves evidence of an absent technical capability implementation.
No qualifying production capability gap was established in this inspection.

LIVE ENGINEER JOB: WAITING FOR GENUINE DEMAND

Aster's first Engineer favor remains available. No live service job or relationship
was manufactured to create demand.

## ASTER CONTROL

Work/Jobs: Work tab and read-only endpoint; empty state is “No delegated work.”
Activity coalescing: repeated read-model events show count/first/last; raw events retained.
web.search representation: installed, permission required, AUTHORITY_GAP.
Engineer visibility: no People entry before a real service relationship.
Skills provenance: provider, job, accepted-by identity and fingerprint for completed jobs.
Existing messaging regression: passed; polling tests include Work and make no model calls.

## RESTART PROOF

Engineer/registry: persisted and inspected after fresh process and worker restart.
Jobs/relationships/reputation/ledger/first-favor/artifacts: isolated lifecycle tests
reload persistent state, prove paid settlement once, and invoke the installed artifact.
Provider continuity: identity records and agreements survive changing model metadata;
an actual two-model engineering inference run was not performed (this runner is model-free).
Live Engineer, Aster operator and dashboard services were restarted successfully.
HTTP checks after restart confirmed empty Work, AUTHORITY_GAP for web.search,
coalesced Activity, and no fabricated Engineer relationship in People. All three
services were active, and Aster identity/grant/installation hashes were unchanged.

## SECURITY

Identity spoofing: forged/mutated in-process sessions and wrong job participants denied.
Authority bypass/delegation laundering: denied before job creation.
Artifact substitution: requester acceptance fails; job cannot complete.
Ledger manipulation: append-only triggers, no identity-facing mint, balance reservation checks.
Free-favor replay: one outstanding reservation and completion-based consumption.
Secret leakage: dashboard allowlists and output hashes; labelled credentials rejected.
Prompt injection: artifact text is never a prompt or executable Python.
Filesystem escape: generated workspace identifiers and symlink checks tested.
Network escape: no network opcode in the supported grammar.
Sybil limitation: trusted host controls identity creation; no cross-host Sybil resistance claimed.
Host compromise: direct owner-level filesystem/SQL mutation is outside this local trust model.

## TESTS

Broad selected regression run: 188 passed, 11 opt-in network tests skipped.
Subsequent final targeted run: 46 passed (28 services cases plus 18 Control cases).
New services tests cover lifecycle, free/paid jobs, process restart, interrupted
recovery, rework, spoofing, malformed contracts, authority laundering, duplicate
requests, concurrency, quote immutability, artifact substitution, unsafe grammar,
workspace escape, economic authority, ledger integrity and sanitized views.
Failures: implementation failures found during development were fixed; final selected
runs have no failures. Full repository suite was not run.

## GIT

Previous commit: `4799dfc`.
Branch: `feat/aster-culture-commons-interop`.
New commit/push: reported after execution; only `fork` is authorized.
No runtime state, private grants, credentials or local service unit is committed.

## NORTH STAR RESULT

PARTIALLY PROVEN

The bounded local service lifecycle, requester acceptance, internal economy and
restart behavior work. Important remaining scope includes broader governed builds,
external provider delivery, specification negotiation for autonomous gaps, and remote
authenticated transport. Usage limits require a checkpoint; this report does not
mislabel the partial implementation as complete architecture or a live specialist win.

## MOST IMPORTANT OBSERVATION

Ability and authority require different state transitions. A specialist service
cannot turn an installed-but-denied action into an authorized one. That distinction
is now preserved both in gap handling and service admission.

## NEXT EMERGENT PRESSURE

A recorded missing skill often lacks a machine-checkable desired outcome. The
runtime can discover a specialist and deduplicate the request, but cannot honestly
invent acceptance criteria. Specification negotiation is the next concrete missing
mechanism exposed by this implementation; no production demand is invented.
