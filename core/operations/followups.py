"""
core/operations/followups.py

Follow-up planning.

Follow-ups are thoughtful and bounded: a relationship that has gone quiet gets
nudges spaced around ``follow_up_after_hours`` (per-relationship cadence
varies deterministically so outreach never looks robotic), and never when
the person has replied, declined, or opted out. The principal and shared
environment relationships are never auto-nudged. When the adaptive cap is
reached with no reply, the relationship is visibly retired to DORMANT with
provenance instead of silently stalling.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import (
    FollowUp,
    FollowUpStatus,
    ProvenanceEntry,
    ProvenancePhase,
    Relationship,
    RelationshipStatus,
    utcnow,
)
from .store import OperationsStore

#: Relationships that must never receive autonomous follow-ups: the human
#: principal (served through notifications/responses, never nagged) and
#: shared environment surfaces (not correspondents).
_NO_NUDGE_PURPOSES = ("principal:builder",)
_NO_NUDGE_PREFIXES = ("culture_commons",)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class FollowUpPlanner:
    def __init__(self, store: OperationsStore) -> None:
        self._store = store

    def due(self, now: Optional[datetime] = None) -> list[FollowUp]:
        return self._store.due_follow_ups(now)

    @staticmethod
    def _nudge_excluded(rel: Relationship) -> bool:
        purpose = str(getattr(rel, "purpose", "") or "")
        if purpose in _NO_NUDGE_PURPOSES:
            return True
        return any(purpose.startswith(prefix) for prefix in _NO_NUDGE_PREFIXES)

    def effective_cap(self, rel: Relationship, controls: Any = None) -> int:
        """Adaptive per-relationship follow-up cap.

        Base configured maximum, extended by one when the contact engaged at
        some point (inbound on record: a re-engaged conversation deserves one
        more chance than cold silence). Deterministic per relationship id.
        """
        controls = controls or self._store.controls()
        base = max(1, int(getattr(controls, "max_follow_ups_per_target", 2) or 2))
        engaged_before = 1 if getattr(rel, "last_inbound_at", None) else 0
        return min(base + engaged_before, base + 1)

    def effective_interval(self, rel: Relationship, controls: Any = None) -> timedelta:
        """Per-relationship follow-up cadence around the configured hours.

        Deterministic ±12h jitter from the relationship id: a couple of days
        for everyone, never robotic same-hour batching.
        """
        controls = controls or self._store.controls()
        base_hours = float(getattr(controls, "follow_up_after_hours", 72.0) or 72.0)
        jitter = (int(hashlib.sha256(rel.id.encode()).hexdigest(), 16) % 25) - 12
        return timedelta(hours=max(1.0, base_hours + jitter))

    def plan(self, now: Optional[datetime] = None) -> list[FollowUp]:
        """Create follow-ups for quiet relationships that are due for a nudge."""
        now = now or datetime.now(timezone.utc)
        controls = self._store.controls()
        created: list[FollowUp] = []

        for rel in self._store.list_relationships():
            if rel.opted_out or rel.status in (RelationshipStatus.DECLINED, RelationshipStatus.OPTED_OUT):
                continue
            if self._nudge_excluded(rel):
                continue
            if rel.status not in (RelationshipStatus.OUTREACH_SENT, RelationshipStatus.ENGAGED):
                continue
            cap = self.effective_cap(rel, controls)
            if rel.follow_up_count >= cap:
                self._retire_quiet(rel, cap)
                continue
            if self._has_scheduled(rel.id):
                continue
            last_out = _parse(rel.last_outbound_at)
            if last_out is None:
                continue
            last_in = _parse(rel.last_inbound_at)
            if last_in is not None and last_in > last_out:
                continue  # they replied; conversation is active
            interval = self.effective_interval(rel, controls)
            if now < last_out + interval:
                continue
            reason = (
                f"no reply {interval.total_seconds() / 3600:.0f}h after "
                f"outreach #{rel.follow_up_count + 1}"
            )
            follow_up = FollowUp(
                relationship_id=rel.id,
                due_at=now.isoformat(),
                reason=reason,
            )
            self._store.add_follow_up(follow_up)
            rel.follow_up_due_at = follow_up.due_at
            self._store.update_relationship(rel)
            created.append(follow_up)
        return created

    def _retire_quiet(self, rel: Relationship, cap: int) -> None:
        """Visibly retire a relationship that exhausted its nudges unanswered."""
        if rel.status not in (RelationshipStatus.OUTREACH_SENT, RelationshipStatus.ENGAGED):
            return
        rel.status = RelationshipStatus.DORMANT
        rel.next_action = (
            f"closed after {rel.follow_up_count} follow-up(s) without reply "
            f"(cap {cap}); reopen on any inbound"
        )
        rel.follow_up_due_at = None
        self._store.update_relationship(rel)
        self._store.append_provenance(ProvenanceEntry(
            phase=ProvenancePhase.FOLLOW_UP,
            summary=f"retired quiet relationship '{rel.display_name}' to dormant",
            action="follow_up_exhausted",
            result=f"{rel.follow_up_count}/{cap} nudges sent, no reply",
            refs={"relationship_id": rel.id},
        ))

    def analyze_quiet(
        self,
        relationship: Relationship,
        *,
        adapter: Any = None,
        identity: Any = None,
        project_name: str = "",
        objective: str = "",
    ) -> str:
        """Research why a quiet contact may not have replied.

        Returns a short re-engagement angle recorded into the relationship
        notes by the caller flow (engine appends it). Model-backed when a
        runtime exists; otherwise a deterministic, honest fallback. Output is
        voice-checked: dirty text falls back rather than shipping U+2014.
        """
        from .voice import repair_outbound, validate_outbound

        now = datetime.now(timezone.utc)
        last_out = _parse(getattr(relationship, "last_outbound_at", None))
        days_quiet = (now - last_out).days if last_out else -1
        angle = ""
        if adapter is not None:
            try:
                prompt_context = (
                    "You are helping a persistent AI operator re-engage a quiet contact. "
                    "Study the facts and propose ONE specific, non-pushy re-engagement angle "
                    "in one or two sentences: a fresh hook, a question, or new information "
                    "worth sharing. No generic flattery, no guilt, no pressure."
                )
                user_input = (
                    f"Contact: {relationship.display_name} ({relationship.organization or 'no org'}). "
                    f"Purpose: {relationship.purpose or 'outreach'}. "
                    f"Quiet for {days_quiet} day(s). "
                    f"Follow-ups already sent: {relationship.follow_up_count}. "
                    f"Recent notes: {'; '.join((relationship.notes or [])[-3:]) or 'none'}. "
                    f"Project: {project_name or 'IdentityOS'}. Objective: {objective or 'collaboration'}."
                )
                candidate = adapter.generate(prompt_context, user_input, identity)
                candidate = (candidate or "").strip()
                if candidate and validate_outbound(candidate).ok:
                    angle = candidate[:400]
                elif candidate:
                    repaired, clean = repair_outbound(candidate)
                    if clean:
                        angle = repaired[:400]
            except Exception:
                angle = ""
        if not angle:
            span = f"{days_quiet} day(s)" if days_quiet >= 0 else "some time"
            angle = (
                f"Quiet for {span} after outreach #{relationship.follow_up_count + 1}; "
                f"try one concrete new hook tied to {relationship.organization or 'their work'}."
            )
        return angle

    def complete(self, follow_up: FollowUp, *, now: Optional[datetime] = None) -> Relationship:
        follow_up.status = FollowUpStatus.SENT
        follow_up.completed_at = (now or datetime.now(timezone.utc)).isoformat()
        self._store.update_follow_up(follow_up)
        rel = self._store.get_relationship(follow_up.relationship_id)
        if rel is not None:
            rel.follow_up_count += 1
            rel.follow_up_due_at = None
            self._store.update_relationship(rel)
        return rel  # type: ignore[return-value]

    def cancel(self, relationship: Relationship, reason: str = "") -> None:
        for follow_up in self._store.list_follow_ups(status=FollowUpStatus.SCHEDULED):
            if follow_up.relationship_id == relationship.id:
                follow_up.status = FollowUpStatus.CANCELLED
                follow_up.reason = reason or follow_up.reason
                self._store.update_follow_up(follow_up)
        relationship.follow_up_due_at = None
        self._store.update_relationship(relationship)

    def _has_scheduled(self, relationship_id: str) -> bool:
        return any(
            f.relationship_id == relationship_id
            for f in self._store.list_follow_ups(status=FollowUpStatus.SCHEDULED)
        )


def next_follow_up_at(relationship: Relationship, hours: float) -> str:
    base = _parse(relationship.last_outbound_at) or datetime.now(timezone.utc)
    return (base + timedelta(hours=hours)).isoformat()
