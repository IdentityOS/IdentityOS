from unittest.mock import patch

import pytest

from benchmarks import coder_llm


def test_local_coder_uses_loopback_and_selected_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "gemma3:270m")
    with patch.object(coder_llm, "_openai_compatible_chat", return_value={"hypothesis": "h", "change": "c", "edits": []}) as call:
        coder_llm._call_ollama("prompt", model=None, timeout_s=10)
    assert call.call_args.kwargs["base_url"] == "http://127.0.0.1:11434/v1"
    assert call.call_args.kwargs["model"] == "gemma3:270m"


def test_local_coder_rejects_remote_endpoint(monkeypatch):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "gemma3:270m")
    monkeypatch.setenv("OLLAMA_CODER_BASE_URL", "https://example.com/v1")
    with pytest.raises(coder_llm.CoderError, match="loopback"):
        coder_llm._call_ollama("prompt", model=None, timeout_s=10)


def test_invalid_local_proposal_is_not_accepted():
    with pytest.raises(coder_llm.CoderError, match="missing"):
        coder_llm._validate_proposal({"hypothesis": "h", "edits": []})
