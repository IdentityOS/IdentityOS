# IdentityOS CLI Reference

Every command in the IdentityOS ecosystem.

---

## Table of Contents

- [identity — Main CLI](#identity--main-cli)
- [identity create](#identity-create)
- [identity chat](#identity-chat)
- [identity inspect](#identity-inspect)
- [identity list](#identity-list)
- [identity snapshot](#identity-snapshot)
- [identity history](#identity-history)
- [identity rollback](#identity-rollback)
- [identity diff](#identity-diff)
- [identity explain](#identity-explain)
- [identity playground](#identity-playground)
- [identity registry](#identity-registry)
- [identity cap](#identity-cap)
- [identity isp](#identity-isp)
- [Runtime Server](#runtime-server)
- [Environment Variables](#environment-variables)
- [Python SDK](#python-sdk)
- [Package Management](#package-management)

---

## identity — Main CLI

The single entry point for all IdentityOS operations.

```bash
identity [--store PATH] [--backend json|sqlite] <command> [options]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--store` | `.identity_store` | Path to identity storage directory |
| `--backend` | `json` | Storage backend: `json` or `sqlite` |

**Global flags apply to all subcommands below.**

---

## identity create

Create a new identity.

```bash
identity create --name "Pluto" --persona companion [--id custom-id]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--name` | yes | Human-readable name |
| `--persona` | no | Persona archetype (default: `default`) |
| `--id` | no | Custom identity ID (auto-generated UUID if omitted) |

**Example:**
```bash
identity create --name "Ruby" --persona assistant
# → Identity created, id: a1b2c3d4, name: Ruby
```

---

## identity chat

Start an interactive REPL session with an identity.

```bash
identity chat --id a1b2c3d4 [--adapter openai] [--model gpt-4o]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--id` | yes | Identity ID to chat with |
| `--adapter` | no | LLM adapter type: `openai`, `anthropic`, `ollama`, `openrouter`, `groq`, `sambanova` |
| `--adapter-config` | no | JSON string with adapter config |
| `--model` | no | Model override (default: `gpt-4o`) |

When `--adapter` is omitted, `chat` scans `.env` for configured API keys, runs a health check on each, and presents an interactive menu of working adapters to choose from. If only one adapter passes the health check, it is selected automatically.

**Special chat commands:**
| Command | Action |
|---------|--------|
| `exit` / `quit` / `bye` | End session |
| `:snapshot` | Checkpoint current state |
| `:history` | List all snapshots |

**Example:**
```bash
identity chat --id a1b2c3d4
# you> Hello!
# Ruby> Hi! How can I help you today?
```

---

## identity inspect

View identity state as JSON, or use `--dashboard` for the rich terminal UI.

```bash
identity inspect --id a1b2c3d4 [--dashboard]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--id` | yes | Identity ID |
| `--dashboard` | no | Show rich terminal dashboard |

**Example (JSON):**
```bash
identity inspect --id a1b2c3d4
# {
#   "identity": { "name": "Ruby", "persona": "assistant", ... },
#   "timeline": { "event_count": 3, ... },
#   ...
# }
```

**Example (dashboard):**
```bash
identity inspect --id a1b2c3d4 --dashboard
# ════════════════════════════════════════════
#   Identity  Ruby
# ════════════════════════════════════════════
#   Current Mission
#   Ship Q2 release
#   ████████░░░░░░░░░░ 42%
#   ...
```

---

## identity list

List all local identities ranked by total experience (interactions + memories + timeline events).

```bash
identity list [--limit N]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--limit` | 0 (unlimited) | Maximum rows to show |

Alias: `identity get`

**Example:**
```bash
identity list
# Rank  ID       Name    Persona    Exp  Ints  TL  Mem  Rel
# 1     a1b2c3d4 Ruby    assistant  12   5     3   4    2
# 2     d5e6f7a8 Pluto   companion  3    1     1   1    0
```

---

## identity snapshot

Manually capture a checkpoint of an identity's current state.

```bash
identity snapshot --id a1b2c3d4 [--label "before-experiment"]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--id` | yes | Identity ID |
| `--label` | no | Snapshot label (default: `manual`) |

**Example:**
```bash
identity snapshot --id a1b2c3d4 --label "v1-before-upgrade"
# → Snapshot captured: 7af5dd29-cbff-4db4-8c99-0f8e2aa2555e
```

---

## identity history

View all snapshots for an identity in chronological order.

```bash
identity history --id a1b2c3d4
```

| Flag | Required | Description |
|------|----------|-------------|
| `--id` | yes | Identity ID |

**Example:**
```bash
identity history --id a1b2c3d4
# Snapshot history for 'a1b2c3d4' (2 total):
#   1. Snapshot b25e6685 [initial] @ 2026-07-26 | modules: identity, memory, ...
#   2. Snapshot 7af5dd29 [v1-before-upgrade] @ 2026-07-26 | modules: identity, ...
```

---

## identity rollback

Roll back an identity to a prior snapshot. Prompts for confirmation.

```bash
identity rollback --id a1b2c3d4 --snap b25e6685-...
```

| Flag | Required | Description |
|------|----------|-------------|
| `--id` | yes | Identity ID |
| `--snap` | yes | Snapshot ID to roll back to |

**Example:**
```bash
identity rollback --id a1b2c3d4 --snap b25e6685-3d29-49d0-8017-fede5dd702c7
# Roll back identity 'a1b2c3d4' to snapshot 'b25e6685...'? [y/N] y
# Rolled back to snapshot b25e6685.
```

---

## identity diff

Show a structured diff between two snapshots.

```bash
identity diff --id a1b2c3d4 --from SNAP_A --to SNAP_B
```

| Flag | Required | Description |
|------|----------|-------------|
| `--id` | yes | Identity ID |
| `--from` | yes | Source snapshot ID |
| `--to` | yes | Target snapshot ID |

**Example:**
```bash
identity diff --id a1b2c3d4 --from b25e6685 --to 7af5dd29
```

---

## identity explain

Trace why an identity behaves as it does — shows goals, facts, evidence chain, timeline, and user profile.

```bash
identity explain <identity_id> <question...>
```

| Argument | Description |
|----------|-------------|
| `identity_id` | Identity ID to explain |
| `question` | One or more words forming the question |

**Example:**
```bash
identity explain a1b2c3d4 "why do you think I should focus on Q2?"
# ════════════════════════════════════════════
#   Explanation
# ════════════════════════════════════════════
#   Goal
#   → Ship Q2 release
#     Complete the dashboard feature by April 15
#   ...
```

---

## identity playground

Launch the IdentityOS Playground web UI.

```bash
identity playground [--port 8000] [--host 0.0.0.0]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `8000` | Port to serve on |
| `--host` | `0.0.0.0` | Host to bind to |

**Example:**
```bash
identity playground --port 8765
# Open http://localhost:8765/playground
```

---

## identity registry

Interact with the Identity Registry — discover and install published identity specs.

```bash
identity registry <list|show|install|publish>
```

### registry list

Browse all identities in the registry:

```bash
identity registry list
#   IdentityOS Registry (github.com)
#   12 identities available
#
#   ID                            Version    Capabilities    Model
#   mentor-planner                v1.0.0     5               gpt-4o
#   code-reviewer                 v1.2.0     8               llama-3.3-70b
```

### registry show

View details of a registry entry:

```bash
identity registry show mentor-planner
#   Identity
#     ID:          mentor-planner
#     Name:        Mentor Planner
#     Description: A planning-focused mentor identity
```

### registry install

Download and install an identity spec from the registry:

```bash
identity registry install mentor-planner
#   ✓ Installed to .identity_store/mentor-planner.json
#   To use: identity inspect mentor-planner
```

### registry publish

Prepare an identity spec for publishing to the registry:

```bash
identity registry publish ./my-identity.json
#   Publishing 'my-identity'...
#   To add to the registry, copy to registry/identities/my-identity.json
#   Then add an entry to registry/index.json and submit a PR.
```

---

## identity cap

Browse, search, and install capabilities from the marketplace.

```bash
identity cap <list|show|install|search|installed>
```

### cap list

List all available capabilities:

```bash
identity cap list
#   <current marketplace count> capabilities available
#
#   datetime             v1.0.0  Current date/time awareness
#   filesystem           v1.0.0  Read/write files on local system
#   github               v1.0.0  Query GitHub repositories and PRs
#   weather              v1.0.0  Current conditions and forecasts
#   ...
```

Capability discovery uses `registry/capabilities/index.json` as the
authoritative marketplace index. Isolated legacy registries that do not have a
capability index continue to fall back to `registry/index.json`. The latter is
otherwise a curated combined identity catalog and may contain fewer capability
entries.

### cap show

View capability details:

```bash
identity cap show datetime
#   id:           datetime
#   name:         Date & Time
#   description:  Knows the current date and time
#   skills (1):
#     get_current_datetime     Returns current date and time
```

### cap install

Install a capability onto an identity:

```bash
identity cap install datetime --identity a1b2c3d4
#   installed: datetime -> a1b2c3d4
#   skills:    1 added
```

### cap search

Search capabilities by keyword:

```bash
identity cap search "file"
#   2 matches for 'file'
#
#   filesystem             Read/write files on local system
#   text                   Word count, keyword extraction, pattern detection
```

### cap installed

List capabilities installed on an identity:

```bash
identity cap installed a1b2c3d4
#   installed capabilities for a1b2c3d4:
#
#   datetime             v1.0.0  Current date/time awareness
#   filesystem           v1.0.0  Read/write files on local system
```

---

## identity isp

Install Identity Skill Packs — bundles of capabilities designed to work together.

```bash
identity isp <list|show|install>
```

### isp list

Browse available packs:

```bash
identity isp list
#   Identity Skill Pack Registry
#   6 packs available
#
#   scribe               v1.0.0  Writing & communication skills
#   sage                 v1.0.0  Research & analysis skills
#   scout                v1.0.0  Information gathering & monitoring
#   planner              v1.0.0  Scheduling & time management
#   reviewer             v1.0.0  Code review & analysis
#   architect            v1.0.0  System design & navigation
```

### isp show

View pack details before installing:

```bash
identity isp show scout
#   id:           scout
#   name:         Scout
#   description:  Information gathering & monitoring
#   capabilities (4):
#     weather              Monitors conditions
#     github               Tracks repository activity
#     web                  Fetches web content
#     datetime             Time-aware monitoring
```

### isp install

Install all capabilities in a pack at once:

```bash
identity isp install scout --identity a1b2c3d4
#     installed: weather (2 skills)
#     installed: github (4 skills)
#     installed: web (3 skills)
#     installed: datetime (1 skill)
#
#   Pack 'scout' installed — 4/4 capabilities, 10 total skills
```

---

## Runtime Server

The IdentityOS runtime exposes a REST API for programmatic access.

### Start the server

```bash
# Option A: Via the CLI
identity playground --port 8000

# Option B: Directly with uvicorn
uvicorn runtime.main:app --port 8000 --reload
```

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/identity` | Create a new identity |
| `POST` | `/process` | Process a user message (chat) |
| `POST` | `/context` | Get augmented context for prompt injection |
| `POST` | `/evaluate` | Evaluate and store a conversation turn |

**Example API calls:**

```bash
# Create identity
curl -X POST http://localhost:8000/identity \
  -H "Content-Type: application/json" \
  -d '{"identity_id":"pluto","name":"Pluto","persona":"companion"}'

# Chat
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello!","identity_id":"pluto","user_id":"me","session_id":"web"}'
```

---

## Environment Variables

IdentityOS is configured through environment variables (loaded from `.env`).

### LLM Provider Keys

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI / any OpenAI-compatible API |
| `GROQ_API_KEY` | Groq (free at console.groq.com) |
| `ANTHROPIC_API_KEY` | Anthropic Claude |
| `OPENROUTER_API_KEY` | OpenRouter |
| `SAMBANOVA_API_KEY` | SambaNova Cloud |
| `OPENAI_BASE_URL` | Custom base URL for any OpenAI-compatible endpoint |
| `OPENAI_TIMEOUT` | Positive request timeout in seconds for OpenAI and compatible local endpoints (default: `120`) |

### Key Rotation

Groq and SambaNova support multiple keys for rate-limit rotation:

| Variable | Purpose |
|----------|---------|
| `GROQ_API_KEY_2` through `_6` | Groq rotation keys |
| `SAMBANOVA_API_KEY_2`, `_3` | SambaNova rotation keys |

### Runtime Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `IDENTITY_ADAPTER` | auto-detect | Force a specific adapter: `openai`, `groq`, `anthropic`, `openrouter`, `sambanova`, `ollama` |
| `IDENTITY_MODEL` | (per adapter) | Override the model name |
| `IDENTITY_ADAPTER_CONFIG` | `{}` | JSON string with additional adapter config |
| `OPENAI_TIMEOUT` | `120` | Positive request timeout in seconds for OpenAI-compatible adapters, including Ollama |

### Local Model Examples

```bash
# Ollama
OPENAI_BASE_URL="http://localhost:11434/v1"
OPENAI_API_KEY="ollama"

# LM Studio
OPENAI_BASE_URL="http://localhost:1234/v1"
OPENAI_API_KEY="not-needed"

# vLLM / llama.cpp
OPENAI_BASE_URL="http://localhost:8000/v1"
OPENAI_API_KEY="not-needed"

# HuggingFace Inference Endpoints
OPENAI_BASE_URL="https://api-inference.huggingface.co/models/your-model"
OPENAI_API_KEY="hf_your_key"
```

---

## Python SDK

The `identityos` package provides programmatic access.

### Install

```bash
pip install identityos
```

Or from source:

```bash
pip install -e .
```

### Quick Reference

```python
from identityos import Identity

# Create
agent = Identity.create("MyBot")

# Load
agent = Identity.load("mybot")

# Teach facts
agent.observe("My name is Alice")

# Set goals
agent.goal("Master FastAPI", priority="high")

# Build relationships
agent.relationship("user-123", trust_level=0.9)

# Export / Import
agent.export("mybot.json")
restored = Identity.from_file("mybot.json")
```

---

## Package Management

```bash
# Install IdentityOS with all runtime dependencies
pip install -e .

# Install from PyPI (when published)
pip install identityos

# Install with dev/test dependencies (pytest, etc.)
pip install -e ".[dev]"

# Install with Anthropic adapter support
pip install -e ".[anthropic]"

# Install everything
pip install -e ".[dev,anthropic]"

# Run tests
pytest tests/ -v

# Run tests (exclude API-key-dependent tests)
pytest tests/ -v --ignore=tests/legacy
```

### What `pip install -e .` Installs

| Package | Size | Why |
|---------|------|-----|
| `openai` | ~500KB | Required by every adapter except Anthropic (Groq, Ollama, OpenRouter, SambaNova, and plain OpenAI all use the OpenAI SDK) |
| `fastapi` | ~300KB | Runtime API server |
| `uvicorn` | ~200KB | ASGI server for the runtime |
| `pydantic` | ~500KB | Data validation (used by runtime and SDK) |
| `httpx` | ~300KB | HTTP client (used by SDK and runtime internally) |
| `jinja2` | ~400KB | Template engine for the Playground web UI |
| `python-dotenv` | ~100KB | Loads `.env` file automatically |

The `identity` CLI command is registered as a `console_scripts` entry point — it becomes available globally once the package is installed in the active environment.

### Optional Extras

| Extra | Installs | For |
|-------|----------|-----|
| `[dev]` | `pytest`, `pytest-asyncio` | Running tests |
| `[anthropic]` | `anthropic` Python SDK | Using `AnthropicAdapter` |

---

## Backward-Compatible Invocations

The following still work but are deprecated:

```bash
# Old style (deprecated — use `identity` instead)
python -m cli.main create --name "X"
python tools/identity inspect <id>
python tools/identity explain <id> "question"

# Old import (deprecated — use `from identityos import Identity`)
from sdk import Identity
```

Both old paths are thin wrappers around the current `identity` CLI and `identityos` package.
