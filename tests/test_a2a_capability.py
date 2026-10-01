"""test_a2a_capability.py — A2A capability: public reads vs gated conversation."""

from __future__ import annotations

import core.capabilities.a2a as a2a_cap_mod
from core.capabilities.registry import CapabilityRegistry
from core.interop import a2a as a2a_proto
from core.interop.http import HttpClient
from runtime.persistence import InMemoryBackend

ECHO_URL = "http://local-peer.invalid"


def _registry(monkeypatch, handler=None):
    real = a2a_cap_mod.A2AClient

    def factory(base_url, *, http=None, agent_card=None):
        agent = a2a_proto.A2ATestAgent(name="peer", handler=handler, url=str(base_url))
        return real(
            base_url,
            http=HttpClient(timeout=1.0, transport=agent.transport()),
            agent_card=agent_card,
        )

    monkeypatch.setattr(a2a_cap_mod, "A2AClient", factory)
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "a2a", config={"agents": {"peer": {"base_url": ECHO_URL}}, "primary": "peer"})
    return registry


def test_install_and_public_card_read(monkeypatch):
    registry = _registry(monkeypatch)
    cap = registry.get("aster", "a2a")
    assert cap is not None
    result = registry.call("aster", "a2a.discover")
    assert result.success is True
    assert result.data["name"] == "peer"

    result = registry.call("aster", "a2a.inspect_agent")
    assert result.success is True
    assert result.data["capabilities"][0]["name"] == "echo"


def test_conversation_requires_grant(monkeypatch):
    registry = _registry(monkeypatch)
    denied = registry.call("aster", "a2a.send", message="hello")
    assert denied.success is False
    assert denied.error.get("type") == "permission_denied"

    registry.grant("aster", "a2a", "a2a.converse")
    allowed = registry.call("aster", "a2a.send", message="hello")
    assert allowed.success is True
    assert allowed.data["task"]["result"] == "echo: hello"


def test_exchange_completes(monkeypatch):
    registry = _registry(monkeypatch, handler=lambda msg, sid: f"reply-to:{msg}")
    registry.grant("aster", "a2a", "a2a.converse")
    result = registry.call("aster", "a2a.exchange", message="ping")
    assert result.success is True
    assert result.data["task"]["status"] == "completed"


def test_oversized_message_rejected(monkeypatch):
    registry = _registry(monkeypatch)
    registry.grant("aster", "a2a", "a2a.converse")
    result = registry.call("aster", "a2a.send", message="x" * 20001)
    assert result.success is False
    assert result.error.get("type") == "invalid_parameters"


def test_unknown_agent_reported(monkeypatch):
    registry = _registry(monkeypatch)
    result = registry.call("aster", "a2a.discover", agent="ghost")
    assert result.success is False
    assert result.error.get("type") == "unknown_agent"