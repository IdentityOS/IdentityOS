# First-time developer experience review

Date: 2026-11-03
Method: independent agent audit followed by automated browser/API and clean-container trials.

## Before

A new developer had to understand repository layout before seeing a response:
clone the source, create and activate a virtual environment, install an editable
checkout, copy `.env`, choose variable names, create an identity through the
advanced CLI, and separately discover the playground. Data location depended on
the current working directory. “Configured” could mean only that an adapter
object existed, and the dashboard's public bind default contradicted a local
tool's expected safety boundary.

## After

The main path has three concepts:

1. Install the isolated application without cloning.
2. Run `idos init` and `idos start --open`.
3. Name an identity, select inference, verify one real request, and chat.

The browser avoids internal terms such as Prometheus, Executive, ISP, context
composer, and adapter chain. It explains the local/cloud privacy distinction at
the point of choice. Advanced runtime inspection remains one link away and the
legacy `identity` CLI remains available.

The screen was rendered in Chromium at 1280×900 and visually inspected. It has
clear hierarchy, labels, focus targets, responsive layout, reduced-motion
handling, alert/live regions, and keyboard-submit forms. The captured welcome
state is `evidence/onboarding/screenshots/welcome.png`.

## Remaining friction

- Python 3.11 plus the operating system's `venv` support is still required.
- `~/.local/bin` may not already be on PATH; the installer prints the exact next command.
- The convenience installer tracks public `main`; production users should pin a tag or full commit.
- Ollama users must install, start, and choose a model themselves. This is
  deliberate because model downloads can be large.
- The observed CPU-only `llama3.2:1b` path was slow. It worked, but final
  clean-artifact provider latency was 102 seconds. Cloud usability could not be
  measured without a credential.
- Provider accounts and key acquisition remain external human steps and are not
  included in the five-minute measurement.
- The onboarding trial was agent-driven and automated. It proves functional
  clarity and browser rendering, not a timed study with an unfamiliar human.
- The installer is shell-based: native Windows is unsupported, while macOS and
  WSL2 require independent trials before support claims.

## Recommended next usability work

1. Conduct three timed human trials from the public README after merge.
2. Add a provider-specific model discovery control for Ollama.
3. Publish signed release artifacts and documented checksums after authorization.
4. Add a small built-in latency probe so users can compare configured providers.
5. Verify macOS and WSL2 in clean CI runners and adjust filesystem guidance from evidence.
