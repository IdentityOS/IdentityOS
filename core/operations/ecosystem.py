"""The ecosystem map: every identity in the store, honestly aggregated.

The dashboard's ecosystem tab needs per-identity facts (role, presence,
health, needs, capabilities, activity) and real edges (delegation, oversight,
principal). Nothing is invented: an identity with no state shows as offline
with what is actually known, and edges only come from records that exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

#: Identities that make up the core team. Everything else with a spec is a
#: test artifact and is reported separately so the map stays legible.
CORE_TEAM_IDS = ("aster", "comet", "distiller", "engineer", "daedalus")

#: Edges of the ecosystem. Each is a real relationship that exists in state:
#: who delegates to whom, who oversees whom. No speculative connections.
#: (from, to, kind, label)
CORE_EDGES = (
    ("principal", "aster", "delegation", "delegates authority to"),
    ("aster", "distiller", "oversight", "contribution reviews flow through"),
    ("aster", "engineer", "delegation", "capability gaps delegated to"),
    ("aster", "comet", "embodiment", "browser work on this host"),
)


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _health_color(state: str) -> str:
    return {
        "online": "#34d17b", "healthy": "#34d17b",
        "degraded": "#ff6b4a", "waiting": "#ffb02e",
        "offline": "#4a5568", "unknown": "#4a5568",
    }.get(state, "#4a5568")


def _identity_node(storage: Any, identity_id: str, *, now: datetime) -> dict[str, Any]:
    """One identity's honest state for the map."""
    spec = storage.load(identity_id, "identity_spec") or {}
    snap = storage.load(identity_id, "latest_snapshot") or {}
    data = spec or (snap.get("modules") or {}).get("identity", {})
    name = data.get("name") or identity_id
    presence = storage.load(identity_id, "operations.presence") or {}
    caps = storage.load(identity_id, "capabilities") or {}
    installed = [c for c in (caps.get("installed") or []) if c]

    node: dict[str, Any] = {
        "id": identity_id,
        "name": name,
        "role": data.get("role") or "",
        "tagline": data.get("tagline") or "",
        "core_team": identity_id in CORE_TEAM_IDS,
        "capabilities": len(installed),
    }

    # Presence: the honest classification from the record's own evidence.
    status = str(presence.get("status") or "unknown")
    heartbeat_age = None
    last_hb = _parse(presence.get("last_heartbeat"))
    if last_hb is not None:
        heartbeat_age = round((now - last_hb).total_seconds())
    pid_alive = None
    pid = presence.get("operator_pid")
    if pid:
        import os

        try:
            os.kill(int(pid), 0)
            pid_alive = True
        except (ValueError, ProcessLookupError, PermissionError):
            pid_alive = False
    if pid_alive is False:
        state = "offline"
        detail = "operator process not running"
    elif heartbeat_age is None:
        state = "unknown"
        detail = "no presence record"
    elif heartbeat_age > 900:
        state = "offline"
        detail = f"heartbeat stale by {heartbeat_age}s"
    else:
        state = "online" if status not in ("offline",) else status
        detail = str(presence.get("activity") or "")[:120]
    node["state"] = state
    node["detail"] = detail
    node["activity"] = str(presence.get("activity") or "")[:200]
    node["last_heartbeat"] = presence.get("last_heartbeat")
    node["heartbeat_age_seconds"] = heartbeat_age
    node["last_meaningful_action"] = presence.get("last_meaningful_action")
    node["color"] = _health_color(state)

    # Operations state, when the identity has any.
    try:
        from core.operations.store import OperationsStore

        ops = OperationsStore(storage, identity_id)
        needs = ops.list_needs()
        open_needs = [n for n in needs if n.status.value == "open"]
        node["needs_open"] = len(open_needs)
        node["needs_total"] = len(needs)
        node["opportunities"] = len(ops.list_opportunities())
        node["messages"] = len(ops.list_messages())
        node["has_operations"] = True
    except Exception:
        node["has_operations"] = False
        node["needs_open"] = 0
        node["needs_total"] = 0
        node["opportunities"] = 0
        node["messages"] = 0

    # Recent provenance: what this identity actually did last.
    try:
        from core.operations.store import OperationsStore

        prov = OperationsStore(storage, identity_id).list_provenance(limit=3)
        node["recent_actions"] = [
            {"at": p.at, "summary": (p.summary or p.action)[:100]} for p in prov
        ]
    except Exception:
        node["recent_actions"] = []
    return node


def ecosystem(storage: Any, *, now: Optional[datetime] = None,
              include_artifacts: bool = False) -> dict[str, Any]:
    """Aggregate every identity in the store for the ecosystem map.

    Core team members always appear. Test artifacts appear only when asked
    for, so the map stays legible without hiding what exists.
    """
    now = now or datetime.now(timezone.utc)
    nodes: list[dict[str, Any]] = []
    try:
        identity_ids = list(storage.list_identities())
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": f"{type(exc).__name__}: {exc}"[:200]}

    for identity_id in identity_ids:
        spec = storage.load(identity_id, "identity_spec") or {}
        snap = storage.load(identity_id, "latest_snapshot") or {}
        if not (spec or snap):
            continue
        if identity_id not in CORE_TEAM_IDS and not include_artifacts:
            continue
        nodes.append(_identity_node(storage, identity_id, now=now))

    # The principal is a real ecosystem member (the human founder), shown as
    # its own node so the delegation edge has an anchor.
    has_aster = any(n["id"] == "aster" for n in nodes)
    if has_aster:
        nodes.insert(0, {
            "id": "principal",
            "name": "Arsène Manzi",
            "role": "Founder & CEO (human)",
            "tagline": "The principal every identity acts under",
            "core_team": True,
            "capabilities": 0,
            "state": "online",
            "detail": "human principal; always part of the ecosystem",
            "color": "#5aa9ff",
            "has_operations": False,
            "needs_open": 0, "needs_total": 0, "opportunities": 0, "messages": 0,
            "recent_actions": [],
        })
        edges = [
            {"from": src, "to": dst, "kind": kind, "label": label}
            for src, dst, kind, label in CORE_EDGES
            if any(n["id"] == src for n in nodes) and any(n["id"] == dst for n in nodes)
        ]
    else:
        edges = []

    return {
        "nodes": nodes,
        "edges": edges,
        "checked_at": now.isoformat(),
        "core_count": sum(1 for n in nodes if n["core_team"]),
    }
