# IdentityOS developer-adoption evidence

Evidence date: 2026-11-03
Baseline source commit: `394bc8c7cb09ffb282b2c1a7532761e63e7ee244`
Artifact: `identityos-2.1.0-py3-none-any.whl`
Artifact SHA-256: `fa6c2d2e41c1ad822b5877d26a64104a57ff8aa1e17458a3aaa18ba10da02c14`

These are unpublished-artifact tests. They prove the built source artifact and
clean installation path; they do not claim that version 2.1.0 is available from
a public package index.

## Clean environments

Three final artifact-harness containers used the pinned image
`python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016`
with four visible CPUs, 8.27 GB visible memory, a fresh virtual environment,
and the pip cache disabled. The repository was mounted read-only only to supply the
verification driver; installation consumed the wheel mounted read-only at
`/artifacts`.

| Run | Install | Automated init | Real inference | Restart | Portability | Result |
|---|---:|---:|---:|---:|---:|---|
| `final-cold-linux-1` | 60.802 s | 1.535 s | not requested | not requested | not requested | success |
| `final-cold-linux-2` | 64.051 s | 1.789 s | not requested | not requested | not requested | success |
| `final-cold-linux-3-real-ollama` | 71.590 s | 1.478 s | 104.486 s | 76.157 s | 2.274 s | success |

Cold installation minimum/median/maximum: **60.802 / 64.051 / 71.590 seconds**.
Bootstrap minimum/median/maximum: **11.857 / 12.005 / 12.817 seconds**.
Automated initialization minimum/median/maximum: **1.478 / 1.535 / 1.789 seconds**.

The third run reached its first runtime-observed real model response **200.135
seconds** after harness start. This includes environment bootstrap, cold wheel
installation, an idempotent reinstall check, CLI and recovery checks, automated
initialization, and inference. It is under five minutes. The result is an
automated journey, not a human usability timing.

## Real inference scope

Run 3 used the clean container installation with the host's already-installed
Ollama service and pre-existing `llama3.2:1b` model. The response provenance was:

- generation mode: `identity_model_generation`
- provider: `OllamaAdapter`
- model: `llama3.2:1b`
- provider latency: 101.597 seconds
- response SHA-256:
  `6b9fa8cf650b894608cd41d846a6d4a9bdfc8f575bd7f92a18c597679efa5cd7`

No response body is committed. Model download time is not part of the result;
the model was pre-existing. This is accurately classified as a **prepared-host
local-provider integration**, not a fresh local-model installation. No cloud
credential was available, so cloud inference remains untested.

## Persistence and portability

The inference, restart, and portability commands were separate processes. The
restart phase loaded the same stable identity ID, found the prior conversation
marker in durable memory, and performed another real inference. The portability
phase exported the identity, proved a synthetic secret canary was absent,
imported into a distinct IdentityOS home, loaded it in another fresh runtime,
and verified stable ID and conversation memory.

## Documented installer path

A fourth successful clean container used the documented pre-publication command
shape, `bash install.sh --artifact /absolute/path/to/wheel`, rather than the
artifact harness's direct pip installation. It required no clone, initialized
state, passed doctor, stayed running after a delayed status check, served the
actual onboarding page over loopback HTTP, and shut down cleanly. Installation
took **90.031 seconds** and the full path through shutdown took **105.109
seconds**. See `final-public-facing-installer.json`.

The no-clone route was also repeated without mounting a repository or wheel:
a new container downloaded `install.sh` and the source archive from public
commit `d59c6fdc8e0fa5916f45d0874d313970e27d96b0`, then passed initialization and
doctor. Installation took **86.696 seconds**. See
`github-exact-ref-no-clone.json`.

After PR #117 merged, two additional fresh containers downloaded the installer
from the public `main` URL and installed exact delivery commit
`3a0f64a0a28d8d7fe5dc02ab0b96d533031c7611` without a clone or mounted artifact.
The final acceptance attempt installed in **113.75 seconds**, initialized in
**1.42 seconds**, passed doctor, and exited zero. The first post-merge attempt
installed in **88.79 seconds** and passed init and doctor, but its outer command
exited 2 because an extra `idos --version` probe is not supported. Both attempts
are preserved in `public-main-post-merge.json`.

Nine clean installer/evidence attempts were made in total: six accepted
successes and three outer-command/evidence-wrapper failures described here and
in `failures.md`. The first post-merge attempt still establishes successful
installation, initialization, and doctor behavior, but is not counted as an
accepted zero-exit journey.

## Failure and recovery exercised

Every cold run included an import of a missing archive that was required to
fail, followed by a successful machine-readable doctor run. Automated tests also
cover invalid/tampered archives, name collisions, invalid configuration,
occupied ports, public-bind refusal, missing credentials, false real-inference
claims, failed installer rollback, repeated installation, data-preserving
uninstall, and explicit purge confirmation.

The first prepared-host local inference attempt timed out after 45 seconds and
was recorded during development. It was not converted into success. Reducing
acceptance context and removing unrelated capability-evolution work from the
verification path produced the later observed success. See `failures.md`.

## Reproduce

Build and validate the wheel:

```bash
mkdir -p dist
docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" python:3.12-slim \
  sh -lc 'python -m pip install --no-cache-dir build twine && python -m build --wheel --outdir /out /src && python -m twine check /out/*'
sha256sum dist/identityos-2.1.0-py3-none-any.whl
```

The exact clean-run commands and sanitized outputs are in each run's
`commands.jsonl` and `installation.log`. The reusable runner is
`scripts/verify_clean_install.py`; real-provider phase logic is
`scripts/verify_adoption_journey.py`.

## Platform classification

- Linux x86_64 container on Linux host: **Verified** for wheel installation,
  CLI initialization/diagnostics, genuine prepared-host Ollama inference,
  process restart, and cross-install portability.
- Linux host: **Verified** for dashboard start/status/stop and Chromium rendering.
- macOS: **Not tested**.
- WSL2: **Not tested**.
- Native Windows: **Unsupported by the shell installer**.
- Machine-restart persistence: **Not tested**; fresh-process persistence is verified.

The rendered welcome screen is at `screenshots/welcome.png`.
