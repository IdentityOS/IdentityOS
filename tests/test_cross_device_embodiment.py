"""Cross-device routing, authorization, evidence, and restart tests."""

from __future__ import annotations

import json
import time

import pytest

from core.capabilities.registry import CapabilityRegistry
from core.embodiment import (
    DeviceAction,
    DeviceAuthorizationError,
    DeviceDescriptor,
    DeviceInvocationContext,
    DeviceObservation,
    EmbodimentError,
    EmbodimentHub,
)
from core.executive.engine import ExecutiveRuntime, register_executive
from core.executive.models import TaskStatus, TaskStepStatus
from core.identity import create_identity
from runtime.event_bus import EventType
from runtime.orchestrator import IdentityRuntime
from runtime.persistence import JSONFileBackend

EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class RecordingDevice:
    def __init__(
        self,
        device_id: str,
        kind: str,
        action: str,
        *,
        replay_safe: bool = False,
        hardware_backed: bool = False,
        claim_hardware: bool = False,
        name: str = "",
    ) -> None:
        self.calls = []
        self.claim_hardware = claim_hardware
        self.descriptor = DeviceDescriptor(
            device_id=device_id,
            name=name or f"Test {kind}",
            kind=kind,
            actions={
                action: DeviceAction(
                    action,
                    f"Test {action}",
                    EMPTY_SCHEMA,
                    effect="read" if replay_safe else "execute",
                    replay_safe=replay_safe,
                ),
            },
            transport="test-loopback",
            hardware_backed=hardware_backed,
        )

    def invoke(
        self,
        action: str,
        params: dict,
        context: DeviceInvocationContext,
    ) -> DeviceObservation:
        self.calls.append((action, params, context))
        return DeviceObservation(
            success=True,
            data={"action": action, "ordinal": len(self.calls)},
            source=f"test-loopback:{self.descriptor.device_id}",
            evidence_class=("hardware" if self.claim_hardware else "simulated"),
            hardware_observed=self.claim_hardware,
        )


def _runtime(tmp_path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    registry = CapabilityRegistry(storage)
    executive = ExecutiveRuntime(storage, capability_registry=registry)
    register_executive(executive)
    hub = EmbodimentHub(storage, executive)
    executive.embodiment_hub = hub
    return storage, registry, executive, hub


def _finish(executive, identity_id, task_id):
    for _ in range(30):
        executive.process_ready(identity_id, max_steps=10)
        task = executive.get_task(identity_id, task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}:
            return task
    return executive.get_task(identity_id, task_id)


def test_one_durable_task_routes_across_all_device_kinds(tmp_path):
    storage, _, executive, hub = _runtime(tmp_path)
    identity_id = "embodied-identity"
    devices = [
        RecordingDevice("camera_one", "camera", "capture", replay_safe=True),
        RecordingDevice("voice_one", "voice", "speak"),
        RecordingDevice("browser_one", "browser", "navigate"),
        RecordingDevice("desktop_one", "desktop", "execute"),
    ]
    for device in devices:
        hub.attach(device)
        hub.authorize(
            identity_id,
            device.descriptor.device_id,
            device.descriptor.actions,
        )

    task = hub.start_task(
        identity_id,
        "observe, speak, browse, and execute",
        [
            {
                "device_id": device.descriptor.device_id,
                "action": next(iter(device.descriptor.actions)),
                "params": {},
            }
            for device in devices
        ],
        autostart=False,
    )
    final = _finish(executive, identity_id, task.task_id)

    assert final.status == TaskStatus.COMPLETED
    assert len(final.steps) == 4
    assert all(step.status == TaskStepStatus.COMPLETED for step in final.steps)
    assert all(step.evidence[0].label == "device_observation" for step in final.steps)
    assert all(device.calls[0][2].task_id == task.task_id for device in devices)
    assert all(device.calls[0][2].identity_id == identity_id for device in devices)
    observations = storage.load(identity_id, "embodiment.observations")["observations"]
    assert len(observations) == 4
    assert {item["task_id"] for item in observations} == {task.task_id}
    assert {item["kind"] for item in observations} == {
        "camera", "voice", "browser", "desktop",
    }
    assert all(item["evidence_class"] == "simulated" for item in observations)
    executive.shutdown()


def test_authorization_and_descriptor_changes_fail_closed(tmp_path):
    _, _, executive, hub = _runtime(tmp_path)
    device = RecordingDevice("camera_secure", "camera", "capture")
    hub.attach(device)

    with pytest.raises(DeviceAuthorizationError, match="not authorized"):
        hub.start_task(
            "owner", "capture", [{
                "device_id": "camera_secure", "action": "capture", "params": {},
            }], autostart=False,
        )

    hub.authorize("owner", "camera_secure", ["capture"])
    changed = RecordingDevice(
        "camera_secure", "camera", "capture", name="Replacement camera",
    )
    hub.attach(changed)
    assert hub.list_devices("owner")[0]["ready"] is False
    with pytest.raises(DeviceAuthorizationError, match="reauthorization required"):
        hub.start_task(
            "owner", "capture", [{
                "device_id": "camera_secure", "action": "capture", "params": {},
            }], autostart=False,
        )
    executive.shutdown()


def test_simulated_adapter_cannot_claim_hardware_observation(tmp_path):
    _, _, executive, hub = _runtime(tmp_path)
    device = RecordingDevice(
        "fake_camera", "camera", "capture",
        hardware_backed=False, claim_hardware=True,
    )
    hub.attach(device)
    hub.authorize("owner", "fake_camera", ["capture"])

    with pytest.raises(EmbodimentError, match="cannot claim a hardware observation"):
        hub.invoke("owner", "task-one", "fake_camera", "capture", {})
    executive.shutdown()


def test_task_blocks_offline_after_restart_then_resumes_when_reattached(tmp_path):
    storage, _, executive, hub = _runtime(tmp_path)
    identity_id = "restart-device-owner"
    first = RecordingDevice("first_device", "camera", "capture", replay_safe=True)
    second = RecordingDevice("second_device", "voice", "speak")
    for device in (first, second):
        hub.attach(device)
        hub.authorize(identity_id, device.descriptor.device_id, device.descriptor.actions)
    task = hub.start_task(
        identity_id,
        "continue across a process restart",
        [
            {"device_id": "first_device", "action": "capture", "params": {}},
            {"device_id": "second_device", "action": "speak", "params": {}},
        ],
        autostart=False,
    )
    executive.process_ready(identity_id, max_steps=1)
    before = executive.get_task(identity_id, task.task_id)
    assert before.steps[0].status == TaskStepStatus.COMPLETED
    executive.shutdown()

    restarted_storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    restarted_registry = CapabilityRegistry(restarted_storage)
    restarted_executive = ExecutiveRuntime(
        restarted_storage, capability_registry=restarted_registry,
    )
    register_executive(restarted_executive)
    restarted_hub = EmbodimentHub(restarted_storage, restarted_executive)
    restarted_executive.embodiment_hub = restarted_hub
    restarted_executive.recover(identity_id)

    blocked = _finish(restarted_executive, identity_id, task.task_id)
    assert blocked.status == TaskStatus.BLOCKED
    assert blocked.steps[0].status == TaskStepStatus.COMPLETED
    assert blocked.steps[1].result["block_type"] == "device_unavailable"

    restarted_hub.attach(second)
    restarted_executive.resume_task(identity_id, task.task_id)
    final = _finish(restarted_executive, identity_id, task.task_id)
    assert final.status == TaskStatus.COMPLETED
    assert len(second.calls) == 1
    restarted_executive.shutdown()


def test_real_desktop_capability_runs_through_embodiment_gateway(tmp_path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "store"))
    runtime = IdentityRuntime(storage=storage)
    identity = create_identity("Desktop Bot", identity_id="desktop-bot")
    runtime.register(identity)
    registry = runtime.capability_registry
    registry.install(identity.id, "command_exec")
    registry.install(identity.id, "embodiment")
    registry.grant(identity.id, "command_exec", "process:execute")
    registry.grant(identity.id, "embodiment", "embodiment:execute")
    runtime.embodiment_hub.authorize(identity.id, "desktop_runtime", ["run"])

    started = registry.call(
        identity.id,
        "embodiment.start_task",
        goal="produce observable desktop command output",
        steps=[{
            "device_id": "desktop_runtime",
            "action": "run",
            "params": {"command": "/bin/echo embodiment-evidence", "timeout": 5},
        }],
    )
    assert started.success is True
    task = None
    for _ in range(100):
        task = runtime.executive.get_task(identity.id, started.data["task_id"])
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            break
        time.sleep(0.01)

    assert task.status == TaskStatus.COMPLETED
    observation = task.steps[0].result
    assert observation["data"]["stdout"] == "embodiment-evidence\n"
    assert observation["source"] == "command_exec.run"
    assert observation["evidence_class"] == "runtime"
    assert observation["hardware_observed"] is False
    runtime.shutdown()


def test_secret_bearing_device_parameters_are_not_persisted(tmp_path):
    _, _, executive, hub = _runtime(tmp_path)
    device = RecordingDevice("voice_secure", "voice", "speak")
    hub.attach(device)
    hub.authorize("owner", "voice_secure", ["speak"])

    with pytest.raises(DeviceAuthorizationError, match="secret-bearing"):
        hub.start_task(
            "owner",
            "do not store credentials",
            [{
                "device_id": "voice_secure",
                "action": "speak",
                "params": {"api_token": "must-not-persist"},
            }],
            autostart=False,
        )
    executive.shutdown()


def test_optional_embodiment_failure_does_not_disable_durable_executive(
    tmp_path, monkeypatch,
):
    def broken_adapter(*args, **kwargs):
        raise RuntimeError("adapter discovery failed")

    monkeypatch.setattr("core.embodiment.CapabilityDeviceAdapter", broken_adapter)
    runtime = IdentityRuntime(
        storage=JSONFileBackend(root_dir=str(tmp_path / "store")),
    )

    assert runtime.executive is not None
    assert runtime.reflex_engine is not None
    assert runtime.embodiment_hub is None
    failures = runtime.event_bus.history(EventType.SUBSYSTEM_FAILED)
    assert failures[-1].payload == {
        "subsystem": "embodiment_initialization",
        "error_type": "RuntimeError",
        "error": "adapter discovery failed",
    }
    runtime.shutdown()


def test_sensitive_device_outputs_are_redacted_before_task_persistence(tmp_path):
    class SecretReturningDevice(RecordingDevice):
        def invoke(self, action, params, context):
            return DeviceObservation(
                success=True,
                data={
                    "access_token": "do-not-persist",
                    "nested": {"password": "also-secret", "safe": "visible"},
                },
                source="test-secret-device",
                evidence_class="simulated",
            )

    storage, _, executive, hub = _runtime(tmp_path)
    identity_id = "redacted-observer"
    device = SecretReturningDevice(
        "browser_secret", "browser", "inspect", replay_safe=True,
    )
    hub.attach(device)
    hub.authorize(identity_id, "browser_secret", ["inspect"])

    task = hub.start_task(
        identity_id,
        "observe without persisting credentials",
        [{"device_id": "browser_secret", "action": "inspect", "params": {}}],
        autostart=False,
    )
    final = _finish(executive, identity_id, task.task_id)
    observed = final.steps[0].result["data"]
    persisted = storage.load(identity_id, "embodiment.observations")

    assert observed == {
        "access_token": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "safe": "visible"},
    }
    assert "do-not-persist" not in json.dumps(persisted)
    assert "also-secret" not in json.dumps(persisted)
    executive.shutdown()
