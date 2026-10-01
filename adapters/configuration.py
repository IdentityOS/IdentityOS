"""Shared, side-effect-free adapter configuration for services and benchmarks."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any, Optional

from .base import BaseAdapter
from .cerebras_adapter import CerebrasAdapter
from .chain import ChainAdapter
from .groq_adapter import GroqAdapter
from .openai_adapter import (
    AnthropicAdapter,
    OllamaAdapter,
    OpenAIAdapter,
    _resolve_openai_timeout,
)
from .openrouter_adapter import OpenRouterAdapter
from .sambanova_adapter import SambaNovaAdapter


def _valid(value: Optional[str]) -> bool:
    return bool(value and "PLACEHOLDER" not in value.upper())


def _numbered_keys(env: Mapping[str, str], prefix: str) -> list[str]:
    pattern = re.compile(rf"^{re.escape(prefix)}(?:_(\d+))?$")
    matches: list[tuple[int, str]] = []
    for name, value in env.items():
        match = pattern.match(name)
        if match and _valid(value):
            matches.append((int(match.group(1) or 1), value))
    return [value for _, value in sorted(matches)]


def _discover_openai_providers(env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Discover all named OpenAI-compatible providers from environment.

    Looks for variables matching:
    - OPENAI_API_KEY (legacy, treated as 'openai')
    - OPENAI_<NAME>_API_KEY (named providers, e.g., OPENAI_GEMINI_API_KEY)
    - OLLAMA_API_KEY (legacy, treated as 'ollama')

    Returns list of dicts with: name, api_key, base_url, model, is_local
    """
    providers = []

    # Legacy OPENAI_API_KEY (treated as 'openai' unless base_url is local)
    openai_key = env.get("OPENAI_API_KEY")
    if _valid(openai_key):
        openai_base = env.get("OPENAI_BASE_URL", "")
        is_local = any(host in openai_base for host in ("localhost", "127.0.0.1"))
        providers.append({
            "name": "ollama" if is_local else "openai",
            "display_name": "Ollama (local)" if is_local else "OpenAI",
            "api_key": openai_key,
            "base_url": openai_base or ("http://localhost:11434/v1" if is_local else None),
            "model": env.get("OLLAMA_MODEL" if is_local else "OPENAI_MODEL", env.get("IDENTITY_MODEL", "llama3.2" if is_local else "gpt-4o")),
            "is_local": is_local,
        })

    # Legacy OLLAMA_API_KEY (explicit Ollama)
    ollama_key = env.get("OLLAMA_API_KEY")
    if _valid(ollama_key):
        ollama_base = env.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        providers.append({
            "name": "ollama",
            "display_name": "Ollama (local)",
            "api_key": ollama_key,
            "base_url": ollama_base,
            "model": env.get("OLLAMA_MODEL", env.get("IDENTITY_MODEL", "llama3.2")),
            "is_local": True,
        })

    # Named OpenAI-compatible providers: OPENAI_<NAME>_API_KEY
    pattern = re.compile(r"^OPENAI_([A-Z0-9_]+)_API_KEY$")
    for key_name, api_key in env.items():
        match = pattern.match(key_name)
        if match and _valid(api_key):
            name = match.group(1).lower()
            # Skip reserved names
            if name in ("api", "key", "base", "url", "model", "organization", "timeout"):
                continue

            base_url = env.get(f"OPENAI_{match.group(1)}_BASE_URL", "")
            model = env.get(f"OPENAI_{match.group(1)}_MODEL", env.get("IDENTITY_MODEL", "gpt-4o"))
            is_local = any(host in base_url for host in ("localhost", "127.0.0.1"))

            providers.append({
                "name": name,
                "display_name": match.group(1).replace("_", " ").title(),
                "api_key": api_key,
                "base_url": base_url or None,
                "model": model,
                "is_local": is_local,
            })

    return providers


def build_adapter_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[BaseAdapter]:
    """Build the configured provider chain without making a network request.

    Explicit ``IDENTITY_ADAPTER`` configuration is tried first. Remaining
    providers with valid credentials form deterministic fallbacks. Duplicate
    provider types are omitted.
    """
    values = os.environ if env is None else env
    candidates: list[BaseAdapter] = []
    configured: set[str] = set()

    explicit = values.get("IDENTITY_ADAPTER", "").strip().lower()
    if explicit:
        from . import get_adapter

        try:
            config: dict[str, Any] = json.loads(values.get("IDENTITY_ADAPTER_CONFIG", "{}") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("IDENTITY_ADAPTER_CONFIG must be valid JSON") from exc
        candidates.append(get_adapter(explicit, **config))
        configured.add(explicit)

    samba_keys = _numbered_keys(values, "SAMBANOVA_API_KEY")
    if samba_keys and "sambanova" not in configured:
        candidates.append(SambaNovaAdapter(
            model=values.get("SAMBANOVA_MODEL", "DeepSeek-V3.1"), api_keys=samba_keys,
        ))
        configured.add("sambanova")

    groq_keys = _numbered_keys(values, "GROQ_API_KEY")
    if groq_keys and "groq" not in configured:
        # ``IDENTITY_MODEL`` names the primary/local model and must not be
        # sent to cloud providers: an Ollama tag such as ``phi4-mini:latest``
        # is not a Groq model ID (live-test finding). Cloud providers use
        # their own MODEL variable with a Groq-hosted default.
        candidates.append(GroqAdapter(
            model=values.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            api_keys=groq_keys,
        ))
        configured.add("groq")

    cerebras_keys = _numbered_keys(values, "CEREBRAS_API_KEY")
    if cerebras_keys and "cerebras" not in configured:
        candidates.append(CerebrasAdapter(
            model=values.get("CEREBRAS_MODEL", "gpt-oss-120b"), api_keys=cerebras_keys,
        ))
        configured.add("cerebras")

    if _valid(values.get("OPENROUTER_API_KEY")) and "openrouter" not in configured:
        candidates.append(OpenRouterAdapter(
            model=values.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            api_key=values.get("OPENROUTER_API_KEY"),
            base_url=values.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        ))
        configured.add("openrouter")

    if _valid(values.get("ANTHROPIC_API_KEY")) and "anthropic" not in configured:
        candidates.append(AnthropicAdapter(
            model=values.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
            api_key=values.get("ANTHROPIC_API_KEY"),
        ))
        configured.add("anthropic")

    # Discover all OpenAI-compatible providers (including named ones)
    openai_providers = _discover_openai_providers(values)
    for provider in openai_providers:
        provider_name = provider["name"]
        if provider_name in configured:
            continue

        timeout = _resolve_openai_timeout(values.get("OPENAI_TIMEOUT"))
        if provider["is_local"]:
            candidates.append(OllamaAdapter(
                model=provider["model"],
                base_url=provider["base_url"],
                timeout=timeout,
            ))
        else:
            candidates.append(OpenAIAdapter(
                model=provider["model"],
                api_key=provider["api_key"],
                base_url=provider["base_url"],
                timeout=timeout,
            ))
        configured.add(provider_name)

    if not candidates:
        return None
    return candidates[0] if len(candidates) == 1 else ChainAdapter(candidates)


def describe_adapter(adapter: Optional[BaseAdapter]) -> dict[str, Any]:
    """Return public provider/model metadata without credentials."""
    if adapter is None:
        return {"configured": False, "providers": []}
    leaves = adapter.adapters if isinstance(adapter, ChainAdapter) else [adapter]
    return {
        "configured": True,
        "providers": [
            {
                "adapter": type(item).__name__,
                "model": str(getattr(item, "model", "") or ""),
                "structured_output": (getattr(item, "structured_output", "prompt_only")
                    if isinstance(getattr(item, "structured_output", None), str) else "prompt_only"),
            }
            for item in leaves
        ],
    }


def list_configured_openai_providers(env: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    """Return list of all configured OpenAI-compatible providers for UI selection."""
    values = os.environ if env is None else env
    providers = _discover_openai_providers(values)
    # Add display info
    for p in providers:
        p["configured"] = True
    return providers