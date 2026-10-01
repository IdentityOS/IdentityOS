"""Operations-surface integration for Culture Commons.

A surface connects the Operations loop to an external environment in a way the
loop can observe without blocking the interactive path.  The Culture Commons
surface drives the *same* installed capability an identity holds, records every
observation in the audit provenance ledger, and persists an evidence snapshot
of what was seen — so "the commons looks like X" is a runtime fact with a
source, not a model claim.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .models import ProvenanceEntry, ProvenancePhase, Relationship, RelationshipStatus

SURFACE_NAMESPACE = "operations.cc_surface"


class CultureCommonsSurface:
    name = "culture_commons"

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self.identity_id = engine.config.identity_id
        self._registry = getattr(engine, "_capability_registry", None)

    # ── capability state ───────────────────────────────────────────────

    def installed(self) -> bool:
        if self._registry is None:
            return False
        return self._registry.get(self.identity_id, "culture_commons") is not None

    def can_post(self) -> bool:
        if self._registry is None:
            return False
        allowed, _ = self._registry.can(self.identity_id, "culture_commons.post")
        return allowed

    def has_standing(self) -> bool:
        if not self.installed():
            return False
        result = self._registry.call(self.identity_id, "culture_commons.standing.inspect")
        if not result.success:
            return False
        standing = (result.data or {}).get("standing") or {}
        return bool(standing.get("signed"))

    def _call(self, skill: str, **params: Any):
        if self._registry is None:
            raise RuntimeError("no capability registry wired")
        return self._registry.call(self.identity_id, skill, **params)

    # ── observe ────────────────────────────────────────────────────────

    def observe(self) -> dict[str, Any]:
        """Read room + boards once, persist provenance and an evidence snapshot."""
        if not self.installed():
            return {"observed": False, "reason": "culture_commons capability not installed"}
        from .progress import meaningful, fingerprint
        progress = self._engine.progress
        now = getattr(self._engine, '_cycle_now', datetime.now(timezone.utc))
        condition = progress.condition('configuration')
        if not progress.eligible('surface:culture_commons',condition,now=now.timestamp()):
            return {'observed':False,'reason':'waiting','suppressed':True}
        cap = self._registry.get(self.identity_id, 'culture_commons')
        usage = cap.read_budget_state(now) if hasattr(cap,'read_budget_state') else None
        reset = (now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp()
        if usage and usage['reads'] >= max(1, int(usage['limit']*0.8)):
            exhausted=usage['reads']>=usage['limit']
            progress.wait('surface:culture_commons','DAILY_BUDGET_EXHAUSTED' if exhausted else 'BUDGET_RESERVED',
                condition,'Culture Commons daily read allowance '+('exhausted' if exhausted else 'reserved for meaningful follow-up'),
                retry=reset,reason='Resume adaptive observation after UTC daily reset',now=now.timestamp())
            return {'observed':False,'reason':'daily_budget_exhausted' if exhausted else 'budget_reserved','reads':usage['reads']}
        progress.resources['surface_reads']=progress.resources.get('surface_reads',0)+1
        result = self._call("culture_commons.observe")
        if not result.success:
            error=(result.error or {}).get('message','failure')
            budget = 'daily budget exhausted' in error
            previous=progress.blocker('surface:culture_commons')
            occurrences=previous.metadata['blocker'].get('occurrence_count',0) if previous else 0
            retry=reset if budget else now.timestamp()+min(21600,300*2**min(occurrences,6))
            progress.wait('surface:culture_commons','DAILY_BUDGET_EXHAUSTED' if budget else 'PROVIDER_UNAVAILABLE',condition,
                'Culture Commons waiting for '+('daily reset' if budget else 'provider recovery'),retry=retry,
                reason='Daily UTC read reset' if budget else 'Retry external provider after bounded backoff',now=now.timestamp())
            self._provenance(
                ProvenancePhase.OBSERVE,
                "culture_commons observe failed",
                action="culture_commons.observe",
                result=result.error.get("message", "failure") if result.error else "failure",
                refs={"surface": self.name},
            )
            return {"observed": False, "error": result.error, "reason": "observe_failed"}

        data = result.data or {}
        room = data.get("room") or {}
        boards = data.get("boards") or {}
        summary = self._summarize(room, boards)

        prior = self._load_snapshot()
        digest = fingerprint(meaningful({'room':room,'boards':boards}))
        previous_digest = prior.get('fingerprint') or fingerprint(meaningful({'room':prior.get('last_room'), 'boards':prior.get('last_boards')}))
        changed = digest != previous_digest
        interval = 300 if changed else min(21600, max(600,prior.get('observation_interval',300)*2))
        snapshot = self._snapshot(state=data, summary=summary)
        snapshot.update(fingerprint=digest, observation_interval=interval)
        progress.wait('surface:culture_commons','UNCHANGED_OBSERVATION',condition,
            'Culture Commons waiting for next eligible observation',retry=now.timestamp()+interval,
            reason='Observe after adaptive interval; unchanged observations back off',now=now.timestamp())
        self._engine.store._storage.save(self.identity_id, SURFACE_NAMESPACE, snapshot)

        if changed:
            self._provenance(
                ProvenancePhase.OBSERVE,
                f"culture_commons observed: {summary}",
            action="culture_commons.observe",
            result=summary,
            evidence=self._evidence_list(snapshot),
            refs={"surface": self.name, "observed_at": data.get("observed_at", "")},
        )
        self._ensure_relationship(summary)
        return {"observed": True, "changed":changed, "summary": summary, "at": data.get("observed_at", "")}

    def status(self) -> dict[str, Any]:
        raw = self._load_snapshot()
        return {
            "name": self.name,
            "installed": self.installed(),
            "can_post": self.can_post(),
            "standing_local": self.has_standing(),
            "observations": raw.get("count", 0),
            "last_observed_at": raw.get("last_observed_at"),
            "last_summary": raw.get("summary", ""),
        }

    def standing_state(self) -> str:
        """Return standing state for presence: 'signed' | 'unsigned' | 'unknown' | 'not_installed'."""
        if not self.installed():
            return "not_installed"
        if not self._registry:
            return "unknown"
        try:
            result = self._call("culture_commons.standing.inspect")
            if not result.success:
                return "unknown"
            standing = (result.data or {}).get("standing") or {}
            return "signed" if standing.get("signed") else "unsigned"
        except Exception:
            return "unknown"

    def snapshot(self) -> dict[str, Any]:
        return self._load_snapshot()

    # ── helpers ────────────────────────────────────────────────────────

    def _summarize(self, room: Any, boards: Any) -> str:
        try:
            room_seats = self._pluck(room, "seats") if isinstance(room, dict) else None
            present = self._present_count(room_seats or room)
            board_count = len(self._as_list(boards))
            return f"room observed (present: {present}); {board_count} board section(s)" if present is not None else "room observed; boards scanned"
        except Exception:
            return "room + boards observed"

    def _present_count(self, seats: Any) -> Optional[int]:
        items = self._as_list(seats)
        if not items:
            return 0
        return sum(1 for item in items if isinstance(item, dict) and item.get("name") is not None)

    @staticmethod
    def _as_list(value: Any) -> list:
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _pluck(value: dict[str, Any], key: str) -> Any:
        if key in value:
            return value[key]
        for nested in value.values():
            if isinstance(nested, dict) and key in nested:
                return nested[key]
        return None

    def _evidence_list(self, snapshot: dict[str, Any]) -> list[str]:
        evidence: list[str] = []
        facts = snapshot.get("facts") or []
        for fact in facts[:6]:
            evidence.append(f"{fact['source']}: {fact['statement']}")
        return evidence

    def _snapshot(self, *, state: dict[str, Any], summary: str) -> dict[str, Any]:
        raw = self._load_snapshot()
        count = int(raw.get("count", 0)) + 1
        facts = list(raw.get("facts") or [])
        facts.append(
            {
                "statement": summary,
                "source": "culture.sbs",
                "observed_at": state.get("observed_at", datetime.now(timezone.utc).isoformat()),
            }
        )
        facts = facts[-12:]
        return {
            "count": count,
            "last_observed_at": state.get("observed_at", datetime.now(timezone.utc).isoformat()),
            "summary": summary,
            "facts": facts,
            "last_room": state.get("room"),
            "last_boards": state.get("boards"),
        }

    def _load_snapshot(self) -> dict[str, Any]:
        raw = self._engine.store._storage.load(self.identity_id, SURFACE_NAMESPACE)
        return raw or {}

    def _provenance(self, phase: ProvenancePhase, summary: str, *, action: str = "", result: str = "", evidence: Optional[list[str]] = None, refs: Optional[dict[str, Any]] = None) -> None:
        self._engine.store.append_provenance(
            ProvenanceEntry(phase=phase, summary=summary, action=action, result=result, evidence=list(evidence or []), refs=dict(refs or {}))
        )

    def _ensure_relationship(self, summary: str) -> None:
        store = self._engine.store
        target = "The Culture Commons"
        existing = None
        for rel in store.list_relationships():
            if rel.organization == target and rel.purpose.startswith("culture_commons"):
                existing = rel
                break
        now = datetime.now(timezone.utc).isoformat()
        if existing is None:
            rel = Relationship(
                display_name="The Culture Commons",
                organization=target,
                role="external shared environment",
                purpose="culture_commons interop environment",
                status=RelationshipStatus.ENGAGED,
                notes=[summary],
                first_contacted_at=now,
                last_inbound_at=now,
                next_action="continue observing; post only when authorized",
            )
            store.add_relationship(rel)
        else:
            existing.notes = (list(existing.notes) + [summary])[-10:]
            existing.last_inbound_at = now
            store.update_relationship(existing)