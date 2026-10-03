from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from .contracts import model_tool_schema
from .result import CapabilityResult


def object_schema(
    properties: Optional[dict[str, Any]] = None,
    *,
    required: tuple[str, ...] = (),
    additional_properties: bool = False,
) -> dict[str, Any]:
    """Build the object-schema subset accepted by the invocation gateway."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


@dataclass
class Skill:
    name: str
    description: str
    permission: str = "public"
    version: str = "1.0.0"
    input_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    })
    effect: str = "read"
    verification_params: Optional[dict[str, Any]] = None

    def tool_definition(self, *, name: Optional[str] = None) -> dict[str, Any]:
        """Return an OpenAI-compatible definition from the runtime contract."""
        return {
            "type": "function",
            "function": {
                "name": name or self.name,
                "description": self.description,
                "parameters": model_tool_schema(self.input_schema),
            },
        }


class Capability(ABC):
    id: str = ""
    name: str = ""
    version: str = "0.1.0"
    author: str = "unknown"
    license: str = "MIT"
    homepage: str = ""
    description: str = ""
    permissions: list[str] = field(default_factory=lambda: ["public"])
    # Permissions granted by an explicit install operation. Sensitive scopes
    # such as credential use should never be listed here.
    default_grants: list[str] = []

    def __init__(self, config: Optional[dict] = None) -> None:
        self._config = config or {}

    @classmethod
    def inspect_installation(cls, config: dict) -> dict:
        """Pure declaration for read-only introspection; never instantiate/install.

        Dynamic capabilities may override this with a bounded, local descriptor.
        UNKNOWN means no readiness evidence, not a failed invocation.
        """
        return {"skills": list(getattr(cls, "_SKILLS", [])),
                "readiness": getattr(cls, "inspection_readiness", "unknown"),
                "complete": hasattr(cls, "_SKILLS")}

    # ── Lifecycle ──────────────────────────────────────────────────────

    @abstractmethod
    def install(self, identity_id: str, storage: Any) -> None:
        """Set up storage namespaces, register state, etc."""

    @abstractmethod
    def uninstall(self, identity_id: str, storage: Any) -> None:
        """Clean up all storage artifacts created by this capability."""

    # ── Runtime interface ──────────────────────────────────────────────

    @abstractmethod
    def prompts(self, identity_id: str) -> list[str]:
        """Prompt fragments injected into the system message."""

    @abstractmethod
    def skills(self) -> list[Skill]:
        """All skills this capability exposes."""

    def tool_defs(self) -> list[dict]:
        """OpenAI-compatible tool definitions for each skill.

        Override to provide precise parameter schemas.
        """
        return [s.tool_definition() for s in self.skills()]

    def can(self, skill_name: str) -> tuple[bool, str]:
        for s in self.skills():
            if s.name == skill_name:
                return (True, "")
        return (False, f"Unknown skill: {skill_name}")

    @abstractmethod
    def call(self, skill_name: str, **params: Any) -> Any:
        """Execute a skill and return the result."""

    def call_scoped(
        self,
        skill_name: str,
        *,
        execution_scope: Optional[str] = None,
        adapter: Any = None,
        **params: Any,
    ) -> Any:
        """Execute with an optional runtime/user isolation scope.

        Most capabilities are stateless and can ignore the scope. Stateful
        capabilities may override this without leaking transport metadata into
        their public model-facing parameter schema.

        Args:
            adapter: Optional model adapter for vision/multimodal operations.
        """
        return self.call(skill_name, **params)

    # ── Event hooks (reserved — no-op by default) ─────────────────────

    def on_message(self, message: Any) -> None:
        pass

    def on_identity_loaded(self, identity_id: str) -> None:
        pass

    def on_memory_created(self, memory: Any) -> None:
        pass

    def on_goal_completed(self, goal: Any) -> None:
        pass

    # ── Inspection ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "license": self.license,
            "homepage": self.homepage,
            "description": self.description,
            "permissions": self.permissions,
            "default_grants": self.default_grants,
            "skills": [s.name for s in self.skills()],
            "skill_contracts": [
                {
                    "name": s.name,
                    "description": s.description,
                    "permission": s.permission,
                    "effect": s.effect,
                    "input_schema": s.input_schema,
                }
                for s in self.skills()
            ],
        }
