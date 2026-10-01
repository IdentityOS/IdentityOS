from __future__ import annotations

import re
import time
from typing import Any, Optional

from core.channels.context import authorize_skill, current_channel

from .base import Capability, Skill
from .contracts import (
    CapabilityContractError,
    normalize_parameters,
    validate_parameters,
)
from .result import CapabilityResult

# ── v0: static in-process registry ─────────────────────────────────────
# Future: entry_points discovery, pip-installed packages, plugins/
_BUILTIN_CAPABILITIES: dict[str, type[Capability]] = {}


def register(cap_cls: type[Capability]) -> type[Capability]:
    _BUILTIN_CAPABILITIES[cap_cls.id] = cap_cls
    return cap_cls


def lookup(cap_id: str) -> type[Capability]:
    cls = _BUILTIN_CAPABILITIES.get(cap_id)
    if cls is None:
        raise ValueError(
            f"Unknown capability '{cap_id}'. "
            f"Available: {list(_BUILTIN_CAPABILITIES.keys())}"
        )
    return cls


def available() -> list[str]:
    return list(_BUILTIN_CAPABILITIES.keys())


# ── Per-identity registry ──────────────────────────────────────────────


class CapabilityRegistry:
    """
    Manages installed capabilities for identities.

    Each identity stores its installed-capability list under the
    ``capabilities`` namespace in its storage backend.  On load the
    registry re-instantiates ``Capability`` objects so they can inject
    prompts, expose skills, and handle ``call()``.
    """

    CAP_NAMESPACE = "capabilities"

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        # identity_id -> {cap_id: Capability}
        self._loaded: dict[str, dict[str, Capability]] = {}

    # ── Internal helpers ───────────────────────────────────────────────

    def _load_identity_caps(self, identity_id: str) -> dict[str, Capability]:
        if identity_id not in self._loaded:
            raw = self._storage.load(identity_id, self.CAP_NAMESPACE)
            entries = raw.get("installed", []) if raw else []
            instances: dict[str, Capability] = {}
            for entry in entries:
                cap_id = entry["id"]
                config = entry.get("config", {})
                try:
                    cls = self._resolve_class(identity_id, cap_id)
                    inst = cls(config=config)
                    inst.install(identity_id, self._storage)
                    for permission in inst.default_grants:
                        self.grant(identity_id, cap_id, permission)
                    instances[cap_id] = inst
                except ValueError:
                    pass  # skip capabilities whose class isn't loaded
            self._loaded[identity_id] = instances
        return self._loaded[identity_id]

    def _save(self, identity_id: str) -> None:
        instances = self._loaded.get(identity_id, {})
        entries = [
            {"id": c.id, "version": c.version, "config": c._config}
            for c in instances.values()
        ]
        self._storage.save(identity_id, self.CAP_NAMESPACE, {"installed": entries})

    # ── Public API ─────────────────────────────────────────────────────

    def install(
        self, identity_id: str, cap_id: str, config: Optional[dict] = None
    ) -> Capability:
        cls = self._resolve_class(identity_id, cap_id)
        cap = cls(config=config or {})
        cap.install(identity_id, self._storage)
        caps = self._load_identity_caps(identity_id)
        caps[cap_id] = cap
        self._save(identity_id)
        for permission in cap.default_grants:
            self.grant(identity_id, cap_id, permission)
        return cap

    def _resolve_class(self, identity_id: str, cap_id: str) -> type[Capability]:
        """Prefer an identity's content-verified portable package when present."""
        try:
            from core.skill_forge.loader import persisted_capability_class

            forged = persisted_capability_class(self._storage, identity_id, cap_id)
            if forged is not None:
                return forged
        except ImportError:
            pass
        return lookup(cap_id)

    def uninstall(self, identity_id: str, cap_id: str) -> None:
        caps = self._load_identity_caps(identity_id)
        if cap_id in caps:
            caps[cap_id].uninstall(identity_id, self._storage)
            del caps[cap_id]
            self._save(identity_id)
            self._revoke_capability_grants(identity_id, cap_id)

    def get(self, identity_id: str, cap_id: str) -> Optional[Capability]:
        return self._load_identity_caps(identity_id).get(cap_id)

    def list(self, identity_id: str) -> list[Capability]:
        return list(self._load_identity_caps(identity_id).values())

    def all_prompts(self, identity_id: str) -> list[str]:
        prompts: list[str] = []
        for cap in self.list(identity_id):
            if current_channel.get() is None or any(authorize_skill(skill.name)[0] for skill in cap.skills()):
                prompts.extend(cap.prompts(identity_id))
        return prompts

    def all_skills(self, identity_id: str) -> list[Skill]:
        skills: list[Skill] = []
        for cap in self.list(identity_id):
            skills.extend(cap.skills())
        return skills

    def get_skill(self, identity_id: str, skill_name: str) -> Optional[Skill]:
        cap = self._find_capability_for_skill(identity_id, skill_name)
        if cap is None:
            return None
        return next((skill for skill in cap.skills() if skill.name == skill_name), None)

    def tool_catalog(
        self,
        identity_id: str,
        *,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> tuple[list[dict], dict[str, str]]:
        """Return an authorized, optionally relevance-bounded model tool catalog.

        Tool schemas count toward every provider request.  Identities can acquire
        many capabilities over time, so passing the entire catalog makes normal
        turns progressively slower and can exhaust provider token quotas.  Query
        ranking is deliberately capability-agnostic: it only compares words in
        the request with each tool's public name and description.
        """
        catalog: list[tuple[dict, str, str]] = []
        for cap in self.list(identity_id):
            for skill in cap.skills():
                allowed, _ = self._authorized(identity_id, cap.id, skill.permission)
                if not allowed or not authorize_skill(skill.name)[0]:
                    continue
                safe_name = skill.name.replace(".", "__")
                catalog.append((skill.tool_definition(name=safe_name), safe_name, skill.name))

        if limit is not None:
            bounded_limit = max(0, int(limit))
            if query:
                input_words = set(re.findall(r"[a-z0-9]+", query.lower()))
                ranked: list[tuple[int, int, tuple[dict, str, str]]] = []
                for position, item in enumerate(catalog):
                    definition, safe_name, _ = item
                    function = definition.get("function", {})
                    searchable = " ".join((
                        safe_name.replace("__", " ").replace("_", " "),
                        str(function.get("description", "")),
                    )).lower()
                    score = len(input_words & set(re.findall(r"[a-z0-9]+", searchable)))
                    dotted_name = safe_name.replace("__", ".").lower()
                    if safe_name.lower() in query.lower() or dotted_name in query.lower():
                        score += 10
                    ranked.append((score, -position, item))
                ranked.sort(key=lambda candidate: (candidate[0], candidate[1]), reverse=True)
                catalog = [candidate[2] for candidate in ranked[:bounded_limit]]
            else:
                catalog = catalog[:bounded_limit]

        definitions = [item[0] for item in catalog]
        mapping = {item[1]: item[2] for item in catalog}
        return definitions, mapping

    def can(self, identity_id: str, skill_name: str) -> tuple[bool, str]:
        allowed, reason = authorize_skill(skill_name)
        if not allowed:
            return allowed, reason
        for cap in self.list(identity_id):
            skill = next((s for s in cap.skills() if s.name == skill_name), None)
            if skill is not None:
                return self._authorized(identity_id, cap.id, skill.permission)
        return (False, f"No installed capability provides skill: {skill_name}")

    def call(
        self,
        identity_id: str,
        skill_name: str,
        *,
        execution_scope: Optional[str] = None,
        **params: Any,
    ) -> Any:
        cap = self._find_capability_for_skill(identity_id, skill_name)
        if cap is None:
            raise ValueError(
                f"No installed capability provides skill: {skill_name}"
            )
        skill = next((s for s in cap.skills() if s.name == skill_name), None)
        if skill is None:
            raise ValueError(f"Capability '{cap.id}' does not define skill: {skill_name}")

        allowed, reason = self._authorized(identity_id, cap.id, skill.permission)
        if not allowed:
            return CapabilityResult.fail(
                cap.id,
                skill_name,
                "permission_denied",
                reason,
                params=params,
            )
        channel_allowed, channel_reason = authorize_skill(skill_name)
        if not channel_allowed:
            return CapabilityResult.fail(cap.id, skill_name, "channel_denied", channel_reason)
        normalized_params = normalize_parameters(skill.input_schema, params)
        try:
            validate_parameters(skill.input_schema, normalized_params)
        except CapabilityContractError as exc:
            return CapabilityResult.fail(
                cap.id,
                skill_name,
                "invalid_parameters",
                str(exc),
                params=params,
            )

        result = cap.call_scoped(
            skill_name,
            execution_scope=execution_scope,
            **normalized_params,
        )
        if isinstance(result, CapabilityResult):
            if not result.params:
                result.params = dict(normalized_params)
            return result.reclassify_soft_errors()
        return CapabilityResult.from_data(
            cap.id,
            skill_name,
            result,
            source=f"capability:{cap.id}",
            params=normalized_params,
        )

    def permissions(self, identity_id: str) -> list[dict[str, Any]]:
        """Return persisted capability permission grants for an identity."""
        raw = self._storage.load(identity_id, "capability.permissions") or {}
        return list(raw.get("grants", []))

    def grant(self, identity_id: str, capability_id: str, permission: str) -> None:
        """Persist an idempotent permission grant."""
        grants = self.permissions(identity_id)
        if not any(
            grant.get("capability") == capability_id
            and grant.get("permission") == permission
            for grant in grants
        ):
            grants.append(
                {
                    "capability": capability_id,
                    "permission": permission,
                    "granted_at": time.time(),
                }
            )
        self._storage.save(
            identity_id,
            "capability.permissions",
            {"grants": grants},
        )

    def revoke(self, identity_id: str, capability_id: str, permission: str) -> bool:
        """Remove an exact permission grant and report whether it existed."""
        grants = self.permissions(identity_id)
        retained = [
            grant
            for grant in grants
            if not (
                grant.get("capability") == capability_id
                and grant.get("permission") == permission
            )
        ]
        changed = len(retained) != len(grants)
        self._storage.save(
            identity_id,
            "capability.permissions",
            {"grants": retained},
        )
        return changed

    def _revoke_capability_grants(self, identity_id: str, capability_id: str) -> None:
        grants = self.permissions(identity_id)
        retained = [
            grant for grant in grants
            if grant.get("capability") != capability_id
        ]
        if len(retained) != len(grants):
            self._storage.save(
                identity_id,
                "capability.permissions",
                {"grants": retained},
            )

    def _authorized(
        self,
        identity_id: str,
        capability_id: str,
        permission: str,
    ) -> tuple[bool, str]:
        if permission in ("", "public", "local"):
            return (True, "")

        raw = self._storage.load(identity_id, "capability.permissions") or {}
        for grant in raw.get("grants", []):
            granted_cap = str(grant.get("capability", ""))
            granted_scope = str(grant.get("permission", ""))
            if granted_cap not in (capability_id, "*"):
                continue
            if _scope_matches(permission, granted_scope):
                return (True, "")
        return (
            False,
            f"Capability '{capability_id}' requires permission '{permission}'",
        )

    def _find_capability_for_skill(
        self, identity_id: str, skill_name: str
    ) -> Optional[Capability]:
        for cap in self.list(identity_id):
            ok, _ = cap.can(skill_name)
            if ok:
                return cap
        return None


def _scope_matches(required: str, granted: str) -> bool:
    if granted in ("*", required):
        return True
    if granted.endswith(":*"):
        return required.startswith(granted[:-1])
    return False
