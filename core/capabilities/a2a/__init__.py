"""Generic A2A capability — discover, inspect, and converse with external agents.

``a2a.discover`` and ``a2a.inspect_agent`` are public reads of the peer's agent
card.  ``a2a.send`` / ``a2a.exchange`` / ``a2a.receive`` require an explicit
``a2a.converse`` grant so a full conversation with an external agent is always
an authorized operation.
"""

from __future__ import annotations

import os
import time
from typing import Any, Mapping, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult
from core.interop.a2a import A2AError, A2ANotFound, A2AClient, A2ATask

MAX_MESSAGE_CHARS = 20000


@register
class A2ACapability(Capability):
    id = "a2a"
    name = "A2A"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Agent-to-Agent interoperability: discover an agent's card, inspect its capabilities, and exchange messages"
    permissions = ["public"]
    default_grants: list[str] = []

    _TIME = time

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._agents = dict(config.get("agents") or {})

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.a2a", {"installed_at": time.time(), "agents": list(self._agents.keys())})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.a2a")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "You can interoperate with external agents over A2A. Use a2a.discover and a2a.inspect_agent "
            "to read an agent's public card; use a2a.exchange to hold a conversation only when authorized.",
        ]

    @classmethod
    def inspect_installation(cls, config: dict) -> dict:
        # skills() is declarative: no constructor, installation, or I/O.
        configured = bool(config.get("agents"))
        return {"skills": cls.skills(None), "readiness": "unknown" if configured else "misconfigured",
                "reason": "Configured; remote readiness not verified" if configured else "No external agents are configured"}

    def skills(self) -> list[Skill]:
        return [
            Skill(name="a2a.discover", description="Fetch an external agent's public card (agent=name)", permission="public",
                  input_schema=object_schema({"agent": {"type": "string"}}, required=())),
            Skill(name="a2a.inspect_agent", description="List capabilities an external agent advertises (agent=name)", permission="public",
                  input_schema=object_schema({"agent": {"type": "string"}}, required=())),
            Skill(name="a2a.send", description="Send one message to an external agent, creating a task (agent=name, message=..., session_id=...)",
                  permission="a2a.converse",
                  input_schema=object_schema({"agent": {"type": "string"}, "message": {"type": "string"}, "session_id": {"type": "string"}},
                                             required=("message",))),
            Skill(name="a2a.receive", description="Fetch the state of a previously created task (agent=name, task_id=...)", permission="a2a.converse",
                  input_schema=object_schema({"agent": {"type": "string"}, "task_id": {"type": "string"}}, required=("task_id",))),
            Skill(name="a2a.exchange", description="Send a message and poll the task to completion (agent=name, message=..., session_id=...)",
                  permission="a2a.converse",
                  input_schema=object_schema({"agent": {"type": "string"}, "message": {"type": "string"}, "session_id": {"type": "string"}},
                                             required=("message",))),
        ]

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        t0 = self._TIME.monotonic()
        try:
            return self._dispatch(skill_name, params)
        except (A2AError, A2ANotFound) as exc:
            return CapabilityResult.fail(self.id, skill_name, type(exc).__name__, str(exc),
                                         duration_ms=(self._TIME.monotonic() - t0) * 1000, params=params)

    def _dispatch(self, skill_name: str, params: Mapping[str, Any]) -> CapabilityResult:
        agent_name = str(params.get("agent") or self._config.get("primary") or self._first_agent_name())
        agent_cfg = self._agents.get(agent_name)
        if agent_cfg is None:
            return CapabilityResult.fail(
                self.id, skill_name, "unknown_agent",
                f"No A2A agent named '{agent_name}'. Configured: {', '.join(self._agents) or 'none'}",
                params=params,
            )
        client = A2AClient(str(agent_cfg.get("base_url") or ""), agent_card=agent_cfg.get("card"))

        if skill_name == "a2a.discover":
            card = client.card()
            return CapabilityResult.from_data(
                self.id, skill_name,
                {k: card.get(k) for k in ("name", "title", "description", "version", "url") if k in card},
                source=f"a2a:{agent_name}",
            )

        if skill_name == "a2a.inspect_agent":
            return CapabilityResult.from_data(self.id, skill_name, {"agent": agent_name, "capabilities": client.capabilities()},
                                              source=f"a2a:{agent_name}")

        if skill_name == "a2a.send":
            message = str(params.get("message") or "")
            if not message.strip():
                return CapabilityResult.fail(self.id, skill_name, "invalid_parameters", "message is required", params=params)
            if len(message) > MAX_MESSAGE_CHARS:
                return CapabilityResult.fail(self.id, skill_name, "invalid_parameters", f"message exceeds {MAX_MESSAGE_CHARS} chars", params=params)
            task = client.send_message(message, session_id=str(params.get("session_id") or ""))
            return CapabilityResult.from_data(self.id, skill_name, {"agent": agent_name, "task": task.to_dict()},
                                              source=f"a2a:{agent_name}", params={"message": message})

        if skill_name == "a2a.receive":
            task = client.receive(str(params.get("task_id") or ""))
            return CapabilityResult.from_data(self.id, skill_name, {"agent": agent_name, "task": task.to_dict()},
                                              source=f"a2a:{agent_name}")

        if skill_name == "a2a.exchange":
            message = str(params.get("message") or "")
            if not message.strip():
                return CapabilityResult.fail(self.id, skill_name, "invalid_parameters", "message is required", params=params)
            if len(message) > MAX_MESSAGE_CHARS:
                return CapabilityResult.fail(self.id, skill_name, "invalid_parameters", f"message exceeds {MAX_MESSAGE_CHARS} chars", params=params)
            task: A2ATask = client.exchange(message, session_id=str(params.get("session_id") or ""), poll_max=10)
            return CapabilityResult.from_data(self.id, skill_name, {"agent": agent_name, "task": task.to_dict()},
                                              source=f"a2a:{agent_name}", params={"message": message})

        return CapabilityResult.fail(self.id, skill_name, "unknown_skill", f"Unknown skill: {skill_name}")

    def _first_agent_name(self) -> str:
        return next(iter(self._agents), "default")