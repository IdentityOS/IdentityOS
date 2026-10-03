from __future__ import annotations

import os
import platform
import shutil
from typing import Any, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult


@register
class SystemInfoCapability(Capability):
    inspection_readiness = "ready"
    id = "system_info"
    name = "System Information"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Get OS information, disk usage, and system details"
    permissions = ["local"]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.system_info", {"installed_at": None})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.system_info")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## System Info Skills (MANDATORY — use for OS, disk, system questions)",
            "When the user asks about the operating system, disk space, or system details, you MUST use the skills below.",
            "Do NOT say you cannot access system information. You CAN. Use the skills.",
        ]

    _SKILLS = [
        Skill(name="system_info.os", description="Get operating system name, version, and architecture", permission="local", input_schema=object_schema(), verification_params={}),
        Skill(name="system_info.disk", description="Get disk usage information (total, used, free)", permission="local", input_schema=object_schema({"path": {"type": "string"}}), verification_params={"path": "/"}),
        Skill(name="system_info.cpu", description="Get CPU information (count, architecture)", permission="local", input_schema=object_schema(), verification_params={}),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        import time as _time
        _t0 = _time.monotonic()
        try:
            dispatch = {
                "system_info.os": self._os,
                "system_info.disk": self._disk,
                "system_info.cpu": self._cpu,
            }
            handler = dispatch.get(skill_name)
            if handler is None:
                return CapabilityResult.fail("system_info", skill_name, "unknown_skill", f"Unknown skill: {skill_name}")
            data = handler(**params)
            return CapabilityResult.from_data("system_info", skill_name, data, source="system API", duration_ms=(_time.monotonic() - _t0) * 1000)
        except Exception as e:
            return CapabilityResult.fail("system_info", skill_name, type(e).__name__, str(e), duration_ms=(_time.monotonic() - _t0) * 1000)

    def _os(self, **kwargs: Any) -> dict[str, Any]:
        uname = platform.uname()
        return {
            "system": uname.system,
            "node": uname.node,
            "release": uname.release,
            "version": uname.version,
            "machine": uname.machine,
            "processor": uname.processor,
        }

    def _disk(self, path: str = "/", **kwargs: Any) -> dict[str, Any]:
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }

    def _cpu(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "count": os.cpu_count() or 0,
            "architecture": platform.machine(),
            "processor": platform.processor() or "unknown",
        }
