"""test_mcp_capability.py — generic MCP capability: skills, grants, risk enforcement."""

from __future__ import annotations

import core.capabilities.mcp as mcp_mod
from core.capabilities.registry import CapabilityRegistry
from runtime.persistence import InMemoryBackend
from tests.interop_stubs import FakeMCPHttpClient, RPCServerError, mcp_server_handler

SAMPLE_TOOLS = [
    {"name": "read_thread", "description": "read a thread", "inputSchema": {"type": "object"}},
    {"name": "send_message", "description": "publish a message", "inputSchema": {"type": "object"}},
    {"name": "delete_user", "description": "remove a user account", "inputSchema": {"type": "object"}},
    {"name": "look_up_balance", "description": "charge or transfer funds", "inputSchema": {"type": "object"}},
]

_calls: list[tuple[str, dict]] = []


def _handler(method, params, headers):
    if method == "tools/call":
        _calls.append((str(params.get("name")), dict(params.get("arguments") or {})))
        return {"content": [{"type": "text", "text": "ok"}]}
    return mcp_server_handler(tools=SAMPLE_TOOLS)(method, params, headers)


def _patched_client(handler=None):
    real = mcp_mod.MCPClient

    def factory(server, timeout=30.0, secret_resolver=None, http=None):
        return real(server, timeout=timeout, secret_resolver=secret_resolver, http=FakeMCPHttpClient(handler=handler or _handler))

    return factory


def _registry(tmp_path, monkeypatch) -> CapabilityRegistry:
    monkeypatch.setattr(mcp_mod, "MCPClient", _patched_client())
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "mcp", config={"servers": {"culture": {"url": "https://server.invalid/mcp"}}, "primary": "culture"})
    return registry


def test_install_persists_capability(tmp_path, monkeypatch):
    _calls.clear()
    registry = _registry(tmp_path, monkeypatch)
    cap = registry.get("aster", "mcp")
    assert cap is not None
    assert cap.id == "mcp"
    names = {s.name for s in cap.skills()}
    assert {"mcp.discover", "mcp.inspect", "mcp.ping", "mcp.call"} <= names


def test_public_read_skills_banish_no_grant(tmp_path, monkeypatch):
    _calls.clear()
    registry = _registry(tmp_path, monkeypatch)
    result = registry.call("aster", "mcp.discover")
    assert result.success is True
    assert result.data["serverInfo"]["name"] == "stub"

    result = registry.call("aster", "mcp.inspect")
    assert result.success is True
    risks = {t["risk"] for t in result.data["tools"]}
    assert "system_mutation" in risks  # delete_user
    assert "financial" in risks  # look_up_balance


def test_mcp_call_requires_explicit_grant(tmp_path, monkeypatch):
    _calls.clear()
    registry = _registry(tmp_path, monkeypatch)
    result = registry.call("aster", "mcp.call", tool="read_thread")
    assert result.success is False
    assert "denied" in str(result.error).lower() or "not authorized" in str(result.error.get("message", "")).lower()

    registry.grant("aster", "mcp", "mcp.call")
    result = registry.call("aster", "mcp.call", tool="read_thread")
    assert result.success is True


def test_dangerous_tool_refused_even_with_grant(tmp_path, monkeypatch):
    _calls.clear()
    registry = _registry(tmp_path, monkeypatch)
    registry.grant("aster", "mcp", "mcp.call")
    result = registry.call("aster", "mcp.call", tool="delete_user", arguments={})
    assert result.success is False
    assert result.error.get("type") == "denied_by_risk_class"
    assert _calls == []  # never reached the server


def test_mutating_tool_requires_confirm(tmp_path, monkeypatch):
    _calls.clear()
    registry = _registry(tmp_path, monkeypatch)
    registry.grant("aster", "mcp", "mcp.call")
    nope = registry.call("aster", "mcp.call", tool="send_message", arguments={"m": "hi"})
    assert nope.success is False
    assert nope.error.get("type") == "confirmation_required"
    assert _calls == []

    yes = registry.call("aster", "mcp.call", tool="send_message", arguments={"m": "hi"}, confirm=True)
    assert yes.success is True
    assert _calls[-1][0] == "send_message"


def test_unknown_server_reported(tmp_path, monkeypatch):
    _calls.clear()
    monkeypatch.setattr(mcp_mod, "MCPClient", _patched_client())
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install("aster", "mcp")
    result = registry.call("aster", "mcp.discover", server="nope")
    assert result.success is False
    assert result.error.get("type") == "unknown_server"


def test_true_read_only_result_has_source(tmp_path, monkeypatch):
    _calls.clear()
    registry = _registry(tmp_path, monkeypatch)
    result = registry.call("aster", "mcp.ping")
    assert result.success is True
    assert result.source.startswith("mcp:")