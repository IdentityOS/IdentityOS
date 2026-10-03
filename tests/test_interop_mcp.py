"""test_interop_mcp.py — generic MCP (Streamable HTTP) client behaviors."""

from __future__ import annotations

import json

import pytest

from core.interop import mcp as m
from core.interop.http import HttpClient
from tests.interop_stubs import (
    FakeMCPHttpClient,
    RPCServerError,
    TransportFailure,
    content_item,
    mcp_server_handler,
)


def test_classify_tool_precedence():
    assert m.classify_tool("delete_all") is m.ToolRisk.SYSTEM_MUTATION
    assert m.classify_tool("delete_user") is m.ToolRisk.SYSTEM_MUTATION
    assert m.classify_tool("sign_contract") is m.ToolRisk.LEGAL
    assert m.classify_tool("store_credentials") is m.ToolRisk.SECRET_ACCESS
    assert m.classify_tool("charge_card") is m.ToolRisk.FINANCIAL
    assert m.classify_tool("send_message") is m.ToolRisk.MESSAGE
    assert m.classify_tool("speak") is m.ToolRisk.POST
    assert m.classify_tool("create_thread") is m.ToolRisk.CREATE
    assert m.classify_tool("watch_room") is m.ToolRisk.OBSERVE
    assert m.classify_tool("search_boards") is m.ToolRisk.SEARCH
    assert m.classify_tool("read_thread") is m.ToolRisk.READ
    assert m.classify_tool("mystery_name") is m.ToolRisk.READ


def test_initialize_and_ping():
    handler = mcp_server_handler()
    client = m.MCPClient(
        m.MCPServer(name="s", url="https://s.invalid/mcp"),
        timeout=1.0,
        http=FakeMCPHttpClient(handler=handler),
    )
    info = client.initialize()
    assert info["serverInfo"]["name"] == "stub"
    assert client.ping() is True


def test_list_and_inspect_tools_with_risk():
    tools = [
        {"name": "read_thread", "description": "read a thread", "inputSchema": {"type": "object"}},
        {"name": "speak", "description": "publish a message in the room"},
    ]
    handler = mcp_server_handler(tools=tools)
    client = m.MCPClient(
        m.MCPServer(name="s", url="https://s.invalid/mcp"),
        timeout=1.0,
        http=FakeMCPHttpClient(handler=handler),
    )
    client.initialize()
    specs = client.inspect_tools()
    by_name = {s.name: s for s in specs}
    assert by_name["read_thread"].risk == m.ToolRisk.READ.value
    assert by_name["read_thread"].mutating is False
    assert by_name["speak"].risk == m.ToolRisk.POST.value
    assert by_name["speak"].mutating is True


def test_call_tool_returns_content():
    handler = mcp_server_handler(
        tool_impl={"speak": lambda a: {"content": [content_item("said: " + a["content"])]}},
    )
    client = m.MCPClient(
        m.MCPServer(name="s", url="https://s.invalid/mcp"),
        timeout=1.0,
        http=FakeMCPHttpClient(handler=handler),
    )
    result = client.call_tool("speak", {"content": "hello"})
    assert result["content"][0]["text"] == "said: hello"


def test_server_jsonrpc_error_is_typed():
    def handler(method, params, headers):
        raise RPCServerError(-32001, "credentials missing")

    client = m.MCPClient(
        m.MCPServer(name="s", url="https://s.invalid/mcp"),
        timeout=1.0,
        http=FakeMCPHttpClient(handler=handler),
    )
    with pytest.raises(m.MCPError) as excinfo:
        client.ping()
    assert excinfo.value.code == -32001


def test_transport_failure_is_server_unavailable():
    def handler(method, params, headers):
        raise TransportFailure(503, "busy")

    client = m.MCPClient(
        m.MCPServer(name="s", url="https://s.invalid/mcp"),
        timeout=1.0,
        http=FakeMCPHttpClient(handler=handler),
    )
    with pytest.raises(m.MCPServerUnavailable):
        client.ping()


def test_secret_header_resolution_and_provisioning_guard():
    resolver_values: dict[str, str] = {"cc-key": "topsecret"}
    seen_headers: list[dict[str, str]] = []

    def handler(method, params, headers):
        seen_headers.append(headers)
        return {}

    server = m.MCPServer(
        name="s",
        url="https://s.invalid/mcp",
        headers={"authorization": "secret://cc-key"},
    )
    client = m.MCPClient(server, timeout=1.0, secret_resolver=resolver_values.get,
                         http=FakeMCPHttpClient(handler=handler))
    client.ping()
    assert seen_headers[0]["authorization"] == "topsecret"

    no_resolver = m.MCPClient(server, timeout=1.0, http=FakeMCPHttpClient(handler=handler))
    with pytest.raises(m.MCPError) as excinfo:
        no_resolver.ping()
    assert excinfo.value.code == -32001


def test_secret_header_without_resolver_refuses_before_network():
    server = m.MCPServer(name="s", url="https://s.invalid/mcp", headers={"authorization": "secret://nope"})
    client = m.MCPClient(server, timeout=1.0, secret_resolver=lambda handle: None,
                         http=FakeMCPHttpClient(handler=lambda *a: {}))
    with pytest.raises(m.MCPError) as excinfo:
        client.ping()
    assert "not provisioned" in str(excinfo.value)


def test_session_id_propagates():
    seen: list[dict[str, str]] = []

    class SessionHttp(HttpClient):
        def __init__(self, **kwargs):
            super().__init__(transport=self._serve, **kwargs)

        @staticmethod
        def _serve(request, timeout):
            seen.append(dict(request["headers"]))
            return 200, "application/json", '{"jsonrpc":"2.0","id":1,"result":{}}', {"mcp-session-id": "sess-1"}

    client = m.MCPClient(
        m.MCPServer(name="s", url="https://s.invalid/mcp"),
        timeout=1.0,
        http=SessionHttp(timeout=1.0),
    )
    client.ping()
    client.ping()
    assert seen[0].get("Mcp-Session-Id") is None
    assert seen[1].get("Mcp-Session-Id") == "sess-1"


def test_streamable_http_202_wrapper_is_unwrapped():
    def serve(request, timeout):
        body = {"status": 200, "body": {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "read"}]}}}
        return 202, "application/json", json.dumps(body), {"content-type": "application/json"}

    client = m.MCPClient(
        m.MCPServer(name="s", url="https://s.invalid/mcp"),
        timeout=1.0,
        http=HttpClient(timeout=1.0, transport=serve),
    )
    assert client.list_tools() == [{"name": "read"}]