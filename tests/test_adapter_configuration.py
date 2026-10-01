import pytest

from adapters import ChainAdapter, build_adapter_from_env, describe_adapter
from adapters.configuration import (
    _discover_openai_providers,
    list_configured_openai_providers,
)
from adapters.groq_adapter import GroqAdapter
from adapters.openai_adapter import OllamaAdapter, OpenAIAdapter


def test_local_model_alias_never_leaks_into_cloud_providers():
    """Live-test finding: with IDENTITY_ADAPTER=ollama pinning phi4-mini:latest,
    the fallback chain sent that Ollama tag to Cerebras (404)."""
    adapter = build_adapter_from_env({
        "IDENTITY_ADAPTER": "ollama",
        "IDENTITY_ADAPTER_CONFIG": '{"model":"phi4-mini:latest"}',
        "IDENTITY_MODEL": "phi4-mini:latest",
        "OLLAMA_MODEL": "phi4-mini:latest",
        "GROQ_API_KEY": "grk",
        "CEREBRAS_API_KEY": "cbk",
    })
    from adapters.cerebras_adapter import CerebrasAdapter
    from adapters.groq_adapter import GroqAdapter

    by_type = {type(leaf).__name__: leaf for leaf in adapter.adapters}
    assert set((by_type["OllamaAdapter"].model, by_type["GroqAdapter"].model,
                by_type[CerebrasAdapter.__name__].model)) == {
        "phi4-mini:latest", "openai/gpt-oss-120b", "gpt-oss-120b",
    }, "the local Ollama tag belongs only to the Ollama adapter"


def test_named_provider_model_and_base_url_do_not_cross_leak():
    """OPENAI_<NAME>_MODEL/BASE_URL must stay paired with <NAME>."""
    adapter = build_adapter_from_env({
        "OPENAI_GEMINI_API_KEY": "g-key",
        "OPENAI_GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "OPENAI_GEMINI_MODEL": "gemini-flash-latest",
        "OPENAI_NVIDIA_API_KEY": "n-key",
        "OPENAI_NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "OPENAI_NVIDIA_MODEL": "moonshotai/kimi-k3",
    })
    leaves = {leaf.model: leaf for leaf in adapter.adapters}
    assert leaves["gemini-flash-latest"].base_url.startswith("https://generativelanguage")
    assert leaves["moonshotai/kimi-k3"].base_url == "https://integrate.api.nvidia.com/v1"


def test_placeholder_openai_key_is_treated_as_local_ollama():
    adapter = build_adapter_from_env({
        "OPENAI_API_KEY": "ollama",
        "OPENAI_BASE_URL": "http://localhost:11434/v1",
    })
    assert isinstance(adapter, OllamaAdapter)
    assert "localhost" in (adapter.base_url or "")


def test_no_credentials_means_no_adapter():
    assert build_adapter_from_env({}) is None


def test_provider_keys_create_a_deterministic_fallback_chain():
    adapter = build_adapter_from_env({
        "GROQ_API_KEY": "groq-test-key",
        "GROQ_API_KEY_2": "groq-second-key",
        "OPENAI_API_KEY": "openai-test-key",
    })
    assert isinstance(adapter, ChainAdapter)
    assert isinstance(adapter.adapters[0], GroqAdapter)
    assert isinstance(adapter.adapters[1], OpenAIAdapter)
    assert adapter.adapters[0]._keys == ["groq-test-key", "groq-second-key"]
    description = describe_adapter(adapter)
    assert description["configured"] is True
    assert "test-key" not in str(description)


def test_local_openai_endpoint_selects_ollama_semantics():
    adapter = build_adapter_from_env({
        "OPENAI_API_KEY": "ollama",
        "OPENAI_BASE_URL": "http://localhost:11434/v1",
        "OLLAMA_MODEL": "qwen3:4b",
    })
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.model == "qwen3:4b"
    assert adapter.timeout == 120.0


@pytest.mark.parametrize(
    ("base_url", "expected_type"),
    [
        ("https://api.openai.com/v1", OpenAIAdapter),
        ("http://localhost:11434/v1", OllamaAdapter),
    ],
)
def test_openai_timeout_propagates_to_openai_compatible_adapters(
    base_url, expected_type
):
    adapter = build_adapter_from_env({
        "OPENAI_API_KEY": "test-key",
        "OPENAI_BASE_URL": base_url,
        "OPENAI_TIMEOUT": "17.5",
    })

    assert isinstance(adapter, expected_type)
    assert adapter.timeout == 17.5


@pytest.mark.parametrize("value", ["invalid", "0", "-1", "nan", "inf"])
def test_invalid_openai_timeout_is_rejected(value):
    with pytest.raises(ValueError, match="OPENAI_TIMEOUT must be a positive number"):
        build_adapter_from_env({
            "OPENAI_API_KEY": "test-key",
            "OPENAI_TIMEOUT": value,
        })


def test_explicit_adapter_precedes_automatic_fallbacks():
    adapter = build_adapter_from_env({
        "IDENTITY_ADAPTER": "ollama",
        "IDENTITY_ADAPTER_CONFIG": '{"model": "llama3.2"}',
        "GROQ_API_KEY": "groq-test-key",
    })
    assert isinstance(adapter, ChainAdapter)
    assert isinstance(adapter.adapters[0], OllamaAdapter)
    assert isinstance(adapter.adapters[1], GroqAdapter)


def test_invalid_explicit_json_is_not_silently_ignored():
    with pytest.raises(ValueError, match="valid JSON"):
        build_adapter_from_env({
            "IDENTITY_ADAPTER": "ollama",
            "IDENTITY_ADAPTER_CONFIG": "{broken",
        })


def test_legacy_single_provider_setup_remains_backward_compatible():
    adapter = build_adapter_from_env({
        "OPENAI_API_KEY": "legacy-key",
        "OPENAI_MODEL": "gpt-4o-mini",
    })
    assert isinstance(adapter, OpenAIAdapter)
    assert adapter.model == "gpt-4o-mini"


@pytest.mark.parametrize(
    ("env", "expected_type"),
    [
        # Legacy OpenAI cloud: OPENAI_API_KEY + remote base
        (
            {"OPENAI_API_KEY": "legacy-key", "OPENAI_BASE_URL": "https://api.openai.com/v1"},
            OpenAIAdapter,
        ),
        # Legacy OpenAI defaulting to a local Ollama endpoint
        (
            {
                "OPENAI_API_KEY": "ollama",
                "OPENAI_BASE_URL": "http://localhost:11434/v1",
                "OLLAMA_MODEL": "llama3.2",
            },
            OllamaAdapter,
        ),
        # Legacy explicit Ollama
        (
            {
                "OLLAMA_API_KEY": "ollama",
                "OLLAMA_BASE_URL": "http://localhost:11434/v1",
                "OLLAMA_MODEL": "qwen2.5:3b",
            },
            OllamaAdapter,
        ),
        # Named provider with a local endpoint keeps local semantics
        (
            {
                "OPENAI_OLLAMA_API_KEY": "local-key",
                "OPENAI_OLLAMA_BASE_URL": "http://localhost:11434/v1",
                "OPENAI_OLLAMA_MODEL": "qwen3:4b",
            },
            OllamaAdapter,
        ),
    ],
)
def test_legacy_env_combos_stay_backward_compatible(env, expected_type):
    adapter = build_adapter_from_env(env)
    assert isinstance(adapter, expected_type)


def test_legacy_openai_and_explicit_ollama_form_fallback_chain():
    adapter = build_adapter_from_env({
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL": "gpt-4o",
        "OLLAMA_API_KEY": "ollama",
        "OLLAMA_BASE_URL": "http://localhost:11434/v1",
        "OLLAMA_MODEL": "llama3.2",
    })
    assert isinstance(adapter, ChainAdapter)
    assert [type(item).__name__ for item in adapter.adapters] == [
        "OpenAIAdapter",
        "OllamaAdapter",
    ]
    assert [item.model for item in adapter.adapters] == ["gpt-4o", "llama3.2"]


def test_placeholder_values_are_treated_as_unconfigured():
    assert _discover_openai_providers({
        "OPENAI_API_KEY": "PLACEHOLDER_API_KEY",
    }) == []
    assert build_adapter_from_env({
        "OPENAI_API_KEY": "PLACEHOLDER-REPLACE_WITH_YOUR_API_KEY",
    }) is None


def test_named_openai_compatible_providers_are_discovered():
    providers = _discover_openai_providers({
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_GEMINI_API_KEY": "gemini-key",
        "OPENAI_NVIDIA_API_KEY": "nvidia-key",
        "OPENAI_NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "OPENAI_NVIDIA_MODEL": "nvidia/nemotron-3.5",
    })
    names = [p["name"] for p in providers]
    assert names == ["openai", "gemini", "nvidia"]
    nvidia = providers[2]
    assert nvidia["api_key"] == "nvidia-key"
    assert nvidia["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert nvidia["model"] == "nvidia/nemotron-3.5"


def test_reserved_openai_names_do_not_become_providers():
    providers = _discover_openai_providers({
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "gpt-4o",
        "OPENAI_BASE_URL": "https://example.com/v1",
        "OPENAI_TIMEOUT": "30",
    })
    assert [p["name"] for p in providers] == ["openai"]


def test_list_configured_openai_providers_marks_all_found():
    listed = list_configured_openai_providers({
        "OPENAI_API_KEY": "a",
        "OPENAI_GEMINI_API_KEY": "b",
    })
    assert all(p["configured"] for p in listed)
    assert [p["name"] for p in listed] == ["openai", "gemini"]


def test_multiple_named_providers_route_credentials_to_dedicated_adapters():
    adapter = build_adapter_from_env({
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL": "gpt-4o",
        "OPENAI_GEMINI_API_KEY": "gemini-key",
        "OPENAI_GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "OPENAI_GEMINI_MODEL": "gemini-2.0-flash",
        "OPENAI_NVIDIA_API_KEY": "nvidia-key",
        "OPENAI_NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "OPENAI_NVIDIA_MODEL": "nvidia/nemotron-3.5",
    })
    assert isinstance(adapter, ChainAdapter)
    leaves = adapter.adapters
    assert [type(item).__name__ for item in leaves] == [
        "OpenAIAdapter",
        "OpenAIAdapter",
        "OpenAIAdapter",
    ]
    assert [item.model for item in leaves] == ["gpt-4o", "gemini-2.0-flash", "nvidia/nemotron-3.5"]
    assert [item.base_url for item in leaves] == [
        "https://api.openai.com/v1",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "https://integrate.api.nvidia.com/v1",
    ]


def test_local_named_provider_selects_ollama_semantics():
    adapter = build_adapter_from_env({
        "OPENAI_OLLAMA_API_KEY": "local-key",
        "OPENAI_OLLAMA_BASE_URL": "http://localhost:11434/v1",
        "OPENAI_OLLAMA_MODEL": "qwen3:4b",
    })
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.model == "qwen3:4b"
    assert adapter.base_url == "http://localhost:11434/v1"
