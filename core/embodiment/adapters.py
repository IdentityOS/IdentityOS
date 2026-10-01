"""Adapters that bridge existing capabilities into embodiment devices."""

from __future__ import annotations

from typing import Any, Optional

from core.capabilities.registry import lookup

from .models import (
    DEVICE_KINDS,
    DeviceAction,
    DeviceDescriptor,
    DeviceInvocationContext,
    DeviceObservation,
)


class CapabilityDeviceAdapter:
    """Expose installed capability skills through the device adapter contract."""

    def __init__(
        self,
        capability_registry: Any,
        capability_id: str,
        *,
        device_id: str,
        kind: str,
        actions: Optional[list[str]] = None,
        name: str = "",
    ) -> None:
        if kind not in DEVICE_KINDS:
            raise ValueError(f"unsupported device kind: {kind}")
        capability = lookup(capability_id)()
        selected = set(actions or [])
        available = {
            skill.name.rsplit(".", 1)[-1]: skill for skill in capability.skills()
        }
        if selected:
            missing = selected - set(available)
            if missing:
                raise ValueError(
                    "unknown capability device action(s): " + ", ".join(sorted(missing))
                )
            available = {key: value for key, value in available.items() if key in selected}
        self.capability_registry = capability_registry
        self.capability_id = capability_id
        self._skills = {
            action: skill.name for action, skill in available.items()
        }
        self.descriptor = DeviceDescriptor(
            device_id=device_id,
            name=name or capability.name,
            kind=kind,
            actions={
                action: DeviceAction(
                    name=action,
                    description=skill.description,
                    input_schema=skill.input_schema,
                    effect=skill.effect,
                    replay_safe=skill.effect == "read",
                )
                for action, skill in available.items()
            },
            transport="capability-gateway",
            hardware_backed=False,
        )

    def preflight(self, identity_id: str, action: str) -> tuple[bool, str]:
        skill = self._skills.get(action, "")
        if not skill:
            return False, f"unknown device action: {action}"
        return self.capability_registry.can(identity_id, skill)

    def invoke(
        self,
        action: str,
        params: dict[str, Any],
        context: DeviceInvocationContext,
    ) -> DeviceObservation:
        skill = self._skills[action]
        result = self.capability_registry.call(
            context.identity_id,
            skill,
            execution_scope=context.execution_scope,
            **params,
        )
        success = bool(getattr(result, "success", False))
        raw_data = getattr(result, "data", {})
        data = raw_data if isinstance(raw_data, dict) else {"result": raw_data}
        error = getattr(result, "error", None) or {}
        message = error.get("message", str(error)) if error else ""
        return DeviceObservation(
            success=success,
            data=data,
            source=skill,
            evidence_class="runtime",
            hardware_observed=False,
            error=message,
        )
