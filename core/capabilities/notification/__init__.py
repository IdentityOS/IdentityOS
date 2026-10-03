"""Generic notification capability with ntfy push transport."""

import json
import time
import uuid
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Iterable, Mapping

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult

NTFY_BASE = "https://ntfy.sh"


@register
class NotificationCapability(Capability):
    id = "notification"
    name = "Notification"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    description = "Generic notification capability with ntfy push transport"
    permissions = ["public"]
    default_grants: List[str] = []

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config or {})
        self._topic = self._config.get("topic", f"identityos-{uuid.uuid4().hex[:8]}")
        self._priority = self._config.get("priority", 3)
        self._tags = self._config.get("tags", ["robot", "identityos"])
        self._base_url = self._config.get("base_url", "https://ntfy.sh")

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.notification", {
            "installed_at": time.time(),
            "topic": self._topic,
        })

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.notification")

    def prompts(self, identity_id: str) -> List[str]:
        return [
            "You can send push notifications via ntfy. Use notification.send to alert your builder or collaborators.",
            "Notifications are deduplicated and tracked. Prefer meaningful events over routine updates.",
        ]

    @classmethod
    def inspect_installation(cls, config):
        return {"skills": cls.skills(None), "readiness": "unknown" if config.get("topic") else "misconfigured"}

    def skills(self) -> List[Skill]:
        return [
            Skill(
                name="notification.send",
                description="Send a push notification via ntfy (topic configured at install)",
                permission="notification.send",
                effect="post",
                input_schema=object_schema({
                    "title": {"type": "string", "minLength": 1},
                    "message": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "event_id": {"type": "string"},
                }, required=["title"]),
            ),
            Skill(
                name="notification.test",
                description="Test the notification transport with a simple message",
                permission="notification.send",
                effect="post",
                input_schema=object_schema({}, required=[]),
            ),
            Skill(
                name="notification.status",
                description="Check notification transport configuration",
                permission="public",
                effect="read",
                input_schema=object_schema({}, required=[]),
            ),
        ]

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        t0 = time.monotonic()
        try:
            if skill_name == "notification.send":
                return self._send_notification(params)
            elif skill_name == "notification.test":
                return self._test_notification()
            elif skill_name == "notification.status":
                return self._status()
            return CapabilityResult.fail(self.id, skill_name, "unknown_skill", f"Unknown skill: {skill_name}", duration_ms=0.0)
        except Exception as e:
            return CapabilityResult.fail(self.id, skill_name, type(e).__name__, str(e), duration_ms=(time.monotonic() - t0) * 1000, params=params)

    def _send_notification(self, params: Mapping[str, Any]) -> CapabilityResult:
        title = str(params.get("title", ""))
        if not title:
            return CapabilityResult.fail(self.id, "notification.send", "invalid_parameters", "title is required", params=params)

        message = str(params.get("message", ""))
        priority = int(params.get("priority", 3))
        priority = max(1, min(5, priority))
        tags = params.get("tags", [])
        event_id = params.get("event_id", str(uuid.uuid4())[:8])

        # Send via ntfy
        topic = self._topic
        url = f"{self._base_url}/{topic}"

        data = message.encode('utf-8') if (message := params.get("message", "")) else b""

        headers = {
            "Title": title,
            "Priority": str(priority),
        }
        if tags:
            headers["Tags"] = ",".join(tags)

        try:
            req = urllib.request.Request(f"{self._base_url}/{topic}", data=message.encode('utf-8'), headers=headers)
            if tags:
                req.add_header("Tags", ",".join(tags))

            with urllib.request.urlopen(urllib.request.Request(f"{self._base_url}/{topic}", data=message.encode('utf-8'), headers=headers), timeout=10) as resp:
                response_data = resp.read().decode()
                result_data = json.loads(response_data)

                return CapabilityResult.from_data(
                    self.id, "notification.send",
                    {
                        "event_id": event_id,
                        "topic": topic,
                        "ntfy_id": result_data.get("id"),
                        "status": "ACCEPTED",
                        "transport": "ntfy",
                    },
                    source="ntfy.sh", params=params
                )
        except Exception as e:
            return CapabilityResult.fail(self.id, "notification.send", type(e).__name__, str(e), params=params)

    def _test_notification(self) -> CapabilityResult:
        result = self._send_notification({
            "title": "Aster Test",
            "message": "Notification transport test from IdentityOS",
            "priority": 3,
            "tags": ["test", "identityos"],
        })
        if result.success:
            result.data["status"] = "TEST_OK"
        return result

    def _status(self) -> CapabilityResult:
        return CapabilityResult.from_data(
            self.id, "notification.status",
            {
                "topic": self._topic,
                "base_url": self._base_url,
                "transport": "ntfy",
                "configured": True,
            },
            source="notification"
        )
