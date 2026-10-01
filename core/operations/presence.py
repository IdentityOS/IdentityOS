"""
core/operations/presence.py

Live presence for operator identities — Discord-style status backed by real
runtime state, not generated status theater.

The presence record is durable operator state persisted under the
``operations.presence`` namespace through the identity's storage backend, so it
survives process restarts.  It answers: is the operator alive, what is it
doing, what is it waiting on, what did it last accomplish, and is anything
degraded?

Truth rules:

- Every presence transition is written by actual execution (operator loop,
  heartbeat thread, or CLI commands).  A presence write never claims work that
  did not happen.
- Health is *classified at read time* from liveness evidence (heartbeat age,
  operator pid, tick recency) plus subsystem health.  A stale presence file
  never masquerades as current ONLINE state.
- Heartbeats are cheap: they never invoke an LLM.
- The record never contains secrets, topics, phone numbers, credentials, or
  chain-of-thought; free-text fields are scrubbed when a scrub function is
  provided.

Health classification:

- OFFLINE: no record, operator process gone, or heartbeat stale beyond
  ``stale_after_seconds``.
- DEGRADED: operator process alive with a fresh heartbeat, but the operator
  loop has stalled or an important subsystem is unhealthy.
- ONLINE: heartbeat and loop both sufficiently recent and no unhealthy
  subsystem.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("identityos.presence")

NAMESPACE = "operations.presence"
CONTROLS_NAMESPACE = "operations.controls"

#: Heartbeat staleness threshold. The operator loop ticks every ~300s and the
#: service heartbeat thread fires every ~60s; anything older than this means
#: the process is almost certainly gone.
HEARTBEAT_STALE_SECONDS = 900.0

#: Loop-staleness threshold: process alive (fresh heartbeat) but the operator
#: loop has not completed a tick in this long — a stalled loop is degraded.
TICK_STALE_SECONDS = 1800.0

#: Subsystem health values that classify the operator as DEGRADED.
_UNHEALTHY = {"degraded", "unhealthy", "failing", "error", "failed", "lost"}

_SCRUB_REDACTED = "[redacted]"


class PresenceStatus(str, enum.Enum):
    """Lifecycle states of a running operator identity."""

    ONLINE = "online"
    IDLE = "idle"
    OBSERVING = "observing"
    THINKING = "thinking"
    ACTING = "acting"
    WAITING = "waiting"
    DEGRADED = "degraded"
    PAUSED = "paused"
    OFFLINE = "offline"

    @classmethod
    def coerce(cls, value: Any) -> "Optional[PresenceStatus]":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                return None
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value: Any, now: datetime) -> Optional[float]:
    ts = _parse(value)
    if ts is None:
        return None
    return (now - ts).total_seconds()


def _pid_alive(pid: Any) -> Optional[bool]:
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _humanize_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 5:
        return "just now"
    if seconds < 90:
        return f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours // 24} days ago"


def _humanize_ahead(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 5:
        return "now"
    if seconds < 90:
        return f"in {seconds} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    return f"in {hours} hour{'s' if hours != 1 else ''}"


class PresenceStore:
    """Durable, sanitized presence record for one operator identity.

    All reads and writes go through the storage backend's atomic save path
    (tmp-file + rename for the JSON backend), so a reader never observes a
    half-written presence record and state survives process restarts.
    """

    def __init__(
        self,
        storage: Any,
        identity_id: str,
        *,
        display_name: Optional[str] = None,
        objective: Optional[str] = None,
        scrub_fn: Optional[Callable[[str], str]] = None,
        stale_after_seconds: float = HEARTBEAT_STALE_SECONDS,
        tick_stale_after_seconds: float = TICK_STALE_SECONDS,
    ) -> None:
        self._storage = storage
        self.identity_id = identity_id
        self.display_name = display_name or identity_id
        self.objective = objective
        self._scrub = scrub_fn
        self._stale_after = float(stale_after_seconds)
        self._tick_stale_after = float(tick_stale_after_seconds)
        self._lock = threading.Lock()

    # ── write path ────────────────────────────────────────────────────

    def record(self) -> Optional[dict[str, Any]]:
        """Load the raw persisted presence record (or None)."""
        return self._storage.load(self.identity_id, NAMESPACE)

    def start_run(self, *, pid: Optional[int] = None, next_planned_action: Optional[str] = None) -> dict[str, Any]:
        """Register a new operator process run.

        Preserves the previous heartbeat and increments the restart counter so
        a restart is explainable from the record itself.
        """

        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            previous = record.get("last_heartbeat")
            if pid is not None:
                record["operator_pid"] = int(pid)
            record["restart_count"] = int(raw.get("restart_count", 0)) + 1 if raw else 1
            if previous:
                record["previous_heartbeat"] = previous
            record["started_at"] = _iso(_now())
            record["last_heartbeat"] = record["started_at"]
            record["status"] = PresenceStatus.ONLINE.value
            record["activity"] = "Operator starting"
            if next_planned_action:
                record["next_planned_action"] = next_planned_action
            return record

        return self._update(mutate)

    def heartbeat(
        self,
        *,
        phase: Optional[str] = None,
        activity: Optional[str] = None,
        next_check_at: Optional[str] = None,
        next_planned_action: Optional[str] = None,
        last_tick_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Cheap liveness update. NO LLM call — proves the operator loop is alive."""

        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            now = _now()
            record["last_heartbeat"] = _iso(now)
            if phase:
                record["status"] = PresenceStatus.coerce(phase).value if PresenceStatus.coerce(phase) else record["status"]
            if activity:
                record["activity"] = activity
            if next_check_at:
                record["next_check_at"] = next_check_at
            if next_planned_action:
                record["next_planned_action"] = next_planned_action
            if last_tick_at:
                record["last_tick_at"] = last_tick_at
            if pid := self._current_pid():
                record["operator_pid"] = pid
            return record

        return self._update(mutate)

    def set_status(
        self,
        status: Any,
        *,
        activity: Optional[str] = None,
        detail: Optional[str] = None,
        next_planned_action: Optional[str] = None,
        next_check_at: Optional[str] = None,
        last_tick_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record an activity transition from actual execution."""

        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            coerced = PresenceStatus.coerce(status)
            if coerced is None:
                raise ValueError(f"Invalid presence status: {status!r}")
            record["status"] = coerced.value
            record["last_heartbeat"] = _iso(_now())
            if activity is not None:
                record["activity"] = activity
            if detail is not None:
                record["activity_detail"] = detail
            if next_planned_action is not None:
                record["next_planned_action"] = next_planned_action
            if next_check_at is not None:
                record["next_check_at"] = next_check_at
            if last_tick_at is not None:
                record["last_tick_at"] = last_tick_at
            return record

        return self._update(mutate)

    def mark_meaningful_action(self, description: str, *, at: Optional[str] = None) -> dict[str, Any]:
        """Record the last real, meaningful action the operator accomplished."""

        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            record["last_meaningful_action"] = description
            record["last_meaningful_action_at"] = at or _iso(_now())
            return record

        return self._update(mutate)

    def set_subsystem(self, name: str, health: str) -> dict[str, Any]:
        """Update one subsystem's health.

        ``email_ingest`` is first-class: a mailbox poller that stopped is
        indistinguishable from an empty inbox unless the poller can say so.
        """
        allowed = {
            "notification_transport_health",
            "model_health",
            "capability_health",
            "commons_standing",
            "email_ingest",
            "email_sender",
            "self_maintenance",
        }
        if name not in allowed:
            raise ValueError(f"Unknown presence subsystem: {name}")

        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            record[name] = health
            return record

        return self._update(mutate)

    def set_capability_summary(self, *, healthy: int, limited: list[str]) -> dict[str, Any]:
        """Record how many required skills are usable vs permission-limited.

        Permission-limited (but installed/available) skills are reported, never
        hidden — but on their own they do NOT degrade global health. Only a
        material capability failure (recorded via ``set_subsystem`` with an
        unhealthy value, or a DEGRADED lifecycle state from actual execution)
        affects the global classification.
        """

        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            record["capability_summary"] = {
                "healthy": max(0, int(healthy)),
                "limited": sorted({str(s) for s in limited if str(s).strip()}),
            }
            return record

        return self._update(mutate)

    def set_counts(self, *, opportunity_count: Optional[int] = None) -> dict[str, Any]:
        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            if opportunity_count is not None:
                record["opportunity_count"] = int(opportunity_count)
            return record

        return self._update(mutate)

    def mark_offline(self, *, reason: str = "operator stopped") -> dict[str, Any]:
        """Record a clean operator shutdown."""

        def mutate(raw: Optional[dict]) -> dict:
            record = self._blank(raw)
            record["status"] = PresenceStatus.OFFLINE.value
            record["activity"] = reason
            record["last_heartbeat"] = _iso(_now())
            record["operator_pid"] = None
            return record

        return self._update(mutate)

    # ── read path: health classification (computed, never stored) ─────

    def classify(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        """Classify operator health from liveness evidence and subsystem state."""
        now = now or _now()
        raw = self.record()
        if raw is None:
            return self._view(raw, health="offline", health_reasons=["no presence record"], status_override=PresenceStatus.OFFLINE.value, now=now)

        reasons: list[str] = []
        pid = raw.get("operator_pid")
        alive = _pid_alive(pid) if pid is not None else None
        stored = PresenceStatus.coerce(raw.get("status"))
        if pid is None and stored is PresenceStatus.OFFLINE:
            reasons.append("operator stopped")
        if pid is not None and alive is False:
            reasons.append("operator process not running")

        hb_age = _age_seconds(raw.get("last_heartbeat"), now)
        if hb_age is None or hb_age > self._stale_after:
            reasons.append("stale heartbeat" if hb_age is not None else "no heartbeat recorded")

        offline = bool(reasons)
        status = PresenceStatus.OFFLINE.value if offline else self._stored_status(raw, now)

        if not offline:
            if status == PresenceStatus.DEGRADED.value:
                detail = (raw.get("activity") or "").strip()
                reasons.append(f"degraded: {detail}" if detail else "operator reported degraded state")
            if self._loop_stalled(raw, now):
                reasons.append("operator loop stalled")
            for subsystem in ("notification_transport_health", "model_health",
                              "capability_health", "self_maintenance"):
                value = str(raw.get(subsystem) or "").strip().lower()
                if value in _UNHEALTHY:
                    reasons.append(f"{subsystem.replace('_health', '')} unhealthy")
            if reasons:
                status = PresenceStatus.DEGRADED.value

        health = "offline" if offline else ("degraded" if reasons else "online")
        if offline:
            service_health = "offline"
        elif "operator loop stalled" in reasons:
            service_health = "degraded"
        else:
            service_health = "online"
        identity_health = self._identity_health(raw)
        return self._view(
            raw,
            health=health,
            health_reasons=reasons,
            status_override=status,
            now=now,
            service_health=service_health,
            identity_health=identity_health,
        )

    def public_view(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        """Sanitized machine-readable presence (for /health, /status, CLI)."""
        return self.classify(now=now)

    def _identity_health(self, raw: Optional[dict]) -> str:
        """Identity continuity: is this the same initialized operator identity?

        The identity outlives any single process run, so this is independent
        of service liveness: "online" when the persisted identity record is
        intact, "unknown" when the operator ran but was never initialized,
        "offline" when nothing is known at all.
        """
        if not raw:
            return "offline"
        try:
            spec = self._storage.load(self.identity_id, "identity_spec")
        except Exception:  # pragma: no cover - defensive
            spec = None
        if isinstance(spec, dict) and spec:
            return "online"
        return "unknown"

    def _view(
        self,
        raw: Optional[dict],
        *,
        health: str,
        health_reasons: list[str],
        status_override: str,
        now: datetime,
        service_health: str = "unknown",
        identity_health: str = "unknown",
    ) -> dict[str, Any]:
        """Build the sanitized presence view from the record + classification."""
        raw = raw or {}
        hb_age = _age_seconds(raw.get("last_heartbeat"), now)
        tick_age = _age_seconds(raw.get("last_tick_at"), now)
        action_age = _age_seconds(raw.get("last_meaningful_action_at"), now)
        start_age = _age_seconds(raw.get("started_at"), now)
        next_check_in: Optional[float] = None
        nxt = _parse(raw.get("next_check_at"))
        if nxt is not None:
            next_check_in = (nxt - now).total_seconds()
        pid = raw.get("operator_pid")
        alive = _pid_alive(pid) if pid is not None else None
        if pid is None or alive is False:
            service_state = "stopped"
        elif health == "offline":
            service_state = "stopped"
        elif health in ("online", "degraded"):
            service_state = "active"
        else:
            service_state = "unknown"
        uptime = start_age if alive else None
        summary_raw = raw.get("capability_summary") or {}
        summary = {
            "healthy": max(0, int(summary_raw.get("healthy", 0) or 0)),
            "limited": [str(s) for s in (summary_raw.get("limited") or []) if str(s).strip()],
        }
        cap_marker = str(raw.get("capability_health") or "unknown").strip().lower()
        if cap_marker in _UNHEALTHY:
            capability_display = "degraded"
        elif summary["limited"]:
            capability_display = "limited"
        elif cap_marker == "healthy":
            capability_display = "healthy"
        else:
            capability_display = "unknown"
        return {
            "identity": raw.get("identity") or self.display_name,
            "identity_id": self.identity_id,
            "external_identity": raw.get("external_identity"),
            "health": health,
            "service_health": service_health,
            "identity_health": identity_health,
            "capability_display": capability_display,
            "capability_summary": summary,
            "status": status_override,
            "activity": raw.get("activity") or "",
            "activity_detail": raw.get("activity_detail") or "",
            "current_objective": raw.get("current_objective") or self.objective,
            "last_heartbeat": raw.get("last_heartbeat"),
            "heartbeat_age_seconds": hb_age,
            "last_tick_at": raw.get("last_tick_at"),
            "last_tick_age_seconds": tick_age,
            "last_meaningful_action": raw.get("last_meaningful_action"),
            "last_meaningful_action_at": raw.get("last_meaningful_action_at"),
            "last_meaningful_action_age_seconds": action_age,
            "next_planned_action": raw.get("next_planned_action"),
            "next_check_at": raw.get("next_check_at"),
            "next_check_in_seconds": next_check_in,
            "uptime_seconds": uptime,
            "started_at": raw.get("started_at"),
            "operator_pid": pid,
            "restart_count": int(raw.get("restart_count", 0)),
            "previous_heartbeat": raw.get("previous_heartbeat"),
            "service_state": service_state,
            "commons_standing": raw.get("commons_standing", "unknown"),
            "notification_transport_health": raw.get("notification_transport_health", "unknown"),
            "model_health": raw.get("model_health", "unknown"),
            "capability_health": raw.get("capability_health", "unknown"),
            "self_maintenance": raw.get("self_maintenance", "unknown"),
            "opportunity_count": raw.get("opportunity_count", 0),
            "health_reasons": list(health_reasons),
            "updated_at": raw.get("updated_at"),
        }

    # ── internals ─────────────────────────────────────────────────────

    def _blank(self, raw: Optional[dict]) -> dict[str, Any]:
        record: dict[str, Any] = dict(raw or {})
        record.setdefault("identity_id", self.identity_id)
        record.setdefault("identity", self.display_name)
        record.setdefault("external_identity", None)
        record.setdefault("status", PresenceStatus.OFFLINE.value)
        record.setdefault("activity", "")
        record.setdefault("current_objective", self.objective)
        record.setdefault("last_heartbeat", None)
        record.setdefault("last_tick_at", None)
        record.setdefault("last_meaningful_action", None)
        record.setdefault("last_meaningful_action_at", None)
        record.setdefault("next_planned_action", None)
        record.setdefault("next_check_at", None)
        record.setdefault("started_at", None)
        record.setdefault("operator_pid", None)
        record.setdefault("restart_count", 0)
        record.setdefault("previous_heartbeat", None)
        record.setdefault("commons_standing", "unknown")
        record.setdefault("notification_transport_health", "unknown")
        record.setdefault("model_health", "unknown")
        record.setdefault("capability_health", "unknown")
        record.setdefault("self_maintenance", "unknown")
        record.setdefault("capability_summary", {"healthy": 0, "limited": []})
        record.setdefault("opportunity_count", 0)
        return record

    def _current_pid(self) -> Optional[int]:
        return os.getpid()

    def _stored_status(self, raw: dict, now: datetime) -> str:
        status = PresenceStatus.coerce(raw.get("status")) or PresenceStatus.OFFLINE
        if status is PresenceStatus.PAUSED and self._controls_paused() is False:
            # Paused flag was lifted; the record is stale until the loop writes.
            return PresenceStatus.IDLE.value
        return status.value

    def _loop_stalled(self, raw: dict, now: datetime) -> bool:
        tick_age = _age_seconds(raw.get("last_tick_at"), now)
        if tick_age is not None:
            return tick_age > self._tick_stale_after
        # Never ticked in this run: stalled only if the run itself is old.
        # A brand-new run that has not completed its first tick is starting,
        # not stalled.
        start_age = _age_seconds(raw.get("started_at"), now)
        return start_age is None or start_age > self._tick_stale_after

    def _controls_paused(self) -> Optional[bool]:
        try:
            controls = self._storage.load(self.identity_id, CONTROLS_NAMESPACE)
        except Exception:  # pragma: no cover - defensive
            return None
        if not isinstance(controls, dict):
            return None
        return bool(controls.get("paused", False))

    def _scrub_value(self, value: Any) -> Any:
        if self._scrub is None:
            return value
        if isinstance(value, str):
            try:
                return self._scrub(value)
            except Exception:  # pragma: no cover - scrub never blocks presence
                return value
        if isinstance(value, list):
            return [self._scrub_value(item) for item in value]
        if isinstance(value, dict):
            return {k: self._scrub_value(v) for k, v in value.items()}
        return value

    def _update(self, mutate: Callable[[Optional[dict]], dict]) -> dict[str, Any]:
        """Load → mutate → persist atomically (backend save is atomic)."""
        with self._lock:
            raw = self.record()
            record = mutate(raw)
            record = self._scrub_value(record)
            record["updated_at"] = _iso(_now())
            self._storage.save(self.identity_id, NAMESPACE, record)
            return record


def format_presence_card(view: dict[str, Any], *, service_unit: str = "aster-operator.service") -> str:
    """Render a concise, human-readable live status card. No LLM involved."""
    identity = view.get("identity") or view.get("identity_id") or "operator"
    health = view.get("health") or "unknown"
    dot = {"online": "●", "degraded": "◐", "offline": "○"}.get(health, "◌")
    lines: list[str] = [f"{identity} {dot} {health.upper()}"]

    status = view.get("status") or "unknown"
    activity = view.get("activity") or "No activity recorded"
    lines.append("")
    lines.append("Activity:")
    lines.append(f"{status} — {activity}")

    objective = view.get("current_objective")
    if objective:
        lines.append("")
        lines.append("Objective:")
        lines.append(str(objective))

    hb_age = _humanize_age(view.get("heartbeat_age_seconds"))
    lines.append("")
    lines.append("Last heartbeat:")
    lines.append(hb_age)

    last_action = view.get("last_meaningful_action")
    lines.append("")
    lines.append("Last meaningful action:")
    lines.append(str(last_action) if last_action else "None recorded yet")
    if last_action:
        action_age = _humanize_age(view.get("last_meaningful_action_age_seconds"))
        lines.append(f"({action_age})")

    next_planned = view.get("next_planned_action")
    next_check = _humanize_ahead(view.get("next_check_in_seconds"))
    lines.append("")
    lines.append("Next:")
    if next_planned:
        lines.append(f"{next_planned} ({next_check})." if next_check != "unknown" else f"{next_planned}.")
    else:
        lines.append(f"Next check {next_check}." if next_check != "unknown" else "No check scheduled.")

    lines.append("")
    lines.append("Service:")
    service_state = view.get("service_state") or "unknown"
    pid = view.get("operator_pid")
    pid_part = f" · pid {pid}" if pid else ""
    lines.append(f"{service_unit} · {service_state}{pid_part}")

    lines.append("")
    lines.append("Commons:")
    lines.append(str(view.get("commons_standing") or "unknown"))

    lines.append("")
    lines.append("Notifications:")
    lines.append(f"ntfy · {view.get('notification_transport_health') or 'unknown'}")

    summary = view.get("capability_summary") or {}
    limited = [str(s) for s in (summary.get("limited") or [])]
    cap_line = f"{summary.get('healthy', 0)} healthy · {len(limited)} limited ({view.get('capability_display') or 'unknown'})"
    lines.append("")
    lines.append("Capabilities:")
    lines.append(cap_line)
    if limited:
        lines.append(f"Limited: {', '.join(limited)}")

    lines.append("")
    lines.append("Opportunities:")
    lines.append(str(view.get("opportunity_count", 0)))

    if view.get("health_reasons"):
        lines.append("")
        lines.append("Notes:")
        for reason in view["health_reasons"]:
            lines.append(f"- {reason}")

    return "\n".join(lines)
