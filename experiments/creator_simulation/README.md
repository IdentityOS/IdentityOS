# Creator community simulation

Runs 16 synthetic actors (12 creators, four consumers) through real SDK and platform APIs in separate Python processes. No model response counts as execution evidence. Every subprocess result and failed verification is appended to `ledger.jsonl`; `summary.json` and `report.md` expose the outcome.

Run from an IdentityOS checkout with dependencies installed:

```sh
python -m experiments.creator_simulation.run \
  --artifact experiments/creator_simulation/artifacts/creator_probe.idcap \
  --output /tmp/idos-community-new-run
```

The output directory must not already exist. Actor stores, imported identities, registry manifests, and published packages reside in that directory. The harness removes remote registry configuration from worker environments. It does not send communications or use production identity stores.

The community includes identity authors, capability authors, SDK application developers, and consumers with creative, educational, accessibility, community, and personal uses. Four authors publish identity manifests through the real `publish --registry-dir` command. Four capability authors adapt the verified probe template, independently verify their packages through Skill Forge, and publish `.idcap` files to a local shared directory. SDK creators and consumers install a different author's package and invoke it after process restart. Consumers import another author's portable identity. Invalid invocation must fail.

## Engineering identity trial

`engineering_response.json` records a real call to Daedalus's ThinkingEngine with its registry persona, using Groq `openai/gpt-oss-120b`. This exercised the engineering reasoning engine directly, rather than the full conversational orchestrator. The initial design invented APIs and several scenario ratios did not satisfy the creator requirement.

Daedalus then authored the probe through `ModelCapabilityAuthor`. Two rejected proposals and their failures are retained; the third passed independently supplied cases through Skill Forge. The exact verified artifact is checked in alongside its readable source. The probe performs SDK creation, memory/goal persistence, export, and reload. Its own reload is within one process; the external harness separately establishes fresh-process continuity for actors and installed packages.

To repeat model authoring in a fresh checkout, configure provider credentials in `.env` or supply `IDOS_ENV_FILE`. Run `python -m experiments.creator_simulation.request_engineer` and `python -m experiments.creator_simulation.forge_engineer`. Model authoring is an opt-in network operation and may produce a failing candidate. Existing source is never silently overwritten. Report paths in the retained historical records identify the original experiment workspace.

## MiroFish

Upstream inspected: https://github.com/666ghj/MiroFish at `7657031ac01184afe2cb220f5ee3545573b5e843`. Its full graph/social workflow requires an LLM and Zep. This experiment did not run MiroFish or predict adoption. `mirofish_profiles.json` exports the cohort using the upstream OASIS Reddit profile fields for later qualitative simulation; importing it into a specific MiroFish project remains to be tested.

## Coverage boundaries and findings

This is an execution simulation, not evidence that real users like the product. It exercises Python, not JavaScript, and deterministic operations rather than model chat or real creative output. Capability authors use template adaptation, not unconstrained independent code generation. Local package publication means a shared filesystem artifact, not public marketplace or GitHub publication.

Two CLI paths need a separate product fix: `registry publish` prints contribution instructions rather than publishing, and `registry install` uses a repository-relative store and does not create namespace parent directories. This harness uses actual top-level `publish --registry-dir`, SDK portable import, and Skill Forge installation; it does not claim those older registry paths passed.

## Validation

- Full suite: 1,722 passed, 43 skipped.
- Focused simulation checks after adding package identity and permission evidence: 2 passed.
- Skill Forge, third-party SDK, and registry lifecycle checks: 20 passed.
- Retained community run: 16 actors, 12 creators, 96/96 observed steps passed.

## Live BAND workplace

`live_band.py` runs one finite community round and publishes START and observed PASS/FAIL messages from the mapped BAND seats. A SQLite outbox retains messages until delivery succeeds. Room delivery failure stops further work; the next invocation retries pending messages. An interrupted round retains its partial ledger and is explicitly reported before a fresh round begins.

Use an explicitly selected room in `bindings.json`; the runner never silently substitutes a different room. The human owner must add the first seat (`IDOS Poet`) to a human-owned room. That seat then invites the remaining seats. The local account hit its remote-agent quota after nine dedicated seats; the seven remaining actors are explicitly labelled when sharing those seats. This maps sixteen synthetic actors to nine BAND identities rather than claiming sixteen independent BAND seats exist.

On this machine the selected room is `5c44dadb-32bf-42c8-92ce-0962c03291ec`, renamed **IDOS Creator Community**. The durable state is `~/.local/state/idos-creator-community`; local systemd service/timer `idos-creator-community` retry readiness every minute while blocked, then schedule a new round ten minutes after completion. Each round is a fresh isolated synthetic cohort with real persisted state/restart checks within the round; these are scripted lifecycle workloads, not free-running LLM identities or continuous creative projects. Failure and publication messages are labelled by actual actor even for shared seats.

Inspect and pause the runner:

```sh
systemctl --user status idos-creator-community.service idos-creator-community.timer
journalctl --user -u idos-creator-community.service -n 30
systemctl --user stop idos-creator-community.timer idos-creator-community.service
```

Room membership is verified before workloads start. If the room is inaccessible to the seats, the service remains in readiness retries and no simulation actions are presented as completed. Room messages use explicit mentions as required by BAND; the user can inspect all messages. Unconsumed mentions are telemetry rather than model conversations.
