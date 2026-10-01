"""Authorized multi-device routing into one durable Executive task."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from core.capabilities.contracts import validate_parameters
from core.executive.models import ReplayPolicy, TaskStep

from .models import (
    DEVICE_KINDS,
    EVIDENCE_CLASSES,
    DeviceAdapter,
    DeviceDescriptor,
    DeviceInvocationContext,
    DeviceObservation,
)
from .store import EmbodimentStore

_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SENSITIVE = (
    "api_key", "authorization", "cookie", "credential", "password", "secret", "token",
)
_MAX_OBSERVATION_BYTES = 64 * 1024
_REDACTED = "[REDACTED]"


class EmbodimentError(RuntimeError):
    pass


class DeviceUnavailableError(EmbodimentError):
    pass


class DeviceAuthorizationError(EmbodimentError):
    pass


class DeviceInvocationError(EmbodimentError):
    """An attached adapter failed after invocation began; outcome may be unknown."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(part in str(key).lower() for part in _SENSITIVE):
                return True
            if _contains_sensitive_key(item):
                return True
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if any(part in str(key).lower() for part in _SENSITIVE)
                else _redact_sensitive_values(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    return value


class EmbodimentHub:
    def __init__(self, storage: Any, executive: Any) -> None:
        self.storage = storage
        self.executive = executive
        self.store = EmbodimentStore(storage)
        self._adapters: dict[str, DeviceAdapter] = {}
        self._lock = threading.RLock()

    def attach(self, adapter: DeviceAdapter) -> DeviceDescriptor:
        descriptor = adapter.descriptor
        self._validate_descriptor(descriptor)
        with self._lock:
            self._adapters[descriptor.device_id] = adapter
        return descriptor

    def detach(self, device_id: str) -> bool:
        with self._lock:
            return self._adapters.pop(device_id, None) is not None

    def authorize(
        self,
        identity_id: str,
        device_id: str,
        actions: Iterable[str],
    ) -> dict[str, Any]:
        adapter = self._adapters.get(device_id)
        if adapter is None:
            raise DeviceUnavailableError(f"device is not attached: {device_id}")
        requested = sorted(set(actions))
        if not requested:
            raise DeviceAuthorizationError("at least one device action is required")
        missing = set(requested) - set(adapter.descriptor.actions)
        if missing:
            raise DeviceAuthorizationError(
                "device does not expose action(s): " + ", ".join(sorted(missing))
            )
        preflight = getattr(adapter, "preflight", None)
        if callable(preflight):
            for action in requested:
                allowed, reason = preflight(identity_id, action)
                if not allowed:
                    raise DeviceAuthorizationError(
                        f"underlying capability denied {device_id}.{action}: {reason}"
                    )
        record = {
            "device_id": device_id,
            "kind": adapter.descriptor.kind,
            "actions": requested,
            "descriptor_sha256": _digest(adapter.descriptor.to_dict()),
            "descriptor": adapter.descriptor.to_dict(),
            "authorized_at": _now(),
        }
        with self._lock:
            grants = self.store.authorizations(identity_id)
            grants[device_id] = record
            self.store.save_authorizations(identity_id, grants)
        return record

    def revoke(self, identity_id: str, device_id: str) -> bool:
        with self._lock:
            grants = self.store.authorizations(identity_id)
            existed = grants.pop(device_id, None) is not None
            self.store.save_authorizations(identity_id, grants)
        return existed

    def list_devices(self, identity_id: str) -> list[dict[str, Any]]:
        records = []
        for device_id, grant in self.store.authorizations(identity_id).items():
            adapter = self._adapters.get(device_id)
            attached = adapter is not None
            descriptor_matches = bool(
                adapter is not None
                and _digest(adapter.descriptor.to_dict()) == grant.get("descriptor_sha256")
            )
            records.append({
                "device_id": device_id,
                "kind": grant.get("kind"),
                "actions": list(grant.get("actions", [])),
                "attached": attached,
                "ready": attached and descriptor_matches,
                "descriptor_matches": descriptor_matches,
                "hardware_backed": bool(
                    (grant.get("descriptor") or {}).get("hardware_backed", False)
                ),
            })
        return records

    def start_task(
        self,
        identity_id: str,
        goal: str,
        steps: list[dict[str, Any]],
        *,
        autostart: bool = True,
    ) -> Any:
        if not goal.strip():
            raise EmbodimentError("device task goal is required")
        if not steps or len(steps) > 32:
            raise EmbodimentError("device task requires between 1 and 32 steps")
        planned = []
        for index, raw in enumerate(steps, 1):
            if not isinstance(raw, dict):
                raise EmbodimentError(f"device step {index} must be an object")
            device_id = str(raw.get("device_id", ""))
            action = str(raw.get("action", ""))
            params = raw.get("params", {})
            if not isinstance(params, dict):
                raise EmbodimentError(f"device step {index} params must be an object")
            action_spec = self.preflight(identity_id, device_id, action, params)
            planned.append(TaskStep(
                action="device_action",
                description=str(
                    raw.get("description") or f"Use {device_id}.{action}"
                ),
                params={
                    "device_id": device_id,
                    "device_action": action,
                    "device_params": params,
                },
                replay_policy=(
                    ReplayPolicy.RETRY if action_spec.replay_safe else ReplayPolicy.BLOCK
                ),
                max_retries=2 if action_spec.replay_safe else 1,
            ))
        return self.executive.start_task(
            goal=goal,
            identity_id=identity_id,
            steps=planned,
            autostart=autostart,
        )

    def preflight(
        self,
        identity_id: str,
        device_id: str,
        action: str,
        params: dict[str, Any],
    ) -> Any:
        if _contains_sensitive_key(params):
            raise DeviceAuthorizationError(
                "durable device tasks cannot persist secret-bearing parameters"
            )
        adapter = self._adapters.get(device_id)
        if adapter is None:
            raise DeviceUnavailableError(f"device is not attached: {device_id}")
        grant = self.store.authorizations(identity_id).get(device_id)
        if grant is None:
            raise DeviceAuthorizationError(
                f"identity is not authorized for device: {device_id}"
            )
        if _digest(adapter.descriptor.to_dict()) != grant.get("descriptor_sha256"):
            raise DeviceAuthorizationError(
                f"device descriptor changed; reauthorization required: {device_id}"
            )
        if action not in grant.get("actions", []):
            raise DeviceAuthorizationError(
                f"identity is not authorized for {device_id}.{action}"
            )
        action_spec = adapter.descriptor.actions.get(action)
        if action_spec is None:
            raise DeviceAuthorizationError(f"device action no longer exists: {action}")
        preflight = getattr(adapter, "preflight", None)
        if callable(preflight):
            allowed, reason = preflight(identity_id, action)
            if not allowed:
                raise DeviceAuthorizationError(reason)
        validate_parameters(action_spec.input_schema, params)
        return action_spec

    def invoke(
        self,
        identity_id: str,
        task_id: str,
        device_id: str,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self.preflight(identity_id, device_id, action, params)
        adapter = self._adapters[device_id]
        try:
            observation = adapter.invoke(
                action,
                params,
                DeviceInvocationContext(
                    identity_id=identity_id,
                    task_id=task_id,
                    device_id=device_id,
                    execution_scope=f"task:{task_id}:device:{device_id}",
                ),
            )
        except Exception as exc:
            raise DeviceInvocationError(
                f"{device_id}.{action} raised after invocation began: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(observation, DeviceObservation):
            raise EmbodimentError("device adapter did not return a DeviceObservation")
        if observation.evidence_class not in EVIDENCE_CLASSES:
            raise EmbodimentError("device adapter returned an invalid evidence class")
        if observation.hardware_observed and not adapter.descriptor.hardware_backed:
            raise EmbodimentError(
                "a non-hardware adapter cannot claim a hardware observation"
            )
        if not isinstance(observation.data, dict):
            raise EmbodimentError("device observation data must be an object")
        safe_data = _redact_sensitive_values(observation.data)
        encoded = json.dumps(
            safe_data, sort_keys=True, separators=(",", ":"),
        ).encode()
        if len(encoded) > _MAX_OBSERVATION_BYTES:
            raise EmbodimentError("device observation exceeds the 64 KiB evidence limit")
        record = {
            "observation_id": str(uuid.uuid4()),
            "identity_id": identity_id,
            "task_id": task_id,
            "device_id": device_id,
            "kind": adapter.descriptor.kind,
            "action": action,
            "success": observation.success,
            "source": observation.source,
            "evidence_class": observation.evidence_class,
            "hardware_observed": observation.hardware_observed,
            "data": safe_data,
            "data_sha256": hashlib.sha256(encoded).hexdigest(),
            "error": observation.error,
            "observed_at": _now(),
        }
        with self._lock:
            self.store.append_observation(identity_id, record)
        return record

    @staticmethod
    def _validate_descriptor(descriptor: DeviceDescriptor) -> None:
        if not _ID.fullmatch(descriptor.device_id):
            raise EmbodimentError("invalid device id")
        if descriptor.kind not in DEVICE_KINDS:
            raise EmbodimentError(f"unsupported device kind: {descriptor.kind}")
        if not descriptor.actions:
            raise EmbodimentError("a device must expose at least one action")
        for name, action in descriptor.actions.items():
            if not _ID.fullmatch(name) or action.name != name:
                raise EmbodimentError(f"invalid device action: {name}")
            if action.input_schema.get("type") != "object":
                raise EmbodimentError(f"device action {name} requires an object schema")
