"""Stable, user-owned filesystem locations for an IdentityOS installation.

The legacy developer CLI historically used paths relative to the checkout.  The
developer-first CLI and browser onboarding use these locations instead so an
identity does not change merely because a command was run from another folder.
Every location can be overridden for isolated tests and portable installations.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _absolute_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value).expanduser() if value else default
    return path.resolve(strict=False)


@dataclass(frozen=True)
class IdentityOSPaths:
    data_dir: Path
    config_dir: Path
    state_dir: Path
    logs_dir: Path
    identity_store: Path
    secrets_dir: Path
    runtime_dir: Path
    config_file: Path
    pid_file: Path

    def ensure(self) -> "IdentityOSPaths":
        """Create private user directories without touching identity contents."""
        for path in (
            self.data_dir,
            self.config_dir,
            self.state_dir,
            self.logs_dir,
            self.identity_store,
            self.secrets_dir,
            self.runtime_dir,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                path.chmod(0o700)
            except OSError:
                # Some mounted/container filesystems do not implement chmod.
                # Callers that require strict permissions validate separately.
                pass
        return self


def get_paths() -> IdentityOSPaths:
    """Return platform-appropriate, overridable paths without creating them.

    ``IDENTITYOS_HOME`` is a convenient all-in-one root for tests and portable
    installs.  The more specific variables win when both are present.
    """
    home_override = os.environ.get("IDENTITYOS_HOME")
    if home_override:
        base = Path(home_override).expanduser().resolve(strict=False)
        default_data = base / "data"
        default_config = base / "config"
        default_state = base / "state"
    elif sys.platform == "darwin":
        user_home = Path.home()
        default_data = user_home / "Library" / "Application Support" / "IdentityOS"
        default_config = user_home / "Library" / "Application Support" / "IdentityOS" / "config"
        default_state = user_home / "Library" / "Logs" / "IdentityOS"
    else:
        user_home = Path.home()
        default_data = Path(os.environ.get("XDG_DATA_HOME", user_home / ".local" / "share")) / "identityos"
        default_config = Path(os.environ.get("XDG_CONFIG_HOME", user_home / ".config")) / "identityos"
        default_state = Path(os.environ.get("XDG_STATE_HOME", user_home / ".local" / "state")) / "identityos"

    data_dir = _absolute_env("IDENTITYOS_DATA_DIR", default_data)
    config_dir = _absolute_env("IDENTITYOS_CONFIG_DIR", default_config)
    state_dir = _absolute_env("IDENTITYOS_STATE_DIR", default_state)
    logs_dir = _absolute_env("IDENTITYOS_LOG_DIR", state_dir / "logs")
    identity_store = _absolute_env(
        "IDENTITY_STORE_PATH",
        data_dir / "identities",
    )
    secrets_dir = _absolute_env(
        "IDENTITY_SECRET_STORE_DIR",
        config_dir / "secrets",
    )
    runtime_dir = _absolute_env("IDENTITYOS_RUNTIME_DIR", state_dir / "run")
    config_file = _absolute_env("IDENTITYOS_CONFIG_FILE", config_dir / "config.json")
    pid_file = _absolute_env("IDENTITYOS_PID_FILE", runtime_dir / "playground.pid")

    return IdentityOSPaths(
        data_dir=data_dir,
        config_dir=config_dir,
        state_dir=state_dir,
        logs_dir=logs_dir,
        identity_store=identity_store,
        secrets_dir=secrets_dir,
        runtime_dir=runtime_dir,
        config_file=config_file,
        pid_file=pid_file,
    )


def environment_for_paths(paths: IdentityOSPaths | None = None) -> dict[str, str]:
    """Environment values consumed by existing runtime components."""
    selected = paths or get_paths()
    return {
        "IDENTITYOS_DATA_DIR": str(selected.data_dir),
        "IDENTITYOS_CONFIG_DIR": str(selected.config_dir),
        "IDENTITYOS_STATE_DIR": str(selected.state_dir),
        "IDENTITYOS_LOG_DIR": str(selected.logs_dir),
        "IDENTITY_STORE_PATH": str(selected.identity_store),
        "IDENTITY_SECRET_STORE_DIR": str(selected.secrets_dir),
        "IDENTITYOS_CONFIG_FILE": str(selected.config_file),
        "IDENTITYOS_PID_FILE": str(selected.pid_file),
    }
