"""Model-facing access to evidence-backed procedure learning."""

from __future__ import annotations

from typing import Any, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult
from core.procedures import ProcedureLearner, ProcedureStore


@register
class ProcedureLearningCapability(Capability):
    id = "procedure_learning"
    name = "Procedure Learning"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Generalize verified multi-step tasks and promote them against protected held-out examples"
    permissions = ["public", "procedure:learn"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._identity_id: Optional[str] = None
        self._storage: Any = None

    def install(self, identity_id: str, storage: Any) -> None:
        self._identity_id = identity_id
        self._storage = storage
        storage.save(identity_id, "capability.procedure_learning", {"installed": True})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.procedure_learning")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Procedure Learning\n"
            "Use procedure_learning.learn only for a completed, evidenced multi-step Executive task "
            "and a host-registered held-out suite. A rejected candidate is not reusable."
        ]

    _SKILLS = [
        Skill(
            name="procedure_learning.list",
            description="List learned procedures and their promoted held-out scores",
            permission="public",
            input_schema=object_schema(),
            verification_params={},
        ),
        Skill(
            name="procedure_learning.learn",
            description=(
                "Learn a parameterized procedure from a completed Executive task "
                "using a protected held-out suite"
            ),
            permission="procedure:learn",
            effect="write",
            input_schema=object_schema(
                {
                    "task_id": {"type": "string", "minLength": 1},
                    "procedure_id": {"type": "string", "minLength": 2},
                    "bindings": {"type": "object"},
                    "held_out_suite_id": {"type": "string", "minLength": 1},
                    "name": {"type": "string"},
                    "minimum_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                required=("task_id", "procedure_id", "bindings", "held_out_suite_id"),
            ),
        ),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        try:
            if skill_name == "procedure_learning.list":
                return CapabilityResult.ok(
                    self.id, skill_name, self._list(), source="procedure library",
                )
            if skill_name == "procedure_learning.learn":
                return CapabilityResult.ok(
                    self.id, skill_name, self._learn(**params), source="held-out evaluator",
                )
            return CapabilityResult.fail(
                self.id, skill_name, "unknown_skill", f"Unknown skill: {skill_name}",
            )
        except Exception as exc:
            return CapabilityResult.fail(
                self.id, skill_name, type(exc).__name__, str(exc),
            )

    def _require_install(self) -> tuple[str, Any]:
        if self._identity_id is None or self._storage is None:
            raise RuntimeError("procedure_learning is not installed")
        return self._identity_id, self._storage

    def _list(self) -> dict[str, Any]:
        identity_id, storage = self._require_install()
        items = []
        for procedure in ProcedureStore(storage).list(identity_id):
            champion = procedure.champion
            items.append({
                "procedure_id": procedure.procedure_id,
                "name": procedure.name,
                "versions": len(procedure.versions),
                "champion_version": procedure.champion_version,
                "held_out_score": champion.held_out_score if champion else None,
            })
        return {"procedures": items, "count": len(items)}

    def _learn(
        self,
        task_id: str,
        procedure_id: str,
        bindings: dict[str, Any],
        held_out_suite_id: str,
        name: str = "",
        minimum_score: float = 0.8,
        **kwargs: Any,
    ) -> dict[str, Any]:
        identity_id, storage = self._require_install()
        from core.executive.engine import get_executive_for

        executive = get_executive_for(storage)
        if executive is None:
            raise RuntimeError("no durable Executive is registered")
        task = executive.get_task(identity_id, task_id)
        version = ProcedureLearner(storage).learn(
            identity_id=identity_id,
            procedure_id=procedure_id,
            source_task=task,
            parameter_bindings=bindings,
            held_out_suite_id=held_out_suite_id,
            name=name,
            minimum_score=minimum_score,
        )
        return {
            "procedure_id": procedure_id,
            "version": version.version,
            "status": version.status,
            "held_out_score": version.held_out_score,
            "template_sha256": version.template_sha256,
            "rejection_reason": version.rejection_reason,
        }
