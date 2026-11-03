"""Shared installation and onboarding configuration.

This module is deliberately UI-agnostic.  Both ``idos`` and the playground use
the same versioned file, provider catalogue, adapter construction, and secret
handles.  Plaintext credentials never enter the JSON configuration.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from core.secrets.store import SecretStore
from identityos.paths import IdentityOSPaths, get_paths

CONFIG_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024


class AdoptionConfigError(ValueError):
    """The local adoption configuration is invalid or unsafe."""


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    display_name: str
    adapter: str
    default_model: str
    default_base_url: str | None
    local: bool
    requires_secret: bool
    description: str

    def public_dict(self) -> dict[str, Any]:
        if self.name == "ollama":
            detected = bool(shutil.which("ollama"))
        elif self.name == "anthropic":
            try:
                detected = importlib.util.find_spec("anthropic") is not None
            except (ImportError, ValueError):
                detected = False
        else:
            detected = True
        return {
            "name": self.name,
            "display_name": self.display_name,
            "default_model": self.default_model,
            "default_base_url": self.default_base_url,
            "local": self.local,
            "requires_secret": self.requires_secret,
            "detected": detected,
            "description": self.description,
        }


# Every entry maps to an adapter the runtime can actually construct. Gemini is
# intentionally an OpenAI-compatible configuration, matching existing runtime
# support rather than pretending there is a separate adapter.
PROVIDERS: dict[str, ProviderDefinition] = {
    "ollama": ProviderDefinition(
        "ollama", "Ollama (local)", "ollama", "llama3.2",
        "http://127.0.0.1:11434/v1", True, False,
        "Runs on your machine. Models are never downloaded automatically.",
    ),
    "openai": ProviderDefinition(
        "openai", "OpenAI", "openai", "gpt-4o-mini", None, False, True,
        "Cloud inference; relevant conversation context is sent to OpenAI.",
    ),
    "anthropic": ProviderDefinition(
        "anthropic", "Anthropic", "anthropic", "claude-3-5-sonnet-20241022",
        None, False, True,
        "Cloud inference; relevant conversation context is sent to Anthropic.",
    ),
    "groq": ProviderDefinition(
        "groq", "Groq", "groq", "openai/gpt-oss-120b",
        "https://api.groq.com/openai/v1", False, True,
        "Cloud inference; relevant conversation context is sent to Groq.",
    ),
    "gemini": ProviderDefinition(
        "gemini", "Google Gemini", "openai", "gemini-2.0-flash",
        "https://generativelanguage.googleapis.com/v1beta/openai/", False, True,
        "Cloud inference through Google's OpenAI-compatible endpoint.",
    ),
    "openrouter": ProviderDefinition(
        "openrouter", "OpenRouter", "openrouter", "openai/gpt-4o-mini",
        "https://openrouter.ai/api/v1", False, True,
        "Cloud routing; context is sent to OpenRouter and the selected model provider.",
    ),
    "sambanova": ProviderDefinition(
        "sambanova", "SambaNova", "sambanova", "DeepSeek-V3.1",
        "https://api.sambanova.ai/v1", False, True,
        "Cloud inference; relevant conversation context is sent to SambaNova.",
    ),
    "cerebras": ProviderDefinition(
        "cerebras", "Cerebras", "cerebras", "gpt-oss-120b",
        "https://api.cerebras.ai/v1", False, True,
        "Cloud inference; relevant conversation context is sent to Cerebras.",
    ),
}


@dataclass
class AdoptionConfig:
    schema_version: int = CONFIG_SCHEMA_VERSION
    installation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    initialized_at: str = field(default_factory=lambda: _now())
    identity_id: str | None = None
    provider: dict[str, Any] | None = None
    dashboard: dict[str, Any] = field(default_factory=lambda: {
        "host": "127.0.0.1",
        "port": 8000,
    })
    onboarding: dict[str, Any] = field(default_factory=lambda: {
        "stage": "welcome",
        "ready": False,
    })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "installation_id": self.installation_id,
            "initialized_at": self.initialized_at,
            "identity_id": self.identity_id,
            "provider": self.provider,
            "dashboard": dict(self.dashboard),
            "onboarding": dict(self.onboarding),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdoptionConfig":
        if value.get("schema_version") != CONFIG_SCHEMA_VERSION:
            raise AdoptionConfigError(
                f"unsupported config schema {value.get('schema_version')!r}; "
                f"expected {CONFIG_SCHEMA_VERSION}"
            )
        _reject_secret_fields(value)
        installation_id = str(value.get("installation_id", ""))
        try:
            uuid.UUID(installation_id)
        except ValueError as exc:
            raise AdoptionConfigError("installation_id must be a UUID") from exc
        provider = value.get("provider")
        if provider is not None:
            provider = validate_provider_config(provider)
        onboarding = value.get("onboarding")
        if not isinstance(onboarding, dict):
            raise AdoptionConfigError("onboarding must be an object")
        dashboard = _validate_dashboard(value.get("dashboard", {}))
        return cls(
            schema_version=CONFIG_SCHEMA_VERSION,
            installation_id=installation_id,
            initialized_at=str(value.get("initialized_at") or _now()),
            identity_id=(str(value["identity_id"]) if value.get("identity_id") else None),
            provider=provider,
            dashboard=dashboard,
            onboarding=dict(onboarding),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_providers() -> list[dict[str, Any]]:
    """Public, credential-free provider options supported by the runtime."""
    return [provider.public_dict() for provider in PROVIDERS.values()]


def load_config(paths: IdentityOSPaths | None = None, *, required: bool = False) -> AdoptionConfig:
    selected = paths or get_paths()
    path = selected.config_file
    if not path.exists():
        if required:
            raise AdoptionConfigError(f"IdentityOS is not initialized: {path} does not exist")
        return AdoptionConfig()
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise AdoptionConfigError("configuration exceeds the 256 KiB safety limit")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except AdoptionConfigError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdoptionConfigError(f"could not read configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdoptionConfigError("configuration root must be an object")
    return AdoptionConfig.from_dict(raw)


def save_config(config: AdoptionConfig, paths: IdentityOSPaths | None = None) -> Path:
    selected = (paths or get_paths()).ensure()
    payload = config.to_dict()
    _reject_secret_fields(payload)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if len(serialized.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise AdoptionConfigError("configuration exceeds the 256 KiB safety limit")
    selected.config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(
        dir=selected.config_file.parent,
        prefix=f".{selected.config_file.name}.",
    )
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, selected.config_file)
        selected.config_file.chmod(0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return selected.config_file


def initialize(paths: IdentityOSPaths | None = None) -> AdoptionConfig:
    """Idempotently create directories and a versioned non-secret config."""
    selected = (paths or get_paths()).ensure()
    if selected.config_file.exists():
        return load_config(selected, required=True)
    config = AdoptionConfig()
    save_config(config, selected)
    return config


def configure_provider(
    provider_name: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    paths: IdentityOSPaths | None = None,
) -> AdoptionConfig:
    """Persist provider metadata and, separately, an optional credential."""
    selected = (paths or get_paths()).ensure()
    config = initialize(selected)
    name = provider_name.strip().lower()
    definition = PROVIDERS.get(name)
    if definition is None:
        raise AdoptionConfigError(
            f"unsupported provider {provider_name!r}; choose from {', '.join(PROVIDERS)}"
        )
    chosen_model = str(model or definition.default_model).strip()
    if not chosen_model or len(chosen_model) > 256 or any(c in chosen_model for c in "\r\n\0"):
        raise AdoptionConfigError("model must be a non-empty single-line value")
    chosen_base = str(base_url or definition.default_base_url or "").strip() or None
    if chosen_base:
        _validate_base_url(chosen_base, local=definition.local)
    secret_handle = f"providers/{name}/api_key" if definition.requires_secret else None
    secret_store = SecretStore(selected.secrets_dir)
    if api_key:
        if not definition.requires_secret:
            raise AdoptionConfigError(f"{definition.display_name} does not require an API key")
        secret_store.put(secret_handle or "", api_key)
    if definition.requires_secret and not secret_store.has(secret_handle or ""):
        raise AdoptionConfigError(f"an API key is required for {definition.display_name}")

    config.provider = validate_provider_config({
        "name": name,
        "adapter": definition.adapter,
        "model": chosen_model,
        "base_url": chosen_base,
        "local": definition.local,
        "secret_handle": secret_handle,
        "configured_at": _now(),
        "verified_at": None,
        "verification": None,
    })
    config.onboarding.update({"stage": "verification", "ready": False})
    save_config(config, selected)
    return config


def validate_provider_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AdoptionConfigError("provider must be an object")
    _reject_secret_fields(value)
    name = str(value.get("name", "")).lower()
    definition = PROVIDERS.get(name)
    if definition is None:
        raise AdoptionConfigError(f"unsupported provider {name!r}")
    adapter = str(value.get("adapter", ""))
    if adapter != definition.adapter:
        raise AdoptionConfigError(f"provider {name!r} must use adapter {definition.adapter!r}")
    model = str(value.get("model", "")).strip()
    if not model or len(model) > 256 or any(c in model for c in "\r\n\0"):
        raise AdoptionConfigError("provider model is invalid")
    base_url = value.get("base_url")
    if base_url:
        base_url = str(base_url)
        _validate_base_url(base_url, local=definition.local)
    secret_handle = value.get("secret_handle")
    expected_handle = f"providers/{name}/api_key" if definition.requires_secret else None
    if secret_handle != expected_handle:
        raise AdoptionConfigError("provider secret handle is invalid")
    return {
        "name": name,
        "adapter": adapter,
        "model": model,
        "base_url": base_url,
        "local": definition.local,
        "secret_handle": secret_handle,
        "configured_at": str(value.get("configured_at") or _now()),
        "verified_at": (str(value["verified_at"]) if value.get("verified_at") else None),
        "verification": (dict(value["verification"]) if isinstance(value.get("verification"), dict) else None),
    }


def build_configured_adapter(paths: IdentityOSPaths | None = None):
    """Construct the configured adapter, resolving plaintext only in memory."""
    from adapters import get_adapter

    selected = paths or get_paths()
    config = load_config(selected, required=True)
    if not config.provider:
        raise AdoptionConfigError("no inference provider is configured")
    provider = validate_provider_config(config.provider)
    kwargs: dict[str, Any] = {}
    if provider["base_url"]:
        kwargs["base_url"] = provider["base_url"]
    handle = provider["secret_handle"]
    if handle:
        key = SecretStore(selected.secrets_dir).get(handle)
        if not key:
            raise AdoptionConfigError(
                f"credential {handle!r} is missing or does not have private permissions"
            )
        kwargs["api_key"] = key
    return get_adapter(provider["adapter"], model=provider["model"], **kwargs)


def record_verification(
    *,
    identity_id: str,
    provenance: Mapping[str, Any],
    response_digest: str,
    paths: IdentityOSPaths | None = None,
) -> AdoptionConfig:
    """Mark ready only for runtime-observed model generation provenance."""
    selected = paths or get_paths()
    config = load_config(selected, required=True)
    if not config.provider:
        raise AdoptionConfigError("no provider is configured")
    mode = str(provenance.get("generation_mode", ""))
    provider = str(provenance.get("provider", ""))
    model = str(provenance.get("model", ""))
    if mode != "identity_model_generation" or not provider or not model:
        raise AdoptionConfigError(
            "readiness requires an actual model-generated response with provider provenance"
        )
    if len(response_digest) != 64 or any(char not in "0123456789abcdef" for char in response_digest):
        raise AdoptionConfigError("response_digest must be a lowercase SHA-256 digest")
    verified_at = _now()
    config.identity_id = identity_id
    config.provider["verified_at"] = verified_at
    config.provider["verification"] = {
        "generation_mode": mode,
        "provider": provider,
        "model": model,
        "latency_ms": provenance.get("latency_ms"),
        "response_sha256": response_digest,
    }
    config.onboarding.update({"stage": "conversation", "ready": True, "verified_at": verified_at})
    save_config(config, selected)
    return config


def public_status(paths: IdentityOSPaths | None = None) -> dict[str, Any]:
    """Return onboarding state with secret values and handles omitted."""
    selected = paths or get_paths()
    config = load_config(selected)
    provider = dict(config.provider or {})
    provider.pop("secret_handle", None)
    return {
        "schema_version": config.schema_version,
        "installation_id": config.installation_id,
        "identity_id": config.identity_id,
        "provider": provider or None,
        "dashboard": dict(config.dashboard),
        "onboarding": dict(config.onboarding),
        "paths": {
            "data": str(selected.data_dir),
            "configuration": str(selected.config_dir),
            "identity_store": str(selected.identity_store),
        },
    }


def _validate_dashboard(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {"host": "127.0.0.1", "port": 8000}
    if not isinstance(value, Mapping):
        raise AdoptionConfigError("dashboard must be an object")
    host = str(value.get("host", "127.0.0.1"))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise AdoptionConfigError("dashboard host must be loopback")
    try:
        port = int(value.get("port", 8000))
    except (TypeError, ValueError) as exc:
        raise AdoptionConfigError("dashboard port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise AdoptionConfigError("dashboard port must be between 1 and 65535")
    return {"host": host, "port": port}


def _validate_base_url(value: str, *, local: bool) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AdoptionConfigError("provider base URL must be an absolute HTTP(S) URL")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (local and loopback):
        raise AdoptionConfigError("cloud provider base URLs must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AdoptionConfigError("provider base URL must not contain credentials, query, or fragment")


def _reject_secret_fields(value: Any, path: str = "config") -> None:
    """Refuse common plaintext credential fields in persisted configuration."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "api_key", "apikey", "token", "access_token", "refresh_token",
                "password", "secret", "authorization",
            }:
                raise AdoptionConfigError(f"plaintext credential field is forbidden at {path}.{key}")
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")
