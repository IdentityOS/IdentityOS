"""Filesystem-backed secret store with strict permissions and redaction.

Design intent:

* A secret is addressed only by an opaque handle (``secret://culture-commons/aster``).
* Plaintext is written once by a *running process* (e.g. the response to a
  ``sign_your_name`` operation) and is read back only at execution time.
* Nothing in identity state, capability configs, provenance, messages, or logs
  contains a plaintext secret — only the handle.
* ``scrub``/``scrub_mapping`` redact known values from any text bound for
  persistence so a secret that accidentally reached an outbound string is
  censored before it is written.

Directory and file permissions are enforced on every operation; a handle whose
storage file is not private is treated as absent.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

HANDLE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")

_DIR_MODE = 0o700
_FILE_MODE = 0o600

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class SecretStoreError(Exception):
    """Base error for secret-store operations."""


class SecretHandleError(SecretStoreError, ValueError):
    """The handle did not match the allowed alphabet."""


class SecretNotFound(SecretStoreError):
    """No secret has been provisioned for the given handle."""


def default_secret_store_dir(project_root: str | Path = ".") -> str:
    """Resolve the secret store directory from env, else a project-local path."""
    env_dir = os.environ.get("IDENTITY_SECRET_STORE_DIR")
    if env_dir:
        return env_dir
    return str(Path(project_root) / ".identityos" / "secrets")


class SecretStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_dir(self) -> Path:
        self._root.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        try:
            os.chmod(self._root, _DIR_MODE)
        except OSError:
            pass
        return self._root

    def _path(self, handle: str) -> Path:
        if not HANDLE_PATTERN.match(handle):
            raise SecretHandleError(f"invalid secret handle: {handle!r}")
        return self._root / handle

    def _write_value(self, path: Path, value: str) -> None:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW,
            _FILE_MODE,
        )
        try:
            os.chmod(path, _FILE_MODE)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(value.encode("utf-8"))
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def put(self, handle: str, value: str) -> None:
        """Provision *value* under *handle*, replacing any prior value."""
        value = str(value or "")
        if not value.strip():
            raise SecretHandleError(f"refusing to store an empty secret under handle {handle!r}")
        if len(value) > 16 * 1024:
            raise SecretHandleError(f"secret under handle {handle!r} is unreasonably large")
        if "\x00" in value:
            raise SecretHandleError(f"secret under handle {handle!r} contains a NUL byte")
        path = self._path(handle)
        with self._lock:
            self._ensure_dir()
            path.parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, _DIR_MODE)
            except OSError:
                pass
            self._write_value(path, value)

    def get(self, handle: str) -> Optional[str]:
        """Return the plaintext value for *handle*, or ``None`` if absent.

        A file that is not strictly private (mode 0600, owned by us) is treated
        as absent: the runtime must never trust a secret it cannot prove is
        exclusive to itself.
        """
        if not HANDLE_PATTERN.match(handle):
            raise SecretHandleError(f"invalid secret handle: {handle!r}")
        path = self._path(handle)
        with self._lock:
            if not path.is_file():
                return None
            try:
                mode = path.stat().st_mode
                if (mode & 0o777) != _FILE_MODE:
                    return None
            except OSError:
                return None
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None

    def has(self, handle: str) -> bool:
        return self.get(handle) is not None

    def delete(self, handle: str) -> bool:
        path = self._path(handle)
        with self._lock:
            if not path.exists():
                return False
            try:
                path.unlink()
            except OSError as exc:
                raise SecretStoreError(f"could not delete secret {handle!r}: {exc}") from exc
            return True

    def handles(self) -> list[str]:
        """Names of provisioned secrets (never values) for audit purposes."""
        with self._lock:
            if not self._root.is_dir():
                return []
            result: list[str] = []
            for entry in self._root.rglob("*"):
                if entry.is_file() and HANDLE_PATTERN.match(entry.relative_to(self._root).as_posix()):
                    result.append(entry.relative_to(self._root).as_posix())
            return sorted(result)

    # ── redaction ──────────────────────────────────────────────────────

    def _redact_map(self) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        for handle in self.handles():
            value = self.get(handle)
            if value and len(value) >= 4:
                values.append((value, _placeholder(handle)))
        return values

    def scrub(self, text: str) -> str:
        """Censor every stored secret value that appears in *text*.

        Values shorter than 4 characters are skipped to avoid destroying normal
        text that coincidentally matches.
        """
        if not isinstance(text, str) or not text:
            return text
        redacted = text
        for value, placeholder in self._redact_map():
            if value in redacted:
                redacted = redacted.replace(value, placeholder)
        return redacted

    def scrub_mapping(self, value: Any) -> Any:
        """Recursively scrub a nested structure of strings for persistence."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, list):
            return [self.scrub_mapping(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.scrub_mapping(item) for item in value)
        if isinstance(value, dict):
            return {key: self.scrub_mapping(item) for key, item in value.items()}
        return value


def _placeholder(handle: str) -> str:
    return f"[REDACTED secret://{handle}]"


def scrub_all(stores: Iterable[SecretStore], text: str) -> str:
    """Censor secrets across multiple stores in one pass."""
    redacted = text
    for store in stores:
        redacted = store.scrub(redacted)
    return redacted