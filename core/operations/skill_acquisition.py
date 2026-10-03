"""Durable, generic skill → capability acquisition for operator identities.

The operations engine's capability-gap phase asks for a *skill* (``mcp.discover``)
and fills it by acquiring a *capability* (``mcp``).  This resolver performs that
translation through a data-driven prefix table and installs the capability into
the identity's registry — never through a planner special-case.

Acquisition is a local, deterministic install: it does not depend on network
availability, so a success is a runtime fact ("capability installed"), not a
model claim about the external world.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# skill-name prefix -> capability id that provides those skills
SKILL_PREFIX_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("mcp.", "mcp"),
    ("a2a.", "a2a"),
    ("culture_commons.", "culture_commons"),
    ("cc.", "culture_commons"),
)


class SkillAcquisitionResolver:
    """Callable resolver bound to one identity's storage + registry.

    ``__call__(skill)`` matches the contract the engine's CapabilityGapDetector
    expects: ``(ok: bool, detail: str)``.
    """

    def __init__(
        self,
        storage: Any,
        identity_id: str,
        *,
        registry: Any = None,
        secret_store_dir: str = "",
        state_dir: str = "",
        install_config_overrides: Optional[dict[str, dict]] = None,
    ) -> None:
        self._storage = storage
        self._identity_id = identity_id
        self._registry = registry
        self._secret_store_dir = secret_store_dir
        self._state_dir = state_dir
        self._overrides = install_config_overrides or {}

    def __call__(self, skill: str) -> tuple[bool, str]:
        if self._registry is None:
            return (False, "no capability registry wired")
        cap_id = self.provider_for(skill)
        if cap_id is None:
            return (False, f"no interop provider knows skill '{skill}'")
        existing = self._registry.get(self._identity_id, cap_id)
        if existing is not None:
            return (True, f"capability '{cap_id}' already installed")
        config = self._build_config(cap_id)
        self._registry.install(self._identity_id, cap_id, config=config)
        installed = self._registry.get(self._identity_id, cap_id) is not None
        if not installed:
            return (False, f"install of '{cap_id}' did not take effect")
        return (True, f"installed capability '{cap_id}' for skill '{skill}'")

    def provider_for(self, skill: str) -> Optional[str]:
        for prefix, cap_id in SKILL_PREFIX_PROVIDERS:
            if skill.startswith(prefix):
                return cap_id
        return None

    def available_skills(self) -> list[str]:
        samples = [f"{prefix}discover", f"{prefix}inspect"] if prefix != "cc." else ["cc.observe", "cc.speak"]
        return samples

    def _build_config(self, cap_id: str) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if cap_id == "culture_commons":
            if self._secret_store_dir:
                config["secret_store_dir"] = self._secret_store_dir
            if self._state_dir:
                config["state_dir"] = self._state_dir
        config.update(self._overrides.get(cap_id, {}))
        return config


def build_interop_required_skills() -> list[str]:
    """Skills the interop environment provides, used as Aster's default platform needs."""
    return ["mcp.discover", "a2a.discover", "culture_commons.observe"]