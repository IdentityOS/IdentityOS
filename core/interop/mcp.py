"""Generic Model Context Protocol (MCP) client — Streamable HTTP transport.

The client speaks the JSON-RPC subset used by MCP (initialize / ping /
tools/list / tools/call) over a single HTTP endpoint.  Non-2xx statuses,
server-side ``error`` objects, and auth failures are surfaced as typed
exceptions.  Tool discovery is paired with a capability-agnostic risk
classifier so a caller can decide *before* invoking an external tool whether
that tool should be treated as read-only, mutating, or dangerous.

Secrets never live here: header values may reference ``secret://<handle>``
handles that the wiring layer resolves at call time.
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

from .http import HttpClient, HttpResponse, HttpError, parse_body

PROTOCOL_VERSION = "2025-03-26"

SECRET_REF_PREFIX = "secret://"

DANGEROUS_RISKS = frozenset({"financial", "legal", "secret_access", "system_mutation"})


class MCPError(Exception):
    """A JSON-RPC error object returned by the MCP server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.data = data
        reason = " (auth required)" if code == -32001 else ""
        super().__init__(f"MCP error {code}: {message}{reason}")


class MCPProtocolError(MCPError):
    """Response was not a well-formed JSON-RPC payload."""


class MCPServerUnavailable(Exception):
    """Transport-level failure while reaching the MCP server."""


class ToolRisk(str, Enum):
    READ = "read"
    SEARCH = "search"
    OBSERVE = "observe"
    POST = "post"
    MESSAGE = "message"
    CREATE = "create"
    FINANCIAL = "financial"
    LEGAL = "legal"
    SECRET_ACCESS = "secret_access"
    SYSTEM_MUTATION = "system_mutation"


# Boundaries treat snake_case / dashed names like words: ``delete_user`` should
# classify as system mutation, not fall through to READ.  Underscore/hyphen are
# acceptable token separators, so boundaries only care about letters/digits.
_BOUNDARY_OPEN = r"(?<![a-z0-9])"
_BOUNDARY_CLOSE = r"(?![a-z0-9])"

_SYSTEM_MUTATION = re.compile(f"{_BOUNDARY_OPEN}(delete|remove|drop|exec|shell|command|write\\s*file|deploy|shutdown|restart|migrate){_BOUNDARY_CLOSE}")
_LEGAL = re.compile(f"{_BOUNDARY_OPEN}(contract|agree|sign|license|legal|court|arbitrate){_BOUNDARY_CLOSE}")
_SECRET_ACCESS = re.compile(f"{_BOUNDARY_OPEN}(credential|password|secret|api[ _-]?key|token|login)s?{_BOUNDARY_CLOSE}")
_FINANCIAL = re.compile(f"{_BOUNDARY_OPEN}(fund|payment|pay|charge|invoice|transfer|donate|credit|purchase|subscribe){_BOUNDARY_CLOSE}")
_MESSAGE = re.compile(f"{_BOUNDARY_OPEN}(post|send|reply|message|speak|announce|comment){_BOUNDARY_CLOSE}")
_CREATE = re.compile(f"{_BOUNDARY_OPEN}(create|add|make|new|open|start|write|append|clone){_BOUNDARY_CLOSE}")
_OBSERVE = re.compile(f"{_BOUNDARY_OPEN}(observe|watch|monitor|poll|track){_BOUNDARY_CLOSE}")
_SEARCH = re.compile(f"{_BOUNDARY_OPEN}(scan|search|query|find|lookup|filter|inspect){_BOUNDARY_CLOSE}")
_READ = re.compile(f"{_BOUNDARY_OPEN}(read|get|fetch|list|status|show|look|view|describe|info){_BOUNDARY_CLOSE}")


def classify_tool(name: str, description: str = "") -> ToolRisk:
    """Classify an external MCP tool by name + description text (best effort).

    Precedence runs from most intrusive to benign; anything without a known
    verb defaults to ``READ``.  Provider text is untrusted, so the result is a
    boundary hint — the capability wrapper still requires explicit confirmation
    for mutating classes and refuses dangerous ones outright.
    """
    text = f"{name} {description}".lower()
    if _SYSTEM_MUTATION.search(text):
        return ToolRisk.SYSTEM_MUTATION
    if _LEGAL.search(text):
        return ToolRisk.LEGAL
    if _SECRET_ACCESS.search(text):
        return ToolRisk.SECRET_ACCESS
    if _FINANCIAL.search(text):
        return ToolRisk.FINANCIAL
    if _MESSAGE.search(text):
        return ToolRisk.POST if "speak" in _normalize(name) else ToolRisk.MESSAGE
    if _CREATE.search(text):
        return ToolRisk.CREATE
    if _OBSERVE.search(text):
        return ToolRisk.OBSERVE
    if _SEARCH.search(text):
        return ToolRisk.SEARCH
    if _READ.search(text):
        return ToolRisk.READ
    return ToolRisk.READ


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


@dataclass
class MCPServer:
    """Static description of one MCP server endpoint."""

    name: str = "default"
    url: str = ""
    protocol_version: str = PROTOCOL_VERSION
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    risk: str = ToolRisk.READ.value

    @property
    def dangerous(self) -> bool:
        return self.risk in DANGEROUS_RISKS

    @property
    def mutating(self) -> bool:
        return self.risk not in (ToolRisk.READ.value, ToolRisk.SEARCH.value, ToolRisk.OBSERVE.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "risk": self.risk,
            "dangerous": self.dangerous,
            "mutating": self.mutating,
        }


class MCPClient:
    """JSON-RPC MCP client over streamable HTTP.

    Args:
        server: endpoint description.  ``headers`` may embed ``secret://handle``
            references; each is resolved through ``secret_resolver(handle)``
            immediately before the request is sent.
        secret_resolver: callable mapping handle -> plaintext secret value.
        http: optional HttpClient (inject a test transport to avoid the network).
    """

    def __init__(
        self,
        server: MCPServer,
        *,
        timeout: float = 30.0,
        secret_resolver: Optional[Callable[[str], Optional[str]]] = None,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.server = server
        self._secret_resolver = secret_resolver
        self._http = http or HttpClient(timeout=timeout)
        self._session_id: Optional[str] = None
        self._server_info: Optional[dict[str, Any]] = None

    # ── lifecycle ──────────────────────────────────────────────────────

    def initialize(self) -> dict[str, Any]:
        if self._server_info is not None:
            return self._server_info
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": self.server.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "identityos-interop", "version": "0.5.0"},
            },
        )
        self._server_info = result if isinstance(result, dict) else {"data": result}
        try:
            self._rpc("notifications/initialized", {}, notification=True)
        except (MCPError, MCPServerUnavailable, HttpError):
            pass
        return self._server_info

    def ping(self) -> bool:
        self._rpc("ping")
        return True

    def close(self) -> None:
        """Terminate the session (no-op for stateless servers)."""
        if not self._session_id:
            self._session_id = None
            return
        try:
            self._http.delete(self.server.url)
        except (MCPError, MCPServerUnavailable, HttpError):
            pass
        finally:
            self._session_id = None

    # ── tools ──────────────────────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list")
        if isinstance(result, dict):
            return list(result.get("tools", []))
        return []

    def inspect_tools(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for tool in self.list_tools():
            name = str(tool.get("name", ""))
            description = str(tool.get("description", ""))
            specs.append(
                ToolSpec(
                    name=name,
                    description=description,
                    input_schema=tool.get("inputSchema", {}) or {},
                    risk=classify_tool(name, description).value,
                )
            )
        return specs

    def call_tool(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Invoke an MCP tool and return its structured ``content`` result."""
        if self._server_info is None:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})
        if isinstance(result, dict) and "content" in result:
            return result
        return {"content": result}

    # ── internals ──────────────────────────────────────────────────────

    def _resolve_headers(self) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for key, value in self.server.headers.items():
            value = str(value or "")
            if value.startswith(SECRET_REF_PREFIX):
                handle = value[len(SECRET_REF_PREFIX):]
                if self._secret_resolver is None:
                    raise MCPError(-32001, f"header '{key}' needs secret handle '{handle}' but no resolver is wired")
                secret = self._secret_resolver(handle)
                if secret is None:
                    raise MCPError(-32001, f"secret handle '{handle}' is not provisioned")
                resolved[key] = value.replace(SECRET_REF_PREFIX + handle, secret)
            else:
                resolved[key] = value
        return resolved

    def _rpc(self, method: str, params: Optional[dict[str, Any]] = None, *, notification: bool = False) -> Any:
        headers = self._resolve_headers()
        headers.setdefault("accept", "application/json, text/event-stream")
        headers["Mcp-Protocol-Version"] = self.server.protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        payload: dict[str, Any] = {"jsonrpc": "2.0"}
        if not notification:
            payload["id"] = _safe_ident()
        payload["method"] = method
        if params is not None:
            payload["params"] = params

        try:
            response = self._http.post(self.server.url, headers=headers, body=payload)
        except HttpError as exc:
            raise MCPServerUnavailable(str(exc)) from exc

        session_id = response.headers.get("mcp-session-id")
        if not session_id and response.headers.get("Mcp-Session-Id"):
            session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id
        if response.status < 200 or response.status >= 300:
            raise MCPServerUnavailable(f"HTTP {response.status}: {_scalar(parse_body(response.text, response.content_type))}")

        body = parse_body(response.text, response.content_type)

        # Streamable-HTTP servers may return a 202 wrapper with `status` + `body`.
        if isinstance(body, dict) and _status_of(body):
            stream_status = _status_of(body)
            if stream_status >= 400:
                raise MCPError(-32001 if stream_status in (401, 403) else -32000, _scalar(body.get("error") or f"stream HTTP {stream_status}"))
            if "body" in body:
                body = body["body"]

        if notification:
            return None
        if not isinstance(body, dict):
            raise MCPProtocolError(-32603, "response was not a JSON-RPC object")

        error = body.get("error")
        if isinstance(error, dict):
            raise MCPError(_as_int(error.get("code"), -32000), str(error.get("message", "")), error.get("data"))
        if "result" in body:
            return body["result"]
        raise MCPProtocolError(-32603, "response did not contain a JSON-RPC result")


def _safe_ident() -> int:
    return int(_secrets.randbits(28))


def _status_of(value: Any) -> int:
    status = value.get("status") if isinstance(value, dict) else None
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return 0


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return "" if value is None else str(value)
    try:
        return json.dumps(value)[:300]
    except (TypeError, ValueError):
        return str(value)[:300]