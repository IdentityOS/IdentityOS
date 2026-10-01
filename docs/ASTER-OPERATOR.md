# Aster — Persistent Autonomous Operator

Aster is a persistent, evidence-backed autonomous operator identity built on
the generalized operator subsystem in `core/operations/`. It observes a
project, detects needs, discovers and evaluates opportunities, sends
individualized permitted outreach, monitors replies, escalates consequential
decisions, and follows up — restart-survivably, within hard control and
permission constraints.

This page explains how Aster is implemented, how it runs, what it may and may
not do autonomously, and how to inspect it.

## Architecture

```
identity aster <command>
        │
        ├─ core/operations/engine.py   OperationsEngine.tick() — the operator loop
        │      observe → gaps → needs → discover → evaluate → act → monitor → followups
        │
        ├─ core/operations/store.py    OperationsStore — durable state (namespaces operations.*)
        ├─ core/operations/policy.py   AuthorityPolicy — what may run without a human
        ├─ core/operations/…           observer, needs, discovery, evaluation, composition,
        │                              monitor, followups, capability_gap, config
        ├─ core/capabilities/email/    EmailCapability — send + read (permission-gated)
        └─ core/capabilities/operations/  OperationsCapability — status/run_tick/authorize/…
```

`core/operations/` is capability- and identity-agnostic. Aster is one
configuration (`core/operations/aster.py`): the identity spec, persona,
default need rules, budget defaults, and control constraints. Other operators
can be built on the same engine without copying code.

### The tick loop

Every `tick()` runs phases that each write to the provenance ledger with
their evidence:

1. **observe** — reads README, pyproject, `docs/`, registry index, tests, git log.
2. **capability gaps** — checks required skills against the capability registry.
3. **detect needs** — idempotent, evidence-backed need detection (`funding`,
   `collaborators`, `compute`, `research`, `adoption`).
4. **discover** — static candidate files and/or a web search source.
5. **evaluate** — factor scoring, dedupe by target/org, duplicate-contact policy.
6. **act** — permission-gated send through `CapabilityTransport`; escalates
   when the authority policy requires a human.
7. **monitor** — reads the inbox, classifies intent (question / opt-out /
   decline / sensitive), replies autonomously or escalates.
8. **followups** — bounded nudges, none after a reply, opt-out, or max count.

## Authority policy (what is autonomous)

Two modes:

- **commitment mode** (cold outreach): only explicit binding/disclosure
  phrases escalate (`COMMITMENT_PHRASES`). Asking about a funding program is
  autonomous; promising equity or signing terms escalates.
- **content mode** (inbound replies): full `SENSITIVE_CATEGORIES` scan.
  *“What are your equity terms?”* escalates for human review.

Messages await a human when `AWAITING_HUMAN_AUTHORIZATION` and are only sent
after `identity aster decide --approve`.

### Autonomy allowed
technical Q&A from verified facts, explaining IdentityOS, links, intros,
asking about programs, scheduling, docs requests, thanks, polite follow-ups.

### Always escalates
investment terms, equity, contracts, legal, money, credentials, repo access,
employment, binding commitments, ownership/IP/license, exclusivity, strategy,
private information.

Every message discloses Aster's identity:

```
— Aster / IdentityOS / Persistent Resource & Collaboration Operator /
  Acting under delegated authority for Arsène Manzi / https://github.com/lacebx/IdentityOS
```

## Running Aster

### 1. Initialise (installs identity + capabilities, grants what you choose)

```bash
identity aster init --project-root . \
    --grant-email --grant-search --grant-operations
```

Grants are explicit and separate from installation — nothing is sent or
searched until you grant the permission.

### 2. Run one supervised tick

```bash
identity aster tick --project-root . --candidates candidates.json
```

`--search` enables live web discovery (requires `web.network` grant); without
it, only the candidates file is used.

### 3. Run in the background

```bash
identity aster run --project-root . --candidates candidates.json \
    --daemon --interval 300
```

This detaches a supervisor process that ticks every 300 seconds, writing to
`<store>/aster-daemon.log`. Manage it with:

```bash
identity aster status --store .identity_store
identity aster stop  --store .identity_store
```

Stopping is not branching: every tick is atomic and persisted, so
`identity aster tick` resumes exactly where the daemon left off, in the same
or a fresh process.

## Control constraints

```bash
identity aster override paused=true
identity aster override max_cold_outreach_per_day=5 \
    require_approval_categories=investment contract
identity aster pause / resume
```

## Inspection

```bash
identity aster status               # real persisted state
identity aster needs                # detected needs + evidence
identity aster opportunities        # discovered/evaluated opportunities
identity aster relationships        # persistent per-target records
identity aster messages             # every persisted message
identity aster provenance           # the audit ledger (evidence per phase)
identity aster escalate             # what awaits a human decision
identity aster decide --message-id <id> --approve --note "..."
```

## Escalation example (Beta Fund style)

```bash
# fresh operator, funding asked about autonomously (commitment mode)
identity aster init --project-root . --grant-email --grant-search --grant-operations
identity aster tick --project-root . --candidates candidates.json
# → outreach about the funding program is sent; body asks only for
#   information about the program and whether IdentityOS qualifies.

# funding declared sensitive (content mode example)
identity aster override require_approval_categories=funding
identity aster tick --project-root . --candidates candidates.json
identity aster escalate
identity aster decide --message-id <id> --approve --note "reviewed; proceed"
```

The exact same mechanics handle inbound replies, opt-outs, follow-ups, and
budgets, so there is no capability-specific or program-specific code path.

## Persistence and restart survival

All operator state lives in the storage backend under namespaces
`operations.*`: needs, opportunities, evaluations, relationships, messages,
follow-ups, provenance, controls, and day-scoped budgets. The engine keeps no
module-level mutable state, so a stopped daemon, a crashed process, or a new
process loading the same identity resumes without resending prior outreach.

## Honest limits

- Discovery finds only targets you provide (candidates file) or that live web
  search returns for the queries Aster composes from its needs.
- Capability gaps are recorded as evidence-backed needs, not silently
  resolved; acquiring a capability requires the registry/permisssion flow
  (`identity cap install` / `identity cap grant`).
- Nothing is sent without either an explicit grant or explicit human
  authorization for the consequential categories.