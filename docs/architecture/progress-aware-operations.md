# Progress-aware Operations

Operations uses existing `Need.metadata.blocker` records for durable waiting and
existing append-only provenance (`cycle.outcome`) for deterministic cycle outcomes.
No new planner, model call, authority, or job is required to render Activity.

A blocker is keyed by its operation; its classification and stable fingerprint
identify the known condition. It records first seen, last checked, last changed,
occurrences, suppressed checks, objective, retry condition, next eligible time,
principal requirement, and resolution evidence. Resolved needs are addressed.
Waiting needs are excluded from outreach discovery: a provider outage is not a
potential outreach recipient.

Authority and configuration failures wait for persisted prerequisite changes.
Configuration eligibility also includes the executable service catalog. Deferred
instructions without an executor wait for capabilities, services, configured
model, or principal controls to change. Transient deferred provider failures have
a bounded retry delay. Condition digests persist no configuration secrets.

Culture Commons checks its existing local daily counter before calling the
provider. Routine observation reserves the final 20% of the read allowance for
other meaningful interactions. Exhaustion waits until UTC reset. Unchanged
observations double the interval from ten minutes up to six hours; meaningful
changes restore the five-minute interval. Failed provider reads use bounded
backoff. This does not change standing or grants.

Project comparison excludes observation timestamps and compares facts plus
stable project metadata. Changed observations retain added/removed facts in
provenance; unchanged checks do not append observation events. A cycle records
progress only for observed new information, completed actions, or resolved
blockers. Liveness is not progress. Last progress carries forward across waiting
cycles and process restarts.

Activity defaults to the latest cycle, objective, last progress, waiting
conditions, principal requirements and eligibility. Detailed, coalesced provenance
remains underneath. Capability readiness shows historical verified calls without
promising present readiness or email delivery. Successful-action evidence write
failure is logged without converting an already performed effect into a retry.

## Current limits

Resource counters cover principal model requests and Commons observation calls;
other model paths and individual provider retries are not comprehensively
instrumented. Existing eligibility hashes use whole persisted capability/grant
configuration, so a change to an unrelated capability can cause one unnecessary
reconsideration. Model routing remains heuristic. A cycle demonstrating truthful
waiting proves restraint, not useful outreach. No production opportunity or
specialist job should be created to make the counters appear productive.
