<p align="center">
  <img src="https://raw.githubusercontent.com/IdentityOS/IdentityOS/main/docs/identityos-banner.png" alt="IdentityOS" width="600"/>
</p>

<h1 align="center">IdentityOS</h1>
<p align="center"><em>Every AI deserves its own soul.</em></p>

---

> **Release reality:** the developer-first installer in this branch is verified
> from built artifacts. It is not a public package claim until a release that
> contains it is published. See [installation](docs/INSTALLATION.md) and the
> [verification record](docs/adoption/VERIFICATION.md).

## Why IdentityOS Exists

Today every AI starts over.

Your planning happens in ChatGPT. Your coding happens in VS Code. Your research happens somewhere else. Your Discord conversations stay in Discord.

Each app has its own AI. None of them know what the others learned. None of them grow.

You tell one assistant about your goals. You tell another about a cancelled project. Neither connects the dots.

**IdentityOS changes that.**

It gives AI a persistent identity, a single self that remembers across apps, learns from every conversation, gains new capabilities, and notices things no isolated AI could.

---

## What IdentityOS Is

IdentityOS is an open-source runtime for **portable AI identities**.

An identity is a persistent digital self. It has:
- A **name and persona**: who it is
- **Memory**: facts it learns about you and the world
- **Goals**: things it's working toward
- **Timeline**: a life story of events
- **Capabilities**: skills it can use (check the weather, read files, search GitHub)
- **Relationships**: trust networks with other identities and users

Identities are **portable**. Export one to JSON, move it to another machine,
and load it with a different LLM provider while preserving persisted state.

> Not shared memory. A shared self.

---

## Core Concepts

**Identity**: A persistent AI personality. It has a name, persona, memories, goals, and capabilities. You create it once and interact with it anywhere.

**Persona**: The character or role of an identity (e.g., "mentor", "analyst", "companion"). This shapes how it responds.

**Capability**: A skill an identity can use. Capabilities are installed at
runtime—no retraining needed—and invoked through validated permission and
evidence contracts. Examples include `datetime`, `filesystem`, and `github`.

**ISP (Identity Skill Pack)**: A bundle of capabilities that work together. Install one ISP and your identity gains multiple skills at once.

**Runtime**: The server that runs identities. Handles memory, context, LLM calls, capability execution, and persistence.

**Adapter**: A connector to an LLM provider. IdentityOS supports Groq, OpenAI, Anthropic, OpenRouter, SambaNova, and any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, etc.).

---

## Quick Start

### 1. Install

Prerequisites: Python 3.11 or newer with `venv`, and `curl`. Linux is the
verified target. macOS and WSL2 are not yet independently verified.

Once this installer is present on the public `main` branch, the no-clone path is:

```bash
curl -fsSL https://raw.githubusercontent.com/IdentityOS/IdentityOS/main/install.sh | bash
```

The installer creates an isolated user-owned environment. It does not modify
system Python, use administrator privileges, download a model, start a service,
or create an account. For a reproducible install, pin a tag or commit:

```bash
ref=<tag-or-full-commit-sha>
curl -fsSL "https://raw.githubusercontent.com/IdentityOS/IdentityOS/$ref/install.sh" -o /tmp/identityos-install.sh
bash /tmp/identityos-install.sh --ref "$ref"
```

Before publication, build a wheel and use `bash install.sh --artifact
/absolute/path/to/wheel`; label that result an unpublished-artifact test.

### 2. Initialize and open onboarding

```bash
~/.local/bin/idos init
~/.local/bin/idos start --open
```

The terminal prints the local URL if a browser cannot be opened. Onboarding
guides you through a name, an inference provider, a real connectivity check,
and the first conversation. No `.env` editing is required. Cloud credentials
are saved in private local secret files and never in identity exports.

Supported onboarding choices are derived from runtime adapters: Ollama, OpenAI,
Anthropic, Groq, Google Gemini's OpenAI-compatible endpoint, OpenRouter,
SambaNova, and Cerebras. Choosing a cloud provider means relevant conversation
context is sent to that provider. Ollama stays local, but IdentityOS never
silently downloads or launches a model.

### 3. Stop, restart, and resume

Press Ctrl+C to stop a foreground server, then restart it with the same command:

```bash
idos start --open
```

The selected identity and its conversation memory are loaded from durable
storage. For an explicit background process:

```bash
idos start --detach --open
idos status
idos stop
```

### Diagnose, export, and import

```bash
idos doctor
idos doctor --json
idos export --identity <identity-id> --output my-identity.identityos.json
idos import my-identity.identityos.json
```

Import validates the archive in a staging store, loads it in a fresh runtime,
and only then commits it. Credentials, installation configuration, installed
capability code, and machine permissions are excluded.

### Data ownership

Linux defaults:

- Identity data: `~/.local/share/identityos/`
- Configuration and private secrets: `~/.config/identityos/`
- Logs and process state: `~/.local/state/identityos/`
- Replaceable application environments: `~/.local/share/identityos-runtime/`

`idos uninstall` removes managed application code but preserves identity data.
Permanent removal requires `idos uninstall --purge-data --yes`.

See [the complete installation and troubleshooting guide](docs/INSTALLATION.md).

---

## Advanced CLI and developer use

The existing `identity` command remains available for advanced runtime work:

```bash
identity list
identity inspect --id <identity-id>
identity snapshot --id gabe --label "before-experiment"
identity rollback --id gabe --snap <snapshot_id>
```

### Launch the Web Playground

```bash
idos start --open
```

Onboarding opens at `http://127.0.0.1:8000/onboarding`. The advanced runtime
view remains at `http://127.0.0.1:8000/playground`.

See [docs/IDENTITY_CHAT.md](docs/IDENTITY_CHAT.md) for session modes, streaming,
state panels, goal/intention controls, restart verification, and export.

For non-Python clients, run the public REST service and use its Swagger UI:

```bash
uvicorn runtime.main:app --host 127.0.0.1 --port 8000
```

See [docs/API.md](docs/API.md) for authentication, rate limiting, deployment,
and the complete endpoint list.

Local voice access uses one SIP switchboard for multiple identities. See the
[phone adapter guide](docs/PHONE_ADAPTER.md) for Asterisk/Linphone setup, local
speech models, caller routing, PIN policy, and the SMS command ingress.

For durable pipeline inspection and identity time travel, see
[docs/DEBUGGER.md](docs/DEBUGGER.md) or run `identity debug --id <id>` and
`identity replay --id <id>`.

IdentityOS also ships reference integrations for continuity outside the CLI:

- [Browser bridge](extension/README.md) for user-controlled continuity across
  ChatGPT, Claude, Gemini, Grok, GitHub, Reddit, and YouTube.
- [VS Code extension](vscode-extension/README.md) for project-partitioned
  coding preferences, persistent software goals, and offline identity state.
- [JavaScript SDK](clients/javascript/README.md) for Node.js integrations that
  use the public API without importing runtime internals.

Long-lived runtime health is measured by IdentityBench's durable endurance
monitor. See [docs/IDENTITYBENCH_ENDURANCE.md](docs/IDENTITYBENCH_ENDURANCE.md)
for recurring runs, restart evidence, trend graphs, and degradation alerts.

---

## Capabilities: Give Your Identity New Skills

An identity with no capabilities is just a chatbot. Add capabilities and it becomes useful.

```bash
# Browse available capabilities
identity cap list

# Install the datetime capability (so Gabriel knows the current time)
identity cap install datetime --identity gabe

# Install filesystem capability (so Gabriel can read/write files)
identity cap install filesystem --identity gabe
```

Now chat with Gabriel again. Ask "what time is it?" or "read my notes.txt". It will use the installed capabilities automatically.

> Capabilities are installed at runtime. No code changes. No retraining.

For autonomous web research and interaction, see the
[browser capability guide](docs/BROWSER-CAPABILITY.md). Browser support is
opt-in and is not installed on existing identities automatically.

<!-- GIF: identity grows by installing capabilities -->

---

## ISP Marketplace: Install Skill Packs

ISPs bundle related capabilities:

```bash
# Browse available skill packs
identity isp list

# Show details of a pack
identity isp show scribe
```

Installing an ISP gives your identity multiple capabilities at once, designed to work together.

---

## Using Local Models

IdentityOS doesn't care which LLM you use. Any OpenAI-compatible API works.

**Ollama:**
```bash
# In one terminal, start Ollama
ollama pull llama3.2
ollama serve
```

```bash
# Then choose Ollama and llama3.2 in browser onboarding.
idos start --open
```

Other OpenAI-compatible endpoints remain available through the advanced adapter
configuration described in [docs/openai_providers.md](docs/openai_providers.md).

---

## Using Cloud Providers

Choose the provider in browser onboarding and enter its key there. The key is
never placed in a browser URL, frontend bundle, identity state, or export. The
provider is not marked ready until a genuine inference request succeeds.

---

## SDK Quickstart

```python
from identityos import Identity

# Create an identity
agent = Identity.create("MyBot")

# Teach it facts
agent.observe("My name is Alice and I love Python")

# Give it a goal
agent.goal("Master FastAPI", priority="high")

# Export , save to a file, share with anyone
agent.export("mybot.json")

# Restore later
restored = Identity.from_file("mybot.json")
```

No API key needed for identity features, facts, goals, memory, export. Add an adapter for chat.

---

## Project Structure

```
├── identityos/      → Python package (from identityos import Identity)
├── core/            → Identity modules (memory, goals, timeline, facts, etc.)
├── runtime/         → FastAPI server, orchestrator, persistence
├── adapters/        → LLM connectors (Groq, OpenAI, Anthropic, Ollama, etc.)
├── cli/             → identity CLI tool
├── registry/        → Published identity specs, capabilities, ISPs
├── docs/            → Constitution, laws, ADRs, roadmap
└── tests/           → Test suite
```

---

## Architecture (Briefly)

IdentityOS organizes identity state into **governed modules**, each with a constitutional foundation:

| Module | What It Does |
|--------|-------------|
| **Identity** | Core properties (name, id, values) |
| **Memory** | Episodic and semantic memory with importance scoring |
| **Goals** | Long-term objectives with lifecycle management |
| **Timeline** | Append-only identity life story |
| **Capabilities** | Installable skills (runtime, no retraining) |
| **Facts** | Evidence-backed belief system |

Full constitution: [docs/constitution/](docs/constitution/)

---

## Demo Recording Guides

Step-by-step guides for recording demos:
- [Demo 1: Identity Growth](docs/demos/01-identity-growth.md)  Capabilities transform an identity
- [Demo 2: ISP Installation](docs/demos/02-isp-install.md)  Multiple skills at once
- [Demo 3: Cross-App Continuity](docs/demos/03-cross-app.md)  Identity remembers across apps
- [Demo 4: Capability Cooperation](docs/demos/04-capability-cooperation.md)  Skills work together
- [Demo 5: Proactive Insight (Hero)](docs/demos/05-proactive-insight.md)  Identity notices what you missed

---

## Running Tests

```bash
# Hermetic default suite
python -m pytest -q

# External integrations (credentials/services may be required)
python -m pytest -q -m network
```

See the evidence-backed [project roadmap](docs/ROADMAP.md) for current North
Star status and remaining runtime gaps.

IdentityBench's paired evaluation, evidence-attestation, and fail-closed
promotion design is documented in
[docs/IDENTITYBENCH_INTEGRITY.md](docs/IDENTITYBENCH_INTEGRITY.md).

```bash
pip install -e .
pytest tests/ -v
```

Tests requiring API keys are skipped automatically when the keys aren't configured.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

Apache License, Version 2.0,  see [LICENSE](LICENSE).

---

<p align="center">
  <em>IdentityOS is infrastructure for persistent AI identities.<br/>Every AI deserves its own soul.</em>
</p>
