"""Generic MCP capability — talk to any MCP server from an identity.

``mcp.discover`` / ``mcp.inspect`` / ``mcp.ping`` are public read surfaces.
``mcp.call`` requires an explicit ``mcp.call`` grant, refuses dangerous tool
classes outright, and requires explicit ``confirm`` for mutating tools.  Header
values may reference ``secret://<handle>`` handles that are resolved from the
secret store only at execution time.
"""

from __future__ import annotations

import os
import time
from typing import Any, Mapping, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult
from core.interop.mcp import (
    DANGEROUS_RISKS,
    MCPClient,
    MCPServer,
    MCPError,
    MCPServerUnavailable,
    SECRET_REF_PREFIX,
    ToolRisk,
    classify_tool,
)

SECRET_HANDLE = "secret://"


@register
class MCPCapability(Capability):
    id = "mcp"
    name = "MCP"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Generic Model Context Protocol client: discover and invoke tools on any MCP server"
    permissions = ["public"]
    # ``mcp.call`` is intentionally NOT a default grant; it must be explicitly
    # authorized per identity.
    default_grants: list[str] = []

    _TIME = time

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._servers = self._normalize_servers(config)

    @classmethod
    def _normalize_servers(cls, config: Optional[dict]) -> dict[str, dict[str, Any]]:
        config = config or {}
        servers = dict(config.get("servers") or {})
        if not servers:
            url = config.get("url") or os.environ.get("IDENTITY_MCP_URL")
            if url:
                servers["default"] = {"url": url, "headers": dict(config.get("headers") or {})}
        return servers

    # ── lifecycle ──────────────────────────────────────────────────────

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.mcp", {"installed_at": time.time(), "servers": list(self._servers.keys())})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.mcp")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "You can talk to external MCP servers. Use mcp.discover / mcp.inspect to learn what a server offers, "
            "and mcp.call to invoke a tool. Read tools are safe; mutating tools require confirmation and "
            "financially/legally/sensitive tools are refused.",
        ]

    # ── skills ────────────────────────────────────────────────────────

    @classmethod
    def inspect_installation(cls, config: dict) -> dict:
        # skills() is declarative: no constructor, installation, or I/O.
        configured = bool(config.get("servers") or config.get("url") or os.environ.get("IDENTITY_MCP_URL"))
        return {"skills": cls.skills(None), "readiness": "unknown" if configured else "misconfigured",
                "reason": "Configured; remote readiness not verified" if configured else "No MCP server or URL is configured"}

    def skills(self) -> list[Skill]:
        return [
            Skill(
                name="mcp.discover",
                description="Return an MCP server's identity and capabilities (server=name)",
                permission="public",
                input_schema=object_schema({"server": {"type": "string"}}, required=()),
            ),
            Skill(
                name="mcp.inspect",
                description="List an MCP server's tools with risk classification (server=name)",
                permission="public",
                input_schema=object_schema({"server": {"type": "string"}}, required=()),
            ),
            Skill(
                name="mcp.ping",
                description="Confirm an MCP server is reachable (server=name)",
                permission="public",
                input_schema=object_schema({"server": {"type": "string"}}, required=()),
            ),
            Skill(
                name="mcp.call",
                description="Invoke a tool on an MCP server. Provide confirm=true for mutating tools; dangerous tools are refused",
                permission="mcp.call",
                effect="mixed",
                input_schema=object_schema(
                    {
                        "server": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                        "confirm": {"type": "boolean"},
                    },
                    required=("tool",),
                ),
            ),
        ]

    # ── dispatch ──────────────────────────────────────────────────────

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        t0 = self._TIME.monotonic()
        try:
            if skill_name in ("mcp.discover", "mcp.inspect", "mcp.ping", "mcp.call"):
                return self._dispatch(skill_name, params)
            return CapabilityResult.fail(self.id, skill_name, "unknown_skill", f"Unknown skill: {skill_name}", duration_ms=0.0)
        except (MCPError, MCPServerUnavailable) as exc:
            return CapabilityResult.fail(
                self.id, skill_name, type(exc).__name__, str(exc),
                duration_ms=(self._TIME.monotonic() - t0) * 1000, params=params,
            )

    def _dispatch(self, skill_name: str, params: Mapping[str, Any]) -> CapabilityResult:
        server_name = str(params.get("server") or self._config.get("primary") or self._first_server_name())
        server_cfg = self._servers.get(server_name)
        if server_cfg is None:
            return CapabilityResult.fail(
                self.id, skill_name, "unknown_server",
                f"No MCP server named '{server_name}'. Configured: {', '.join(self._servers) or 'none'}",
                params=params,
            )
        client = self._client(server_name, server_cfg)
        store = self._secret_store()

        if skill_name == "mcp.discover":
            info = client.initialize()
            scrubbed = store.scrub_mapping(info) if store else info
            return CapabilityResult.from_data(self.id, skill_name, {"server": server_name, **scrubbed}, source=f"mcp:{server_name}")

        if skill_name == "mcp.inspect":
            tools = [t.to_dict() for t in client.inspect_tools()]
            return CapabilityResult.from_data(self.id, skill_name, {"server": server_name, "tools": tools}, source=f"mcp:{server_name}")

        if skill_name == "mcp.ping":
            client.ping()
            return CapabilityResult.from_data(self.id, skill_name, {"server": server_name, "ok": True}, source=f"mcp:{server_name}")

        return self._call_tool(server_name, client, store, params)

    def _call_tool(self, server_name: str, client: MCPClient, store: Any, params: Mapping[str, Any]) -> CapabilityResult:
        tool_name = str(params.get("tool") or "")
        if not tool_name:
            return CapabilityResult.fail(self.id, "mcp.call", "invalid_parameters", "tool is required", params=params)
        arguments = dict(params.get("arguments") or {})
        confirm = bool(params.get("confirm", False))

        if self._contains_secret_ref(arguments):
            return CapabilityResult.fail(self.id, "mcp.call", "unresolved_secret",
                                         "tool arguments must not contain secret:// or secret-ref:// handles", params=params)

        candidate = next((t for t in client.inspect_tools() if t.name == tool_name), None)
        risk = candidate.risk if candidate else classify_tool(tool_name, "").value
        if risk in DANGEROUS_RISKS:
            return CapabilityResult.fail(self.id, "mcp.call", "denied_by_risk_class",
                                         f"tool '{tool_name}' is classified {risk} and is refused by the generic MCP capability",
                                         params=params)
        if risk in (ToolRisk.POST.value, ToolRisk.MESSAGE.value, ToolRisk.CREATE.value) and not confirm:
            return CapabilityResult.fail(self.id, "mcp.call", "confirmation_required",
                                         f"tool '{tool_name}' is mutating ({risk}); pass confirm=true to invoke it",
                                         params=params)

        result = client.call_tool(tool_name, arguments)
        scrubbed = store.scrub_mapping(result) if store else result
        return CapabilityResult.from_data(
            self.id, "mcp.call", {"server": server_name, "tool": tool_name, "risk": risk, "result": scrubbed},
            source=f"mcp:{server_name}", params={"tool": tool_name, "arguments": arguments},
        )

    # ── helpers ───────────────────────────────────────────────────────

    def _first_server_name(self) -> str:
        return next(iter(self._servers), "default")

    def _secret_store(self):
        from core.secrets.store import SecretStore, default_secret_store_dir

        root = self._config.get("secret_store_dir") or default_secret_store_dir()
        return SecretStore(root)

    def _client(self, server_name: str, server_cfg: dict[str, Any]) -> MCPClient:
        url = str(server_cfg.get("url") or "")
        if not url:
            raise MCPServerUnavailable(f"MCP server '{server_name}' has no url configured")
        store = self._secret_store()
        headers = dict(server_cfg.get("headers") or {})

        def resolver(handle: str) -> Optional[str]:
            if store is not None and store.has(handle):
                return store.get(handle)
            return None

        return MCPClient(
            MCPServer(name=server_name, url=url, headers=headers),
            timeout=float(self._config.get("timeout", 30.0)),
            secret_resolver=resolver,
        )

    @staticmethod
    def _contains_secret_ref(value: Any) -> bool:
        if isinstance(value, str):
            return value.startswith(SECRET_REF_PREFIX) or value.startswith("secret-ref://")
        if isinstance(value, dict):
            return any(MCPCapability._contains_secret_ref(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(MCPCapability._contains_secret_ref(v) for v in value)
        return False