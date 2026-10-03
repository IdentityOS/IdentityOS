"""test_secrets_store.py — SecretStore isolation, permissions, redaction, persistence."""

from __future__ import annotations

import os
import stat

import pytest

from core.secrets.store import (
    HANDLE_PATTERN,
    SecretHandleError,
    SecretNotFound,
    SecretStore,
    default_secret_store_dir,
)


def test_put_and_get_roundtrip(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.put("culture-commons/aster", "s3cret-value-x")
    assert store.get("culture-commons/aster") == "s3cret-value-x"
    assert store.has("culture-commons/aster")


def test_handles_never_include_values(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.put("srv/key", "a-long-secret-value")
    assert store.handles() == ["srv/key"]
    assert "a-long-secret-value" not in store.handles()[0]


def test_file_permissions_are_private(tmp_path):
    root = tmp_path / "secrets"
    store = SecretStore(root)
    store.put("handle/one", "value-long-enough")
    file_path = root / "handle" / "one"
    assert stat.S_IMODE(file_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "handle").stat().st_mode) == 0o700


def test_untrusted_mode_treated_as_absent(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.put("h", "a-generously-long-value")
    path = tmp_path / "secrets" / "h"
    os.chmod(path, 0o644)
    assert store.get("h") is None


def test_invalid_handles_rejected():
    store = SecretStore("/tmp/nowhere")
    with pytest.raises(SecretHandleError):
        store.put("../../etc/passwd", "x" * 10)
    with pytest.raises(SecretHandleError):
        store.get("UPPER/Case")


def test_refuses_empty_and_huge_and_nul(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    with pytest.raises(SecretHandleError):
        store.put("h", "")
    with pytest.raises(SecretHandleError):
        store.put("h", "x" * (16 * 1024 + 1))
    with pytest.raises(SecretHandleError):
        store.put("h", "abc\x00def" + "x" * 12)


def test_delete_returns_report(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.put("h", "value-long-enough")
    assert store.delete("h") is True
    assert store.has("h") is False
    assert store.delete("h") is False


def test_scrub_redacts_stored_secret_values(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    secret = "standingSecret-9f2b"
    store.put("culture-commons/aster", secret)
    out = store.scrub(f"leaking {secret} everywhere")
    assert secret not in out
    assert "secret://culture-commons/aster" in out
    assert "leaking" in out


def test_scrub_skips_short_values(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.put("h", "hi")
    assert store.scrub("say hi there") == "say hi there"


def test_scrub_mapping_recurses(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    secret = "deep-value-token-42"
    store.put("h", secret)
    mapping = {"nested": {"text": f"token {secret}"}, "items": [secret, "plain"]}
    out = store.scrub_mapping(mapping)
    blob = repr(out)
    assert secret not in blob
    assert "plain" in blob


def test_persistence_across_processes(tmp_path):
    root = tmp_path / "secrets"
    SecretStore(root).put("culture-commons/aster", "persist-me-secret-88")
    fresh = SecretStore(root)
    assert fresh.get("culture-commons/aster") == "persist-me-secret-88"


def test_default_dir_uses_env_when_set(monkeypatch):
    monkeypatch.setenv("IDENTITY_SECRET_STORE_DIR", "/tmp/env-secrets-xyz")
    assert default_secret_store_dir(".") == "/tmp/env-secrets-xyz"