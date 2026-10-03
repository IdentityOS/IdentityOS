"""
core/operations/models.py

Data models for the Operations subsystem.

The Operations subsystem lets a persistent identity run a *constrained,
evidence-backed operator loop*: observe a project, detect real needs, discover
opportunities that could address those needs, evaluate them, perform permitted
outreach, monitor the resulting conversations, follow up, and escalate anything
consequential to a human.

These models are deliberately domain-agnostic.  "Opportunity" could be an
investor, a grant, a collaborator, a researcher, a compute provider, or a
vendor.  Aster is simply one configured operator built on these primitives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────


class NeedStatus(str, Enum):
    OPEN = "open"
    ADDRESSING = "addressing"
    ADDRESSED = "addressed"
    DISMISSED = "dismissed"


class OpportunityStatus(str, Enum):
    DISCOVERED = "discovered"
    EVALUATING = "evaluating"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    CLOSED = "closed"


class RelationshipStatus(str, Enum):
    NEW = "new"
    OUTREACH_SENT = "outreach_sent"
    AWAITING_AUTHORIZATION = "awaiting_human_authorization"
    ENGAGED = "engaged"
    DORMANT = "dormant"
    DECLINED = "declined"
    OPTED_OUT = "opted_out"


class MessageDirection(str, Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_AUTHORIZATION = "awaiting_human_authorization"
    SENT = "sent"
    RECEIVED = "received"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # A fully-composed outbound message that would have been transmitted but was
    # not, because the operator is in observation mode. Kept so a human can
    # review exactly what would have been sent before switching to autonomous.
    WOULD_SEND = "would_send"
    # Principal-messaging lifecycle (Aster Control): an inbound principal
    # message moves RECEIVED -> QUEUED -> PROCESSING -> COMPLETED, or lands in
    # DEFERRED / PERMISSION_REQUIRED / FAILED. Transitions are written only by
    # actual execution, never manufactured.
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    DEFERRED = "deferred"
    PERMISSION_REQUIRED = "permission_required"


class FollowUpStatus(str, Enum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"


class EmailJobStatus(str, Enum):
    """Durable lifecycle of one inbound email response job.

    DISCOVERED: seen in a poll, not yet owned by any worker.
    CLAIMED: one worker (run id + pid) owns generation; others must not touch it.
    GENERATING: model call in flight.
    READY_TO_SEND: validated response persisted, awaiting transport.
    SENT: transport accepted and outbound id recorded. A restart finding a
      job here adopts the existing outbound record instead of resending.
    COMPLETED: bookkeeping finished AND a response was actually sent or the
      message was deliberately not owed one (ignored / opt-out / decline /
      already answered). Never used for a message that is still owed a reply.
    HELD: a real reply is owed but outbound policy requires human approval.
      NOT terminal and NOT success: it stays visible, keeps the inbound
      unanswered, and is retried once the escalation clears.
    FAILED: terminal or retryable failure with reason and attempt count.
    """

    DISCOVERED = "discovered"
    CLAIMED = "claimed"
    GENERATING = "generating"
    READY_TO_SEND = "ready_to_send"
    SENT = "sent"
    COMPLETED = "completed"
    HELD = "held"
    FAILED = "failed"


#: Job states after which a worker must never touch the record again.
TERMINAL_JOB_STATUSES = frozenset({EmailJobStatus.COMPLETED.value, EmailJobStatus.FAILED.value})

#: Job states in which Aster is *still owed* a response to an inbound message.
#: Anything not in this set either answered, deliberately declined, or was
#: never answerable. Used to decide conversation state and to report "unanswered
#: mail" — the signal that must never be inferred from timestamps.
AWAITING_RESPONSE_STATUSES = frozenset({
    EmailJobStatus.DISCOVERED.value,
    EmailJobStatus.CLAIMED.value,
    EmailJobStatus.GENERATING.value,
    EmailJobStatus.READY_TO_SEND.value,
    # HELD is included deliberately: the reply is owed, it is just waiting on
    # human approval. That is a state a human needs to see, not a success.
    EmailJobStatus.HELD.value,
})

#: Pipeline stages an :class:`EmailJob` may timestamp. Used to validate
#: :meth:`EmailJob.mark` so a typo can never create a bogus field.
_EMAIL_JOB_STAGE_FIELDS = frozenset({
    "gmail_received_at", "discovered_at", "created_at", "claimed_at",
    "generation_started_at", "generation_finished_at", "send_started_at",
    "smtp_accepted_at", "completed_at", "updated_at",
})


class ProvenancePhase(str, Enum):
    OBSERVE = "observe"
    DETECT_NEEDS = "detect_needs"
    DISCOVER = "discover"
    EVALUATE = "evaluate"
    PLAN = "plan"
    AUTHORIZE = "authorize"
    ACT = "act"
    MONITOR = "monitor"
    FOLLOW_UP = "follow_up"
    ESCALATE = "escalate"
    CONTROL = "control"
    PRINCIPAL = "principal"


# ─────────────────────────────────────────────────────────────────────────────
# Core records
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Need:
    """A real, evidence-backed gap in the project the operator works for."""

    id: str = field(default_factory=lambda: new_id("need"))
    category: str = ""
    description: str = ""
    evidence: list[str] = field(default_factory=list)
    urgency: float = 0.5          # 0..1
    impact: float = 0.5           # 0..1
    status: NeedStatus = NeedStatus.OPEN
    discovered_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> float:
        return round((self.urgency * 0.5) + (self.impact * 0.5), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
            "evidence": list(self.evidence),
            "urgency": self.urgency,
            "impact": self.impact,
            "status": self.status.value,
            "discovered_at": self.discovered_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Need":
        return cls(
            id=data.get("id") or new_id("need"),
            category=data.get("category", ""),
            description=data.get("description", ""),
            evidence=list(data.get("evidence", [])),
            urgency=float(data.get("urgency", 0.5)),
            impact=float(data.get("impact", 0.5)),
            status=NeedStatus(data.get("status", "open")),
            discovered_at=data.get("discovered_at", utcnow().isoformat()),
            updated_at=data.get("updated_at", utcnow().isoformat()),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Opportunity:
    """A candidate target that could address a Need."""

    id: str = field(default_factory=lambda: new_id("opp"))
    need_id: str = ""
    target_name: str = ""
    organization: str = ""
    contact_email: str = ""
    contact_url: str = ""
    channel: str = "email"
    category: str = ""
    relevant_work: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    fit_reason: str = ""
    value_proposition: str = ""
    potential_ask: str = ""
    confidence: float = 0.0
    # True when a human explicitly designated this candidate for a live/offline
    # test, so a zero-confidence candidate may be pursued through the normal
    # pipeline under a manual override instead of being auto-rejected.
    test_candidate: bool = False
    risks: list[str] = field(default_factory=list)
    status: OpportunityStatus = OpportunityStatus.DISCOVERED
    discovered_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "need_id": self.need_id,
            "target_name": self.target_name,
            "organization": self.organization,
            "contact_email": self.contact_email,
            "contact_url": self.contact_url,
            "channel": self.channel,
            "category": self.category,
            "relevant_work": list(self.relevant_work),
            "evidence": list(self.evidence),
            "fit_reason": self.fit_reason,
            "value_proposition": self.value_proposition,
            "potential_ask": self.potential_ask,
            "confidence": self.confidence,
            "test_candidate": self.test_candidate,
            "risks": list(self.risks),
            "status": self.status.value,
            "discovered_at": self.discovered_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Opportunity":
        return cls(
            id=data.get("id") or new_id("opp"),
            need_id=data.get("need_id", ""),
            target_name=data.get("target_name", ""),
            organization=data.get("organization", ""),
            contact_email=data.get("contact_email", ""),
            contact_url=data.get("contact_url", ""),
            channel=data.get("channel", "email"),
            category=data.get("category", ""),
            relevant_work=list(data.get("relevant_work", [])),
            evidence=list(data.get("evidence", [])),
            fit_reason=data.get("fit_reason", ""),
            value_proposition=data.get("value_proposition", ""),
            potential_ask=data.get("potential_ask", ""),
            confidence=float(data.get("confidence", 0.0)),
            test_candidate=bool(data.get("test_candidate", False)),
            risks=list(data.get("risks", [])),
            status=OpportunityStatus(data.get("status", "discovered")),
            discovered_at=data.get("discovered_at", utcnow().isoformat()),
            updated_at=data.get("updated_at", utcnow().isoformat()),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Relationship:
    """A persistent relationship with a person or organization."""

    id: str = field(default_factory=lambda: new_id("rel"))
    display_name: str = ""
    organization: str = ""
    email: str = ""
    role: str = ""
    purpose: str = ""
    need_id: str = ""
    opportunity_id: str = ""
    status: RelationshipStatus = RelationshipStatus.NEW
    first_contacted_at: Optional[str] = None
    last_inbound_at: Optional[str] = None
    last_outbound_at: Optional[str] = None
    message_ids: list[str] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_action: str = ""
    follow_up_due_at: Optional[str] = None
    follow_up_count: int = 0
    opted_out: bool = False
    notes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    def touch(self) -> None:
        self.updated_at = utcnow().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "organization": self.organization,
            "email": self.email,
            "role": self.role,
            "purpose": self.purpose,
            "need_id": self.need_id,
            "opportunity_id": self.opportunity_id,
            "status": self.status.value,
            "first_contacted_at": self.first_contacted_at,
            "last_inbound_at": self.last_inbound_at,
            "last_outbound_at": self.last_outbound_at,
            "message_ids": list(self.message_ids),
            "thread_ids": list(self.thread_ids),
            "commitments": list(self.commitments),
            "open_questions": list(self.open_questions),
            "next_action": self.next_action,
            "follow_up_due_at": self.follow_up_due_at,
            "follow_up_count": self.follow_up_count,
            "opted_out": self.opted_out,
            "notes": list(self.notes),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Relationship":
        return cls(
            id=data.get("id") or new_id("rel"),
            display_name=data.get("display_name", ""),
            organization=data.get("organization", ""),
            email=data.get("email", ""),
            role=data.get("role", ""),
            purpose=data.get("purpose", ""),
            need_id=data.get("need_id", ""),
            opportunity_id=data.get("opportunity_id", ""),
            status=RelationshipStatus(data.get("status", "new")),
            first_contacted_at=data.get("first_contacted_at"),
            last_inbound_at=data.get("last_inbound_at"),
            last_outbound_at=data.get("last_outbound_at"),
            message_ids=list(data.get("message_ids", [])),
            thread_ids=list(data.get("thread_ids", [])),
            commitments=list(data.get("commitments", [])),
            open_questions=list(data.get("open_questions", [])),
            next_action=data.get("next_action", ""),
            follow_up_due_at=data.get("follow_up_due_at"),
            follow_up_count=int(data.get("follow_up_count", 0)),
            opted_out=bool(data.get("opted_out", False)),
            notes=list(data.get("notes", [])),
            created_at=data.get("created_at", utcnow().isoformat()),
            updated_at=data.get("updated_at", utcnow().isoformat()),
        )


@dataclass
class Message:
    """A single outbound or inbound message tied to a relationship."""

    id: str = field(default_factory=lambda: new_id("msg"))
    relationship_id: str = ""
    direction: MessageDirection = MessageDirection.OUTBOUND
    channel: str = "email"
    subject: str = ""
    body: str = ""
    # Full decoded text (pre quote-stripping) plus its SHA-256, kept so the
    # audit trail can prove what was actually received even though
    # ``body`` holds only the new contribution used for classification.
    raw_body: str = ""
    body_sha256: str = ""
    sent_at: Optional[str] = None
    received_at: Optional[str] = None
    external_id: str = ""
    thread_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    status: MessageStatus = MessageStatus.DRAFT
    authorization: str = ""
    evidence: list[str] = field(default_factory=list)
    need_id: str = ""
    opportunity_id: str = ""
    # For outbound messages: how the content was produced and from what.
    # mode distinguishes ``identity_model_generation`` from ``template_fallback``
    # so a canned reply is never presented as model-backed.
    generation: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "relationship_id": self.relationship_id,
            "direction": self.direction.value,
            "channel": self.channel,
            "subject": self.subject,
            "body": self.body,
            "raw_body": self.raw_body,
            "body_sha256": self.body_sha256,
            "sent_at": self.sent_at,
            "received_at": self.received_at,
            "external_id": self.external_id,
            "thread_id": self.thread_id,
            "in_reply_to": self.in_reply_to,
            "references": list(self.references),
            "status": self.status.value,
            "authorization": self.authorization,
            "evidence": list(self.evidence),
            "need_id": self.need_id,
            "opportunity_id": self.opportunity_id,
            "generation": dict(self.generation),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=data.get("id") or new_id("msg"),
            relationship_id=data.get("relationship_id", ""),
            direction=MessageDirection(data.get("direction", "outbound")),
            channel=data.get("channel", "email"),
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            raw_body=data.get("raw_body", ""),
            body_sha256=data.get("body_sha256", ""),
            sent_at=data.get("sent_at"),
            received_at=data.get("received_at"),
            external_id=data.get("external_id", ""),
            thread_id=data.get("thread_id", ""),
            in_reply_to=data.get("in_reply_to", ""),
            references=list(data.get("references", [])),
            status=MessageStatus(data.get("status", "draft")),
            authorization=data.get("authorization", ""),
            evidence=list(data.get("evidence", [])),
            need_id=data.get("need_id", ""),
            opportunity_id=data.get("opportunity_id", ""),
            generation=dict(data.get("generation", {})),
            created_at=data.get("created_at", utcnow().isoformat()),
        )


@dataclass
class FollowUp:
    id: str = field(default_factory=lambda: new_id("fu"))
    relationship_id: str = ""
    due_at: str = field(default_factory=lambda: utcnow().isoformat())
    reason: str = ""
    status: FollowUpStatus = FollowUpStatus.SCHEDULED
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    completed_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "relationship_id": self.relationship_id,
            "due_at": self.due_at,
            "reason": self.reason,
            "status": self.status.value,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FollowUp":
        return cls(
            id=data.get("id") or new_id("fu"),
            relationship_id=data.get("relationship_id", ""),
            due_at=data.get("due_at", utcnow().isoformat()),
            reason=data.get("reason", ""),
            status=FollowUpStatus(data.get("status", "scheduled")),
            created_at=data.get("created_at", utcnow().isoformat()),
            completed_at=data.get("completed_at"),
        )


@dataclass
class EmailJob:
    """Durable record for exactly one inbound email response job.

    The idempotency key is ``inbound_message_id`` — Gmail's immutable
    message id when the provider exposes one, else the RFC ``Message-ID`` —
    never subject, sender, timestamp, or thread alone. One inbound message
    owns at most one normal outbound response; retries adopt existing sends
    instead of duplicating them.

    Every pipeline stage is timestamped so a slow response can be attributed
    to a real cause instead of guessed at. ``latency_breakdown()`` splits the
    total into delivery / discovery / queue / model / send segments, where
    ``gmail_received_at`` is Gmail's server-side ``internalDate`` when the API
    exposes it, with the sender's ``Date`` header only as a fallback (never the
    moment we happened to poll for it).
    """

    id: str = field(default_factory=lambda: new_id("job"))
    inbound_message_id: str = ""
    rfc_message_id: str = ""
    gmail_message_id: str = ""
    gmail_thread_id: str = ""
    thread_id: str = ""
    sender: str = ""
    subject: str = ""
    received_at: str = ""
    gmail_received_at: str = ""
    discovered_at: str = ""
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    status: EmailJobStatus = EmailJobStatus.DISCOVERED
    claimed_at: Optional[str] = None
    claimed_by: str = ""
    generation_started_at: str = ""
    generation_finished_at: str = ""
    send_started_at: str = ""
    smtp_accepted_at: str = ""
    completed_at: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    context_version: str = ""
    provider: str = ""
    model: str = ""
    generated_response_hash: str = ""
    outbound_message_id: str = ""
    failure_reason: str = ""
    retryable: bool = True
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "inbound_message_id": self.inbound_message_id,
            "rfc_message_id": self.rfc_message_id,
            "gmail_message_id": self.gmail_message_id,
            "gmail_thread_id": self.gmail_thread_id,
            "thread_id": self.thread_id,
            "sender": self.sender,
            "subject": self.subject,
            "received_at": self.received_at,
            "gmail_received_at": self.gmail_received_at,
            "discovered_at": self.discovered_at,
            "created_at": self.created_at,
            "status": self.status.value,
            "claimed_at": self.claimed_at,
            "claimed_by": self.claimed_by,
            "generation_started_at": self.generation_started_at,
            "generation_finished_at": self.generation_finished_at,
            "send_started_at": self.send_started_at,
            "smtp_accepted_at": self.smtp_accepted_at,
            "completed_at": self.completed_at,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "context_version": self.context_version,
            "provider": self.provider,
            "model": self.model,
            "generated_response_hash": self.generated_response_hash,
            "outbound_message_id": self.outbound_message_id,
            "failure_reason": self.failure_reason,
            "retryable": self.retryable,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmailJob":
        return cls(
            id=data.get("id") or new_id("job"),
            inbound_message_id=data.get("inbound_message_id", ""),
            rfc_message_id=data.get("rfc_message_id", ""),
            gmail_message_id=data.get("gmail_message_id", ""),
            gmail_thread_id=data.get("gmail_thread_id", ""),
            thread_id=data.get("thread_id", ""),
            sender=data.get("sender", ""),
            subject=data.get("subject", ""),
            received_at=data.get("received_at", ""),
            gmail_received_at=data.get("gmail_received_at", ""),
            discovered_at=data.get("discovered_at", ""),
            # Jobs written before created_at existed carry no creation stamp.
            # Backfill from the oldest timestamp the record actually holds.
            # Using utcnow() here would stamp every load with the time of the
            # *read*, and because the store persists what it loads, that would
            # permanently rewrite history and make delivery latency nonsense.
            created_at=(data.get("created_at")
                        or data.get("discovered_at")
                        or data.get("claimed_at")
                        or data.get("received_at")
                        or data.get("completed_at")
                        or data.get("updated_at")
                        or ""),
            status=EmailJobStatus(data.get("status", "discovered")),
            claimed_at=data.get("claimed_at"),
            claimed_by=data.get("claimed_by", ""),
            generation_started_at=data.get("generation_started_at", ""),
            generation_finished_at=data.get("generation_finished_at", ""),
            send_started_at=data.get("send_started_at", ""),
            smtp_accepted_at=data.get("smtp_accepted_at", ""),
            completed_at=data.get("completed_at"),
            attempt_count=int(data.get("attempt_count", 0) or 0),
            max_attempts=int(data.get("max_attempts", 3) or 3),
            context_version=data.get("context_version", ""),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            generated_response_hash=data.get("generated_response_hash", ""),
            outbound_message_id=data.get("outbound_message_id", ""),
            failure_reason=data.get("failure_reason", ""),
            retryable=bool(data.get("retryable", True)),
            updated_at=data.get("updated_at", utcnow().isoformat()),
        )

    def mark(self, field_name: str, at: Optional[str] = None) -> None:
        """Stamp one pipeline stage, ignoring unknown/absent timestamps."""
        if not field_name or field_name not in _EMAIL_JOB_STAGE_FIELDS:
            return
        if not at:
            at = utcnow().isoformat()
        setattr(self, field_name, at)
        self.updated_at = at

    def latency_breakdown(self) -> dict[str, Any]:
        """Segment the total response time by real cause.

        Each key is seconds and is ``None`` when the segment cannot be
        measured from the record, so a partial chain is never reported as a
        complete one.
        """
        def _t(value: Optional[str]) -> Optional[float]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value).timestamp()
            except (TypeError, ValueError):
                return None

        arrived = _t(self.gmail_received_at) or _t(self.received_at)
        discovered = _t(self.discovered_at) or _t(self.created_at)
        claimed = _t(self.claimed_at)
        gen_start = _t(self.generation_started_at)
        gen_end = _t(self.generation_finished_at)
        send_start = _t(self.send_started_at)
        accepted = _t(self.smtp_accepted_at)
        done = _t(self.completed_at) or accepted

        def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
            if a is None or b is None or b < a:
                return None
            return round(b - a, 3)

        total = _delta(arrived, done)
        return {
            "total_seconds": total,
            # Time the message sat in the mailbox before we knew about it.
            "delivery_seconds": _delta(arrived, discovered),
            # Time between first seeing it and a worker owning it.
            "queue_seconds": _delta(discovered, claimed or discovered),
            # Actual model time, claim to response text.
            "model_seconds": _delta(claimed or gen_start, gen_end),
            # Persist-ready body to transport acceptance.
            "send_seconds": _delta(gen_end or send_start, accepted),
            # Pre-send handling (classify, compose, record).
            "handling_seconds": _delta(claimed, gen_start),
            "stages_present": sum(1 for v in (arrived, discovered, claimed,
                                             gen_start, gen_end, send_start,
                                             accepted, done) if v is not None),
        }

    def diagnostics(self) -> dict[str, Any]:
        """Secret-free per-message diagnostic view for the dashboard."""
        latency = self.latency_breakdown()
        return {
            "job_id": self.id,
            "status": self.status.value,
            "inbound_message_id": self.inbound_message_id,
            "gmail_message_id": self.gmail_message_id,
            "rfc_message_id": self.rfc_message_id,
            "gmail_thread_id": self.gmail_thread_id,
            "thread_id": self.thread_id,
            "sender": self.sender,
            "subject": self.subject,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "claimed_by": self.claimed_by,
            "retryable": self.retryable,
            "provider": self.provider,
            "model": self.model,
            "failure_reason": self.failure_reason,
            "outbound_message_id": self.outbound_message_id,
            "context_version": self.context_version,
            "timestamps": {
                "gmail_received_at": self.gmail_received_at or None,
                "discovered_at": self.discovered_at or None,
                "claimed_at": self.claimed_at,
                "generation_started_at": self.generation_started_at or None,
                "generation_finished_at": self.generation_finished_at or None,
                "send_started_at": self.send_started_at or None,
                "smtp_accepted_at": self.smtp_accepted_at or None,
                "completed_at": self.completed_at,
            },
            "latency": latency,
        }

    def is_terminal(self) -> bool:
        """True when no worker may touch this job again."""
        if self.status is EmailJobStatus.COMPLETED:
            return True
        if self.status is EmailJobStatus.FAILED and not self.retryable:
            return True
        if self.status is EmailJobStatus.FAILED and self.attempt_count >= self.max_attempts:
            return True
        return False


@dataclass
class ProvenanceEntry:
    """
    An append-only record of a single operator decision/action.

    This is the audit ledger that makes autonomy inspectable and prevents
    fabricated success: every action references the evidence that justified it
    and the observed result.
    """

    id: str = field(default_factory=lambda: new_id("prov"))
    at: str = field(default_factory=lambda: utcnow().isoformat())
    phase: ProvenancePhase = ProvenancePhase.OBSERVE
    summary: str = ""
    action: str = ""
    result: str = ""
    evidence: list[str] = field(default_factory=list)
    refs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "phase": self.phase.value,
            "summary": self.summary,
            "action": self.action,
            "result": self.result,
            "evidence": list(self.evidence),
            "refs": dict(self.refs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProvenanceEntry":
        return cls(
            id=data.get("id") or new_id("prov"),
            at=data.get("at", utcnow().isoformat()),
            phase=ProvenancePhase(data.get("phase", "observe")),
            summary=data.get("summary", ""),
            action=data.get("action", ""),
            result=data.get("result", ""),
            evidence=list(data.get("evidence", [])),
            refs=dict(data.get("refs", {})),
        )


@dataclass
class NotificationEntry:
    """A pending/read notification for the principal.

    The ledger is the repository-side notification mechanism: it records every
    event the principal should be aware of (currently: anything that was raised
    for human approval), is durable across restarts, and may be delivered via a
    future push hook. ``kind`` names the event class and ``refs`` tie the
    notification to the audit trail (e.g. the escalated message id).
    """

    id: str = field(default_factory=lambda: new_id("ntf"))
    at: str = field(default_factory=lambda: utcnow().isoformat())
    kind: str = "escalation"
    summary: str = ""
    refs: dict[str, Any] = field(default_factory=dict)
    read: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "kind": self.kind,
            "summary": self.summary,
            "refs": dict(self.refs),
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NotificationEntry":
        return cls(
            id=data.get("id") or new_id("ntf"),
            at=data.get("at", utcnow().isoformat()),
            kind=data.get("kind", "escalation"),
            summary=data.get("summary", ""),
            refs=dict(data.get("refs", {})),
            read=bool(data.get("read", False)),
        )


@dataclass
class ControlState:
    """Human overrides and operating constraints for an operator identity."""

    # Operating mode for OUTBOUND communication:
    #   "observe"            -> nothing is ever sent; WOULD_SEND drafts are recorded
    #   "autonomous"         -> permitted low-risk communication is sent directly
    #   "approval_required"  -> every outbound message awaits human approval first
    outbound_mode: str = "observe"
    # When non-empty, Aster may only initiate COLD outreach to recipients that
    # match this allowlist (exact address or an "@domain" suffix). Replies and
    # follow-ups to established relationships are not bound by the allowlist.
    allowed_external_recipients: list[str] = field(default_factory=list)
    # Principal work domains for inbound relevance: free-text terms Arsène
    # maintains for work outside the declared need rules (matched
    # case-insensitively against unsolicited mail before Aster engages).
    principal_domains: list[str] = field(default_factory=list)
    paused: bool = False
    never_contact: list[str] = field(default_factory=list)
    require_approval_categories: list[str] = field(default_factory=list)
    max_cold_outreach_per_day: int = 3
    max_follow_ups_per_target: int = 2
    max_research_calls_per_day: int = 25
    follow_up_after_hours: float = 72.0
    # Control keys the principal has locked: override() refuses to change them.
    # Locking is how a deliberate setting ("3/day, always") survives future
    # overrides instead of being silently relaxed by a later command.
    locked_keys: list[str] = field(default_factory=list)
    notes: str = ""
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbound_mode": self.outbound_mode,
            "allowed_external_recipients": list(self.allowed_external_recipients),
            "principal_domains": list(self.principal_domains),
            "paused": self.paused,
            "never_contact": list(self.never_contact),
            "require_approval_categories": list(self.require_approval_categories),
            "max_cold_outreach_per_day": self.max_cold_outreach_per_day,
            "max_follow_ups_per_target": self.max_follow_ups_per_target,
            "max_research_calls_per_day": self.max_research_calls_per_day,
            "follow_up_after_hours": self.follow_up_after_hours,
            "locked_keys": list(self.locked_keys),
            "notes": self.notes,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlState":
        return cls(
            outbound_mode=normalize_outbound_mode(data.get("outbound_mode", "observe")),
            allowed_external_recipients=list(data.get("allowed_external_recipients", [])),
            principal_domains=list(data.get("principal_domains", [])),
            paused=bool(data.get("paused", False)),
            never_contact=list(data.get("never_contact", [])),
            require_approval_categories=list(data.get("require_approval_categories", [])),
            max_cold_outreach_per_day=int(data.get("max_cold_outreach_per_day", 3)),
            max_follow_ups_per_target=int(data.get("max_follow_ups_per_target", 2)),
            max_research_calls_per_day=int(data.get("max_research_calls_per_day", 25)),
            follow_up_after_hours=float(data.get("follow_up_after_hours", 72.0)),
            locked_keys=list(data.get("locked_keys", [])),
            notes=data.get("notes", ""),
            updated_at=data.get("updated_at", utcnow().isoformat()),
        )


OUTBOUND_MODES = ("observe", "autonomous", "approval_required")


def normalize_outbound_mode(mode: str) -> str:
    """Return a valid outbound mode, defaulting to the conservative 'observe'."""
    candidate = str(mode or "").strip().lower()
    if candidate in OUTBOUND_MODES:
        return candidate
    return "observe"


@dataclass
class BudgetState:
    """Per-day usage counters enforcing rate limits."""

    day: str = ""
    cold_outreach: int = 0
    follow_ups: int = 0
    replies: int = 0
    research_calls: int = 0
    # Interop-surface usage (Culture Commons and friends).
    cc_reads: int = 0
    cc_posts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "cold_outreach": self.cold_outreach,
            "follow_ups": self.follow_ups,
            "replies": self.replies,
            "research_calls": self.research_calls,
            "cc_reads": self.cc_reads,
            "cc_posts": self.cc_posts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetState":
        return cls(
            day=data.get("day", ""),
            cold_outreach=int(data.get("cold_outreach", 0)),
            follow_ups=int(data.get("follow_ups", 0)),
            replies=int(data.get("replies", 0)),
            research_calls=int(data.get("research_calls", 0)),
            cc_reads=int(data.get("cc_reads", 0)),
            cc_posts=int(data.get("cc_posts", 0)),
        )


@dataclass
class ProjectFact:
    """A single verified statement about the observed project.

    Every fact carries its own provenance so a reply can cite *where* the
    statement came from (source type, source path, observation timestamp and the
    git commit the observation reflected).  A ``statement`` without a source is
    not a runtime fact — it is a claim.
    """

    id: str = field(default_factory=lambda: new_id("fact"))
    statement: str = ""
    source_type: str = ""        # e.g. "readme" | "docs" | "manifest" | "git" | "tests" | "identity"
    source_path: str = ""        # relative path that evidences the statement ("" for git/derived)
    observed_at: str = field(default_factory=lambda: utcnow().isoformat())
    commit_sha: str = ""         # git commit the observation reflects, when available

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "source_type": self.source_type,
            "source_path": self.source_path,
            "observed_at": self.observed_at,
            "commit_sha": self.commit_sha,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectFact":
        return cls(
            id=data.get("id") or new_id("fact"),
            statement=data.get("statement", ""),
            source_type=data.get("source_type", ""),
            source_path=data.get("source_path", ""),
            observed_at=data.get("observed_at", utcnow().isoformat()),
            commit_sha=data.get("commit_sha", ""),
        )


@dataclass
class ProjectState:
    """A snapshot of what the operator currently understands about its project."""

    project_id: str = ""
    name: str = ""
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    observed_at: str = field(default_factory=lambda: utcnow().isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    # Per-fact provenance. ``facts`` remains the concise statement list for
    # compatibility; ``fact_details`` carries the source of each statement.
    fact_details: list[ProjectFact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "summary": self.summary,
            "facts": list(self.facts),
            "evidence": list(self.evidence),
            "observed_at": self.observed_at,
            "metadata": dict(self.metadata),
            "fact_details": [f.to_dict() for f in self.fact_details],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectState":
        return cls(
            project_id=data.get("project_id", ""),
            name=data.get("name", ""),
            summary=data.get("summary", ""),
            facts=list(data.get("facts", [])),
            evidence=list(data.get("evidence", [])),
            observed_at=data.get("observed_at", utcnow().isoformat()),
            metadata=dict(data.get("metadata", {})),
            fact_details=[ProjectFact.from_dict(f) for f in data.get("fact_details", [])],
        )


@dataclass
class MailboxCursor:
    """High-water mark for inbound mailbox processing.

    ``uid_validity`` guards against a provider re-folding the mailbox: if it
    changes, the cursor is reseeded at the new UIDNEXT so historical messages are
    never re-ingested. ``last_uid`` is the highest UID already processed.
    """

    uid_validity: str = ""
    last_uid: int = 0
    seeded: bool = False
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid_validity": self.uid_validity,
            "last_uid": self.last_uid,
            "seeded": self.seeded,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MailboxCursor":
        return cls(
            uid_validity=str(data.get("uid_validity", "")),
            last_uid=int(data.get("last_uid", 0)),
            seeded=bool(data.get("seeded", False)),
            updated_at=data.get("updated_at", utcnow().isoformat()),
        )


@dataclass
class Evaluation:
    """Evidence-backed assessment of whether an opportunity is worth pursuing."""

    opportunity_id: str = ""
    factors: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    rationale: str = ""
    recommendation: str = "reject"   # pursue | hold | reject
    confidence: float = 0.0
    created_at: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def score(self) -> float:
        if not self.factors:
            return 0.0
        return round(sum(self.factors.values()) / len(self.factors), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "factors": dict(self.factors),
            "evidence": list(self.evidence),
            "rationale": self.rationale,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "score": self.score,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evaluation":
        return cls(
            opportunity_id=data.get("opportunity_id", ""),
            factors=dict(data.get("factors", {})),
            evidence=list(data.get("evidence", [])),
            rationale=data.get("rationale", ""),
            recommendation=data.get("recommendation", "reject"),
            confidence=float(data.get("confidence", 0.0)),
            created_at=data.get("created_at", utcnow().isoformat()),
        )
