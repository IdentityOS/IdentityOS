"""Shared in-process stubs for interop protocol tests (never touches the network)."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from core.interop.http import HttpClient


class RPCServerError(Exception):
    """Signal a JSON-RPC error object to return (code, message)."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


class TransportFailure(Exception):
    """Signal an HTTP-level failure (status, message)."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


class FakeMCPHttpClient(HttpClient):
    """An HttpClient that speaks the MCP JSON-RPC subset to an in-process handler."""

    def __init__(
        self,
        *,
        handler: Callable[[str, dict[str, Any], dict[str, str]], Any],
        timeout: float = 1.0,
        user_agent: Optional[str] = None,
        base_url: str = "https://server.invalid/mcp",
    ) -> None:
        self.handler = handler
        self.base_url = base_url
        super().__init__(timeout=timeout, user_agent=user_agent or "identityos-test/0.0.0", transport=self._serve)

    def _serve(
        self, request: Mapping[str, Any], timeout: float
    ) -> tuple[int, str, str, dict[str, str]]:
        payload: dict[str, Any] = {}
        raw = request.get("body")
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = {}
        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        headers = dict(request.get("headers") or {})
        try:
            result = self.handler(method, params, headers)
            body = {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
            return 200, "application/json", json.dumps(body), {"content-type": "application/json"}
        except RPCServerError as exc:
            body = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": exc.code, "message": exc.message},
            }
            return 200, "application/json", json.dumps(body), {"content-type": "application/json"}
        except TransportFailure as exc:
            return (
                exc.status,
                "application/json",
                json.dumps({"error": exc.message}),
                {"content-type": "application/json"},
            )


def mcp_server_handler(
    *,
    server_info: Optional[dict[str, Any]] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    tool_impl: Optional[Mapping[str, Callable[[dict[str, Any]], Any]]] = None,
    initialized: Optional[Callable[[], None]] = None,
) -> Callable[[str, dict[str, Any], dict[str, str]], Any]:
    """Build a deterministic MCP handler: initialize / ping / tools/list / tools/call."""
    seen = {"initialized": False}
    tool_impl = tool_impl or {}
    tools = tools or []

    def handle(method: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        if method == "initialize":
            seen["initialized"] = True
            if initialized:
                initialized()
            return server_info or {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub", "version": "9.9.9"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools}
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            impl = tool_impl.get(name)
            if impl is None:
                raise RPCServerError(-32602, f"unknown tool: {name}")
            return impl(dict(arguments))
        raise RPCServerError(-32601, f"method not found: {method}")

    return handle


def content_item(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}