"""
core/operations/engine.py

The Operations engine — a durable, evidence-recording operator loop.

Each :meth:`OperationsEngine.tick` runs a sequence of phases against persisted
state:

    observe → capability-gap check → detect needs → discover opportunities
    → evaluate → act (outreach, within budget) → monitor replies → follow up

Every phase appends to an append-only provenance ledger.  Because all state is
loaded from storage at construction, a brand-new process with the same identity
resumes exactly where the previous one stopped: no duplicate outreach, no lost
conversations, no in-memory-only progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .capability_gap import CapabilityGap, CapabilityGapDetector, CapabilityStatus
from .composition import OutreachBrief, OutreachComposer
from .config import OperatorConfig
from .discovery import OpportunityDiscoverer
from .evaluation import DuplicateContactPolicy, TargetEvaluator
from .followups import FollowUpPlanner
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
from .monitor import ConversationMonitor, InboundDisposition, InboundResult
from .needs import NeedDetector
from .observer import ProjectStateObserver
from .policy import AuthorityPolicy
from .store import OperationsStore, _norm


@dataclass
class TickReport:
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
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
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
        self.gap_detector = CapabilityGapDetector(
            capability_registry=capability_registry,
            identity_id=config.identity_id,
            acquisition=acquisition,
            store=self.store,
        )

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
        """Update control constraints (never-contact, budgets, approval categories)."""
        controls = self.store.controls()
        for key, value in changes.items():
            if not hasattr(controls, key):
                raise ValueError(f"Unknown control field: {key}")
            setattr(controls, key, value)
        self.store.set_controls(controls)
        self._provenance(
            ProvenancePhase.CONTROL,
            "human override applied",
            action="override",
            result=", ".join(f"{k}={v}" for k, v in changes.items()),
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

        # Fresh state from storage before any phase: a long-running process's
        # in-memory cache goes stale as other writers persist newer state.
        self.store.refresh()

        if self.store.controls().paused:
            self._provenance(ProvenancePhase.CONTROL, "tick skipped: operator paused", action="tick")
            report.skipped.append({"reason": "paused"})
            return report

        if surfaces:
            report.observed = self._phase_surfaces(report) or report.observed
        if observe:
            report.observed = self._phase_observe()
        if detect_needs:
            self._phase_gaps(report)
            report.needs_created = [n.id for n in self.detector.detect(self.store, self.store.project_state())] if self.store.project_state() else []
            if report.needs_created:
                self._provenance(
                    ProvenancePhase.DETECT_NEEDS,
                    f"detected {len(report.needs_created)} new need(s)",
                    action="detect_needs",
                    result=", ".join(report.needs_created),
                )
        if discover:
            report.opportunities_created = self._phase_discover(report)
        if evaluate:
            report.evaluated, report.qualified = self._phase_evaluate()
        if act:
            report.outreach_sent, report.escalations, act_skips = self._phase_act(report)
            report.skipped.extend(act_skips)
        if monitor:
            _, report.replies_sent, monitor_skips = self._phase_monitor()
            report.skipped.extend(monitor_skips)
        if follow_ups:
            report.follow_ups_sent, follow_skips = self._phase_follow_ups(now)
            report.skipped.extend(follow_skips)

        return report

    # ── phases ────────────────────────────────────────────────────────

    def _phase_observe(self) -> bool:
        state = self.observer.observe()
        self.store.set_project_state(state)
        self._provenance(
            ProvenancePhase.OBSERVE,
            f"observed project '{state.name}' with {len(state.facts)} fact(s)",
            action="observe_project",
            result=state.summary[:200],
            evidence=state.evidence[:8],
            refs={"project_id": state.project_id},
        )
        return True

    def _phase_surfaces(self, report: TickReport) -> bool:
        observed_any = False
        for surface in self._surfaces:
            try:
                observed = bool((surface.observe() or {}).get("observed"))
            except Exception as exc:
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
        gaps = self.gap_detector.check(self.config.required_skills)
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
            self._notify_gap(gap)

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
        open_needs = self.store.list_needs(status=NeedStatus.OPEN)[: self.config.max_needs_per_tick]
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
                if self._is_actionable(opportunity):
                    opportunity.status = OpportunityStatus.QUALIFIED
                    qualified.append(opportunity.id)
                else:
                    # Pursue-worthy but not yet actionable: only a general
                    # directory/homepage exists. Keep researching; never
                    # present it as a qualified opportunity.
                    opportunity.status = OpportunityStatus.RESEARCH_LEAD
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

    @staticmethod
    def _is_actionable(opportunity) -> bool:
        """Whether an opportunity is actionable, not just a research lead.

        Actionable requires a contact route (a specific person/agent to engage)
        or a specific program record with evidence beyond a generic directory.
        """
        if opportunity.contact_email:
            return True
        # A specific program page: evidence must name something more concrete
        # than a directory/homepage listing.
        relevant = [w for w in (opportunity.relevant_work or []) if w]
        specific_evidence = [
            e for e in (opportunity.evidence or [])
            if e.startswith(("result:", "url:", "search:")) and "directory" not in e.lower()
        ]
        return bool(relevant and specific_evidence and (getattr(opportunity, "metadata", None) or {}).get("source_urls"))

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
                skips.append({"opportunity_id": opportunity.id, "reason": "no_contact_email"})
                self._provenance(
                    ProvenancePhase.PLAN,
                    "no contact email discovered; cannot send individualized outreach",
                    action="outreach_blocked",
                    result="no_contact_email",
                    refs={"opportunity_id": opportunity.id},
                )
                continue

            if budget.cold_outreach >= controls.max_cold_outreach_per_day:
                skips.append({"opportunity_id": opportunity.id, "reason": "daily_budget_exhausted"})
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
                signature=self.config.signature,
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
                opportunity.relationship_id = store_rel.id
                self.store.update_opportunity(opportunity)
                message.relationship_id = store_rel.id
                self.store.append_message(message)
                escalations.append(message.id)
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
                self._provenance(
                    ProvenancePhase.CONTROL,
                    "daily cold-outreach budget exhausted",
                    action="budget",
                    result=f"{budget.cold_outreach}/{controls.max_cold_outreach_per_day}",
                )
                break

            send_result = self._send(
                to=opportunity.contact_email,
                subject=subject,
                body=body,
            )
            if not send_result.get("ok"):
                message.status = MessageStatus.FAILED
                message.evidence = [f"send_error:{send_result.get('error')}"]
                store_rel = self.store.add_relationship(relationship)
                message.relationship_id = store_rel.id
                self.store.append_message(message)
                report.errors.append({"message_id": message.id, "error": send_result.get("error")})
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
            opportunity.relationship_id = store_rel.id
            self.store.update_opportunity(opportunity)
            self.store.record_usage("cold_outreach")
            budget = self.store.budget()
            sent.append(message.id)
            self._provenance(
                ProvenancePhase.ACT,
                f"sent individualized outreach to '{relationship.display_name}'",
                action="send",
                result=message.external_id or "sent",
                evidence=message.evidence[:5],
                refs={"message_id": message.id, "opportunity_id": opportunity.id, "relationship_id": store_rel.id},
            )
        return sent, escalations, skips

    def _phase_monitor(self) -> tuple[list[InboundResult], list[str], list[dict[str, Any]]]:
        results: list[InboundResult] = []
        replies: list[str] = []
        skips: list[dict[str, Any]] = []
        if self._transport is None or not hasattr(self._transport, "fetch_inbox"):
            return results, replies, skips
        try:
            incoming, new_cursor = self._fetch_inbox()
        except Exception as exc:  # pragma: no cover - defensive
            skips.append({"reason": "inbox_fetch_failed", "error": str(exc)})
            return results, replies, skips
        if new_cursor is not None:
            self.store.set_mailbox_cursor(new_cursor)

        for item in incoming:
            external_id = str(item.get("external_id", "") or item.get("id", ""))
            if external_id and self._already_processed(external_id):
                continue
            relationship_id = ""
            raw_sender = str(item.get("from", item.get("sender_email", "")) or "unknown")
            display = raw_sender
            body_text = str(item.get("body", item.get("text", "")))
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
            )
            results.append(result)
            if result.responded and result.relationship is not None:
                replies.append(result.relationship.id)
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
        from .ambassador import ambassador_status

        return {
            "identity_id": self.config.identity_id,
            "mode": self.mode,
            "ambassador": ambassador_status(self),
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

    def _send(self, *, to: str, subject: str, body: str, thread_id: str = "",
              in_reply_to: str = "", references=None) -> dict[str, Any]:
        if self._transport is None:
            return {"ok": False, "error": "no transport configured"}
        try:
            result = self._transport.send(
                to=to, subject=subject, body=body, thread_id=thread_id,
                in_reply_to=in_reply_to, references=list(references or []),
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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
        if self._secret_store is not None:
            summary = self._scrub(summary)
            result = self._scrub(result)
            action = self._scrub(action)
            evidence = [self._scrub(e) for e in (evidence or [])]
            if refs:
                refs = {
                    k: self._scrub(v) if isinstance(v, str)
                    else [self._scrub(i) for i in v] if isinstance(v, list)
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

    def _scrub(self, text: str) -> str:
        if self._secret_store is None:
            return text
        try:
            return self._secret_store.scrub(text)
        except Exception:
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
