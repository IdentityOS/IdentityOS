# Independent installation verification

IdentityOS installation claims must be backed by a fresh environment and a
built artifact. Running from a repository checkout proves contributor setup; it
does not prove that a new user can install the package.

The verification harness installs one wheel into a newly created virtual
environment, removes the repository from `PYTHONPATH`, disables the pip cache,
runs requested checks without a shell, and writes sanitized evidence. Each run
has a unique ID and must use a new environment.

## Build and run

Build the wheel first:

```bash
python -m pip install --no-cache-dir build
python -m build --wheel
```

Until that wheel is uploaded to a public index, describe the result as an
**unpublished-artifact test**:

```bash
wheel="$(find dist -maxdepth 1 -type f -name '*.whl' -print -quit)"
python scripts/verify_clean_install.py \
  --wheel "$wheel" \
  --publication-status unpublished \
  --artifact-source local-build \
  --base-image 'python:3.12-slim@sha256:<digest>' \
  --network-state online \
  --reinstall-check \
  --smoke-command "{python} -c 'import identityos; print(identityos.__file__)'" \
  --smoke-command "idos --help" \
  --smoke-command "identity --help" \
  --onboarding-command "idos init --json" \
  --inference-mode none
```

Commands support `{python}`, `{venv}`, and `{run_dir}` placeholders. They are
parsed as argument lists and are never passed to a shell.

Browser onboarding is covered by the deterministic API/UI regression suite.
`idos init` in the artifact harness proves isolated configuration creation, not
human usability or real inference. A mocked integration run can add a dedicated
test driver, but it must remain labeled mocked:

```bash
python scripts/verify_clean_install.py \
  --wheel "$wheel" \
  --publication-status unpublished \
  --onboarding-command "idos init --json" \
  --smoke-command "idos doctor --json" \
  --portability-command "{python} /path/to/isolated-portability-check.py" \
  --inference-mode mocked \
  --inference-command "{python} /path/to/deterministic-onboarding-driver.py"
```

The example is a deterministic regression run. Its `mocked` label prevents it
from satisfying the genuine-inference acceptance criterion.

## Real-provider runs

Real inference requires an actual provider request and runtime-observed result.
Allow only the necessary credential variable into child processes; its value is
removed from commands and output before evidence is written:

```bash
python scripts/verify_clean_install.py \
  --wheel "$wheel" \
  --publication-status unpublished \
  --credential-env GROQ_API_KEY \
  --provider groq \
  --model '<tested-model>' \
  --inference-mode real \
  --inference-command "{python} /path/to/real-provider-acceptance.py"
```

Do not put credential values in command-line arguments. The harness strips
credential-shaped environment variables by default and only passes names listed
with `--credential-env`. It never records their values. Output is also redacted
using the exact values present in those variables.

For `real` mode, command success is insufficient. The final stdout line must be
a JSON object containing runtime-observed `generation_provenance` (or
`provenance`) with `generation_mode: identity_model_generation`, a non-empty
provider and model, plus a SHA-256 digest in `response_sha256`. The harness
records only the digest and provenance metadata. A mocked response may verify
state transitions but cannot be reported as real inference.

## Failure and recovery hooks

Expected failure checks must return nonzero; recovery checks must return zero.
This makes controlled invalid-key, timeout, unavailable-model, invalid-archive,
and port-conflict scenarios observable without weakening the main journey:

```bash
python scripts/verify_clean_install.py \
  --wheel "$wheel" \
  --expected-failure-command "invalid-config::{python} -c 'raise SystemExit(7)'" \
  --recovery-command "fresh-config::idos doctor --json" \
  --reinstall-check
```

Use synthetic credentials and local provider stubs for deterministic `401`,
`429`, and timeout regression tests. Test genuine provider connectivity
separately. For offline verification, disable networking outside the harness
(for example with a disposable container's `--network none`) and record
`--network-state offline`.

The harness currently provides hooks; it does not pretend to simulate an
operating system, machine reboot, network outage, or provider response itself.

## Evidence schema

One successful invocation creates:

```text
evidence/onboarding/runs/<run-id>/
  environment.json
  baseline.txt
  artifact.json
  commands.jsonl
  installation.log
  inference-verification.json
  restart-verification.json
  portability-verification.json
  onboarding-events.jsonl
  timings.json
  security-checks.json
  failures.md
  run-summary.json
```

- `environment.json` records OS, architecture, Python, isolation, network/cache
  declarations, and credential variable names only.
- `artifact.json` records filename, byte size, SHA-256, source, and publication
  status.
- `commands.jsonl` records sanitized argv, UTC timestamps, monotonic start/end,
  elapsed time, exit status, timeout state, and expected outcome.
- `installation.log` contains sanitized stdout and stderr.
- `inference-verification.json` distinguishes `none`, `mocked`, and `real`.
- `timings.json` separates bootstrap, installation, launch, onboarding,
  inference, restart, portability, and total duration when those stages run.
- `security-checks.json` records what the harness did and did not verify.
- `failures.md` is always present and never silently discards a failure.

Generated run directories are ignored by Git. Preserve chosen run directories
through CI artifacts or copy reviewed, sanitized evidence into a deliberate
delivery commit. Do not commit caches, virtual environments, API keys, or raw
provider transcripts.

## Independent-run rules

For performance reporting, run at least three new environments and include
failed attempts. Do not reuse a venv, installation directory, or pip cache and
call the attempts independent. Report minimum, median, and maximum from the raw
stage records. Record model downloads and initialization separately from normal
package installation.

A Linux container can verify a Linux artifact path. It does not verify macOS,
WSL2, native Windows, system-service behavior, or persistence through a machine
restart. Mark those platforms and behaviors as untested until executed there.
