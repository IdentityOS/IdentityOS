# Developer adoption baseline

Date: 2026-11-03
Repository commit: `394bc8c7cb09ffb282b2c1a7532761e63e7ee244`

This audit records the state observed before the developer-first installation and
onboarding work began. Statements below are based on source inspection and
executable checks; documentation claims alone are not treated as evidence.

## Existing architecture to preserve

IdentityOS already has the right core separation for this work:

- `IdentityRuntime` coordinates identities, context, adapters, events, and
  persistence.
- `JSONFileBackend` provides path-confined, atomic JSON persistence.
- The adapter package supplies OpenAI, Anthropic, Ollama, OpenRouter, Groq,
  SambaNova, Cerebras, and named OpenAI-compatible configurations.
- `SecretStore` provides local files with restrictive permissions, opaque secret
  handles, and redaction helpers.
- The playground is an existing FastAPI dashboard with identity creation, chat,
  restart, and export endpoints.
- The capability registry remains independent of onboarding; consequential
  capabilities are not installed automatically.

The implementation should therefore add a shared adoption/configuration service
and make the CLI and existing playground consume it. It must not introduce a
second identity store, adapter registry, or chat runtime.

## Installation and onboarding friction

1. The README requires cloning the repository, creating a virtual environment,
   installing an editable checkout, copying `.env`, and editing it manually.
2. The source distribution exposes `identity` and `identitybench`, but no
   lifecycle-oriented command such as `idos init`, `start`, `status`, or
   `doctor`.
3. Runtime state defaults to `.identity_store` relative to the current working
   directory, so the same user can accidentally create unrelated stores.
4. The playground binds to `0.0.0.0` by default and its mutation endpoints have
   no browser-session or origin protection.
5. Adapter configuration in the playground proves only that an adapter object
   could be constructed. It does not perform genuine inference before implying
   readiness, and raw credentials are not durably configured.
6. When no adapter is configured the runtime can return a normal-looking
   `[No adapter configured ...]` string. The legacy CLI can also fall back to an
   echo response after runtime initialization fails. Neither is inference
   evidence.
7. Export and import are not transactional. Import reconstructs some state in
   memory but does not persist every restored namespace for a subsequent fresh
   process. Facts and profile state are not fully restored.
8. There is no clean-install CI path, versioned bootstrap installer, uninstall
   path, or fresh-environment evidence bundle.

## Packaging reality

The repository uses Hatchling and declares Python 3.11 or newer. A clean install
of the public `identityos==2.0.0` package was tested separately. That public
artifact declared only `httpx`, exposed no console scripts, and did not represent
this repository's dependency or CLI surface. It is therefore not a usable or
truthful installation route for this work.

Until an authorized release is published, verification must use a locally built,
hashed wheel and label it as an **unpublished-artifact test**. A public bootstrap
command may target a pinned GitHub ref, but its availability cannot be claimed
until the corresponding source is actually on the public ref.

## Executable baseline

Two targeted suites were run from isolated temporary state:

- Adapter, SDK, secrets, chat, and portability selection:
  `90 passed, 1 skipped, 1 failed` in 79.66 seconds. The failure was an OpenAI
  transport-type subprocess that exceeded its 30 second timeout.
- Persistence, chat, public API, and SDK selection:
  `20 passed, 1 failed` in 54.51 seconds. The failure showed an empty exported
  constitution.
- A separate runtime-adjacent selection passed `53 tests` in 39.82 seconds.

These failures predate the adoption changes and are retained as baseline facts.
They must be rerun during integration rather than explained away.

Direct portability probing also established a concrete defect: an imported
identity could expose memory and goals immediately in the importing process, but
a new runtime opened on the destination store saw zero imported memories and
goals.

## Platform and provider baseline

- Linux x86_64 is available for host and clean-container verification.
- Docker is available; a minimal Python 3.12 image is cached.
- macOS, WSL2, and native Windows are not available and cannot be reported as
  verified.
- No cloud-provider credential is available to the test environment.
- A host Ollama service and pre-existing models are available. Any use of those
  models is genuine local inference, but it must be labeled a prepared-host
  provider test rather than a fresh model installation. Download and model
  initialization timings remain separate.

## Implementation boundary

The selected design is intentionally small:

1. Stable per-user paths and a versioned, non-secret installation config.
2. A shared onboarding service using the existing runtime, adapter registry,
   persistence backend, and secret store.
3. A new `idos` lifecycle CLI while retaining the existing `identity` CLI.
4. A rerunnable user-space bootstrap installer using an isolated virtual
   environment and pinned source/artifacts.
5. A staged route inside the existing playground, bound to loopback by default,
   with verified-inference readiness and browser request protections.
6. Bounded, scrubbed, transactional portability that is proven across fresh
   processes and distinct stores.
7. A machine-readable doctor command and reproducible clean-install harness.

No package, release, DNS record, or hosted service will be published as part of
this mission without separate authorization.
