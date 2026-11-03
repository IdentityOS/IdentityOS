"""Bounded, credential-scrubbed, transactional identity portability."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.secrets.store import SecretStore
from identityos.paths import IdentityOSPaths, get_paths
from runtime.persistence import JSONFileBackend

PORTABLE_FORMAT = "identityos-portable-v2"
PORTABLE_SCHEMA_VERSION = 2
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_NAMESPACES = 512
MAX_MEMORIES = 100_000

# Capability installation/configuration is intentionally excluded: installed
# code and machine permissions must be re-authorized at the destination.
_PORTABLE_NAMESPACES = {
    "identity_spec",
    "latest_snapshot",
    "latest",
    "timeline",
    "relationships",
    "goals",
    "intentions",
    "fact_store",
    "_user_profile",
    "debug.index",
}
_PORTABLE_PREFIXES = ("user_profile:", "debug.interaction:", "snapshot:")
_SENSITIVE_KEYS = {
    "api_key", "apikey", "password", "access_token", "refresh_token",
    "authorization", "cookie", "client_secret", "private_key",
}


class PortabilityError(ValueError):
    """An export or import failed validation without changing destination state."""


def export_identity(
    identity_id: str,
    destination: str | Path,
    *,
    paths: IdentityOSPaths | None = None,
) -> dict[str, Any]:
    selected = paths or get_paths()
    backend = JSONFileBackend(str(selected.identity_store))
    if identity_id not in backend.list_identities():
        raise PortabilityError(f"identity {identity_id!r} does not exist")

    secret_store = SecretStore(selected.secrets_dir)
    namespaces: dict[str, Any] = {}
    excluded: list[str] = []
    for namespace in backend.list_namespaces(identity_id):
        if namespace in _PORTABLE_NAMESPACES or namespace.startswith(_PORTABLE_PREFIXES):
            data = backend.load(identity_id, namespace)
            if data is not None:
                namespaces[namespace] = _sanitize(data, secret_store)
        else:
            excluded.append(namespace)

    memories = _sanitize(backend.load_memories(identity_id), secret_store)
    payload: dict[str, Any] = {
        "format": PORTABLE_FORMAT,
        "schema_version": PORTABLE_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "identity_id": identity_id,
        "namespaces": namespaces,
        "memories": memories,
        "exclusions": {
            "namespaces": excluded,
            "always": [
                "provider credentials",
                "installation configuration",
                "installed capability code and machine permissions",
            ],
        },
    }
    payload["sha256"] = _payload_digest(payload)
    serialized = _canonical_json(payload, pretty=True)
    if len(serialized) > MAX_ARCHIVE_BYTES:
        raise PortabilityError("portable archive exceeds the 50 MiB safety limit")
    destination_path = Path(destination).expanduser().resolve(strict=False)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", dir=destination_path.parent,
    )
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination_path)
        destination_path.chmod(0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return {
        "identity_id": identity_id,
        "path": str(destination_path),
        "sha256": hashlib.sha256(serialized).hexdigest(),
        "content_sha256": payload["sha256"],
        "namespace_count": len(namespaces),
        "memory_count": len(memories),
        "excluded_namespaces": excluded,
    }


def inspect_archive(source: str | Path) -> dict[str, Any]:
    payload = _read_archive(source)
    return {
        "identity_id": payload["identity_id"],
        "exported_at": payload["exported_at"],
        "namespace_count": len(payload["namespaces"]),
        "memory_count": len(payload["memories"]),
        "content_sha256": payload["sha256"],
    }


def import_identity(
    source: str | Path,
    *,
    paths: IdentityOSPaths | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Validate in a staging backend and atomically install one identity.

    Existing identity data is never merged implicitly.  ``replace=True`` keeps
    a recoverable backup until the staged directory has been committed.
    """
    selected = (paths or get_paths()).ensure()
    payload = _read_archive(source)
    identity_id = payload["identity_id"]
    destination = JSONFileBackend(str(selected.identity_store))
    exists = identity_id in destination.list_identities()
    if exists and not replace:
        raise PortabilityError(
            f"identity {identity_id!r} already exists; use explicit replacement or another installation"
        )

    stage_parent = Path(tempfile.mkdtemp(prefix=".import-", dir=destination.root))
    stage_root = stage_parent / "store"
    stage = JSONFileBackend(str(stage_root))
    backup: Path | None = None
    destination_dir: Path | None = None
    try:
        for namespace, data in payload["namespaces"].items():
            stage.save(identity_id, namespace, data)
        for memory in payload["memories"]:
            stage.save_memory(identity_id, memory)

        # A portable archive without a loadable identity is not successful.
        from runtime.orchestrator import IdentityRuntime

        verifier = IdentityRuntime(storage=stage)
        try:
            verifier.load_persisted()
            spec = verifier.load(identity_id)
            if spec is None or spec.id != identity_id:
                raise PortabilityError("archive does not contain a loadable identity snapshot")
        finally:
            verifier.shutdown()

        staged_dir = stage._identity_dir(identity_id)  # confined, hashed backend path
        destination_dir = destination._identity_dir(identity_id)
        if destination_dir.exists():
            backup = destination.root / f".replace-backup-{uuid.uuid4().hex}"
            os.replace(destination_dir, backup)
        try:
            os.replace(staged_dir, destination_dir)
        except BaseException:
            if backup is not None and backup.exists() and not destination_dir.exists():
                os.replace(backup, destination_dir)
            raise

        # Verify from the real destination in a new runtime before deleting the
        # replacement backup. A failed verification rolls back automatically.
        verifier = IdentityRuntime(storage=JSONFileBackend(str(selected.identity_store)))
        try:
            verifier.load_persisted()
            spec = verifier.load(identity_id)
            if spec is None or spec.id != identity_id:
                raise PortabilityError("destination verification failed after import")
        except BaseException:
            if destination_dir.exists():
                failed = destination.root / f".failed-import-{uuid.uuid4().hex}"
                os.replace(destination_dir, failed)
                shutil.rmtree(failed, ignore_errors=True)
            if backup is not None and backup.exists():
                os.replace(backup, destination_dir)
            raise
        finally:
            verifier.shutdown()
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    except PortabilityError:
        raise
    except Exception as exc:
        raise PortabilityError(f"identity import failed without changing existing data: {exc}") from exc
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)

    return {
        "identity_id": identity_id,
        "namespace_count": len(payload["namespaces"]),
        "memory_count": len(payload["memories"]),
        "content_sha256": payload["sha256"],
        "replaced": exists,
        "verified_after_restart": True,
    }


def _read_archive(source: str | Path) -> dict[str, Any]:
    unresolved = Path(source).expanduser()
    if unresolved.is_symlink():
        raise PortabilityError("portable archive must be a regular, non-symlink file")
    path = unresolved.resolve(strict=True)
    if not path.is_file():
        raise PortabilityError("portable archive must be a regular, non-symlink file")
    size = path.stat().st_size
    if size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise PortabilityError("portable archive is empty or exceeds the 50 MiB safety limit")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PortabilityError(f"portable archive is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PortabilityError("portable archive root must be an object")
    if payload.get("format") != PORTABLE_FORMAT or payload.get("schema_version") != PORTABLE_SCHEMA_VERSION:
        raise PortabilityError("unsupported portable archive format or schema")
    identity_id = payload.get("identity_id")
    if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 1024:
        raise PortabilityError("portable archive identity_id is invalid")
    namespaces = payload.get("namespaces")
    memories = payload.get("memories")
    if not isinstance(namespaces, dict) or len(namespaces) > MAX_NAMESPACES:
        raise PortabilityError("portable archive namespaces are invalid or excessive")
    if not isinstance(memories, list) or len(memories) > MAX_MEMORIES:
        raise PortabilityError("portable archive memories are invalid or excessive")
    for namespace, value in namespaces.items():
        if (
            not isinstance(namespace, str)
            or not (namespace in _PORTABLE_NAMESPACES or namespace.startswith(_PORTABLE_PREFIXES))
            or not isinstance(value, dict)
        ):
            raise PortabilityError(f"portable archive contains unsupported namespace {namespace!r}")
    if any(not isinstance(item, dict) for item in memories):
        raise PortabilityError("portable archive contains a non-object memory")
    _bounded_structure(payload)
    supplied = payload.get("sha256")
    if not isinstance(supplied, str) or supplied != _payload_digest(payload):
        raise PortabilityError("portable archive integrity check failed")
    return payload


def _payload_digest(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "sha256"}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _canonical_json(value: Any, *, pretty: bool = False) -> bytes:
    kwargs = {"sort_keys": True, "ensure_ascii": False, "default": str}
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + ("\n" if pretty else "")).encode("utf-8")


def _sanitize(value: Any, store: SecretStore) -> Any:
    if isinstance(value, str):
        return store.scrub(value)
    if isinstance(value, list):
        return [_sanitize(item, store) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, store) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_api_key"):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _sanitize(item, store)
        return result
    return value


def _bounded_structure(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if depth > 64:
        raise PortabilityError("portable archive nesting is excessive")
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > 1_000_000:
        raise PortabilityError("portable archive contains too many values")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 4096:
                raise PortabilityError("portable archive contains an invalid key")
            _bounded_structure(item, depth=depth + 1, count=count)
    elif isinstance(value, list):
        for item in value:
            _bounded_structure(item, depth=depth + 1, count=count)
