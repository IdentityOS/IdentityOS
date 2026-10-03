"""
core/operations/engine.py

The Operations engine — a durable, evidence-recording operator loop.

Each :meth:`OperationsEngine.tick` runs a sequence of phases against persisted
state:

    self-maintain → observe → capability-gap check → detect needs → discover opportunities
    → evaluate → act (outreach, within budget) → monitor replies → follow up

Every phase appends to an append-only provenance ledger.  Because all state is
loaded from storage at construction, a brand-new process with the same identity
resumes exactly where the previous one stopped: no duplicate outreach, no lost
conversations, no in-memory-only progress.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Mapping

from .capability_gap import CapabilityGap, CapabilityGapDetector, CapabilityStatus
from .composition import OutreachBrief, OutreachComposer
from .config import OperatorConfig
from .discovery import OpportunityDiscoverer
from .evaluation import DuplicateContactPolicy, TargetEvaluator
from .followups import FollowUpPlanner
from .maintenance import SelfMaintenance
from .models import (
    Evaluation,
    Message,
    MessageDirection,
    MessageStatus,
    NotificationEntry,
    Opportunity,
    OpportunityStatus,
    ProvenanceEntry,
    ProvenancePhase,
    Relationship,
    RelationshipStatus,
    utcnow,
)
from .monitor import ConversationMonitor, InboundDisposition, InboundResult, message_answered
from .needs import NeedDetector
from .observer import ProjectStateObserver
from .policy import AuthorityPolicy
from .presence import PresenceStatus
from .store import OperationsStore, _norm

logger = logging.getLogger("identityos.operations")


@dataclass
class TickReport:
    cycle_outcome: dict[str, Any] = field(default_factory=dict)
    observed: bool = False
    capability_gaps: list[dict[str, Any]] = field(default_factory=list)
    needs_created: list[str] = field(default_factory=list)
    opportunities_created: list[str] = field(default_factory=list)
    evaluated: list[str] = field(default_factory=list)
    qualified: list[str] = field(default_factory=list)
    outreach_sent: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    replies_sent: list[str] = field(default_factory=list)
    follow_ups_sent: list[str] = field(default_factory=list)
    principal_processed: list[str] = field(default_factory=list)
    principal_deferred: list[str] = field(default_factory=list)
    notifications_created: list[str] = field(default_factory=list)
    maintenance: dict[str, Any] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_outcome": dict(self.cycle_outcome),
            "observed": self.observed,
            "capability_gaps": list(self.capability_gaps),
            "needs_created": list(self.needs_created),
            "opportunities_created": list(self.opportunities_created),
            "evaluated": list(self.evaluated),
            "qualified": list(self.qualified),
            "outreach_sent": list(self.outreach_sent),
            "escalations": list(self.escalations),
            "replies_sent": list(self.replies_sent),
            "follow_ups_sent": list(self.follow_ups_sent),
            "principal_processed": list(self.principal_processed),
            "principal_deferred": list(self.principal_deferred),
            "notifications_created": list(self.notifications_created),
            "maintenance": dict(self.maintenance),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


class OperationsEngine:
    def __init__(
        self,
        storage: Any,
        config: OperatorConfig,
        *,
        transport: Any = None,
        adapter: Any = None,
        identity: Any = None,
        capability_registry: Any = None,
        acquisition: Any = None,
        search_fn: Any = None,
        secret_store: Any = None,
        surfaces: Iterable[Any] = (),
        presence: Any = None,
        communication_identity: Any = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.store = OperationsStore(storage, config.identity_id)
        self._mode = "live" if transport is not None else "dry-run"
        self._transport = transport
        self._adapter = adapter
        self._identity = identity
        self._capability_registry = capability_registry
        self._acquisition = acquisition
        self._search_fn = search_fn
        self._secret_store = secret_store
        self._surfaces = list(surfaces)
        self._presence = presence
        self._communication_identity = communication_identity
        from .email_jobs import new_run_id

        self._run_id = new_run_id()
        # Serializes inbound processing inside this process. The operator tick
        # and the dedicated email ingest thread both call the same pipeline, and
        # they share one store whose in-memory namespace cache is not atomic
        # across threads.
        self._ingest_lock = threading.RLock()
        # Per-thread worker identity (see _worker_id). thread-local gives each
        # live thread its own token without touching a shared dict.
        self._worker_local = threading.local()

        from .progress import Progress
        self.progress = Progress(self.store, config.purpose)

        # Adaptive polling state
        self._last_observation_fingerprint: Optional[str] = (self._compute_observation_fingerprint(self.store.project_state()) if self.store.project_state() else None)
        self._current_poll_interval = float(config.poll_interval or 300.0)
        self._min_poll_interval = 300.0  # 5 minutes
        self._max_poll_interval = 3600.0  # 1 hour
        self._adaptive_polling = bool(config.adaptive_polling)
        self._consecutive_unchanged = 0

        self.observer = ProjectStateObserver(config.project_root)
        self.detector = NeedDetector(config.need_rules)
        sources = list(config.candidate_sources)
        self.discoverer = OpportunityDiscoverer(sources)
        self.evaluator = TargetEvaluator(
            pursue_threshold=config.pursue_threshold,
            hold_threshold=config.hold_threshold,
        )
        self.duplicates = DuplicateContactPolicy(self.store)
        self.composer = OutreachComposer(
            sender_name=config.sender_name,
            project_name=config.project_name,
            signature=config.signature,
            transparency=config.transparency,
        )
        self.monitor = ConversationMonitor(
            self.composer,
            transport=self._transport,
            identity=self._identity,
            adapter=self._adapter,
            self_address=self.config.sender_email,
        )
        self.follow_ups = FollowUpPlanner(self.store)
        self.maintenance = SelfMaintenance(self.store)
        # Bind only an explicitly initialized service world. No schema writes or
        # identity creation on ordinary runtime startup.
        from core.services.integration import runtime_for
        self.services = runtime_for(storage, capability_registry) if capability_registry else None
        delegation = None
        if self.services and storage.load(config.identity_id, 'identity_spec'):
            session = self.services.bind(config.identity_id)
            delegation = lambda gap: self.services.escalate_gap(session, gap)
        self.gap_detector = CapabilityGapDetector(
            capability_registry=capability_registry,
            identity_id=config.identity_id,
            acquisition=acquisition,
            store=self.store,
            delegation=delegation,
        )

    def _compute_observation_fingerprint(self, state: Any) -> str:
        """Compute a deterministic fingerprint of the observed state."""
        import hashlib
        content_parts = []
        if hasattr(state, 'facts') and state.facts:
            content_parts.extend(sorted(state.facts))
        if hasattr(state, 'metadata') and state.metadata:
            for k, v in sorted(state.metadata.items()):
                content_parts.append(f"{k}:{v}")
        fingerprint = hashlib.sha256("|".join(content_parts).encode()).hexdigest()[:32]
        return fingerprint

    def _update_poll_interval(self, state_changed: bool) -> None:
        """Adaptively adjust poll interval based on state changes."""
        if not self._adaptive_polling:
            return
        if state_changed:
            self._current_poll_interval = max(self._min_poll_interval, self._current_poll_interval * 0.5)
            self._consecutive_unchanged = 0
        else:
            self._consecutive_unchanged += 1
            if self._consecutive_unchanged >= 2:
                self._current_poll_interval = min(self._max_poll_interval, self._current_poll_interval * 1.5)

    def get_current_poll_interval(self) -> float:
        """Return the current adaptive poll interval in seconds."""
        return self._current_poll_interval

    # ── lifecycle helpers ─────────────────────────────────────────────

    # ── lifecycle helpers ─────────────────────────────────────────────

    @property
    def mode(self) -> str:
        if self.store.controls().paused:
            return "paused"
        return self._mode

    def pause(self, note: str = "") -> None:
        controls = self.store.controls()
        controls.paused = True
        if note:
            controls.notes = note
        self.store.set_controls(controls)
        self._provenance(ProvenancePhase.CONTROL, "operator paused", action="pause", result=note)

    def resume(self, note: str = "") -> None:
        controls = self.store.controls()
        controls.paused = False
        if note:
            controls.notes = note
        self.store.set_controls(controls)
        self._provenance(ProvenancePhase.CONTROL, "operator resumed", action="resume", result=note)

    def override(self, **changes: Any) -> dict[str, Any]:
        """Update control constraints (never-contact, budgets, approval categories).

        Locked keys are refused: a setting the principal locked (e.g. the
        3-per-day outreach floor) survives future overrides instead of being
        silently relaxed by a later command. The refusal is loud, not silent.
        """
        controls = self.store.controls()
        for key, value in changes.items():
            if not hasattr(controls, key):
                raise ValueError(f"Unknown control field: {key}")
            if key in (controls.locked_keys or []) and key != "locked_keys":
                raise ValueError(
                    f"'{key}' is locked by the principal and cannot be changed "
                    "through override; unlock it explicitly first"
                )
            setattr(controls, key, value)
        self.store.set_controls(controls)
        self._provenance(
            ProvenancePhase.CONTROL,
            "human override applied",
            action="override",
            result=", ".join(f"{k}={v}" for k, v in changes.items()),
        )
        return controls.to_dict()

    def lock_controls(self, *keys: str) -> dict[str, Any]:
        """Lock control keys so future overrides cannot change them.

        A locked setting is a deliberate, durable decision — the outreach
        floor, for example — not a default to be relaxed by the next command.
        """
        controls = self.store.controls()
        current = list(controls.locked_keys or [])
        merged = list(dict.fromkeys([*current, *keys]))
        controls.locked_keys = merged
        self.store.set_controls(controls)
        self._provenance(
            ProvenancePhase.CONTROL,
            "control keys locked by the principal",
            action="lock_controls",
            result=", ".join(merged),
        )
        return controls.to_dict()

    def set_outbound_mode(self, mode: str) -> dict:
        """Switch the outbound operating mode (observe/autonomous/approval_required)."""
        from .models import normalize_outbound_mode

        controls = self.store.controls()
        prior = controls.outbound_mode
        controls.outbound_mode = normalize_outbound_mode(mode)
        self.store.set_controls(controls)
        self._provenance(
            ProvenancePhase.CONTROL,
            "outbound mode changed",
            action="outbound_mode",
            result=f"{prior} -> {controls.outbound_mode}",
        )
        return controls.to_dict()

    # ── the loop ──────────────────────────────────────────────────────

    def tick(
        self,
        now: Optional[datetime] = None,
        *,
        observe: bool = True,
        detect_needs: bool = True,
        discover: bool = True,
        evaluate: bool = True,
        act: bool = True,
        monitor: bool = True,
        follow_ups: bool = True,
        surfaces: bool = False,
    ) -> TickReport:
        now = now or datetime.now(timezone.utc)
        report = TickReport()
        self.store.refresh_controls()
        self.progress.begin(now)
        self._cycle_now = now
        self._surfaces_changed = False

        if self.store.controls().paused:
            self._provenance(ProvenancePhase.CONTROL, "tick skipped: operator paused", action="tick")
            report.skipped.append({"reason": "paused"})
            self._presence_update(
                "set_status",
                PresenceStatus.PAUSED,
                activity="Operator paused by principal",
            )
            return report

        self._presence_update(
            "heartbeat",
            phase=PresenceStatus.OBSERVING.value,
            activity="Observing project state" + (" and external surfaces" if surfaces else ""),
            last_tick_at=now.isoformat(),
        )
        self._refresh_principal_profile()

        if surfaces:
            report.observed = self._phase_surfaces(report) or report.observed

        # Observe and check for state changes
        state_changed = False
        if observe:
            # Capture previous fingerprint
            prev_fingerprint = self._last_observation_fingerprint
            report.observed = self._phase_observe()
            # Compute new fingerprint
            current_state = self.store.project_state()
            if current_state:
                new_fingerprint = self._compute_observation_fingerprint(current_state)
                self._last_observation_fingerprint = new_fingerprint
                if new_fingerprint != prev_fingerprint:
                    state_changed = True

        # Adaptive polling interval
        self._update_poll_interval(state_changed)

        if detect_needs:
            self._presence_update(
                "set_status",
                PresenceStatus.THINKING,
                activity="Evaluating project needs and opportunities",
            )
            if self.services:
                from core.services.worker import requester_tick
                service_result = requester_tick(self.services, self.config.identity_id)
                if service_result != 'idle':
                    self._provenance(ProvenancePhase.CONTROL, 'Service workflow: '+service_result,
                        action='service.requester', result=service_result)
                    if service_result == 'principal_review_required':
                        report.escalations.append('service contract review')
                session = self.services.bind(self.config.identity_id)
                for spec in self.services.negotiation.list(session):
                    if spec['requester'] != self.config.identity_id or spec['state'] != 'CLARIFICATION_REQUIRED':
                        continue
                    key = spec['id'] + ':' + str(spec['revision'])
                    if not any(n.refs.get('specification_revision') == key for n in self.store.list_notifications()):
                        self._notify(kind='principal_instruction', summary='A service specification needs acceptance criteria before work can begin.', refs={'specification_revision':key})
            self._phase_gaps(report)
            report.needs_created = [n.id for n in self.detector.detect(self.store, self.store.project_state())] if self.store.project_state() else []
            if report.needs_created:
                self._provenance(
                    ProvenancePhase.DETECT_NEEDS,
                    f"detected {len(report.needs_created)} new need(s)",
                    action="detect_needs",
                    result=", ".join(report.needs_created),
                )
                self._presence_update(
                    "mark_meaningful_action",
                    f"Detected {len(report.needs_created)} new need(s)",
                )
        if discover:
            report.opportunities_created = self._phase_discover(report)
        if evaluate:
            report.evaluated, report.qualified = self._phase_evaluate()
        if act:
            self._presence_update(
                "set_status",
                PresenceStatus.ACTING,
                activity="Evaluating autonomous action opportunities",
            )
            report.outreach_sent, report.escalations, act_skips = self._phase_act(report)
            report.skipped.extend(act_skips)
        if monitor:
            _, report.replies_sent, monitor_skips = self._phase_monitor()
            report.skipped.extend(monitor_skips)
            principal_results = self._phase_principal(now)
            report.principal_processed = [r["message_id"] for r in principal_results if r.get("outcome") == "completed"]
            report.principal_deferred = [r["message_id"] for r in principal_results if r.get("outcome") != "completed"]
        # Diagnose the state after monitor recovery, but clean scheduled work
        # before follow-ups can act on it. The persisted report therefore
        # describes the final state Aster actually established this cycle.
        report.maintenance = self._phase_maintenance(now)
        if follow_ups:
            report.follow_ups_sent, follow_skips = self._phase_follow_ups(now)
            report.skipped.extend(follow_skips)

        if self._secret_store is not None:
            try:
                from .notify import reconcile as _reconcile_notifications

                notified = _reconcile_notifications(self)
                report.notifications_created = list(notified.get("created", []))
            except Exception as exc:
                logger.warning("notification reconcile failed: %s", exc)

        self._presence_after_tick(report, state_changed, now)
        report.cycle_outcome = self.progress.finish(report, project_changed=state_changed, surfaces_changed=self._surfaces_changed)
        outcome = report.cycle_outcome
        if not report.errors and not report.escalations and not any(s.get('reason') == 'daily_budget_exhausted' for s in report.skipped):
            self._presence_update('set_status', PresenceStatus.WAITING if outcome['waiting_conditions'] else PresenceStatus.IDLE,
                activity='Autonomous cycle: '+outcome['result'], next_planned_action=outcome['next_eligible_actions'][0]['description'])
        return report

    def _phase_maintenance(self, now: datetime) -> dict[str, Any]:
        """Diagnose and reconcile durable state without communicating.

        This phase never composes or sends. It restores bookkeeping invariants
        and persists an evidence-backed report; the later monitor and follow-up
        phases independently decide whether any owed communication may run.
        """
        try:
            with self._ingest_lock:
                report = self.maintenance.run(
                    now=now,
                    reconcile_jobs=self._reconcile_email_jobs,
                )
            self._presence_update(
                "set_subsystem", "self_maintenance", report.get("status", "unknown"))
            return report
        except Exception as exc:
            logger.exception("self-maintenance failed")
            self._presence_update("set_subsystem", "self_maintenance", "degraded")
            return {
                "status": "degraded",
                "checked_at": now.isoformat(),
                "findings": [{
                    "code": "maintenance_failed",
                    "severity": "error",
                    "detail": f"{type(exc).__name__}: {exc}"[:200],
                    "refs": {},
                }],
                "actions": [],
            }

    # ── principal knowledge ───────────────────────────────────────────

    def _refresh_principal_profile(self) -> list[str]:
        """Refresh cached public facts about the human principal if stale.

        Fetches only public sources (GitHub profile/repos, portfolio site)
        through the permission-gated web capability. Best-effort: any failure
        keeps the last cached profile, and an empty cache simply means
        replies proceed without principal facts. Never blocks the tick.
        """
        from .principal_knowledge import (
            derive_github_user,
            fetch_principal_profile,
            load_profile,
            principal_context_lines,
            save_profile,
        )

        try:
            profile = load_profile(self.storage, self.config.identity_id)
            if profile.is_fresh():
                return principal_context_lines(profile)
            registry = self._capability_registry
            if registry is None:
                return principal_context_lines(profile)
            allowed, _ = registry.can(self.config.identity_id, "web.fetch")
            if not allowed:
                return principal_context_lines(profile)
            from .aster import ASTER_GITHUB_URL

            user = derive_github_user(ASTER_GITHUB_URL)

            def _fetch(url: str) -> str:
                result = registry.call(self.config.identity_id, "web.fetch", url=url)
                if not result.success:
                    return ""
                data = result.data or {}
                text = data.get("text", "") if isinstance(data, dict) else ""
                return str(text or "")

            def _extract(url: str) -> str:
                result = registry.call(self.config.identity_id, "web.extract", url=url)
                if not result.success:
                    return ""
                data = result.data or {}
                text = data.get("extracted_text", "") if isinstance(data, dict) else ""
                return str(text or "")

            fresh = fetch_principal_profile(_fetch, github_user=user, extractor=_extract)
            if not fresh.is_empty():
                save_profile(self.storage, self.config.identity_id, fresh)
                self._provenance(
                    ProvenancePhase.OBSERVE,
                    "refreshed public principal profile",
                    action="principal_profile.refresh",
                    result=f"{len(fresh.sources)} source(s): {', '.join(fresh.sources)}",
                )
                return principal_context_lines(fresh)
            return principal_context_lines(profile)
        except Exception as exc:
            logger.warning("principal profile refresh failed: %s", exc)
            return []

    # ── presence ──────────────────────────────────────────────────────

    def _record_ingest_health(self, *, ok: bool, fetched: int = 0, error: str = "") -> None:
        """Record real ingest liveness so a dead poller is *visible*.

        The distinction that matters: "no new mail" (healthy idle) versus
        "the poller is not running" (unhealthy). Presence classifies the
        operator from heartbeat age; this records the last time the email
        monitor actually talked to the mailbox, so an unanswered message can
        be attributed to a stopped ingest instead of an unexplained silence.
        """
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            payload = {
                "ok": bool(ok),
                "fetched": int(fetched),
                "at": stamp,
                "error": (error or "")[:300],
                "run_id": self._run_id,
            }
            self.storage.save(self.config.identity_id, "operations.ingest_health", payload)
        except Exception as exc:
            logger.warning("ingest health write failed: %s", exc)
        if not ok:
            self._presence_update("set_subsystem", "email_ingest", "degraded")
        else:
            stalled = self.stalled_email_jobs()
            if stalled:
                self._presence_update("set_subsystem", "email_ingest", "degraded")
            else:
                self._presence_update("set_subsystem", "email_ingest", "healthy")

    def stalled_email_jobs(self, *, now: Optional[datetime] = None,
                           older_than_seconds: float = 900.0) -> list[Any]:
        """Non-terminal email jobs that have not moved for too long.

        These are the messages Aster is still owed an answer for. Surfacing
        them is what turns an unexplained "no reply" into a specific,
        actionable state (held / failed / claimed-stuck).
        """
        from .models import EmailJobStatus as _S

        now = now or datetime.now(timezone.utc)
        out = []
        for job in self.store.list_email_jobs():
            if job.is_terminal() or job.status is _S.HELD:
                continue
            last = job.updated_at or job.claimed_at or job.created_at
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds()
            except (TypeError, ValueError):
                continue
            if age >= older_than_seconds:
                out.append(job)
        return out

    def _presence_update(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Update presence without ever breaking the operator loop.

        A presence write failure must stay observable (logged) but must never
        crash a tick; a failed write surfaces honestly as staleness on the
        next read.
        """
        if self._presence is None:
            return
        try:
            getattr(self._presence, method)(*args, **kwargs)
        except Exception as exc:
            logger.warning("presence update failed (%s): %s", method, exc)

    def _presence_after_tick(self, report: TickReport, state_changed: bool, now: datetime) -> None:
        """Derive the resting presence state from what this tick actually did."""
        if self._presence is None:
            return
        budget_wait = any(s.get("reason") == "daily_budget_exhausted" for s in report.skipped)
        acted = bool(report.outreach_sent or report.replies_sent or report.follow_ups_sent
                     or report.needs_created or report.opportunities_created)
        if report.escalations:
            status = PresenceStatus.WAITING
            activity = f"Awaiting principal review of {len(report.escalations)} escalation(s)"
            next_planned = "Check for principal authorization decisions"
        elif budget_wait:
            status = PresenceStatus.WAITING
            activity = "Outreach paused: daily budget exhausted"
            next_planned = "Resume outreach after budget reset"
        elif report.errors:
            # _phase_act already recorded DEGRADED with the send failure.
            return
        else:
            status = PresenceStatus.IDLE
            if state_changed:
                activity = "Observed changes to project state"
            elif acted:
                activity = "Awaiting next observation"
            else:
                activity = "No meaningful environmental changes"
            next_planned = f"Observe again in {max(1, int(self._current_poll_interval // 60))} minute(s)"
        self._presence_update(
            "set_status",
            status,
            activity=activity,
            next_planned_action=next_planned,
            next_check_at=(now + timedelta(seconds=self._current_poll_interval)).isoformat(),
            last_tick_at=now.isoformat(),
        )
        self._presence_update("set_counts", opportunity_count=len(self.store.list_opportunities()))
        for surface in self._surfaces:
            if getattr(surface, "name", "") != "culture_commons":
                continue
            try:
                self._presence_update("set_subsystem", "commons_standing", surface.standing_state())
            except Exception as exc:
                logger.warning("presence commons_standing update failed: %s", exc)

    # ── phases ────────────────────────────────────────────────────────

    def _phase_observe(self) -> bool:
        state = self.observer.observe()
        prior = self.store.project_state()
        changed = prior is None or self._compute_observation_fingerprint(prior) != self._compute_observation_fingerprint(state)
        self.store.set_project_state(state)
        if not changed:
            return True
        self._provenance(
            ProvenancePhase.OBSERVE,
            f"Project '{state.name}' changed: {len(set(state.facts)-set(prior.facts if prior else []))} added, {len(set(prior.facts if prior else [])-set(state.facts))} removed facts",
            action="observe_project",
            result=state.summary[:200],
            evidence=state.evidence[:8],
            refs={"project_id": state.project_id, "added_facts": sorted(set(state.facts)-set(prior.facts if prior else [])), "removed_facts": sorted(set(prior.facts if prior else [])-set(state.facts))},
        )
        return True

    def _phase_surfaces(self, report: TickReport) -> bool:
        """Poll optional external surfaces (opt-in per tick; default off).

        Surfaces never block a normal conversational or operator tick: they are
        wired only when a caller explicitly asks for them.
        """
        observed_any = False
        for surface in self._surfaces:
            try:
                result = surface.observe() or {}
                observed = bool(result.get("observed"))
                self._surfaces_changed = self._surfaces_changed or bool(result.get('changed'))
            except Exception as exc:  # pragma: no cover - defensive
                report.skipped.append({"surface": surface.name, "reason": "observe_failed", "error": str(exc)})
                self._provenance(
                    ProvenancePhase.OBSERVE,
                    f"surface '{surface.name}' observe failed",
                    action="surface.observe",
                    result=str(exc),
                    refs={"surface": surface.name},
                )
                continue
            if observed:
                observed_any = True
        return observed_any

    def _phase_gaps(self, report: TickReport) -> None:
        eligible = []
        for skill in self.config.required_skills:
            need = self.progress.blocker('skill:'+skill)
            domain = 'authority' if need and need.metadata['blocker']['classification']=='AUTHORITY_GAP' else 'configuration'
            condition = self.progress.condition(domain)
            if self.progress.eligible('skill:'+skill,condition):eligible.append(skill)
        gaps = self.gap_detector.check(eligible)
        missing = {g.required_skill for g in gaps}
        for skill in eligible:
            if skill not in missing:self.progress.resolve('skill:'+skill,'Registry inspection now permits use')
        for gap in gaps:
            resolved = self.gap_detector.resolve(gap)
            report.capability_gaps.append(resolved.to_dict())
            self._provenance(
                ProvenancePhase.CONTROL,
                f"capability gap: {gap.required_skill} [{gap.status}]",
                action="capability_gap",
                result=gap.resolution or "unresolved",
                evidence=gap.evidence,
                refs={"required_skill": gap.required_skill, "resolved": gap.resolved, "status": gap.status},
            )
            if resolved.resolved:
                self.progress.resolve('skill:'+gap.required_skill, resolved.resolution or 'Runtime verified resolution')
                continue
            classification=gap.classification
            condition=self.progress.condition('authority' if classification=='AUTHORITY_GAP' else 'configuration')
            delay = None if classification in {'AUTHORITY_GAP','CONFIGURATION_ERROR'} else self.progress.now + 300
            self.progress.wait('skill:'+gap.required_skill,classification,condition,
                gap.required_skill+' — '+(gap.reason or classification), retry=delay,principal=classification=='AUTHORITY_GAP',
                reason=('Relevant permission state changes; principal decision only if external discovery is desired' if classification=='AUTHORITY_GAP' else 'Configuration or executable service catalog changes; no repair service is currently assumed'))
            self._notify_gap(gap)

        if report.capability_gaps:
            unresolved = [g for g in report.capability_gaps if not g.get("resolved")]
            limited = sorted({g.get("required_skill", "") for g in unresolved if g.get("required_skill")})
            required = list(getattr(self.config, "required_skills", []) or [])
            healthy = max(0, len(required) - len(limited))
            # Permission-limited skills are reported, never hidden — but on
            # their own they do not degrade global health. Only a material
            # failure (e.g. a failed send, marked "degraded" in _phase_act)
            # does that. Reconciling the marker every tick also self-heals
            # stale markers from earlier runs.
            self._presence_update("set_capability_summary", healthy=healthy, limited=limited)
            if not unresolved:
                self._presence_update("set_subsystem", "capability_health", "healthy")
                skill = report.capability_gaps[-1].get("required_skill", "")
                self._presence_update("mark_meaningful_action", f"Resolved capability gap: {skill}")
            else:
                self._presence_update("set_subsystem", "capability_health", "limited")

    def _notify_gap(self, gap: "CapabilityGap") -> None:
        """Notify the principal about gaps that need a human decision, once per
        (kind, skill) — re-ticking must not re-notify."""
        if gap.resolved:
            return
        if gap.status == CapabilityStatus.INSTALLED_PERMISSION_MISSING.value:
            kind, summary = "permission_required", (
                f"Required skill '{gap.required_skill}' is installed but permission is denied: {gap.reason}"
            )
        elif gap.status == CapabilityStatus.AVAILABLE_NOT_INSTALLED.value:
            kind, summary = "capability_available", (
                f"A built-in capability provides required skill '{gap.required_skill}' but it is not installed"
            )
        else:
            return
        existing = self.store.list_notifications()
        for entry in existing:
            if entry.kind != kind:
                continue
            if str((entry.refs or {}).get("required_skill", "")) == gap.required_skill:
                return
        self._notify(kind=kind, summary=summary, refs={
            "required_skill": gap.required_skill, "capability_gap_status": gap.status,
        })

    def _phase_discover(self, report: TickReport) -> list[str]:
        from .models import NeedStatus

        created: list[str] = []
        open_needs = [n for n in self.store.list_needs(status=NeedStatus.OPEN)
                      if not n.metadata.get("blocker")] [: self.config.max_needs_per_tick]
        for need in open_needs:
            for opportunity in self.discoverer.discover(self.store, need):
                created.append(opportunity.id)
                self._provenance(
                    ProvenancePhase.DISCOVER,
                    f"discovered opportunity '{opportunity.target_name or opportunity.organization}'",
                    action="discover",
                    result=opportunity.id,
                    evidence=opportunity.evidence[:6],
                    refs={"need_id": need.id, "opportunity_id": opportunity.id},
                )
        if created:
            self._provenance(
                ProvenancePhase.DISCOVER,
                f"discovered {len(created)} new opportunity(ies)",
                action="discover",
                result=", ".join(created),
            )
        return created

    def _phase_evaluate(self) -> tuple[list[str], list[str]]:
        evaluated: list[str] = []
        qualified: list[str] = []
        for opportunity in self.store.list_opportunities(status=OpportunityStatus.DISCOVERED):
            need = self.store.get_need(opportunity.need_id)
            if need is None:
                opportunity.status = OpportunityStatus.REJECTED
                self.store.update_opportunity(opportunity)
                continue
            evaluation = self.evaluator.evaluate(opportunity, need)
            self.store.save_evaluation(evaluation)
            evaluated.append(opportunity.id)
            if evaluation.recommendation == "pursue":
                opportunity.status = OpportunityStatus.QUALIFIED
                qualified.append(opportunity.id)
            elif evaluation.recommendation == "reject":
                opportunity.status = OpportunityStatus.REJECTED
            else:
                opportunity.status = OpportunityStatus.EVALUATING
            self.store.update_opportunity(opportunity)
            self._provenance(
                ProvenancePhase.EVALUATE,
                f"evaluated '{opportunity.target_name or opportunity.organization}': {evaluation.recommendation} (score {evaluation.score})",
                action="evaluate",
                result=evaluation.recommendation,
                evidence=evaluation.evidence[:6],
                refs={"opportunity_id": opportunity.id, "score": evaluation.score},
            )
        return evaluated, qualified

    def _phase_act(self, report: TickReport) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        sent: list[str] = []
        escalations: list[str] = []
        skips: list[dict[str, Any]] = []
        controls = self.store.controls()
        budget = self.store.budget()

        qualified = [
            o for o in self.store.list_opportunities(status=OpportunityStatus.QUALIFIED)
        ]
        qualified.sort(
            key=lambda o: (
                self.store.get_evaluation(o.id).score if self.store.get_evaluation(o.id) else 0.0
            ),
            reverse=True,
        )

        for opportunity in qualified[: self.config.max_outreach_per_tick]:
            decision = self.duplicates.evaluate(opportunity)
            if not decision.allowed:
                opportunity.status = OpportunityStatus.CLOSED
                self.store.update_opportunity(opportunity)
                skips.append({"opportunity_id": opportunity.id, "reason": decision.code})
                self._provenance(
                    ProvenancePhase.PLAN,
                    "outreach blocked by duplicate-contact policy",
                    action="duplicate_check",
                    result=decision.reason,
                    refs={"opportunity_id": opportunity.id, "code": decision.code},
                )
                continue

            if not opportunity.contact_email:
                # A candidate without a contact is not an outreach target. Track
                # the attempts and demote after a bounded number so a page-only
                # lead cannot occupy the top of the queue every tick and starve
                # the contact-bearing candidates behind it (live finding: three
                # junk leads blocked outreach for days).
                attempts = int((opportunity.metadata or {}).get("contact_lookup_attempts", 0))
                if attempts >= 3:
                    # Park it: EVALUATING keeps the record visible for a later
                    # re-evaluation without occupying the act queue.
                    opportunity.status = OpportunityStatus.EVALUATING
                    opportunity.metadata = dict(opportunity.metadata or {})
                    opportunity.metadata["hold_cause"] = "no_contact_evidence"
                    self.store.update_opportunity(opportunity)
                    self._provenance(
                        ProvenancePhase.PLAN,
                        f"held after {attempts} ticks with no contact evidence; demoted so others can proceed",
                        action="outreach_blocked",
                        result="no_contact_evidence",
                        refs={"opportunity_id": opportunity.id, "attempts": attempts},
                    )
                    continue
                opportunity.metadata = dict(opportunity.metadata or {})
                opportunity.metadata["contact_lookup_attempts"] = attempts + 1
                self.store.update_opportunity(opportunity)
                skips.append({"opportunity_id": opportunity.id, "reason": "no_contact_email"})
                self._provenance(
                    ProvenancePhase.PLAN,
                    "no contact email discovered; cannot send individualized outreach",
                    action="outreach_blocked",
                    result="no_contact_email",
                    refs={"opportunity_id": opportunity.id, "attempts": attempts + 1},
                )
                continue

            if budget.cold_outreach >= controls.max_cold_outreach_per_day:
                skips.append({"opportunity_id": opportunity.id, "reason": "daily_budget_exhausted"})
                self._presence_update(
                    "set_status",
                    PresenceStatus.WAITING,
                    activity="Outreach paused: daily budget exhausted",
                )
                self._provenance(
                    ProvenancePhase.CONTROL,
                    "daily cold-outreach budget exhausted",
                    action="budget",
                    result=f"{budget.cold_outreach}/{controls.max_cold_outreach_per_day}",
                )
                break

            if float(opportunity.confidence or 0.0) <= 0 and not opportunity.test_candidate:
                # Defense in depth: an autonomous operator never pursues a candidate
                # with no source confidence unless explicitly marked test_candidate.
                skips.append({"opportunity_id": opportunity.id, "reason": "confidence_zero"})
                self._provenance(
                    ProvenancePhase.PLAN,
                    "outreach skipped: source confidence is zero",
                    action="confidence_gate",
                    result="candidate not marked test_candidate=true",
                    refs={"opportunity_id": opportunity.id, "confidence": opportunity.confidence},
                )
                continue

            need = self.store.get_need(opportunity.need_id)
            if need is None:
                continue
            brief = OutreachBrief.from_opportunity(
                opportunity, need,
                sender_name=self.config.sender_name,
                transparency=self.config.transparency,
                signature=self._signature_for(first_contact=True),
            )
            subject, body = self.composer.compose(brief, adapter=self._adapter, identity=self._identity)

            auth = AuthorityPolicy(controls).evaluate(
                "send cold outreach about a program",
                category=opportunity.category,
                content=f"{subject}\n{body}",
                mode="commitment",
            )

            if not self._allowlist_allows(opportunity.contact_email):
                skips.append({"opportunity_id": opportunity.id, "reason": "not_in_allowlist"})
                self._provenance(
                    ProvenancePhase.PLAN,
                    "cold outreach gated by recipient allowlist",
                    action="allowlist",
                    result=f"recipient {opportunity.contact_email} is not allowlisted",
                    refs={"opportunity_id": opportunity.id, "recipient": opportunity.contact_email},
                )
                continue

            message = Message(
                relationship_id="",
                direction=MessageDirection.OUTBOUND,
                subject=subject,
                body=body,
                status=MessageStatus.DRAFT,
                need_id=need.id,
                opportunity_id=opportunity.id,
            )

            relationship = Relationship(
                display_name=opportunity.target_name or opportunity.organization,
                organization=opportunity.organization,
                email=opportunity.contact_email,
                purpose=self.config.purpose,
                need_id=need.id,
                opportunity_id=opportunity.id,
                status=RelationshipStatus.NEW,
            )

            approval_mode = controls.outbound_mode == "approval_required"
            if auth.requires_human or approval_mode or self._transport is None:
                reason = (
                    auth.reason if auth.requires_human else
                    "outbound_mode requires human approval" if approval_mode else
                    "no transport configured (dry-run)"
                )
                message.status = MessageStatus.AWAITING_AUTHORIZATION
                message.authorization = "awaiting_human_authorization"
                store_rel = self.store.add_relationship(relationship)
                store_rel.status = RelationshipStatus.AWAITING_AUTHORIZATION
                store_rel.next_action = "human authorization required"
                self.store.update_relationship(store_rel)
                message.relationship_id = store_rel.id
                self.store.append_message(message)
                escalations.append(message.id)
                self._presence_update("mark_meaningful_action", "Escalated outreach for human authorization")
                self._presence_update(
                    "set_status",
                    PresenceStatus.WAITING,
                    activity="Escalated outreach: awaiting human authorization",
                )
                self._notify(kind="escalation", summary=f"outreach requires human authorization ({auth.reason})",
                             refs={"message_id": message.id, "opportunity_id": opportunity.id})
                self._provenance(
                    ProvenancePhase.ESCALATE,
                    "outreach requires human authorization",
                    action="escalate",
                    result=reason,
                    evidence=[f"matched:{auth.matched_terms}"] if auth.matched_terms else [f"mode:{controls.outbound_mode}"],
                    refs={"message_id": message.id, "opportunity_id": opportunity.id, "notify": "principal:escalation"},
                )
                continue

            if controls.outbound_mode == "observe":
                if self._has_would_send(opportunity_id=opportunity.id):
                    skips.append({"opportunity_id": opportunity.id, "reason": "would_send_already_recorded"})
                    continue
                message.status = MessageStatus.WOULD_SEND
                message.authorization = "observe_would_send"
                message.evidence = [f"policy:{auth.reason}"]
                self.store.append_message(message)
                self._provenance(
                    ProvenancePhase.PLAN,
                    "outreach drafted in observation mode (not sent)",
                    action="would_send",
                    result=auth.reason,
                    evidence=[f"mode=observe", f"to={opportunity.contact_email}"],
                    refs={"message_id": message.id, "opportunity_id": opportunity.id},
                )
                continue

            if budget.cold_outreach >= controls.max_cold_outreach_per_day:
                skips.append({"opportunity_id": opportunity.id, "reason": "daily_budget_exhausted"})
                self._presence_update(
                    "set_status",
                    PresenceStatus.WAITING,
                    activity="Outreach paused: daily budget exhausted",
                )
                self._provenance(
                    ProvenancePhase.CONTROL,
                    "daily cold-outreach budget exhausted",
                    action="budget",
                    result=f"{budget.cold_outreach}/{controls.max_cold_outreach_per_day}",
                )
                break

            html_body, signature_variant = "", "unsigned"
            if self._communication_identity is not None:
                try:
                    from .voice import render_html_body

                    html_body, signature_variant = render_html_body(
                        body, self._communication_identity)
                except Exception:
                    html_body, signature_variant = "", "unsigned"
            send_result = self._send(
                to=opportunity.contact_email,
                subject=subject,
                body=body,
                html_body=html_body,
            )
            if not send_result.get("ok"):
                message.status = MessageStatus.FAILED
                message.evidence = [f"send_error:{send_result.get('error')}"]
                store_rel = self.store.add_relationship(relationship)
                message.relationship_id = store_rel.id
                self.store.append_message(message)
                report.errors.append({"message_id": message.id, "error": send_result.get("error")})
                self._presence_update("set_subsystem", "capability_health", "degraded")
                self._presence_update(
                    "set_status",
                    PresenceStatus.DEGRADED,
                    activity=f"Outreach send failed: {send_result.get('error')}",
                )
                self._provenance(
                    ProvenancePhase.ACT,
                    "outreach send failed",
                    action="send",
                    result=str(send_result.get("error")),
                    refs={"message_id": message.id, "opportunity_id": opportunity.id},
                )
                continue

            message.external_id = str(send_result.get("external_id", ""))
            message.thread_id = str(send_result.get("thread_id", ""))
            message.status = MessageStatus.SENT
            message.sent_at = utcnow().isoformat()
            message.authorization = "autonomous_outreach"
            message.evidence = list(send_result.get("evidence", []))
            store_rel = self.store.add_relationship(relationship)
            store_rel.status = RelationshipStatus.OUTREACH_SENT
            store_rel.first_contacted_at = message.sent_at
            store_rel.last_outbound_at = message.sent_at
            if message.thread_id:
                store_rel.thread_ids.append(message.thread_id)
            store_rel.message_ids.append(message.id)
            store_rel.next_action = "await reply; follow up if quiet"
            self.store.update_relationship(store_rel)
            message.relationship_id = store_rel.id
            self.store.append_message(message)

            opportunity.status = OpportunityStatus.CONTACTED
            self.store.update_opportunity(opportunity)
            self.store.record_usage("cold_outreach")
            budget = self.store.budget()
            sent.append(message.id)
            self._presence_update(
                "set_status",
                PresenceStatus.ACTING,
                activity=f"Sent individualized outreach to '{relationship.display_name}'",
            )
            self._presence_update(
                "mark_meaningful_action",
                f"Sent individualized outreach to '{relationship.display_name}'",
            )
            from .voice import count_em_dashes

            self._provenance(
                ProvenancePhase.ACT,
                f"sent individualized outreach to '{relationship.display_name}'",
                action="send",
                result=message.external_id or "sent",
                evidence=message.evidence[:5],
                refs={"message_id": message.id, "opportunity_id": opportunity.id, "relationship_id": store_rel.id,
                      "sender": self._sender_email(),
                      "sender_display_name": self._sender_display_name(),
                      "signature": signature_variant,
                      "style_validation": "passed",
                      "em_dash_count": count_em_dashes(subject) + count_em_dashes(body)},
            )
        return sent, escalations, skips

    def _retry_stale_deferred(self, *, limit: int = 2) -> list[dict[str, Any]]:
        """Re-drive owed inbound email left over from outages or shutdowns.

        Explicitly deferred messages are eligible, as are durable inbound
        records whose non-terminal job was recovered after a crash. The latter
        matters because the mailbox cursor has already moved past the message:
        resetting its job to DISCOVERED is not enough to make IMAP fetch it
        again. Ignored, quarantined, answered, closed, and policy-held messages
        are never swept into sending. Work is oldest first and capped per tick
        so a prolonged outage burns a bounded number of model calls.
        Already-answered items get their stale marker cleared without action.
        """
        from .monitor import message_answered as _message_answered

        outcomes: list[dict[str, Any]] = []

        def _answered(message: Any) -> bool:
            return bool(_message_answered(self.store, message))
        from .models import EmailJobStatus

        recoverable: set[str] = set()
        for job in self.store.list_email_jobs():
            if job.is_terminal():
                continue
            # HELD means policy or human review is intentionally blocking the
            # reply. Only an observe-mode draft becomes automatically eligible
            # after the principal changes the mode to autonomous.
            if job.status is EmailJobStatus.HELD and not (
                self.store.controls().outbound_mode == "autonomous"
                and job.failure_reason.startswith("observe mode:")
            ):
                continue
            if job.status is EmailJobStatus.FAILED and not job.retryable:
                continue
            recoverable.update(filter(None, (
                job.inbound_message_id,
                job.rfc_message_id,
                job.gmail_message_id,
            )))

        candidates = []
        for message in self.store.list_messages():
            if (message.direction is not MessageDirection.INBOUND
                    or message.channel != "email"
                    or message.status is not MessageStatus.RECEIVED):
                continue
            explicitly_deferred = str(message.authorization or "").startswith("deferred:")
            recovered_job = bool({message.id, message.external_id} & recoverable)
            if explicitly_deferred or recovered_job:
                candidates.append(message)
        candidates.sort(key=lambda m: m.created_at or "")
        for message in candidates[: max(1, limit)]:
            if _answered(message):
                message.authorization = ""
                self.store.update_message(message)
                continue
            outcomes.append(self.retry_deferred_reply(message.id))
        return outcomes

    def retry_deferred_reply(self, message_id: str) -> dict[str, Any]:
        """Re-run reply processing for a deferred inbound email message.

        Deferred monitor replies (e.g. model outage at tick time) are skipped
        forever by the duplicate guard, which would strand them. This explicit
        recovery reuses the exact same respond path — policy, budgets, mode —
        without recording a second copy. Refuses when the message is not a
        retryable inbound email or the relationship is closed.
        """
        message = self.store.get_message(message_id)
        if message is None:
            return {"ok": False, "error": f"unknown message: {message_id}"}
        if message.direction is not MessageDirection.INBOUND or message.channel != "email":
            return {"ok": False, "error": "only inbound email messages can be retried"}
        if message_answered(self.store, message):
            return {"ok": False, "error": "message already answered; refusing duplicate"}
        if message.status is not MessageStatus.RECEIVED:
            return {"ok": False, "error": f"message is not pending: {message.status.value}"}
        relationship = self.store.get_relationship(message.relationship_id)
        if relationship is None:
            return {"ok": False, "error": "message has no relationship"}
        if relationship.opted_out or relationship.status in (
            RelationshipStatus.DECLINED, RelationshipStatus.OPTED_OUT
        ):
            return {"ok": False, "error": "relationship is closed"}
        sender_email = relationship.email or ""
        from .email_jobs import claim_job, ensure_job

        job = self._email_job_for_message(message)
        if job is None:
            job = ensure_job(
                self.store,
                inbound_message_id=message.external_id or message.id,
                rfc_message_id=message.external_id or "",
                thread_id=message.thread_id or "",
                sender=sender_email,
                received_at=message.received_at or message.created_at or "",
            )
        worker_id = self._worker_id()
        granted, claim_reason = claim_job(self.store, job, worker_id)
        if not granted:
            return {"ok": False, "error": f"job not claimable: {claim_reason}"}
        result = self.monitor.respond_to_recorded(
            self.store, relationship, message,
            sender_email=sender_email, body=message.body, subject=message.subject,
            disposition=None, job=job, worker_id=worker_id,
        )
        if result.treated_as not in ("deferred", "failed"):
            message.authorization = ""
            self.store.update_message(message)
        self._provenance(
            ProvenancePhase.MONITOR,
            f"retried deferred reply to '{relationship.display_name or sender_email}'",
            action="retry_deferred_reply",
            result=f"{result.treated_as}: {result.reason}",
            refs={"message_id": message.id, "relationship_id": relationship.id},
        )
        if result.responded:
            self._presence_update(
                "mark_meaningful_action",
                f"Replied to inbound from '{relationship.display_name}' on retry",
            )
        return {"ok": True, "treated_as": result.treated_as, "reason": result.reason,
                "responded": result.responded, "escalated": result.escalated,
                "relationship_id": relationship.id}

    def _email_job_for_message(self, message: Message) -> Any:
        """Find the durable job for a recorded inbound without guessing.

        Gmail jobs use the immutable Gmail id while message.external_id is
        commonly the RFC Message-ID. Check every persisted provider identity
        so crash recovery adopts the original job rather than creating a
        second job for the same human message.
        """
        keys = {message.id, message.external_id}
        for job in self.store.list_email_jobs():
            if keys & {
                job.inbound_message_id,
                job.rfc_message_id,
                job.gmail_message_id,
            }:
                return job
        return None

    def _ensure_project_context(self) -> None:
        """Guarantee verified project facts exist before composing a reply.

        Email ingest can run between operator ticks, on a fresh process, so it
        cannot assume the observe phase already ran. A reply generated with no
        project context is deferred rather than answered from nothing, so the
        observation is primed here (cheap and idempotent) instead of leaving
        the first human message of a restart unanswered.
        """
        try:
            if self.store.project_state() is None:
                self._phase_observe()
        except Exception as exc:
            logger.warning("project context priming failed: %s", exc)

    def poll_email(self) -> dict[str, Any]:
        """Run only the email monitor phase (fetch, reconcile, ingest).

        This is the whole of email ingest. Splitting it from :meth:`tick` is
        what allows a human's mail to be answered in seconds while the
        operator's discovery/evaluation/follow-up cycle keeps running on its
        own slower, deliberate cadence. It is idempotent: the same job
        claiming and cursor rules apply, so a concurrent operator tick and
        ingest thread cannot both answer the same message.
        """
        now = datetime.now(timezone.utc)
        self._cycle_now = now
        report = TickReport()
        self.store.refresh_controls()
        # The Gmail push drain may run in a different process; reload the job
        # ledger so its work is visible here and cannot be repeated.
        self.store.refresh_email_jobs()
        if self.store.controls().paused:
            report.skipped.append({"reason": "paused"})
            return {"ok": False, "reason": "paused", "replies": []}
        self._ensure_project_context()
        try:
            results, replies, skips = self._phase_monitor()
        except Exception as exc:
            logger.exception("email ingest poll failed")
            return {"ok": False, "reason": str(exc), "replies": []}
        report.replies_sent.extend(replies)
        report.skipped.extend(skips)
        report.errors.extend(
            str(item.get("error")) for item in skips
            if item.get("reason") == "inbox_fetch_failed"
        )
        for result in results:
            if result.responded and result.relationship is not None:
                self._presence_update(
                    "mark_meaningful_action",
                    f"Replied to inbound from '{result.relationship.display_name}'",
                )
        return {
            "ok": True,
            "replies": replies,
            "inbound": len(results),
            "skipped": skips,
        }

    def ingest_messages(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Ingest messages already fetched by an event-driven source.

        Gmail push hands over messages it pulled from the history API; this
        runs them through exactly the same durable job creation, claiming,
        policy, and send path as a polled message. Keeping one pipeline is the
        point: two pipelines would mean two chances to answer one message, and
        two places for a reply to be silently dropped.
        """
        if not items:
            return {"ok": True, "inbound": 0, "replies": [], "skipped": []}
        now = datetime.now(timezone.utc)
        self._cycle_now = now
        self.store.refresh_controls()
        self.store.refresh_email_jobs()
        if self.store.controls().paused:
            return {"ok": False, "reason": "paused", "replies": []}
        self._ensure_project_context()
        try:
            results, replies, skips = self._process_inbound_items(list(items))
        except Exception as exc:
            logger.exception("event-driven ingest failed")
            self._record_ingest_health(ok=False, error=str(exc))
            return {"ok": False, "reason": str(exc), "replies": []}
        self._record_ingest_health(ok=True, fetched=len(items))
        for result in results:
            if result.responded and result.relationship is not None:
                self._presence_update(
                    "mark_meaningful_action",
                    f"Replied to inbound from '{result.relationship.display_name}'",
                )
        return {
            "ok": True,
            "inbound": len(results),
            "replies": replies,
            "skipped": skips,
        }

    def _worker_id(self) -> str:
        """Identity of the *worker* claiming jobs, not just the process.

        The operator tick and the dedicated email ingest thread run in one
        process and would otherwise share ``_run_id``. ``claim_job`` treats a
        second claim by the same owner as reentrant and grants it, so both
        threads could generate and send a reply to the same message. Including
        the thread makes them distinct owners, which is what they are.

        The per-thread token is a UUID held in thread-local storage rather
        than ``threading.get_ident()``: CPython recycles thread idents once a
        thread dies, so a new thread could inherit the id of a dead one. A
        recycled id would make a dead worker's claim look live and strand the
        job until the stale timeout.
        """
        token = getattr(self._worker_local, "token", None)
        if token is None:
            token = uuid.uuid4().hex
            self._worker_local.token = token
        return f"{self._run_id}:{token}"

    def _process_inbound_items(self, incoming):
        """Turn fetched messages into durable jobs and dispositions.

        Shared by the polling transport and event-driven push ingest so both
        run identical job creation, claiming, policy, and send logic. A second
        copy of this loop would be a second answer to the same message.

        Held under a process-wide lock: the operator tick and the email ingest
        thread both reach this, and the store's namespace cache is not atomic
        across threads, so interleaved batch writes could lose a job.
        """
        with self._ingest_lock:
            return self._process_inbound_items_locked(incoming)

    def _process_inbound_items_locked(self, incoming):
        results = []
        replies = []
        skips = []
        for item in incoming:
            external_id = str(item.get("external_id", "") or item.get("id", ""))
            if external_id:
                existing = self.store.find_message_by_external_id(external_id)
                if existing is not None and message_answered(self.store, existing):
                    continue
            relationship_id = ""
            raw_sender = str(item.get("from", item.get("sender_email", "")) or "unknown")
            display = raw_sender
            body_text = str(item.get("body", item.get("text", "")))
            project_state = self.store.project_state()
            from .email_jobs import claim_job, ensure_job

            job = None
            if external_id:
                gmail_msgid = str(item.get("gmail_message_id", "") or "")
                rfc_id = str(item.get("rfc_message_id", "") or external_id)
                job = ensure_job(
                    self.store,
                    inbound_message_id=gmail_msgid or rfc_id,
                    rfc_message_id=rfc_id,
                    gmail_message_id=gmail_msgid,
                    gmail_thread_id=str(item.get("gmail_thread_id", "") or ""),
                    thread_id=str(item.get("thread_id", "")),
                    subject=str(item.get("subject", "")),
                    gmail_received_at=str(item.get("received_at", "") or ""),
                    sender=raw_sender,
                    received_at=datetime.now(timezone.utc).isoformat(),
                )
                granted, claim_reason = claim_job(self.store, job, self._worker_id())
                if not granted:
                    fresh = self.store.get_email_job(job.id)
                    if fresh is not None and fresh.is_terminal():
                        continue
                    skips.append({"reason": "job-claim-refused",
                                  "external_id": external_id, "detail": claim_reason})
                    continue
            result = self.monitor.ingest(
                self.store,
                sender_email=raw_sender,
                body=body_text,
                raw_body=str(item.get("raw_body", body_text)),
                subject=str(item.get("subject", "")),
                thread_id=str(item.get("thread_id", "")),
                external_id=external_id,
                in_reply_to=str(item.get("in_reply_to", "")),
                references=list(item.get("references") or []),
                need_rules=list(getattr(self.detector, "rules", []) or []),
                project_facts=list(project_state.facts) if project_state else [],
                principal_domains=list(self.store.controls().principal_domains or []),
                job=job,
                worker_id=self._worker_id(),
            )
            self._settle_email_job(job, result)
            results.append(result)
            if result.responded and result.relationship is not None:
                replies.append(result.relationship.id)
                self._presence_update(
                    "mark_meaningful_action",
                    f"Replied to inbound from '{result.relationship.display_name}'",
                )
            if result.relationship is not None:
                relationship_id = result.relationship.id
                # Name real trusted senders by their relationship, but always
                # surface the *actual* address when the message is automated,
                # quarantined (thread intrusion), or from any sender mismatch.
                if result.disposition in (InboundDisposition.TRUSTED_THREAD, InboundDisposition.APPROVED_SENDER) and (
                    _norm(raw_sender) == _norm(result.relationship.email or "")
                ):
                    display = result.relationship.display_name
            if result.disposition is not None:
                display = f"{display} [{result.disposition.value}]"
            self._provenance(
                ProvenancePhase.MONITOR if not result.escalated else ProvenancePhase.ESCALATE,
                f"inbound from '{display}': {result.treated_as}",
                action="monitor",
                result=result.reason,
                refs={"relationship_id": relationship_id, "message_id": result.message.id},
            )
        return results, replies, skips

    def _phase_monitor(self) -> tuple[list[InboundResult], list[str], list[dict[str, Any]]]:
        results: list[InboundResult] = []
        replies: list[str] = []
        skips: list[dict[str, Any]] = []
        if self._transport is None or not hasattr(self._transport, "fetch_inbox"):
            return results, replies, skips
        try:
            incoming, new_cursor = self._fetch_inbox()
        except Exception as exc:  # pragma: no cover - defensive
            # A fetch failure is a real ingest fault: record it as such so a
            # dashboard reader can tell "no new mail" from "poller broken",
            # and never advance the cursor past unread mail.
            skips.append({"reason": "inbox_fetch_failed", "error": str(exc)})
            self._record_ingest_health(ok=False, error=str(exc))
            return results, replies, skips
        self._record_ingest_health(ok=True, fetched=len(incoming))
        if new_cursor is not None:
            self.store.set_mailbox_cursor(new_cursor)

        self._reconcile_email_jobs()

        # Pre-record the whole fetch first so same-tick bursts are visible
        # to each other for batch answering. ingest() adopts these records
        # instead of duplicating them; answered ones are skipped below.
        for item in incoming:
            external_id = str(item.get("external_id", "") or item.get("id", ""))
            if not external_id:
                continue
            if self.store.find_message_by_external_id(external_id) is None:
                body_text = str(item.get("body", item.get("text", "")))
                self.monitor._record_inbound(
                    self.store,
                    relationship_id="",
                    sender_email=str(item.get("from", item.get("sender_email", "")) or "unknown"),
                    body=body_text,
                    subject=str(item.get("subject", "")),
                    thread_id=str(item.get("thread_id", "")),
                    external_id=external_id,
                    in_reply_to=str(item.get("in_reply_to", "")),
                    references=list(item.get("references") or []),
                    raw_body=str(item.get("raw_body", body_text)),
                )

        # Retry previously deferred mail FIRST so a message failed earlier
        # this same tick is not immediately re-driven (which previously
        # cleared its own retry marker and stranded it). Collected separately
        # because _process_inbound_items returns its own lists; appending here
        # and then rebinding `replies` would silently drop these.
        retry_replies: list[str] = []
        with self._ingest_lock:
            for outcome in self._retry_stale_deferred():
                if outcome.get("responded") and outcome.get("relationship_id"):
                    retry_replies.append(outcome["relationship_id"])
                    relationship = self.store.get_relationship(outcome["relationship_id"])
                    self._presence_update(
                        "mark_meaningful_action",
                        f"Replied to inbound from "
                        f"'{relationship.display_name if relationship else 'unknown'}' on retry",
                    )
        results, replies, skips = self._process_inbound_items(incoming)
        return results, retry_replies + replies, skips

    def _sent_search_fn(self):
        """Best-effort Gmail Sent lookup for crash reconciliation (or None)."""
        registry = self._capability_registry
        if registry is None:
            return None

        def _search(in_reply_to: str) -> Optional[str]:
            try:
                result = registry.call(
                    self.config.identity_id, "email.search_sent",
                    in_reply_to=in_reply_to,
                )
            except Exception:
                return None
            if not getattr(result, "success", False):
                return None
            found = ((result.data or {}).get("message_id", "") or "").strip()
            return found or None

        return _search

    def _reconcile_email_jobs(self) -> list[dict[str, Any]]:
        """Crash recovery before new work: adopt provable sends, reset the rest."""
        from .email_jobs import reconcile_job
        from .models import EmailJobStatus

        actions: list[dict[str, Any]] = []
        for job in self.store.list_email_jobs():
            if job.is_terminal():
                continue
            if job.status is EmailJobStatus.HELD and not (
                self.store.controls().outbound_mode == "autonomous"
                and job.failure_reason.startswith("observe mode:")
            ):
                # HELD is a policy decision, not a crashed worker. Releasing a
                # human-review hold on a timer would let maintenance override
                # authority. Observe-mode drafts are the only exception, and
                # only after the principal explicitly switches to autonomous.
                continue
            try:
                actions.append(reconcile_job(
                    self.store, job, self._run_id,
                    search_sent=self._sent_search_fn(),
                ))
            except Exception as exc:
                logger.warning("email job reconcile failed (%s): %s", job.id, exc)
        return actions

    def _settle_email_job(self, job: Any, result: Any) -> None:
        """Complete jobs for outcomes decided before respond_to_recorded runs.

        Anything reaching respond() owns its own transitions there; this
        covers only ingest early returns (ignored, opt-out, declined,
        quarantined, already-answered) so no claimed job strands. Deferred
        and failed outcomes carry their own job transitions from respond().
        """
        if job is None:
            return
        try:
            from .email_jobs import advance_job
            from .models import EmailJobStatus

            fresh = self.store.get_email_job(job.id)
            if fresh is None or fresh.is_terminal():
                return
            treated = getattr(result, "treated_as", "") or ""
            if treated in ("ignored", "opt_out", "declined", "quarantined", "answered"):
                advance_job(self.store, fresh, EmailJobStatus.COMPLETED)
        except Exception as exc:
            logger.warning("email job settle failed: %s", exc)

    def _fetch_inbox(self) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        """Fetch the inbox, advancing the durable high-water-mark cursor when the
        transport supports cursor-based delivery (so historical mail is never
        reprocessed after a restart or a first-run backfill)."""
        fetcher = getattr(self._transport, "fetch_inbox_with_cursor", None)
        if fetcher is None:
            return list(self._transport.fetch_inbox() or []), None
        cursor = self.store.mailbox_cursor()
        result = fetcher(cursor=cursor.to_dict() if cursor else None)
        messages = list(result.get("messages") or [])
        new_cursor = result.get("cursor")
        return messages, new_cursor

    def _phase_principal(self, now: datetime) -> list[dict[str, Any]]:
        """Process queued principal (Aster Control) messages as the operator.

        Every transition is written by actual execution: RECEIVED/QUEUED →
        PROCESSING → COMPLETED, or DEFERRED / PERMISSION_REQUIRED / FAILED.
        A substantive reply REQUIRES a working model runtime; without one the
        message is DEFERRED, never answered by a template masquerading as
        Aster. Existing permissions, budgets, and escalation rules remain
        authoritative — the phone UI cannot bypass them.
        """
        from .principal import (
            CONTROL_CHANNEL,
            CommandClass,
            build_principal_context,
            classify_command,
            pending_principal_messages,
            thread_messages,
        )

        outcomes: list[dict[str, Any]] = []
        # Cross-process visibility: the control server persists inbound
        # principal messages through its own store instance. Refresh before
        # scanning or this long-lived process would never see them.
        self.store.refresh_messages()
        self.store.refresh_relationships()
        # Control-plane actions land on needs (blocker force-retry); reload so
        # the long-lived operator's cache sees them.
        self.store.refresh_needs()
        pending = pending_principal_messages(self.store)
        if not pending:
            return outcomes
        self._presence_update(
            "set_status", PresenceStatus.THINKING,
            activity=f"Processing {len(pending)} principal message(s)",
        )
        for inbound in pending:
            condition=self.progress.condition('principal',self._adapter)
            if inbound.status is MessageStatus.DEFERRED and not self.progress.eligible('principal:'+inbound.id,condition,now=now.timestamp()):
                continue
            outcomes.append(self._process_principal_message(inbound, now))
        return outcomes

    def _process_principal_message(self, inbound: Message, now: datetime) -> dict[str, Any]:
        from .principal import (
            CONTROL_CHANNEL,
            CommandClass,
            build_principal_context,
            classify_command,
            thread_messages,
        )

        inbound.status = MessageStatus.PROCESSING
        self.store.update_message(inbound)
        command = classify_command(inbound.body or "")

        if command in (CommandClass.EXECUTE, CommandClass.COMMUNICATE):
            gate = self._principal_policy_gate(inbound, command)
            if gate is not None:
                return gate

        from core.self_knowledge import needs_grounding
        if self._adapter is None and not needs_grounding(inbound.body):
            return self._settle_principal(
                inbound, MessageStatus.DEFERRED,
                reason="no model runtime configured; substantive replies require a working model",
                notify=False,
            )
        response_text, generation = self._generate_principal_response(inbound, command)
        if response_text is None:
            return self._settle_principal(
                inbound, MessageStatus.DEFERRED,
                reason=generation.get("detail", "model unavailable; will retry on a later tick"),
                notify=False,
            )
        relationship = self.store.get_relationship(inbound.relationship_id)
        response = Message(
            relationship_id=inbound.relationship_id,
            direction=MessageDirection.OUTBOUND,
            channel=CONTROL_CHANNEL,
            subject="",
            body=response_text,
            sent_at=utcnow().isoformat(),
            thread_id=inbound.thread_id or "principal",
            in_reply_to=inbound.id,
            status=MessageStatus.SENT,
            authorization="principal:response",
            generation=generation,
        )
        self.store.append_message(response)
        if relationship is not None:
            relationship.message_ids.append(response.id)
            relationship.last_outbound_at = response.sent_at
            relationship.next_action = "awaiting principal message"
            self.store.update_relationship(relationship)
        self._provenance(
            ProvenancePhase.PRINCIPAL,
            "replied to principal via Aster Control",
            action="principal.respond",
            result=f"command={command.value} mode={generation.get('mode', '')}",
            refs={"message_id": inbound.id, "response_id": response.id,
                  "relationship_id": inbound.relationship_id},
        )
        self._presence_update(
            "mark_meaningful_action", "Replied to Arsène via Aster Control"
        )
        return self._settle_principal(
            inbound, MessageStatus.COMPLETED,
            reason=f"responded ({command.value})",
            response_id=response.id,
            notify=False,
        )

    def _principal_policy_gate(self, inbound: Message, command: CommandClass) -> Optional[dict[str, Any]]:
        """Enforce existing policy on COMMUNICATE/EXECUTE instructions.

        Returns None when the instruction may proceed to response generation
        (which for these classes includes executing the allowed action first);
        otherwise settles the message as PERMISSION_REQUIRED and returns the
        outcome. The phone UI never bypasses IdentityOS permissions.
        """
        from .principal import CommandClass

        controls = self.store.controls()
        decision = AuthorityPolicy(controls).evaluate(
            f"principal instruction ({command.value}): {(inbound.body or '')[:200]}",
            category="principal_instruction",
            content=inbound.body or "",
            mode="commitment",
        )
        if decision.requires_human:
            self._notify(
                kind="principal_instruction",
                summary=f"principal instruction requires authorization ({command.value})",
                refs={"message_id": inbound.id},
            )
            self._provenance(
                ProvenancePhase.PRINCIPAL,
                "principal instruction held for authorization",
                action="principal.gate",
                result=decision.reason,
                refs={"message_id": inbound.id, "command": command.value,
                      "notify": "principal:instruction"},
            )
            return self._settle_principal(
                inbound, MessageStatus.PERMISSION_REQUIRED,
                reason=decision.reason or "authorization required by policy",
                notify=False,
            )
        if command is CommandClass.EXECUTE:
            lowered = (inbound.body or "").strip().lower()
            if re.search(r"\bpause\b", lowered) and "operator" in lowered:
                self.pause(note="principal instruction via Aster Control")
                return None
            if re.search(r"\bresume\b", lowered) and "operator" in lowered:
                self.resume(note="principal instruction via Aster Control")
                return None
            # Outreach instructions name a recipient and ask for contact. The
            # executor below is the safe mechanism for that: policy already
            # gated this instruction, the voice gate still applies at the
            # transport, and every step records provenance.
            outcome = self._execute_principal_outreach(
                inbound, datetime.now(timezone.utc))
            if outcome is not None:
                return outcome
            return self._settle_principal(
                inbound, MessageStatus.DEFERRED,
                reason="policy allows autonomous action but no safe executor is wired "
                       "for this instruction; recorded for principal review",
                notify=False,
            )
        return None

    def _execute_principal_outreach(self, inbound: Message, now: datetime) -> Optional[dict[str, Any]]:
        """Execute a principal-directed outreach instruction end to end.

        Returns None when the instruction is not an outreach task (so the
        caller defers it). The recipient must be an explicit email address in
        the instruction text; the attachment must be an explicit filesystem
        path. Aster composes the message body herself from the instruction's
        substance — the principal's intent, never a canned template — and the
        send goes through the normal transport with the voice gate intact.
        """
        body_text = inbound.body or ""
        if self._transport is None:
            return None
        lowered = body_text.lower()
        if not re.search(
            r"\b(send|contact|email|invite|reach out|join\b|onboard\w*|hire\w*|ask\s+\w+ to)\b",
            lowered,
        ):
            return None
        # Recipient: an explicit email address in the instruction.
        addresses = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", body_text)
        recipient = next((a for a in addresses if not a.lower().endswith("@identityos")), None)
        if recipient is None:
            return None
        # Attachment: an explicit filesystem path in the instruction. A missing
        # file never blocks the send, and the compose prompt names only what
        # actually attaches so no claim is fabricated.
        paths = re.findall(r"(?:/[A-Za-z0-9._~/-]+)+\.[A-Za-z0-9]{2,4}", body_text)
        attachments = []
        for path in paths:
            from pathlib import Path

            p = Path(path)
            if not p.is_file():
                continue
            attachments.append({"path": str(p), "filename": p.name})
        named_paths = [a["path"] for a in attachments]

        # Never invent a recipient: one explicit address, one send.
        relationship = self.store.find_relationship_by_email(recipient)
        if relationship is None:
            from .models import Relationship

            display = recipient.split("@", 1)[0].replace(".", " ").title()
            relationship = Relationship(
                display_name=display,
                email=recipient,
                role="principal-directed contact",
                status=RelationshipStatus.NEW,
                notes=[f"Introduced at principal instruction ({inbound.id})."],
            )
            self.store.add_relationship(relationship)

        # Aster composes from the instruction's substance. The compose prompt
        # names the attachments that ACTUALLY exist — the model once claimed
        # "I've attached the contract" for a file that was skipped, which is
        # exactly the fabrication the voice rules exist to prevent.
        subject, outreach_body = self._compose_principal_directed(
            inbound, relationship,
            attachment_names=[a["filename"] for a in attachments],
        )
        if not outreach_body:
            return self._settle_principal(
                inbound, MessageStatus.DEFERRED,
                reason="reply generation unavailable; will retry on a later tick",
                notify=False,
            )

        result = self._send(
            to=recipient,
            subject=subject,
            body=outreach_body,
            attachments=attachments,
        )
        if not result.get("ok"):
            return self._settle_principal(
                inbound, MessageStatus.DEFERRED,
                reason=f"send failed: {result.get('error')}",
                notify=False,
            )
        # Record the send as real evidence.
        message = Message(
            relationship_id=relationship.id,
            direction=MessageDirection.OUTBOUND,
            channel="email",
            subject=subject,
            body=outreach_body,
            sent_at=utcnow().isoformat(),
            status=MessageStatus.SENT,
            authorization=f"principal_directed:{inbound.id}",
            external_id=str(result.get("external_id", "")),
            generation={"to": recipient, "attachments": [a["filename"] for a in attachments]},
        )
        self.store.append_message(message)
        relationship.status = RelationshipStatus.OUTREACH_SENT
        relationship.first_contacted_at = message.sent_at
        relationship.last_outbound_at = message.sent_at
        if message.thread_id:
            relationship.thread_ids.append(message.thread_id)
        relationship.next_action = "await reply; follow up if quiet"
        self.store.update_relationship(relationship)
        self._provenance(
            ProvenancePhase.PRINCIPAL,
            f"executed principal-directed outreach to {recipient}",
            action="principal.execute_outreach",
            result=result.get("external_id", ""),
            refs={"message_id": inbound.id, "outbound_id": message.id,
                  "recipient": recipient,
                  "attachments": [a["filename"] for a in attachments]},
        )
        return self._settle_principal(
            inbound, MessageStatus.COMPLETED,
            reason=f"sent to {recipient} with {len(attachments)} attachment(s)",
            notify=False,
        )

    def _compose_principal_directed(
        self, inbound: Message, relationship: Any,
        attachment_names: Optional[list[str]] = None,
    ) -> tuple[str, str]:
        """Aster drafts the outreach from the instruction's substance.

        The model writes the email; the voice invariant is enforced downstream.
        The prompt names the attachments actually being sent so the model can
        never claim an attachment that is not there. Returns (subject, body);
        body empty means generation unavailable.
        """
        from .principal import thread_messages

        repo_hint = ""
        for token in re.findall(r"https?://\S+", inbound.body or ""):
            if "github.com" in token:
                repo_hint = token
                break
        instruction_summary = (inbound.body or "")[:1200]
        names = [n for n in (attachment_names or []) if n]
        attachment_fact = (
            f"Attachments actually being sent: {', '.join(names)}." if names
            else "NO attachments are being sent with this email. Do not mention "
                 "or claim any attachment."
        )
        context = (
            "You are Aster, a persistent AI identity operating through IdentityOS, "
            "acting under delegated authority for your principal. You are composing "
            "an outreach email the principal explicitly directed. Be warm, direct, "
            "and specific. NEVER use em dashes. Never claim something happened unless "
            "it did — including attachments. Disclose that you are an AI identity on "
            "first contact."
        )
        user_input = (
            f"The principal's instruction: {instruction_summary}\n\n"
            f"Recipient: {relationship.display_name} <{relationship.email}>\n"
            + (f"Repository to share: {repo_hint}\n" if repo_hint else "")
            + f"\n{attachment_fact}\n"
            + "\nWrite the email body. Sign it as Aster."
        )
        try:
            raw = self._adapter.generate(context, user_input, self._identity) if self._adapter else None
        except Exception:
            raw = None
        if not raw:
            return "", ""
        text = str(raw).strip()
        subject = ""
        body = text
        if text.lower().startswith("subject:"):
            lines = text.split("\n", 1)
            subject = lines[0][len("subject:"):].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
        return subject or "A personal invitation from IdentityOS", body

    def _generate_principal_response(
        self, inbound: Message, command: CommandClass
    ) -> tuple[Optional[str], dict[str, Any]]:
        from .principal import build_principal_context, thread_messages

        presence_summary = ""
        if self._presence is not None:
            try:
                view = self._presence.public_view()
                presence_summary = (
                    f"activity={view.get('status')}: {view.get('activity')}; "
                    f"last heartbeat {view.get('heartbeat_age_seconds')}s ago; "
                    f"last meaningful action: {view.get('last_meaningful_action')}; "
                    f"commons={view.get('commons_standing')}; "
                    f"opportunities={view.get('opportunity_count')}"
                )
            except Exception:
                presence_summary = ""
        history = thread_messages(self.store, limit=10)
        try:
            from .principal_knowledge import load_profile, principal_context_lines

            principal_lines = principal_context_lines(
                load_profile(self.storage, self.config.identity_id))
        except Exception:
            principal_lines = []
        context, user_input = build_principal_context(
            identity_name=self.config.sender_name or "Aster",
            objective=self.config.purpose,
            history=history,
            command=command,
            presence_summary=presence_summary,
            principal_lines=principal_lines,
        )
        from core.self_knowledge import SelfKnowledge, needs_grounding, grounding_context, guard_response
        reader = SelfKnowledge(self.storage, self.config.identity_id,
                               scrub=self._secret_store.scrub if self._secret_store else None)
        snapshot = reader.snapshot() if needs_grounding(inbound.body) else None
        if snapshot is not None:
            context += grounding_context(snapshot)
        from .voice import repair_outbound, style_constraint_prompt, validate_outbound

        def _attempt(extra_constraint: str = "") -> tuple[str, dict[str, Any], bool]:
            """One generation attempt. Returns (text, metadata, snapshot_used)."""
            try:
                from adapters.contracts import options
                self.progress.resources["model_calls"] = self.progress.resources.get("model_calls",0)+1
                attempt_text = self._adapter.generate(
                    context, user_input + extra_constraint, self._identity,
                    **(options(self._adapter, snapshot) if snapshot else {}))
            except Exception as exc:
                if snapshot is None:
                    return "", {"mode": "unavailable",
                                "detail": f"model call failed: {type(exc).__name__}"}, False
                fallback_text, grounding = guard_response("", snapshot, current=reader.snapshot())
                return fallback_text, {"mode": "runtime_grounded_fallback", "adapter": "", "model": "",
                              "grounding": grounding, "failure": type(exc).__name__,
                              "attempted": [{"provider": a.get("provider"), "error": "provider unavailable"}
                                            for a in (getattr(self._adapter, "last_selection", None) or {}).get("attempted", [])]}, True
            attempt_text = (attempt_text or "").strip()
            if not attempt_text and snapshot is None:
                return "", {"mode": "unavailable", "detail": "model returned an empty response"}, False
            attempt_metadata = self._generation_metadata()
            if snapshot is not None:
                attempt_text, grounding = guard_response(attempt_text, snapshot, current=reader.snapshot())
                attempt_metadata['grounding'] = grounding
                if grounding['guard'] == 'fallback':
                    attempt_metadata['mode'] = 'runtime_grounded_fallback'
            return attempt_text, attempt_metadata, snapshot is not None

        text, metadata, _ = _attempt()
        if text and not validate_outbound(text).ok:
            repaired, clean = repair_outbound(text)
            if clean:
                return repaired, metadata
            # Bounded single regeneration with the identity constraint made
            # explicit. If still dirty, defer rather than ship U+2014.
            retry_text, retry_metadata, _ = _attempt(
                "\n\nReminder of a hard identity rule for this reply: "
                + style_constraint_prompt())
            if retry_text and validate_outbound(retry_text).ok:
                return retry_text, retry_metadata
            repaired_retry, retry_clean = repair_outbound(retry_text)
            if retry_text and retry_clean:
                return repaired_retry, retry_metadata
            return None, {"mode": "unavailable",
                          "detail": "response violated the no-em-dash invariant after repair and one regeneration"}
        if not text:
            return None, metadata
        return text, metadata

    def _generation_metadata(self) -> dict[str, Any]:
        # Attribute the provider that ACTUALLY generated. A ChainAdapter
        # records its winning leaf in last_selection; naively naming the
        # first chain entry misattributes fall-through responses.
        selection = getattr(self._adapter, "last_selection", None) or {}
        if selection.get("provider"):
            return {
                "mode": "identity_model_generation",
                "adapter": selection.get("provider", ""),
                "model": selection.get("model", ""),
                "latency_ms": selection.get("latency_ms"),
            }
        try:
            from adapters.configuration import describe_adapter

            described = describe_adapter(self._adapter)
            providers = described.get("providers") or []
            first = providers[0] if providers else {}
            return {
                "mode": "identity_model_generation",
                "adapter": first.get("adapter", type(self._adapter).__name__),
                "model": first.get("model", str(getattr(self._adapter, "model", "") or "")),
            }
        except Exception:
            return {
                "mode": "identity_model_generation",
                "adapter": type(self._adapter).__name__,
                "model": str(getattr(self._adapter, "model", "") or ""),
            }

    def _settle_principal(
        self,
        inbound: Message,
        status: MessageStatus,
        *,
        reason: str = "",
        response_id: str = "",
        notify: bool = False,
    ) -> dict[str, Any]:
        from .principal import CommandClass, classify_command

        command = classify_command(inbound.body or "")
        inbound.status = status
        if status is MessageStatus.DEFERRED:
            condition=self.progress.condition('principal',self._adapter)
            executor_missing = 'no safe executor' in reason or 'no model runtime' in reason
            retry = None if executor_missing else self.progress.now + 900
            self.progress.wait('principal:'+inbound.id,'WAITING_FOR_EXECUTOR' if executor_missing else 'PROVIDER_UNAVAILABLE',
                condition,'Principal instruction waiting for executable prerequisites',retry=retry,
                reason='Capabilities, services, model configuration or principal controls change')
        elif status is MessageStatus.COMPLETED:
            self.progress.resolve('principal:'+inbound.id,'Principal message completed with persisted response')
        if response_id:
            inbound.evidence = list(inbound.evidence or []) + [f"response:{response_id}"]
        if reason and status is not MessageStatus.COMPLETED:
            inbound.evidence = list(inbound.evidence or []) + [reason[:300]]
        self.store.update_message(inbound)
        if notify:
            self._notify(
                kind="principal_instruction",
                summary=f"principal message {status.value}: {reason[:140]}",
                refs={"message_id": inbound.id},
            )
        self._provenance(
            ProvenancePhase.PRINCIPAL,
            f"principal message {status.value}",
            action="principal.settle",
            result=reason[:300],
            refs={"message_id": inbound.id, "command": command.value,
                  "response_id": response_id},
        )
        return {
            "message_id": inbound.id,
            "outcome": "completed" if status is MessageStatus.COMPLETED else status.value,
            "command": command.value,
            "response_id": response_id,
            "reason": reason,
        }

    def _phase_follow_ups(self, now: datetime) -> tuple[list[str], list[dict[str, Any]]]:
        sent: list[str] = []
        skips: list[dict[str, Any]] = []
        controls = self.store.controls()
        for follow_up in self.follow_ups.plan(now):
            self._provenance(
                ProvenancePhase.FOLLOW_UP,
                "scheduled follow-up",
                action="schedule_follow_up",
                result=follow_up.reason,
                refs={"follow_up_id": follow_up.id, "relationship_id": follow_up.relationship_id},
            )

        for follow_up in self.follow_ups.due(now):
            relationship = self.store.get_relationship(follow_up.relationship_id)
            if relationship is None or relationship.opted_out or relationship.status in (
                RelationshipStatus.DECLINED, RelationshipStatus.OPTED_OUT
            ):
                self.follow_ups.cancel(relationship, "relationship closed") if relationship else None
                continue
            try:
                angle = self.follow_ups.analyze_quiet(
                    relationship, adapter=self._adapter, identity=self._identity,
                    project_name=self.config.project_name, objective=self.config.purpose,
                )
                if angle and not any(n.startswith("re-engagement angle:") and angle in n
                                     for n in (relationship.notes or [])):
                    relationship.notes = (list(relationship.notes or []) + [
                        f"re-engagement angle: {angle}"])[-10:]
                    self.store.update_relationship(relationship)
            except Exception as exc:
                logger.warning("quiet-relationship analysis failed: %s", exc)
            subject, body = self.composer.compose_follow_up(
                relationship, adapter=self._adapter, identity=self._identity
            )
            auth = AuthorityPolicy(controls).evaluate(
                "send follow-up", category=relationship.purpose, content=f"{subject}\n{body}", mode="commitment"
            )
            if auth.requires_human:
                skips.append({"follow_up_id": follow_up.id, "reason": "requires_authorization"})
                continue
            thread = relationship.thread_ids[-1] if relationship.thread_ids else ""

            if controls.outbound_mode == "approval_required":
                if self._has_would_send(relationship_id=follow_up.relationship_id, kind="follow_up_approval"):
                    skips.append({"follow_up_id": follow_up.id, "reason": "already_awaiting_authorization"})
                    continue
                message = Message(
                    relationship_id=relationship.id,
                    direction=MessageDirection.OUTBOUND,
                    subject=subject,
                    body=body,
                    status=MessageStatus.AWAITING_AUTHORIZATION,
                    authorization="awaiting_human_authorization",
                    thread_id=thread,
                )
                self.store.append_message(message)
                relationship.message_ids.append(message.id)
                relationship.status = RelationshipStatus.AWAITING_AUTHORIZATION
                relationship.next_action = "human authorization required"
                self.store.update_relationship(relationship)
                skips.append({"follow_up_id": follow_up.id, "reason": "outbound_mode_requires_approval"})
                self._notify(kind="escalation", summary="follow-up requires human authorization",
                             refs={"message_id": message.id, "follow_up_id": follow_up.id, "relationship_id": relationship.id})
                self._provenance(
                    ProvenancePhase.ESCALATE,
                    "follow-up requires human authorization",
                    action="escalate",
                    result="outbound_mode requires human approval",
                    refs={"message_id": message.id, "follow_up_id": follow_up.id, "relationship_id": relationship.id, "notify": "principal:escalation"},
                )
                continue

            if controls.outbound_mode == "observe":
                if self._has_would_send(relationship_id=follow_up.relationship_id, kind="follow_up"):
                    skips.append({"follow_up_id": follow_up.id, "reason": "would_send_already_recorded"})
                    continue
                message = Message(
                    relationship_id=relationship.id,
                    direction=MessageDirection.OUTBOUND,
                    subject=subject,
                    body=body,
                    status=MessageStatus.WOULD_SEND,
                    authorization="observe_would_send:follow_up",
                    thread_id=thread,
                )
                self.store.append_message(message)
                relationship.message_ids.append(message.id)
                self.store.update_relationship(relationship)
                self._provenance(
                    ProvenancePhase.FOLLOW_UP,
                    "follow-up drafted in observation mode (not sent)",
                    action="would_send",
                    result="mode=observe",
                    refs={"message_id": message.id, "follow_up_id": follow_up.id, "relationship_id": relationship.id},
                )
                continue

            if self._transport is None:
                skips.append({"follow_up_id": follow_up.id, "reason": "dry_run"})
                continue
            if self.store.budget().follow_ups >= controls.max_follow_ups_per_target * max(controls.max_cold_outreach_per_day, 1):
                skips.append({"follow_up_id": follow_up.id, "reason": "daily_budget_exhausted"})
                self._presence_update(
                    "set_status",
                    PresenceStatus.WAITING,
                    activity="Follow-ups paused: daily budget exhausted",
                )
                break
            result = self._send(
                to=relationship.email,
                subject=subject,
                body=body,
                thread_id=thread,
            )
            if not result.get("ok"):
                skips.append({"follow_up_id": follow_up.id, "reason": result.get("error", "send_failed")})
                continue
            message = Message(
                relationship_id=relationship.id,
                direction=MessageDirection.OUTBOUND,
                subject=subject,
                body=body,
                sent_at=utcnow().isoformat(),
                status=MessageStatus.SENT,
                authorization="autonomous_follow_up",
                external_id=str(result.get("external_id", "")),
                thread_id=str(result.get("thread_id", "")),
            )
            self.store.append_message(message)
            relationship.message_ids.append(message.id)
            relationship.last_outbound_at = message.sent_at
            self.store.update_relationship(relationship)
            self.follow_ups.complete(follow_up, now=now)
            self.store.record_usage("follow_ups")
            sent.append(message.id)
            self._presence_update(
                "mark_meaningful_action",
                f"Sent follow-up to '{relationship.display_name}'",
            )
            self._provenance(
                ProvenancePhase.FOLLOW_UP,
                f"sent follow-up to '{relationship.display_name}'",
                action="send_follow_up",
                result=message.external_id or "sent",
                refs={"message_id": message.id, "relationship_id": relationship.id},
            )
        return sent, skips

    # ── authorization queue ───────────────────────────────────────────

    def pending_authorizations(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for message in self.store.list_messages():
            if message.status is MessageStatus.AWAITING_AUTHORIZATION:
                relationship = self.store.get_relationship(message.relationship_id)
                pending.append({
                    "message_id": message.id,
                    "subject": message.subject,
                    "body": message.body,
                    "opportunity_id": message.opportunity_id,
                    "need_id": message.need_id,
                    "recipient": relationship.email if relationship else "",
                    "relationship_id": relationship.id if relationship else "",
                })
        return pending

    def authorize(self, message_id: str, *, approved: bool = True, note: str = "", approver: str = "principal") -> dict[str, Any]:
        message = self.store.get_message(message_id)
        if message is None:
            return {"ok": False, "error": f"unknown message: {message_id}"}
        if message.status is not MessageStatus.AWAITING_AUTHORIZATION:
            return {"ok": False, "error": f"message is not awaiting authorization: {message.status.value}"}

        relationship = self.store.get_relationship(message.relationship_id)
        if not approved:
            message.status = MessageStatus.FAILED
            message.authorization = f"human_rejected:{note}" if note else "human_rejected"
            self.store.update_message(message)
            if relationship is not None:
                relationship.status = RelationshipStatus.NEW
                relationship.next_action = "human rejected outreach"
                self.store.update_relationship(relationship)
            self._notify(kind="authorization", summary="authorization request rejected by principal",
                         refs={"message_id": message.id})
            self._provenance(
                ProvenancePhase.AUTHORIZE,
                "human rejected outreach",
                action="authorize",
                result=f"{note or 'rejected'} (approver={approver})",
                refs={"message_id": message.id, "decision": "reject", "approver": approver,
                      "scope": {"relationship_id": message.relationship_id}, "authorization": message.authorization},
            )
            return {"ok": True, "status": "rejected", "message_id": message.id}

        if relationship is None:
            return {"ok": False, "error": "message has no relationship"}
        if self._transport is None:
            return {"ok": False, "error": "no transport configured; cannot send"}

        result = self._send(
            to=relationship.email,
            subject=message.subject,
            body=message.body,
            thread_id=message.thread_id or (relationship.thread_ids[-1] if relationship.thread_ids else ""),
            in_reply_to=message.in_reply_to,
            references=message.references,
        )
        if not result.get("ok"):
            message.status = MessageStatus.FAILED
            message.evidence = [f"send_error:{result.get('error')}"]
            self.store.update_message(message)
            return {"ok": False, "error": result.get("error", "send failed")}

        message.status = MessageStatus.SENT
        message.sent_at = utcnow().isoformat()
        message.external_id = str(result.get("external_id", ""))
        message.thread_id = str(result.get("thread_id", ""))
        message.authorization = f"human_authorized:{note}:{message_id}" if note else f"human_authorized:{message_id}"
        self.store.update_message(message)
        relationship.status = RelationshipStatus.OUTREACH_SENT
        relationship.first_contacted_at = message.sent_at
        relationship.last_outbound_at = message.sent_at
        if message.thread_id:
            relationship.thread_ids.append(message.thread_id)
        relationship.next_action = "await reply; follow up if quiet"
        self.store.update_relationship(relationship)
        self.store.record_usage("cold_outreach")
        self._notify(kind="authorization", summary="authorization request approved; message sent",
                     refs={"message_id": message.id, "external_id": message.external_id})
        self._provenance(
            ProvenancePhase.AUTHORIZE,
            "human authorized outreach; sent",
            action="authorize",
            result=f"{note or 'approved'} (approver={approver})",
            refs={"message_id": message.id, "relationship_id": relationship.id, "decision": "approve",
                  "approver": approver,
                  "scope": {"relationship_id": relationship.id, "opportunity_id": message.opportunity_id,
                            "need_id": message.need_id, "outbound_mode": self.store.controls().outbound_mode},
                  "authorization": message.authorization},
        )
        return {"ok": True, "status": "sent", "message_id": message.id, "external_id": message.external_id}

    # ── inspection ────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        state = self.store.project_state()
        needs = self.store.list_needs()
        opportunities = self.store.list_opportunities()
        relationships = self.store.list_relationships()
        messages = self.store.list_messages()
        return {
            "identity_id": self.config.identity_id,
            "mode": self.mode,
            "project": state.to_dict() if state else None,
            "needs": {
                "total": len(needs),
                "open": sum(1 for n in needs if n.status.value == "open"),
                "items": [n.to_dict() for n in needs],
            },
            "opportunities": {
                "total": len(opportunities),
                "by_status": _count_by(o.status.value for o in opportunities),
                "items": [o.to_dict() for o in opportunities],
            },
            "relationships": {
                "total": len(relationships),
                "by_status": _count_by(r.status.value for r in relationships),
                "items": [r.to_dict() for r in relationships],
            },
            "messages": {
                "total": len(messages),
                "outbound": sum(1 for m in messages if m.direction.value == "outbound"),
                "inbound": sum(1 for m in messages if m.direction.value == "inbound"),
            },
            "pending_authorizations": len(self.pending_authorizations()),
            "notifications": {"unread": self.store.unread_notification_count()},
            "would_send": sum(1 for m in messages if m.status is MessageStatus.WOULD_SEND),
            "budget": self.store.budget().to_dict(),
            "controls": self.store.controls().to_dict(),
            "provenance_count": len(self.store.list_provenance()),
        }

    def provenance(self, limit: int = 50) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.store.list_provenance(limit=limit)]

    def would_send(self, limit: int = 50) -> list[dict[str, Any]]:
        """Composed messages recorded in observation mode but never transmitted."""
        items = [m.to_dict() for m in self.store.list_messages() if m.status is MessageStatus.WOULD_SEND]
        return items[-limit:]

    # ── internals ─────────────────────────────────────────────────────

    def _allowlist_allows(self, email: str) -> bool:
        """Gate cold outreach against the recipient allowlist (exact or @domain)."""
        controls = self.store.controls()
        allow = [entry for entry in controls.allowed_external_recipients if entry and str(entry).strip()]
        if not allow:
            return True
        target = _norm(email)
        if not target:
            return False
        for entry in allow:
            pattern = _norm(entry)
            if not pattern:
                continue
            if pattern == target:
                return True
            if pattern.startswith("@") and target.endswith(pattern):
                return True
        return False

    def _has_would_send(self, *, opportunity_id: str = "", relationship_id: str = "", kind: str = "") -> bool:
        """Already-recorded observation draft for this target / relationship."""
        for message in self.store.list_messages():
            if message.status is not MessageStatus.WOULD_SEND:
                continue
            if opportunity_id and message.opportunity_id == opportunity_id:
                return True
            if relationship_id and message.relationship_id == relationship_id:
                if not kind or kind in message.authorization:
                    return True
        return False

    def _sender_email(self) -> str:
        return self.config.sender_email or ""

    def _sender_display_name(self) -> str:
        voice = self._communication_identity
        if voice is not None:
            try:
                return voice.display_sender()
            except Exception:
                pass
        return self.config.sender_name or ""

    def _signature_for(self, *, first_contact: bool, formal: bool = False) -> str:
        voice = self._communication_identity
        if voice is not None:
            try:
                return voice.signature_for(first_contact=first_contact, formal=formal)
            except Exception:
                pass
        return self.config.signature

    def _send(self, *, to: str, subject: str, body: str, thread_id: str = "",
              in_reply_to: str = "", references=None, sender: str = "",
              sender_display_name: str = "", reply_to: str = "",
              html_body: str = "", attachments: Optional[list[dict]] = None,
              job: Any = None) -> dict[str, Any]:
        if self._transport is None:
            return {"ok": False, "error": "no transport configured"}
        # Final voice gate: the transport artifact must contain zero U+2014.
        # Composer-level handling repairs safe cases; anything still dirty
        # fails closed here rather than shipping a violation.
        try:
            from .voice import assert_clean

            assert_clean(subject, body, context="email.send")
        except Exception as exc:
            return {"ok": False, "error": f"style_violation: {exc}"}
        if job is not None:
            from .email_jobs import stamp_job

            stamp_job(self.store, job, "send_started_at")
        try:
            result = self._transport.send(
                to=to, subject=subject, body=body, thread_id=thread_id,
                in_reply_to=in_reply_to, references=list(references or []),
                sender=sender or self._sender_email(),
                sender_display_name=sender_display_name or self._sender_display_name(),
                reply_to=reply_to, html_body=html_body,
                attachments=list(attachments or []),
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if job is not None:
            stamp_job(self.store, job, "smtp_accepted_at")
        if isinstance(result, dict):
            result.setdefault("ok", True)
            return result
        return {"ok": True, "external_id": str(result)}

    def _already_processed(self, external_id: str) -> bool:
        if not external_id:
            return False
        return any(m.external_id == external_id for m in self.store.list_messages())

    def _provenance(
        self,
        phase: ProvenancePhase,
        summary: str,
        *,
        action: str = "",
        result: str = "",
        evidence: Optional[list[str]] = None,
        refs: Optional[dict[str, Any]] = None,
    ) -> ProvenanceEntry:
        scrub = self._secret_scrub if self._secret_store is not None else None
        if scrub is not None:
            summary = scrub(summary)
            result = scrub(result)
            action = scrub(action)
            evidence = [scrub(e) for e in (evidence or [])]
            if refs:
                refs = {
                    k: scrub(v) if isinstance(v, str)
                    else [scrub(i) for i in v] if isinstance(v, list)
                    else v
                    for k, v in refs.items()
                }
        return self.store.append_provenance(
            ProvenanceEntry(
                phase=phase,
                summary=summary,
                action=action,
                result=result,
                evidence=list(evidence or []),
                refs=dict(refs or {}),
            )
        )

    def _secret_scrub(self, text: str) -> str:
        if self._secret_store is None:
            return text
        try:
            return self._secret_store.scrub(text)
        except Exception:  # pragma: no cover - defensive scrub never blocks provenance
            return text

    def _notify(
        self, *, kind: str = "escalation", summary: str, refs: Optional[dict[str, Any]] = None
    ) -> None:
        """Record a principal notification (durable ledger; future push hook)."""
        self.store.append_notification(
            NotificationEntry(kind=kind, summary=summary, refs=dict(refs or {}))
        )


def _count_by(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts
