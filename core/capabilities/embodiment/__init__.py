"""Model-facing access to host-authorized cross-device task execution."""

from __future__ import annotations

from typing import Any, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult


@register
class EmbodimentCapability(Capability):
    id = "embodiment"
    name = "Cross-Device Embodiment"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Use host-authorized cameras, voice, browsers, and desktops in one durable task"
    permissions = ["public", "embodiment:execute"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._identity_id: Optional[str] = None
        self._storage: Any = None

    def install(self, identity_id: str, storage: Any) -> None:
        self._identity_id = identity_id
        self._storage = storage
        storage.save(identity_id, "capability.embodiment", {"installed": True})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.embodiment")
        storage.delete(identity_id, "embodiment.authorizations")
        storage.delete(identity_id, "embodiment.observations")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Cross-Device Embodiment\n"
            "Use embodiment.list_devices before requesting a device task. Only "
            "host-authorized actions are available. Device tasks are durable and "
            "their observations are evidence; never describe unattached or simulated "
            "devices as physical observations."
        ]

    _SKILLS = [
        Skill(
            name="embodiment.list_devices",
            description="List this identity's authorized devices, actions, attachment state, and evidence class",
            permission="public",
            input_schema=object_schema(),
            verification_params={},
        ),
        Skill(
            name="embodiment.start_task",
            description="Start one durable task containing one or more authorized device actions",
            permission="embodiment:execute",
            effect="execute",
            input_schema=object_schema(
                {
                    "goal": {"type": "string", "minLength": 1},
                    "steps": {"type": "array"},
                },
                required=("goal", "steps"),
            ),
        ),
        Skill(
            name="embodiment.task_status",
            description="Read durable status and evidence counts for a cross-device task",
            permission="public",
            input_schema=object_schema(
                {"task_id": {"type": "string", "minLength": 1}},
                required=("task_id",),
            ),
        ),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        try:
            identity_id, hub = self._hub()
            if skill_name == "embodiment.list_devices":
                devices = hub.list_devices(identity_id)
                data = {"devices": devices, "count": len(devices)}
            elif skill_name == "embodiment.start_task":
                task = hub.start_task(
                    identity_id,
                    params["goal"],
                    params["steps"],
                    autostart=True,
                )
                data = {
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "steps": len(task.steps),
                    "identity_id": identity_id,
                }
            elif skill_name == "embodiment.task_status":
                data = hub.executive.task_status(identity_id, params["task_id"])
            else:
                return CapabilityResult.fail(
                    self.id, skill_name, "unknown_skill", f"Unknown skill: {skill_name}",
                )
            return CapabilityResult.ok(
                self.id, skill_name, data, source="embodiment runtime",
            )
        except Exception as exc:
            return CapabilityResult.fail(
                self.id, skill_name, type(exc).__name__, str(exc),
            )

    def _hub(self) -> tuple[str, Any]:
        if self._identity_id is None or self._storage is None:
            raise RuntimeError("embodiment is not installed")
        from core.executive.engine import get_executive_for

        executive = get_executive_for(self._storage)
        hub = getattr(executive, "embodiment_hub", None) if executive else None
        if hub is None:
            raise RuntimeError("no durable Executive embodiment hub is registered")
        return self._identity_id, hub
