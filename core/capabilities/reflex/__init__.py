"""Capability interface for deterministic promoted-procedure execution."""

from __future__ import annotations

from typing import Any, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult
from core.reflexes import ReflexEngine


@register
class ReflexCapability(Capability):
    id = "reflex"
    name = "Reflex Execution"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Dispatch promoted procedures from exact triggers without model replanning"
    permissions = ["public", "reflex:manage", "task:execute"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._identity_id: Optional[str] = None
        self._storage: Any = None

    def install(self, identity_id: str, storage: Any) -> None:
        self._identity_id = identity_id
        self._storage = storage
        storage.save(identity_id, "capability.reflex", {"installed": True})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.reflex")
        storage.delete(identity_id, "reflex.state")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Reflex Execution\n"
            "A reflex may be registered only for a promoted procedure and an exact, "
            "parameterized trigger. Reflex execution queues a durable Executive task "
            "without asking a model to plan it again."
        ]

    _SKILLS = [
        Skill(
            name="reflex.list",
            description="List registered reflexes and whether their pinned procedure is still current",
            permission="public",
            input_schema=object_schema(),
            verification_params={},
        ),
        Skill(
            name="reflex.register",
            description="Bind an exact parameterized trigger to a promoted procedure champion",
            permission="reflex:manage",
            effect="write",
            input_schema=object_schema(
                {
                    "reflex_id": {"type": "string", "minLength": 2},
                    "procedure_id": {"type": "string", "minLength": 2},
                    "trigger_template": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                required=("reflex_id", "procedure_id", "trigger_template"),
            ),
        ),
        Skill(
            name="reflex.execute",
            description="Queue the one promoted procedure matching an exact reflex utterance",
            permission="task:execute",
            effect="execute",
            input_schema=object_schema(
                {"utterance": {"type": "string", "minLength": 1}},
                required=("utterance",),
            ),
        ),
        Skill(
            name="reflex.reconcile",
            description="Verify a reflex run against its durable task plan and execution evidence",
            permission="public",
            input_schema=object_schema(
                {"run_id": {"type": "string", "minLength": 1}},
                required=("run_id",),
            ),
        ),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        try:
            identity_id, engine = self._engine()
            if skill_name == "reflex.list":
                items = engine.list(identity_id)
                data = {"reflexes": items, "count": len(items)}
            elif skill_name == "reflex.register":
                data = engine.register(identity_id, **params).to_dict()
            elif skill_name == "reflex.execute":
                data = engine.dispatch(identity_id, params["utterance"], autostart=True)
                if data is None:
                    return CapabilityResult.fail(
                        self.id, skill_name, "no_match", "no registered reflex matched exactly",
                    )
            elif skill_name == "reflex.reconcile":
                data = engine.reconcile(identity_id, params["run_id"])
            else:
                return CapabilityResult.fail(
                    self.id, skill_name, "unknown_skill", f"Unknown skill: {skill_name}",
                )
            return CapabilityResult.ok(self.id, skill_name, data, source="reflex runtime")
        except Exception as exc:
            return CapabilityResult.fail(
                self.id, skill_name, type(exc).__name__, str(exc),
            )

    def _engine(self) -> tuple[str, ReflexEngine]:
        if self._identity_id is None or self._storage is None:
            raise RuntimeError("reflex is not installed")
        from core.executive.engine import get_executive_for

        executive = get_executive_for(self._storage)
        if executive is None or executive.capability_registry is None:
            raise RuntimeError("no durable Executive with a capability registry is registered")
        return self._identity_id, ReflexEngine(
            self._storage, executive, executive.capability_registry,
        )
