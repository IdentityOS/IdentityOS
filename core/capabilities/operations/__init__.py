"""
operations — capability gateway to the autonomous Operations engine.

This exposes an operator identity's persistent loop to the model: inspect
status, run a bounded tick, and view/authorize escalations.  It contains no
mission logic; it simply delegates to the live :class:`OperationsEngine`.
"""

from __future__ import annotations

from typing import Any, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult
from core.operations.runtime_registry import get_engine_for


@register
class OperationsCapability(Capability):
    id = "operations"
    name = "Operations"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    description = "Autonomous operator loop: needs, opportunities, outreach, monitoring, escalations"
    permissions = ["operations.run"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._storage = None

    def install(self, identity_id: str, storage: Any) -> None:
        self._storage = storage
        storage.save(identity_id, "capability.operations", {"installed_at": None})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.operations")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Operations Engine",
            "You are an autonomous operator. Use operations.status to see real state, "
            "operations.run_tick to advance the loop, and operations.pending_authorizations "
            "to surface decisions that require a human. Never claim an action happened "
            "unless operations.status or operations.provenance shows it did.",
        ]

    _SKILLS = [
        Skill(name="operations.status", description="Return the operator's real persisted state", permission="public", effect="read"),
        Skill(
            name="operations.run_tick",
            description="Advance the operator loop by one bounded tick",
            permission="operations.run",
            effect="write",
            input_schema=object_schema({
                "discover": {"type": "boolean"},
                "evaluate": {"type": "boolean"},
                "act": {"type": "boolean"},
                "monitor": {"type": "boolean"},
                "follow_ups": {"type": "boolean"},
            }),
        ),
        Skill(name="operations.needs", description="List detected needs", permission="public", effect="read"),
        Skill(name="operations.pending_authorizations", description="List outreach awaiting human authorization", permission="public", effect="read"),
        Skill(
            name="operations.authorize",
            description="Approve or reject an escalated outreach message",
            permission="operations.run",
            effect="write",
            input_schema=object_schema(
                {"message_id": {"type": "string"}, "approved": {"type": "boolean"}, "note": {"type": "string"}},
                required=("message_id",),
            ),
        ),
        Skill(
            name="operations.provenance",
            description="Return recent provenance ledger entries",
            permission="public",
            effect="read",
            input_schema=object_schema({"limit": {"type": "integer"}}),
        ),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        import time as _time

        _t0 = _time.monotonic()
        identity_id = str(params.pop("identity_id", "") or params.pop("identity", "") or "")
        engine = get_engine_for(self._storage, identity_id) if self._storage is not None else None
        if engine is None:
            return CapabilityResult.fail(
                "operations", skill_name, "no_engine",
                "No operations engine is registered for this identity/storage.",
            )
        try:
            if skill_name == "operations.status":
                data = engine.status()
            elif skill_name == "operations.run_tick":
                report = engine.tick(
                    discover=bool(params.get("discover", True)),
                    evaluate=bool(params.get("evaluate", True)),
                    act=bool(params.get("act", True)),
                    monitor=bool(params.get("monitor", True)),
                    follow_ups=bool(params.get("follow_ups", True)),
                )
                data = report.to_dict()
            elif skill_name == "operations.needs":
                data = {"needs": [n.to_dict() for n in engine.store.list_needs()]}
            elif skill_name == "operations.pending_authorizations":
                data = {"pending": engine.pending_authorizations()}
            elif skill_name == "operations.authorize":
                data = engine.authorize(
                    str(params.get("message_id", "")),
                    approved=bool(params.get("approved", True)),
                    note=str(params.get("note", "")),
                )
            elif skill_name == "operations.provenance":
                data = {"entries": engine.provenance(limit=int(params.get("limit", 25)))}
            else:
                return CapabilityResult.fail("operations", skill_name, "unknown_skill", f"Unknown skill: {skill_name}")
            return CapabilityResult.from_data(
                "operations", skill_name, data, source="operations engine",
                duration_ms=(_time.monotonic() - _t0) * 1000,
            )
        except Exception as exc:
            return CapabilityResult.fail(
                "operations", skill_name, type(exc).__name__, str(exc),
                duration_ms=(_time.monotonic() - _t0) * 1000,
            )
