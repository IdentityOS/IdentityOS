# IdentityOS — Agent Engineering Contract

This repository is an experimental open-source identity runtime.

Agents working in this repository have broad implementation autonomy. Do not wait for human approval for routine engineering decisions.

## You MAY

- Inspect the repository and architecture
- Modify source code
- Create and modify tests
- Create capabilities and skill packs
- Improve adapters
- Refactor modules when necessary
- Fix bugs
- Improve performance
- Improve observability
- Run local tests and experiments
- Create identities for testing
- Create benchmark scenarios
- Create documentation
- Commit changes
- Create branches
- Open pull requests
- Review pull requests
- Improve CI
- Add instrumentation
- Remove dead code
- Recover from failures
- Retry reasonable approaches
- Make architectural decisions within the boundaries below

## You MUST

- Verify behavior with executable evidence
- Run relevant tests after changes
- Preserve working behavior unless intentionally changing it
- Explain failures honestly
- Distinguish model claims from runtime facts
- Prefer general mechanisms over task-specific hacks
- Keep changes focused
- Preserve backward compatibility where practical
- Document meaningful architectural changes
- Inspect existing architecture before introducing duplicate systems

---

# Primary Project Priorities

Work should be prioritized in this order:

1. Smooth, responsive interaction
2. Truthful and evidence-backed behavior
3. Reliable long-running task execution
4. Reliable capability creation, acquisition, installation, execution, and persistence
5. Identity continuity and memory correctness
6. Security and safe execution boundaries
7. Maintainability and architectural clarity
8. Benchmarks and observability
9. New features

Do not sacrifice priorities 1–7 merely to make a demo look more impressive.

---

# Core Principle

## The model proposes. The runtime establishes reality.

An LLM response is **not** evidence that something happened.

These statements are **not** proof:

- “I created the capability.”
- “I installed it.”
- “I ran the command.”
- “The command returned X.”
- “I checked GitHub.”
- “I saved the file.”
- “I remember this.”
- “The task is complete.”

Only runtime-observed events, capability results, execution results, persisted state, or other independently verifiable evidence may establish that something happened.

Never convert an LLM claim into a runtime fact.

---

# Anti-Fabrication Rule

Never make the system appear successful when execution failed, did not happen, or could not be verified.

If an action fails:

1. Record the failure
2. Expose the failure to the reasoning layer
3. Attempt recovery when appropriate
4. Retry only when justified
5. Verify the retry
6. Report the actual outcome

Do not manufacture plausible output.

Do not replace missing evidence with assumptions.

Do not silently convert uncertainty into confidence.

When uncertain, say so.

---

# Capability Principles

Capabilities are one of the central abstractions of IdentityOS.

A capability is not considered functional merely because:

- Its Python file exists
- It imports successfully
- Its manifest exists
- It appears in the registry
- Installation succeeds
- Its function returns `{"status": "completed"}`

A capability is functional only when its intended behavior has been demonstrated through real execution.

For generated capabilities:

```text
generate
→ validate
→ publish
→ install
→ activate
→ invoke
→ observe
→ verify
→ persist
→ reuse
```

All stages must be distinguishable.

A syntactically valid capability is not necessarily a behaviorally valid capability.

## Capability Development Requirements

When creating a capability:

1. Define the actual intended behavior.
2. Define its inputs and outputs.
3. Implement real behavior.
4. Validate syntax and interface.
5. Execute it with representative inputs.
6. Verify the returned result.
7. Record evidence.
8. Install it into an identity.
9. Restart the runtime when persistence matters.
10. Invoke it again after restart.
11. Add regression tests.

Do not create capability-specific planner branches such as:

```python
if "speech" in request:
    ...
```

Or:

```python
if "neofetch" in request:
    ...
```

Prefer generic mechanisms that work for arbitrary capabilities.

---

# Tool Execution

Text that looks like a tool call is not a tool call.

For example:

```xml
```

This is only text unless the runtime actually parsed, validated, executed, and recorded the invocation.

Never treat model-generated pseudo-tool syntax as execution evidence.

Native or structured tool calls should ultimately follow:

```text
model request
→ runtime validation
→ capability resolution
→ actual execution
→ actual result
→ evidence
→ model receives result
```

---

# Long-Running Tasks

Long-running work is a first-class requirement of IdentityOS.

A task must not depend on a single model response remaining active.

Tasks should have durable state. The system should be able to:

- Start a task
- Record progress
- Pause
- Recover
- Retry
- Continue after interruption
- Persist state
- Resume after process restart
- Determine completion independently of the LLM’s claims

Never use an unbounded synchronous loop merely because it is convenient.

Never make ordinary conversational messages wait for unrelated long-running work.

Separate:

```text
interactive response path
```

from:

```text
durable background execution
```

when the task does not need to block the response.

---

# Chat Performance

Normal conversation should be responsive.

Do not introduce architecture that causes every message to synchronously execute:

- Unrelated Executive tasks
- Unnecessary capability calls
- Unnecessary Prometheus analysis
- Unnecessary network requests
- Expensive persistence
- Repeated model calls

Before adding work to the interaction path, ask:

> Does this operation need to finish before the user can receive a correct response?

If not, prefer deferred or background execution using existing infrastructure.

Measure latency rather than guessing.

Important latency stages include:

- Policy
- Executive
- Prometheus
- Capability routing
- Context composition
- Model request
- Tool execution
- Post-processing
- Persistence

Performance regressions should be measurable and tested.

---

# Memory and Identity Integrity

Identity state and user state must remain distinct.

Do not mix:

- Facts about the identity
- Facts about the user
- Episodic conversation history
- Capability execution evidence
- Runtime state
- Speculative inference

Never use a timestamp from one domain as evidence for another.

For example:

> A fact’s `last_confirmed` timestamp is not automatically the time of the last conversation.

A remembered statement is not automatically a current fact.

A model inference is not automatically user-provided information.

---

# Persistence

If the system claims something persists, prove it.

For important state:

1. Create or change state.
2. Persist it.
3. Terminate the process.
4. Start a new process.
5. Reload the identity.
6. Verify the state.
7. Use the state again.

In-memory success is not persistence.

---

# Failure Handling

Failures are expected. Do not hide them.

Prefer:

```text
FAILED
→ reason
→ evidence
→ recovery attempt
→ recovery result
```

Over:

```text
FAILED
→ pretend success
```

Avoid broad `except Exception: pass` blocks when doing so could hide a meaningful failure.

When existing broad exception handling is necessary for non-critical components, preserve observability through logging or structured diagnostics.

---

# Architectural Boundaries

Respect the current architecture.

The major layers are:

```text
CLI / SDK / HTTP
        ↓
IdentityRuntime
        ↓
Identity + Memory + Facts + Context
        ↓
Capabilities / Prometheus / Executive
        ↓
Adapters
        ↓
Models / Providers
        ↓
Persistence / Registry
```

Do not create a second planning system when an existing generic planner can be extended.

Do not bypass the Executive when the task belongs to the Executive.

Do not bypass Prometheus when capability evolution belongs to Prometheus.

Do not put domain logic into `runtime/orchestrator.py` unless it is genuinely orchestration.

If a subsystem is duplicated, determine which implementation is authoritative before extending either one.

---

# Orchestrator

`runtime/orchestrator.py` is the runtime hub. Treat it carefully.

Before adding code:

1. Identify which existing subsystem should own the behavior.
2. Add a service, helper, or stage if the logic has its own responsibility.
3. Avoid growing `process()` with unrelated business logic.

The orchestrator should coordinate systems rather than secretly becoming every system.

---

# Testing

Every meaningful change requires tests.

At minimum:

1. Reproduce the bug.
2. Implement the fix.
3. Prove the bug is fixed.
4. Prove unrelated behavior still works.

For capabilities, test:

- Creation
- Validation
- Installation
- Invocation
- Real output
- Failure
- Recovery
- Persistence
- Reuse

For identity behavior, test:

- New identity
- Existing identity
- Fresh process
- Multiple sessions
- Contradictory information
- Failure conditions

Prefer deterministic tests where possible.

Do not weaken tests merely to obtain a green build.

---

# Benchmarks

IdentityBench is an engineering observability system, not merely a score generator.

Every score should have:

- Evidence
- Explanation
- Cause, where possible
- Actionable follow-up

Do not optimize behavior specifically for benchmark prompts.

Avoid benchmark-only branches.

A benchmark improvement that makes real-world behavior worse is a regression.

---

# Security

Treat generated code and installed capabilities as untrusted until validated.

Never weaken execution boundaries merely to make an autonomous demo succeed.

Be especially cautious with:

- Subprocess execution
- Filesystem writes
- Network access
- Credentials
- Tokens
- Generated Python
- Dynamic imports
- GitHub write operations
- Autonomous pull-request or issue creation

Do not commit secrets.

Do not print secrets.

Do not persist credentials in identity state.

---

# Autonomous GitHub Work

Agents may:

- Create branches
- Commit
- Push branches
- Open pull requests
- Comment on pull requests
- Create issues
- Review changes
- Improve CI

Agents may **not**:

- Merge directly into protected main branches without the repository’s normal review policy
- Delete important production or history data without justification
- Rotate credentials unless explicitly required by the task
- Expose tokens or secrets
- Silently rewrite unrelated history

When an automated GitHub action makes a change, identify the reason and evidence in its pull request or issue.

---

# Autonomous Communication Boundary

Engineering agents must never manually send, replay, authorize, or induce an
email on Aster's behalf. This includes acceptance-test messages, follow-ups,
replies, direct transport/API calls, and edits to cursors or job state whose
purpose is to make a message send.

Email decisions belong to Aster's durable operator loop. Agents may:

- Implement and test the generic scheduling, diagnosis, recovery, policy, and
  transport mechanisms with fake/local transports
- Inspect live communication evidence and health without changing its outcome
- Restart the configured runtime when explicitly requested
- Add observability that explains why Aster did or did not act

Agents may not substitute themselves for the runtime. A live message is valid
only when Aster independently notices durable work, evaluates policy and
controls, claims the job, invokes the configured transport, observes the real
result, and persists the evidence. Never manually advance mailbox cursors,
create fake inbound work, alter relationship state, or invoke a send command to
obtain a desired demonstration result.

If Aster fails to notice work, fix the general discovery/recovery mechanism and
wait for the ordinary loop to establish the result. Do not manually trigger the
specific message.

---

# Daedalus and Autonomous Agents

Daedalus and other autonomous identities are engineering collaborators.

They have implementation autonomy.

They should:

- Inspect
- Reason
- Implement
- Test
- Review
- Document
- Report

They should also criticize poor engineering decisions.

They are not required to agree with the human.

However, autonomy does not override the engineering contract above.

Daedalus may reject or recommend against a change when:

- Evidence is insufficient
- Tests are missing
- An architectural boundary is violated
- A change introduces unnecessary complexity
- A capability does not actually work
- A model claim is being treated as runtime truth
- A regression is being hidden

---

# Change Discipline

Prefer:

- Small, focused changes
- Explicit commits
- Regression tests
- Understandable abstractions
- Observable behavior

Avoid:

- Giant speculative rewrites
- Unrelated cleanup mixed into feature work
- Duplicate implementations
- Temporary hacks that become permanent
- Changing many systems to fix one symptom
- Deleting tests because they fail
- Weakening verification because it is inconvenient

When an architectural change is necessary, document why.

---

# When Something Breaks

Do not immediately patch the visible symptom.

Instead:

1. Reproduce.
2. Trace the execution path.
3. Identify the actual owner of the behavior.
4. Identify the violated invariant.
5. Fix the underlying mechanism.
6. Add a regression test.
7. Rerun broader tests.
8. Verify a fresh identity where relevant.

The question is not:

> “How do I make this example pass?”

The question is:

> “What general rule is currently broken?”

---

# Definition of Done

A feature is **not** done when:

- Code compiles
- Tests pass
- The model says it works
- The CLI prints a success message

A feature is done when:

- The implementation exists
- Real execution succeeds
- Failure is handled
- Evidence exists
- State persists where required
- Tests cover the behavior
- Unrelated behavior remains intact
- The behavior works with a fresh identity when identity state matters

---

# Current Project North Star

IdentityOS should evolve toward:

```text
persistent identities
+
portable capabilities
+
truthful execution
+
durable long-running tasks
+
evidence-backed memory
+
model independence
+
cross-session continuity
```

The goal is not to make an impressive chatbot.

The goal is to build infrastructure in which an identity can:

```text
understand
→ decide
→ acquire capability
→ execute
→ observe reality
→ learn
→ persist
→ continue
```

Without pretending that something happened when it did not.

That standard applies to every agent working in this repository.
