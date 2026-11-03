from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.base import BaseAdapter
from core.identity import create_identity
from core.secrets.store import SecretStore
from identityos.adoption import (
    AdoptionConfigError,
    build_configured_adapter,
    configure_provider,
    initialize,
    load_config,
    public_status,
    record_verification,
    save_config,
)
from identityos.paths import get_paths
from identityos.portability import PortabilityError, export_identity, import_identity
from runtime.orchestrator import IdentityRuntime, InteractionRequest
from runtime.persistence import JSONFileBackend


class _ObservedAdapter(BaseAdapter):
    def generate(self, context, user_input, identity, **kwargs):
        return f"Observed response to: {user_input}"


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("IDENTITYOS_HOME", str(tmp_path / "identityos"))
    for name in (
        "IDENTITYOS_DATA_DIR",
        "IDENTITYOS_CONFIG_DIR",
        "IDENTITYOS_STATE_DIR",
        "IDENTITYOS_LOG_DIR",
        "IDENTITY_STORE_PATH",
        "IDENTITY_SECRET_STORE_DIR",
        "IDENTITYOS_RUNTIME_DIR",
        "IDENTITYOS_CONFIG_FILE",
        "IDENTITYOS_PID_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    return get_paths()


def test_paths_and_initialization_are_stable_private_and_idempotent(isolated_home):
    paths = isolated_home
    first = initialize(paths)
    second = initialize(paths)

    assert first.installation_id == second.installation_id
    assert paths.identity_store == paths.data_dir / "identities"
    assert paths.config_file.stat().st_mode & 0o777 == 0o600
    assert paths.secrets_dir.stat().st_mode & 0o777 == 0o700
    assert json.loads(paths.config_file.read_text())["schema_version"] == 1


def test_plaintext_credentials_cannot_be_written_to_config(isolated_home):
    config = initialize(isolated_home)
    config.onboarding["api_key"] = "should-not-persist"

    with pytest.raises(AdoptionConfigError, match="plaintext credential"):
        save_config(config, isolated_home)
    assert "should-not-persist" not in isolated_home.config_file.read_text()


def test_cloud_provider_uses_secret_store_and_public_status_omits_handle(isolated_home):
    configure_provider(
        "groq",
        model="model-for-test",
        api_key="test-secret-value",
        paths=isolated_home,
    )

    config_text = isolated_home.config_file.read_text()
    assert "test-secret-value" not in config_text
    assert SecretStore(isolated_home.secrets_dir).get("providers/groq/api_key") == "test-secret-value"
    status = public_status(isolated_home)
    assert "secret_handle" not in status["provider"]
    adapter = build_configured_adapter(isolated_home)
    assert adapter.model == "model-for-test"


def test_only_runtime_model_provenance_can_mark_onboarding_ready(isolated_home):
    configure_provider("ollama", model="unit-test", paths=isolated_home)

    with pytest.raises(AdoptionConfigError, match="actual model-generated"):
        record_verification(
            identity_id="identity-a",
            provenance={"generation_mode": "unavailable", "provider": "", "model": ""},
            response_digest="a" * 64,
            paths=isolated_home,
        )
    assert load_config(isolated_home).onboarding["ready"] is False

    record_verification(
        identity_id="identity-a",
        provenance={
            "generation_mode": "identity_model_generation",
            "provider": "OllamaAdapter",
            "model": "unit-test",
            "latency_ms": 12,
        },
        response_digest="a" * 64,
        paths=isolated_home,
    )
    assert load_config(isolated_home).onboarding["ready"] is True


def test_portability_scrubs_secrets_and_survives_a_fresh_runtime(monkeypatch, tmp_path):
    source_home = tmp_path / "source"
    monkeypatch.setenv("IDENTITYOS_HOME", str(source_home))
    source_paths = get_paths().ensure()
    initialize(source_paths)
    secret = "canary-secret-never-export"
    SecretStore(source_paths.secrets_dir).put("providers/test/api_key", secret)

    source_backend = JSONFileBackend(str(source_paths.identity_store))
    runtime = IdentityRuntime(storage=source_backend, adapter=_ObservedAdapter(model="observed-model"))
    spec = create_identity(name="Portable Ada", identity_id="portable-ada")
    runtime.register(spec)
    response = runtime.process(InteractionRequest(
        identity_id=spec.id,
        user_id="portable-user",
        user_input=f"Remember this transfer marker; credential canary is {secret}",
    ))
    assert response.metadata["generation_provenance"]["generation_mode"] == "identity_model_generation"
    runtime.shutdown()

    archive = tmp_path / "portable-ada.identityos.json"
    exported = export_identity(spec.id, archive, paths=source_paths)
    assert exported["memory_count"] >= 1
    assert secret not in archive.read_text()
    assert "[REDACTED secret://providers/test/api_key]" in archive.read_text()

    destination_home = tmp_path / "destination"
    monkeypatch.setenv("IDENTITYOS_HOME", str(destination_home))
    destination_paths = get_paths().ensure()
    initialize(destination_paths)
    imported = import_identity(archive, paths=destination_paths)
    assert imported["verified_after_restart"] is True

    fresh = IdentityRuntime(storage=JSONFileBackend(str(destination_paths.identity_store)))
    try:
        assert fresh.load_persisted() == 1
        loaded = fresh.load(spec.id)
        assert loaded is not None
        assert loaded.name == "Portable Ada"
        memories = fresh.memory_store.by_identity(spec.id)
        assert any("transfer marker" in memory.content for memory in memories)
        assert all(secret not in memory.content for memory in memories)
    finally:
        fresh.shutdown()


def test_invalid_or_tampered_import_never_replaces_existing_data(monkeypatch, tmp_path):
    monkeypatch.setenv("IDENTITYOS_HOME", str(tmp_path / "destination"))
    paths = get_paths().ensure()
    initialize(paths)
    backend = JSONFileBackend(str(paths.identity_store))
    runtime = IdentityRuntime(storage=backend)
    runtime.register(create_identity(name="Existing", identity_id="existing"))
    runtime.shutdown()

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"format":"identityos-portable-v2","sha256":"wrong"}')
    with pytest.raises(PortabilityError):
        import_identity(invalid, paths=paths, replace=True)

    fresh = IdentityRuntime(storage=JSONFileBackend(str(paths.identity_store)))
    try:
        assert fresh.load("existing").name == "Existing"
    finally:
        fresh.shutdown()


def test_interrupted_replacement_rolls_back_existing_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("IDENTITYOS_HOME", str(tmp_path / "source"))
    source_paths = get_paths().ensure()
    initialize(source_paths)
    source_runtime = IdentityRuntime(storage=JSONFileBackend(str(source_paths.identity_store)))
    source_runtime.register(create_identity(name="Incoming", identity_id="same-id"))
    source_runtime.shutdown()
    archive = tmp_path / "same-id.identityos.json"
    export_identity("same-id", archive, paths=source_paths)

    monkeypatch.setenv("IDENTITYOS_HOME", str(tmp_path / "destination"))
    destination_paths = get_paths().ensure()
    initialize(destination_paths)
    destination_runtime = IdentityRuntime(storage=JSONFileBackend(str(destination_paths.identity_store)))
    destination_runtime.register(create_identity(name="Existing", identity_id="same-id"))
    destination_runtime.shutdown()

    import identityos.portability as portability

    real_replace = portability.os.replace
    backend = JSONFileBackend(str(destination_paths.identity_store))
    destination_dir = backend._identity_dir("same-id")
    backup_created = False

    def interrupted_replace(source, destination):
        nonlocal backup_created
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path == destination_dir and destination_path.name.startswith(".replace-backup-"):
            backup_created = True
        elif backup_created and destination_path == destination_dir and ".import-" in str(source_path):
            raise OSError("simulated interruption before staged commit")
        return real_replace(source, destination)

    monkeypatch.setattr(portability.os, "replace", interrupted_replace)
    with pytest.raises(PortabilityError, match="without changing existing data"):
        import_identity(archive, paths=destination_paths, replace=True)

    fresh = IdentityRuntime(storage=JSONFileBackend(str(destination_paths.identity_store)))
    try:
        assert fresh.load("same-id").name == "Existing"
    finally:
        fresh.shutdown()
