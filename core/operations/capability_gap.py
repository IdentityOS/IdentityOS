"""
core/operations/capability_gap.py

Capability-gap detection and acquisition.

An operator adapts to its environment: if a required skill (e.g. sending email)
is not installed, that is a *runtime fact* to be detected, recorded as a need,
and resolved through the existing capability-acquisition machinery — never
assumed away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from .store import OperationsStore


class CapabilityStatus(str, Enum):
    """Why a required skill is currently unavailable (or whether it is ready)."""

    READY = "ready"
    CAPABILITY_MISSING = "capability_missing"
    AVAILABLE_NOT_INSTALLED = "available_not_installed"
    INSTALLED_PERMISSION_MISSING = "installed_permission_missing"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    CONFIGURATION_ERROR = "configuration_error"
    BUG = "bug"
    EXTERNAL_LIMITATION = "external_limitation"
    UNKNOWN = "unknown"


@dataclass
class CapabilityGap:
    required_skill: str
    category: str = "capability"
    reason: str = ""
    resolved: bool = False
    resolution: str = ""
    evidence: list[str] = field(default_factory=list)
    status: str = CapabilityStatus.CAPABILITY_MISSING.value

    @property
    def classification(self):
        return {
            "installed_permission_missing": "AUTHORITY_GAP",
            "capability_missing": "CAPABILITY_GAP",
            "available_not_installed": "CAPABILITY_GAP",
        }.get(self.status, self.status.upper() if self.status != "ready" else "UNKNOWN")

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_skill": self.required_skill,
            "category": self.category,
            "reason": self.reason,
            "resolved": self.resolved,
            "resolution": self.resolution,
            "evidence": list(self.evidence),
            "status": self.status,
            "classification": self.classification,
        }


class CapabilityGapDetector:
    """Checks required skills against installed capabilities and tries to fill gaps."""

    def __init__(
        self,
        *,
        capability_registry: Any = None,
        identity_id: str = "",
        acquisition: Optional[Callable[[str], tuple[bool, str]]] = None,
        store: Optional[OperationsStore] = None,
        delegation: Optional[Callable[[CapabilityGap], Optional[str]]] = None,
    ) -> None:
        self._registry = capability_registry
        self._identity_id = identity_id
        self._acquisition = acquisition
        self._store = store
        self._delegation = delegation

    def check(self, required_skills: list[str]) -> list[CapabilityGap]:
        gaps: list[CapabilityGap] = []
        for skill in required_skills:
            if self._registry is None:
                # No registry wired in: cannot verify availability. Report the
                # gap honestly rather than claiming the skill exists.
                gaps.append(CapabilityGap(
                    required_skill=skill,
                    reason="no capability registry wired",
                    evidence=["registry=absent"],
                    status=CapabilityStatus.UNKNOWN.value,
                ))
                continue
            try:
                if hasattr(self._registry, "inspect_state"):
                    view = self._registry.inspect_state(self._identity_id)
                    entry = view['skills'].get(skill)
                    if entry and entry['state'] in {'INSTALLED_MISCONFIGURED', 'INSTALLED_DEPENDENCY_UNAVAILABLE', 'INSTALLED_PROVIDER_UNAVAILABLE'}:
                        status = {'INSTALLED_MISCONFIGURED':'configuration_error', 'INSTALLED_DEPENDENCY_UNAVAILABLE':'dependency_unavailable', 'INSTALLED_PROVIDER_UNAVAILABLE':'provider_unavailable'}[entry['state']]
                        gaps.append(CapabilityGap(skill, reason=entry['reason'], status=status, evidence=[entry['state']]))
                        continue
                    if not entry and not view['complete']:
                        gaps.append(CapabilityGap(skill, reason="incomplete provider inspection", status='unknown', evidence=['inspection=incomplete']))
                        continue
                allowed, reason = self._registry.can(self._identity_id, skill)
            except Exception as exc:  # pragma: no cover - defensive
                gaps.append(CapabilityGap(
                    required_skill=skill, reason=f"registry error: {type(exc).__name__}",
                    evidence=["registry=error"],
                    status=CapabilityStatus.UNKNOWN.value,
                ))
                continue
            if not allowed:
                status, detail, evidence = self._diagnose(skill, reason)
                gaps.append(CapabilityGap(
                    required_skill=skill, reason=detail,
                    evidence=evidence, status=status.value,
                ))
        return gaps

    def _diagnose(self, skill: str, permission_reason: str) -> tuple[CapabilityStatus, str, list[str]]:
        """Classify *why* a skill is unavailable.

        Installed but permission-denied beats merely available; available-but-
        not-installed beats entirely missing.  This lets the operator distinguish
        "install it" from "ask the principal to grant `web.search`".
        """
        installed_provider = self._find_installed_provider(skill)
        if installed_provider is not None:
            return (
                CapabilityStatus.INSTALLED_PERMISSION_MISSING,
                permission_reason or f"installed '{installed_provider.id}' but permission denied",
                ["installed=yes", "permission=denied", f"capability={installed_provider.id}"],
            )
        available_cap = self._find_available_provider(skill)
        if available_cap:
            return (
                CapabilityStatus.AVAILABLE_NOT_INSTALLED,
                f"capability '{available_cap}' is available but not installed",
                ["installed=no", f"available_capability={available_cap}"],
            )
        return (
            CapabilityStatus.CAPABILITY_MISSING,
            permission_reason or "no capability provides this skill",
            ["installed=no", "available=no"],
        )

    def _find_installed_provider(self, skill: str) -> Any:
        if self._registry is None:
            return None
        try:
            for cap in self._registry.list(self._identity_id):
                try:
                    if any(s.name == skill for s in cap.skills()):
                        return cap
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _find_available_provider(self, skill: str) -> str:
        """Return a built-in (registered) capability id that provides the skill."""
        try:
            from core.capabilities.registry import available, lookup

            for cap_id in available():
                try:
                    description = lookup(cap_id).inspect_installation({})
                    if any(s.name == skill for s in description.get("skills", [])):
                        return cap_id
                except Exception:
                    continue
        except Exception:
            return ""
        return ""

    def resolve(self, gap: CapabilityGap) -> CapabilityGap:
        """Attempt acquisition through the configured resolver."""
        if gap.status == CapabilityStatus.INSTALLED_PERMISSION_MISSING.value:
            # Acquiring another implementation cannot authorize a denied action.
            # Preserve the diagnosis for the existing principal notification path.
            gap.resolved = False
            gap.resolution = f"PERMISSION_REQUIRED: {gap.reason}"
            self._record_gap_need(gap)
            return gap
        if gap.classification != "CAPABILITY_GAP":
            gap.resolution = "DEFERRED: " + gap.classification + "; gather evidence or repair configuration before acquisition"
            self._record_gap_need(gap)
            return gap
        if self._store is not None:
            import time
            cached = self._store._storage.load(self._identity_id, "operations.gap_attempts") or {}
            previous = cached.get(gap.required_skill, {})
            if previous.get('status') == gap.status and time.time() - previous.get('at', 0) < 300:
                gap.resolution = previous['resolution']
                self._record_gap_need(gap)
                return gap
        if self._acquisition is None:
            gap.resolution = "no acquisition mechanism configured"
            self._record_gap_need(gap)
            return gap
        try:
            ok, detail = self._acquisition(gap.required_skill)
        except Exception as exc:  # pragma: no cover - defensive
            gap.resolution = f"acquisition failed: {exc}"
            self._record_gap_need(gap)
            return gap
        gap.resolved = bool(ok)
        gap.resolution = detail or ("acquired" if ok else "acquisition declined")
        gap.evidence.append(f"acquisition:{gap.resolution}")
        if not gap.resolved and self._delegation is not None:
            try:
                job = self._delegation(gap)
                if job:
                    gap.resolution = f"specification exchange {job}; awaiting agreement, not resolved"
                    gap.evidence.append(f"service_specification:{job}")
            except PermissionError:
                gap.resolution = "PERMISSION_REQUIRED: service delegation denied"
        if self._store is not None and not gap.resolved:
            import time
            cached = self._store._storage.load(self._identity_id, "operations.gap_attempts") or {}
            cached[gap.required_skill] = {'at':time.time(), 'status':gap.status, 'resolution':gap.resolution}
            self._store._storage.save(self._identity_id, "operations.gap_attempts", dict(list(cached.items())[-100:]))
        self._record_gap_need(gap)
        return gap

    def _record_gap_need(self, gap: CapabilityGap) -> None:
        if self._store is None or gap.resolved:
            return
        from .needs import NeedDetector

        NeedDetector.ensure_need(
            self._store,
            category=gap.category,
            description=f"Unresolved {gap.classification} for skill '{gap.required_skill}'",
            evidence=list(gap.evidence),
            urgency=0.6,
            impact=0.7,
        )
