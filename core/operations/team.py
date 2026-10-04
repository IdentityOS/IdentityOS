"""Generic agent engine builder — any persisted identity can run.

The Aster-specific builder wires mission data (need rules, candidate sources,
email transport) into an OperatorConfig. Team identities that do not do
outreach — Comet (browser embodiment), Distiller (contribution oversight),
Engineer (technical services) — still need to be alive and observable: a
presence heartbeat, the self-maintenance pass, and honest idle state.

This module builds that minimal engine from the identity's own spec. It never
invents a persona: an identity without a spec cannot run.
"""

from __future__ import annotations

from typing import Any, Optional

from .config import OperatorConfig


def build_team_engine(
    storage: Any,
    identity_id: str,
    *,
    presence: Any = None,
    capability_registry: Any = None,
) -> Optional[Any]:
    """Build a minimal operator engine for a persisted team identity.

    Returns None when the identity has no real spec: the engine never invents
    a persona. Team identities run observe + maintenance + heartbeat; they do
    no outreach, so no need rules or candidate sources are wired.
    """
    from .engine import OperationsEngine

    spec = storage.load(identity_id, "identity_spec") or {}
    snap = storage.load(identity_id, "latest_snapshot") or {}
    data = spec or (snap.get("modules") or {}).get("identity", {})
    if not data:
        return None
    config = OperatorConfig(
        identity_id=identity_id,
        project_name="IdentityOS",
        purpose=str(data.get("role") or data.get("tagline") or "identity runtime"),
        need_rules=[],
        candidate_sources=[],
        required_skills=[],
        poll_interval=300.0,
    )
    if presence is None:
        from .presence import PresenceStore

        presence = PresenceStore(
            storage, identity_id,
            display_name=str(data.get("name") or identity_id),
            objective=config.purpose,
        )
    return OperationsEngine(
        storage, config,
        presence=presence,
        capability_registry=capability_registry,
    )
