from unittest.mock import Mock

import pytest

from adapters.chain import ChainAdapter
from cli.phone import phone_adapter


def test_cloud_configuration_is_explicit_and_filters_unrelated_secrets(tmp_path, monkeypatch):
    env = tmp_path / "keys.env"
    env.write_text(
        "GROQ_API_KEY=test-groq\nOPENROUTER_API_KEY=test-router\nGITHUB_TOKEN=unrelated\nIDENTITY_ADAPTER=ollama\n"
    )
    adapter = phone_adapter(
        {"provider_env_file": str(env), "cloud_providers": ["groq", "openrouter"], "model_timeout": 12}
    )
    assert isinstance(adapter, ChainAdapter)
    assert [type(a).__name__ for a in adapter.adapters] == ["GroqAdapter", "OpenRouterAdapter"]
    assert all(a.timeout == 12 for a in adapter.adapters)
    assert all(a.max_tokens == 256 for a in adapter.adapters)
    assert "test-groq" not in repr(adapter)
    assert "unrelated" not in repr(adapter)


def test_missing_cloud_keys_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="No configured"):
        phone_adapter({"provider_env_file": str(tmp_path / "absent")})
    with pytest.raises(ValueError, match="cloud_providers"):
        phone_adapter({"provider_env_file": str(tmp_path / "absent"), "cloud_providers": ["unknown"]})


@pytest.mark.parametrize(
    "failure",
    [
        "429 rate limit",
        "500 internal server error",
        "402 payment required",
        "connection timeout",
        "401 invalid api key",
    ],
)
def test_failover_and_cooldown_preserve_request(failure):
    first, second = Mock(model="first"), Mock(model="second")
    first.generate.side_effect = RuntimeError(failure)
    second.generate.return_value = "verified response"
    chain = ChainAdapter([first, second], cooldown_seconds=60)
    for _ in range(2):
        assert chain.generate("same context", "same question", "same identity") == "verified response"
    assert first.generate.call_count == 1
    assert second.generate.call_count == 2
    assert second.generate.call_args.kwargs["context"] == "same context"
    assert second.generate.call_args.kwargs["identity"] == "same identity"


def test_failover_never_replays_started_capability():
    first, second = Mock(model="first"), Mock(model="second")
    action = Mock()

    def invoke_then_fail(**kwargs):
        kwargs["execute_tool"]("email.send", {"body": "hello"})
        raise RuntimeError("503 unavailable")

    first.generate.side_effect = invoke_then_fail
    with pytest.raises(RuntimeError, match="503"):
        ChainAdapter([first, second]).generate("context", "request", None, execute_tool=action)
    action.assert_called_once()
    second.generate.assert_not_called()


def test_bad_request_is_not_hidden_by_fallback():
    first, second = Mock(model="first"), Mock(model="second")
    first.generate.side_effect = RuntimeError("400 bad request")
    with pytest.raises(RuntimeError, match="400"):
        ChainAdapter([first, second]).generate("context", "request", None)
    second.generate.assert_not_called()
