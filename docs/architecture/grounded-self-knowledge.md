# Grounded self-knowledge

Runtime truth outranks model assertions. `core.self_knowledge.SelfKnowledge` is a read-only, identity-bound projection, not another mutable memory store. Every current or future persistent identity can use it. Operations generation and IdentityRuntime share the same implementation.

## Authoritative sources

| Section | Source and scope |
| --- | --- |
| Identity | Persisted identity specification, allowlisted metadata only |
| Presence/objective | Operations presence with liveness evaluation |
| Capabilities | Installed registry entries and pure capability-owned descriptors |
| Permissions | Persisted capability grants; nonpublic scopes default to denied |
| Services | Actual service advertisements, not inferred implementations or trust |
| Jobs | Requester/provider jobs and acceptance events |
| Relationships | Operations relationships and identity graph; contacts withheld |
| Economy/reputation | Ledger entries, accounts, jobs, acceptance events |
| Artifacts | Governed service artifacts joined to this identity's jobs |
| Recent effects/provenance | Service events and bounded Operations message evidence |
| Needs | Persisted Operations needs |

An installed provider is not proof of execution. Ability and authority are separate. Remote readiness remains unknown without evidence; known configuration/dependency failures and permission restrictions are explicit. Descriptors do not install capabilities, instantiate remote clients, invoke skills, or probe networks. Custom providers can implement `inspect_installation`; unknown providers remain unknown rather than claiming absence. Action-time capability policy remains authoritative, including after a snapshot has been read. Delegation transfers no authority.

## Access and grounding

- Python: `SelfKnowledge(storage, identity_id).snapshot(sections=None)`.
- CLI: `identity self ID --store PATH --section capabilities` (omit section for all).
- Existing private Control server: `/api/self` and `/api/self/<section>` bound to its identity.
- Operational IdentityRuntime turns receive `identity__self__inspect`, with optional sections and no identity override.

Operational/status/planning text triggers automatic structured context in both generation paths; routine greetings do not. Polling and snapshot reads require zero model calls. HTTP exposure follows the existing private Control server; this change creates no listener or public route configuration.

Sections contain status, completeness, source, evidence reference, and sanitized data. Snapshots carry observation time, content revision, identity, and a 30-second TTL. Sections are observations over a read window, not a global transactional freeze; the economy projection uses a single read transaction. Grounded responses are checked against a fresh projection. Changes during generation or expired observations cause a refreshed factual fallback. Execution still revalidates independently.

## Evidence-bound expression

`core/expression.py` supplies runtime-issued fact IDs and natural-language renderings. The model selects IDs in an internal envelope; operative prose is rendered from authoritative values, never from the model's message. Legacy claims are validated but their free prose is discarded. Inferences/proposals remain explicitly unverified commentary, never executable requests. Arbitrary conversation remains outside this supported operative path; routing is heuristic.

Adapters declare prompt-only, JSON object or strict schema support. Groq GPT-OSS contract requests use strict schemas without tools, according to [provider documentation](https://console.groq.com/docs/structured-outputs). Other providers conservatively remain prompt-only unless configured otherwise. Operational OpenAI-compatible requests use bounded budgets; rotating providers do not replay contract attempts. Chain fallback stops after any tool attempt. The runtime no longer retries an adapter after an internal TypeError. All-provider failure produces an attributed deterministic runtime response; ordinary greetings remain conversational.

Generation contracts check schema, evidence IDs, snapshot revision and age. Invalid output uses a concise first-person factual renderer. No second critic model is involved. Specifications are also projected as a self-state section. None of these reads expands execution authority.

## Bounds and privacy

JSON namespaces above 512 KB become UNVERIFIED instead of being parsed. Registry descriptors are capped at 100 providers/skills; relational result lists at 30, message inspection at the latest 100. Aggregates use SQL. Truncation is explicit and cannot prove absence. Generation projections cap serialized snapshot size at 30,000 characters, dropping sections if needed; omitted facts remain unknown. Reads may still require database aggregation work; this is a payload/read-size bound, not a universal constant-time guarantee.

Projection uses field allowlists, configured-secret redaction, sensitive configuration value collection, URL/secret-handle removal, and bounded strings. Hidden prompts, raw provider config, transport contacts, credentials, and chain-of-thought are not selected. Sanitizer failure closes the projection. Redaction values are cached only within one snapshot operation, not operational truths across turns.

Artifact introspection currently covers governed service artifacts, not every arbitrary file or draft. Submitted-email evidence is not proof of recipient delivery. An advertisement is not a completed job, reputation endorsement, or proof that every advertised service is generally implemented. Missing/unavailable sources are explicitly unverified.

## UI and extension

Skills uses this projection rather than capability load/install hooks, separating permission and readiness. Operational messages have expandable Runtime evidence with timestamps and guard status. Existing Work and People continue using their actual stores.

Future identities inherit this primitive through IdentityRuntime. New capabilities should supply pure inspection descriptors. New evidence domains should extend the projection with explicit source, scope, completeness, and sanitization rather than copying model statements into facts.
