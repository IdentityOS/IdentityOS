# Inter-identity services: bounded local implementation

## Ownership

`core/services` owns service agreements, messaging, discovery and internal credit
accounting. It does not plan arbitrary code execution. The existing Executive
executes `service_job` steps; Prometheus owns the bounded artifact grammar,
validation, workspace and governed registry installation. Operations projects real
service interactions into its existing relationship records. No orchestrator domain
logic, substitute Aster identity or permission grant was added.

SQLite is the authority for cross-identity messages, agreements, quotes, credit
reservations, settlement and append-only service events. Existing JSON identity
storage remains authoritative for identity specifications, installed capabilities
and Operations relationships. The relationship projection is rebuildable from jobs;
it is not a second relationship authority. Rebuilding currently occurs on request,
acceptance and explicit CLI inspection. Cross-process projection writes still have
the existing JSON backend's concurrency limitations.

## Authority

A host-issued session binds each action to its identity. Message payloads cannot
supply a sender, acceptance cannot be submitted by the provider, and no model-facing
API can insert reputation events, settle credits, mint credits or bind identities.
The local CLI and storage owner are trusted principals. This is not a remote
identity-authentication protocol. Networked MCP/A2A identity transport remains work.

Known permission-denied skills stop before acquisition and delegation. Service
requests repeat the permission check and accept only structured local effects.
Renaming a forbidden operation as diagnosis does not authorize it. A request for
network access, secret reads, authorization changes or arbitrary code has no
executable representation in the supported artifact grammar. Explicitly labelled
credentials are rejected before message/agreement persistence. Dashboards omit
contracts, inputs, outputs and message contents; test results retain hashes.

This is not a promise to recognize all possible unlabelled secrets in arbitrary
text. Callers must not submit credentials. There is no credential access facility
in this service format. The SQLite file is private to its OS user.

## Agreements, execution and evidence

Agreement fields are exact-schema JSON: service, skill, outcome, constraints,
acceptance tests, budget and request key. The contract and participants are
immutable. Quotes are immutable once issued. Repeated request keys return the same
job; changed contents under the same key are rejected.

Executable jobs proceed through REQUESTED, QUOTED, ACCEPTED, IN_PROGRESS,
DELIVERED, ACCEPTANCE_TESTING and COMPLETED. Failures remain FAILED or
REWORK_REQUIRED; missing specifications remain BLOCKED. Failed/cancelled jobs do
not consume a free favor. Tests happen through the real CapabilityRegistry first
as provider, then as requester. Failed requester acceptance restores its previous
artifact configuration. No externally supplied `passed=true` field exists.

The initial artifact format is a bounded local transformation pipeline (text
normalization, line processing, counting and hashing). It is actual executable
implementation data, not Python, a shell command or an instruction prompt. It has
no I/O, imports or network operations. Packages are SHA-256 fingerprinted over
canonical JSON, validated on installation and every invocation, and retained in
per-job workspaces outside runtime source. Existing packaged artifacts are tested
for reuse before constructing a new one. Registry candidates and built-in matches
are recorded. An existing built-in match returns to normal acquisition rather than
being shadowed. External provider discovery is not authorized by this local-only
agreement format.

Only `capability.develop` is now advertised: it builds and tests bounded local-transform-v1 packages. Repair, testing-as-a-service, integration debugging and runtime diagnosis advertisements were removed because distinct governed workflows were not implemented. General code execution and external delivery remain unsupported.

Automatic unresolved capability gaps now create deduplicated specification exchanges, not blocked jobs. `core/services/negotiation.py` persists participant-bound, revision-checked proposals, counterproposals, clarification requests, declines and agreements. Unknown acceptance criteria are never invented. Workers accept executable specifications or request clarification; requester workers create idempotent jobs only after agreement. Price remains separately quoted and requester-approved under the existing budget and spending policy. The complete-contract compatibility API invokes the same deterministic provider negotiation workflow and persists agreement before job creation; vague direct requests are rejected. Clarification creates a deduplicated principal notification. CLI propose/respond/start-job and Work specification cards expose this exchange.

## Credits and concurrency

IDC has no monetary value and cannot be redeemed. Accounts start at zero.
`ServiceStore.bootstrap` is a trusted-host operation with an explicit allocation ID;
identities have no mint API. It records opposite entries against the system account.
Balances derive from append-only entries. System bootstrap is the only permitted
negative issuer account; requester balances cannot go negative through service APIs.

The first completed job per requester/provider is free. One accepted free job per
pair reserves that favor, preventing concurrent free completion. Subsequent quotes
use the catalog's fixed price, initially 10 IDC. Acceptance checks both agreement
budget and persisted principal spending limit, which defaults to zero. Outstanding
accepted jobs reserve funds. Completion and paid settlement commit in the same SQL
transaction; job-linked settlement has a UNIQUE key. Concurrent/replayed acceptance
cannot debit twice. Host SQL/file compromise is outside the model/session boundary;
SQLite triggers are integrity defenses, not protection against the OS owner.

## Recovery and operation

The worker performs one bounded job phase per tick, with no model calls. `serve`
uses a cheap five-second queue poll and a process lock; it does not run a reasoning
heartbeat. Local transformation steps are replay-safe: installs converge on the
same fingerprint and transforms have no external effects. After a stopped worker,
recovery requeues interrupted local execution/acceptance. Completion and payment
are atomic and never replayed. A future external-effect adapter must use blocked
reconciliation instead.

The Operations background tick can accept quotes within existing authority and
run requester acceptance. Ordinary conversation and dashboard polling do not run
service work. Aster Control's Work page exposes actual jobs only. Skills shows
accepted artifact provenance and explicit permission-required status. Activity
coalesces a bounded recent read window while preserving raw provenance.

## CLI

Use `identity services --store <identity-store> --identity <id> <command>`.
Commands include `init-engineer`, `directory`, `status`, `jobs`, `show <job>`,
`messages`, `relationships`, `reputation`, `balance`, `ledger`, `artifacts`,
`provenance`, `request <provider> <contract.json>`, `quote`, `accept-quote`,
`accept-delivery`, `cancel`, `rework`, `tick`, `requester-tick`, `serve`, and `recover`.
Stop a worker before invoking manual recovery. The host CLI can bind identities;
it must not be exposed as an untrusted tool or remote endpoint.

Live service unit installation is local operator configuration and is not committed.
Use the configured Python interpreter, repository working directory and absolute
identity-store path when supervising `identity services --identity engineer serve`.

## Boundaries

Cross-host identity authentication, external effect delivery and general-purpose engineering are not implemented. Local session bindings and the bounded artifact interpreter are the enforced execution boundary. They must not be represented as a remote authenticated marketplace or unrestricted engineering service.
