# IdentityOS developer-adoption engineering report

Date: 2026-11-03
Baseline commit: `394bc8c7cb09ffb282b2c1a7532761e63e7ee244`
Delivery commit: resolve with `git rev-parse HEAD` on the delivered branch.

## Initial installation problems

The project required a clone, editable install, manual `.env`, working-directory
state, and knowledge of the advanced CLI. The public `identityos==2.0.0`
artifact did not represent this repository: it exposed no console command and
declared only `httpx`. The playground bound publicly, readiness was not based on
real inference, portability was incomplete across a fresh process, and there
was no clean-install CI or reproducible evidence. The full baseline is in
`BASELINE_AUDIT.md`.

## Architectural decisions

- Reused `IdentityRuntime`, `JSONFileBackend`, adapter registry, `SecretStore`,
  and the existing FastAPI playground.
- Added one shared adoption configuration rather than a second identity system.
- Kept the advanced `identity` CLI and added the lifecycle-oriented `idos` CLI.
- Separated replaceable application environments from stable per-user data.
- Required runtime generation provenance before onboarding can report ready.
- Made portability a bounded, scrubbed, integrity-checked, staged transaction.
- Kept onboarding loopback-only instead of pretending to offer secure remote hosting.

## Implemented user commands

```bash
idos init
idos start --open
idos status
idos doctor [--json] [--connectivity]
idos stop
idos export --identity ID --output FILE
idos import FILE [--replace]
idos update --version VERSION
idos uninstall [--purge-data --yes]
```

The installer supports a public Git ref, exact package version, or local built
artifact. It creates a candidate virtual environment, verifies the `idos`
entrypoint, then atomically changes the active link. It never downloads models,
starts a persistent service, binds publicly, or edits `.env`.

## Major files

- `install.sh`: isolated rerunnable installer.
- `cli/idos.py`: lifecycle, doctor, update, uninstall, export, and import.
- `identityos/paths.py`: stable user-owned paths.
- `identityos/adoption.py`: non-secret config, actual providers, adapter
  construction, and verified readiness.
- `identityos/portability.py`: safe export/import.
- `runtime/playground/app.py` and onboarding assets: guided setup and real chat.
- `scripts/verify_clean_install.py`: sanitized, monotonic clean-install evidence.
- `scripts/verify_adoption_journey.py`: separate-process real-provider acceptance.
- `.github/workflows/clean-install.yml`: Python 3.11/3.12 artifact smoke CI.
- `README.md`, `docs/INSTALLATION.md`, and `docs/adoption/`: new user and engineering guidance.
- `evidence/onboarding/`: reviewed run records and measurements.

## Verification results

- Final focused adoption, installer, onboarding, and harness suite: 29 passed.
- Full repository suite: 1789 passed, 43 skipped, and one known baseline
  socket-timeout subprocess exceeded its outer limit; its immediate isolated
  rerun passed. This is not represented as an all-green full-suite run.
- Wheel built in a clean Python 3.12 container and passed `twine check`.
- Wheel contains `idos`, `identity`, onboarding assets, and adoption modules.
- Three independent clean Linux container installations succeeded with disabled pip cache.
- A separate clean container fetched the installer and exact source commit
  directly from GitHub without a clone or mounted artifact, then passed init and doctor.
- Real Ollama inference returned `identity_model_generation` provenance.
- A separate process reopened the stable identity, found prior conversation
  memory, and generated another real response.
- A distinct installation imported the stable ID and memory; a secret canary was absent.
- Dashboard start/status/stop and Chromium rendering succeeded on the Linux host.

Exact run evidence is in `../../evidence/onboarding/README.md` and
`../../evidence/onboarding/runs/`.

## Performance

Cold wheel installation across three final runs was 60.802 seconds minimum,
64.051 seconds median, and 71.590 seconds maximum. The real-provider run reached its
first response in 200.135 seconds from harness start, so the automated
five-minute target was met. The full run including fresh-process inference and
portability took 280.028 seconds. The documented installer path itself took
90.031 seconds on a separate clean container.

These timings exclude provider-account acquisition and Ollama model download.
The model was already present on the host. Automated onboarding is not a human
timing study.

## Security

Credentials live in private secret files and are resolved only in memory.
Configuration and public API responses contain no plaintext key or secret
handle. Browser mutations use SameSite cookie plus CSRF header; Host and Origin
are restricted; framing and sensitive browser features are denied. Exports
scrub known secret values and sensitive fields. Consequential capabilities are
not enabled by onboarding. See `../../evidence/onboarding/security-checks.md`.

## Compatibility

- Linux x86_64: verified for the stated container and host paths.
- macOS: not tested.
- WSL2: not tested.
- Native Windows: unsupported by the shell installer.
- Machine restart: not tested; actual fresh-process restart is verified.

## Remaining limitations

- The artifact is unpublished until an authorized release is made.
- The no-clone `main` installer becomes publicly usable only after merge.
- No cloud provider was verified because no credential was available.
- No human-first-run timing, macOS, WSL2, or machine reboot trial was available.
- Local CPU inference is materially slower than the installation and UI path.
- Remote dashboard hosting is deliberately not supplied; use a separately
  authenticated proxy if needed.

## Recommended next improvements

1. Merge, then repeat the installer test from the public raw URL and Git ref.
2. Run timed unfamiliar-human trials.
3. Add macOS and WSL2 clean runners.
4. Publish signed/tagged artifacts only with explicit release authorization.
5. Add Ollama model discovery and quantify smaller-context performance without
   weakening identity continuity.
