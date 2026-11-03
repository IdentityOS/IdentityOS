# Installing IdentityOS

IdentityOS installs into an isolated Python environment owned by your user. It
does not require administrator access, a repository clone, a hand-written
`.env` file, or a centralized IdentityOS account.

## Supported environments

| Environment | Status |
|---|---|
| Linux with Python 3.11+ | Implementation target; see committed evidence for exact verified environments |
| macOS with Python 3.11+ | Designed for, not yet independently verified |
| Windows through WSL2 | Designed for as a Linux environment, not yet independently verified |
| Native Windows | Not currently supported by `install.sh` |

Python must provide the standard `venv` module. On distributions that split it
into a separate operating-system package, the installer reports the failure and
leaves the active IdentityOS installation unchanged.

## Install without cloning

After this installer reaches the public `main` branch:

```bash
curl -fsSL https://raw.githubusercontent.com/IdentityOS/IdentityOS/main/install.sh | bash
```

That convenience command installs the current public `main` archive. For a
reproducible deployment, download the installer from and install the same exact
tag or commit:

```bash
ref=<tag-or-full-commit-sha>
curl -fsSL "https://raw.githubusercontent.com/IdentityOS/IdentityOS/$ref/install.sh" -o /tmp/identityos-install.sh
bash /tmp/identityos-install.sh --ref "$ref"
```

The installer prints the downloaded source archive SHA-256. A release package
may instead be selected with `--version X.Y.Z`, but this is valid only after
that exact source release has actually been published to the configured Python
package index.

Until a release containing the developer-first installer is actually published,
use a locally built artifact and describe it as an unpublished-artifact test:

```bash
bash install.sh --artifact /absolute/path/to/identityos-X.Y.Z-py3-none-any.whl
```

The installer builds each version in a new directory and changes the active
link only after the artifact installs successfully and the `idos` command can be
imported. Re-running it is safe. A failed attempt does not replace the working
version.

Default locations on Linux are:

- Managed Python environments: `~/.local/share/identityos-runtime/`
- Command links: `~/.local/bin/idos` and `~/.local/bin/identity`
- Identity data: `~/.local/share/identityos/`
- Configuration and secrets: `~/.config/identityos/`
- Runtime state and logs: `~/.local/state/identityos/`

Set `IDENTITYOS_HOME` to put data, configuration, and state under one root for a
portable or test installation. Individual path environment variables are
documented by `identityos.paths` and take precedence.

## Initialize and start

```bash
idos init
idos start --open
```

`idos init` is idempotent. It creates private user directories and a minimal
configuration without overwriting identities. `idos start` runs in the current
terminal and binds to `127.0.0.1` by default. It does not download a model or
enable a background service.

To start a background process explicitly:

```bash
idos start --detach --open
idos status
idos stop
```

The onboarding dashboard rejects non-loopback binds. Deliberate remote access
must be placed behind a separately configured authenticated reverse proxy; the
installer does not expose one automatically.

## Diagnose a problem

```bash
idos doctor
idos doctor --json
```

Doctor checks the platform, Python version, configuration, writable storage,
dashboard state, port availability, and configured providers. It never prints
credential values. Network provider checks are opt-in:

```bash
idos doctor --connectivity
```

Common recovery actions:

- **Port already in use:** `idos start --port 8001`
- **Invalid configuration:** repair the path reported by `idos doctor`; identity
  contents are stored separately and are not deleted.
- **Dashboard failed to start:** inspect the log path printed by `idos start`.
- **No provider configured:** open onboarding and select a local or cloud
  provider. Configuration alone is not reported as successful inference.

## Update

Updates require an exact version or a local artifact:

```bash
idos update --version X.Y.Z
idos update --artifact /absolute/path/to/identityos-X.Y.Z-py3-none-any.whl
```

Use `--dry-run` to print the operation without changing the installation. If
IdentityOS was installed through another package manager, `idos update` refuses
to guess and tells you to use that same manager.

## Move an identity

```bash
idos export --identity <identity-id> --output identity.identityos.json
idos import identity.identityos.json
```

Exports contain supported durable identity state and conversation memory. They
exclude provider credentials, installation configuration, capability code, and
machine permissions. Import is bounded and transactional: it verifies integrity
and loads the identity in a staging runtime before atomically committing it.
Existing identities are not overwritten unless `--replace` is explicit.

## Uninstall

```bash
idos uninstall
```

This removes the managed environments and command link but preserves identities,
configuration, and secrets. Permanent data removal is a separate, explicit
operation:

```bash
idos uninstall --purge-data --yes
```

Export important identities before purging. Data removal cannot be undone.

## Security properties

- No credential is accepted as an installer argument.
- No `.env` file is required.
- No model is silently downloaded.
- No service is enabled at login or boot.
- No public network interface is used by default.
- Identity data is separate from replaceable application environments.
- Uninstall preserves user data unless both `--purge-data` and `--yes` are used.
