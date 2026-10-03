"""operations overseer — second identity that watches contributors, not ops.

Only the smallest possible unit: an identity spec separate from Aster with a
clear name, purpose, and stop-prompt so hires and contributors go through a
visible pipeline. Stored beside Aster, never sent to email backends.
"""
from __future__ import annotations

from typing import Any


DISTILLER_ID = "distiller"
# A genuine overseer of contributor capacity. Named for what it does: distills
# a day of scattered contributions into a coherent picture without needing
# technical depth.


def create_distiller_identity() -> Any:
    """Persist only the fields the identity needs."""
    from core.identity import IdentityClass, create_identity

    spec = create_identity(
        name="Distiller",
        identity_id=DISTILLER_ID,
        identity_class=IdentityClass.AGENT,
    )
    # create_identity's unchanged kwargs; the caller-facing fields live on the
    # instance attributes that frameworks expect.
    from dataclasses import fields
    allowed = {f.name for f in fields(spec.__class__)}
    for name, value in {
        "role": "Personnel & contribution overseer",
        "persona": (
            "Distiller is the oversight identity for IdentityOS contributors."
        ),
    }.items():
        if name in allowed:
            object.__setattr__(spec, name, value)
    return spec


def persist_distiller_identity(storage: Any, identity: Optional[Any] = None) -> Any:
    spec = identity or create_distiller_identity()
    storage.save(spec.id, "identity_spec", spec.to_dict())
    storage.save(spec.id, "latest_snapshot", {"modules": {"identity": spec.to_dict()}})
    return spec
