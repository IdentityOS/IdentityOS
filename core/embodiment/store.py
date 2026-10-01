"""Persistent authorization and observation records for device use."""

from __future__ import annotations

from typing import Any


class EmbodimentStore:
    AUTH_NAMESPACE = "embodiment.authorizations"
    OBSERVATION_NAMESPACE = "embodiment.observations"

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def authorizations(self, identity_id: str) -> dict[str, dict[str, Any]]:
        raw = self.storage.load(identity_id, self.AUTH_NAMESPACE) or {}
        return dict(raw.get("devices") or {})

    def save_authorizations(
        self, identity_id: str, devices: dict[str, dict[str, Any]],
    ) -> None:
        self.storage.save(
            identity_id,
            self.AUTH_NAMESPACE,
            {"schema_version": 1, "devices": devices},
        )

    def append_observation(
        self, identity_id: str, observation: dict[str, Any],
    ) -> None:
        raw = self.storage.load(identity_id, self.OBSERVATION_NAMESPACE) or {}
        items = list(raw.get("observations") or [])
        items.append(observation)
        self.storage.save(
            identity_id,
            self.OBSERVATION_NAMESPACE,
            {"schema_version": 1, "observations": items[-1000:]},
        )
