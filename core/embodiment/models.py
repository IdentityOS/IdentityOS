"""Contracts for authorized device adapters and their observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

DEVICE_KINDS = {"camera", "voice", "browser", "desktop"}
EVIDENCE_CLASSES = {"hardware", "runtime", "simulated"}


@dataclass(frozen=True)
class DeviceAction:
    name: str
    description: str
    input_schema: dict[str, Any]
    effect: str = "read"
    replay_safe: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "effect": self.effect,
            "replay_safe": self.replay_safe,
        }


@dataclass(frozen=True)
class DeviceDescriptor:
    device_id: str
    name: str
    kind: str
    actions: dict[str, DeviceAction]
    transport: str = "local"
    hardware_backed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "actions": {
                name: action.to_dict() for name, action in self.actions.items()
            },
            "transport": self.transport,
            "hardware_backed": self.hardware_backed,
        }


@dataclass(frozen=True)
class DeviceInvocationContext:
    identity_id: str
    task_id: str
    device_id: str
    execution_scope: str


@dataclass
class DeviceObservation:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    evidence_class: str = "runtime"
    hardware_observed: bool = False
    error: str = ""


class DeviceAdapter(Protocol):
    descriptor: DeviceDescriptor

    def invoke(
        self,
        action: str,
        params: dict[str, Any],
        context: DeviceInvocationContext,
    ) -> DeviceObservation:
        ...
