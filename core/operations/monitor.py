"""
core/operations/monitor.py

Conversation monitoring and reply handling.

The monitor ingests inbound messages, classifies their *disposition*, associates
them with a persistent relationship when the sender is within trusted scope, and
decides whether the operator may respond autonomously.

Disposition is the primary inbound boundary.  Only messages in a trusted thread
or from an approved (already-known) sender may be answered autonomously.
Automated, bounce, self-copy, spam/bulk, and unsolicited-unknown messages are
never answered — they are recorded so the audit trail stays complete.
Anything that touches a consequential commitment is escalated and the
relationship is marked ``AWAITING_HUMAN_AUTHORIZATION`` rather than answered.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .composition import OutreachComposer
from .models import (
    EmailJobStatus,
    Message,
    MessageDirection,
    MessageStatus,
    NotificationEntry,
    ProvenanceEntry,
    ProvenancePhase,
    Relationship,
    RelationshipStatus,
    normalize_outbound_mode,
    utcnow,
)
from .policy import AuthorityPolicy, is_conversational, policy_text_excluding_quoted
from .store import OperationsStore, _norm

_OPT_OUT_PATTERNS = (
    r"\bunsubscribe\b", r"\bopt[ -]?out\b", r"\bstop (emailing|contacting)\b",
    r"\bremove me\b", r"\bdo not contact\b", r"\bdon'?t contact\b",
)

_DECLINE_PATTERNS = (
    r"\bnot interested\b", r"\bno thank you\b", r"\bno thanks\b",
    r"\bnot a fit\b", r"\bpass on this\b", r"\bdecline\b",
)

_INTENT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("scheduling", (r"\bschedul", r"\bmeet(ing)?\b", r"\bcalendar\b", r"\btime (next|this) week\b", r"\bcall\b")),
    ("documentation_request", (r"\bdocs?\b", r"\bdocumentation\b", r"\bwhitepaper\b", r"\bdeck\b", r"\bmore (info|information|details)\b", r"\bsend .*(link|material)\b")),
    ("intro_request", (r"\bintro(duction)?\b", r"\bconnect me\b", r"\bwho else\b", r"\brefer\b")),
    ("interest", (r"\binterested\b", r"\blove to\b", r"\bwould like to\b", r"\bhappy to\b", r"\bexcited\b", r"\btell me more\b")),
    ("decline", _DECLINE_PATTERNS),
    ("thanks", (r"\bthank(s| you)\b", r"\bappreciate\b", r"\bgreat,? thanks\b")),
    ("question", (r"\?", r"\bhow\b", r"\bwhat\b", r"\bwhen\b", r"\bwhere\b", r"\bwhy\b", r"\bcan you\b", r"\bcould you\b")),
]


class InboundDisposition(str, Enum):
    """Why an inbound message deserves (or does not deserve) a response.

    Only ``TRUSTED_THREAD`` and ``APPROVED_SENDER`` may reach an autonomous
    reply.  The remaining dispositions are recorded but never answered.
    """

    TRUSTED_THREAD = "trusted_thread"
    APPROVED_SENDER = "approved_sender"
    UNSOLICITED_UNKNOWN = "unsolicited_unknown"
    AUTOMATED = "automated"
    BOUNCE = "bounce"
    SELF_COPY = "self_copy"
    SPAM_OR_BULK = "spam_or_bulk"


# Secondary defensive filter: recognizable automated/unmailable sender aliases.
# Trust scope (thread/relationship) is the *primary* boundary; these patterns
# only tighten it.
_AUTOMATED_SENDER_RE = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply|"
    r"mailer[-_.]?daemon|postmaster|delivery[-_.]?(?:status|failure)|"
    r"auto[-_.]?reply|bounce|mail[-_.]?administrator)@",
    re.IGNORECASE,
)

# Automation tokens matched ANYWHERE in the local part (with separator
# boundaries so human addresses like bouncer@ or bouncehouse@ never match):
# payments-noreply@, noreply-accounts@, transactional@, newsletter-team@.
_LOCAL_AUTOMATED_RE = re.compile(
    r"(?:^|[-_.])(?:no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|"
    r"mailer[-_.]?daemon|postmaster|auto[-_.]?reply|bounce|"
    r"transactional|newsletter|listserv)(?:[-_.]|$)",
    re.IGNORECASE,
)


def _is_automated_sender(sender_email: str) -> bool:
    """True when the sender address itself marks automation."""
    address = (sender_email or "").strip()
    if not address:
        return False
    if _AUTOMATED_SENDER_RE.search(address):
        return True
    local = address.split("@")[0] if "@" in address else address
    return bool(_LOCAL_AUTOMATED_RE.search(local))

_BOUNCE_SUBJECT_RE = (
    r"delivery status notification", r"undelivered(?: mail)?", r"undeliverable",
    r"mail delivery (?:failed|failure)", r"returned mail", r"returned to sender",
    r"mail status notification", r"delivery has failed", r"failure notice",
    r"non[- ]delivery",
)
_BOUNCE_BODY_RE = (
    r"this is the mail system at host", r"delivery status notification",
    r"message could not be delivered", r"permanent(?: delivery)? failure",
    r"the mime part of the returned message", r"mail delivery failed",
    r"cannot deliver|was not delivered", r"remote host has closed the connection",
)

_AUTOMATED_SUBJECT_RE = (
    r"auto[-\s]?gen", r"automated message", r"automatic reply", r"autorespond",
    r"out[- ]of[- ]office", r"auto[- ]reply", r"automated response",
    r"this mailbox (?:is|isn'?t) monitored", r"e?mail (?:status|notification)",
)
_AUTOMATED_BODY_RE = (
    r"this is (?:a|an) (?:automated|automatic) message",
    r"auto[- ]?generated (?:reply|email|message|response)",
    r"out[- ]of[- ]office(?: autoreply)?", r"i am currently (?:away|out of the office)",
    r"automatic response", r"no one(?: is)? (?:reads|monitors) this mailbox",
)

_SPAM_SUBJECT_RE = (
    r"^\[[^\]]+\](\s|:)", r"\bnewsletter\b", r"(?:limited time|act now)",
    r"congratulations!?", r"you'?ve won", r"\bviagra\b|\bcialis\b",
    r"\bcrypto\b.{0,40}\b(?:gift|win)\b", r"\bbitcoin\b.{0,40}\b(?:double|gift)\b",
    r"\bsponsored\b", r"\bpromo(?:tion)?\b.*\b\d+%?\s+off\b",
)
_SPAM_BODY_RE = (
    r"unsubscribe.{0,80}(?:click|link)", r"this email is (?:an|a) advertisement",
    r"you are subscribed to", r"to (?:unsubscribe|stop receiving)",
    r"\bviagra\b|\bcialis\b", r"\bcasino\b|\blottery\b", r"earn \$\d",
)


def classify_intent(body: str) -> str:
    text = (body or "").lower()
    for intent, patterns in _INTENT_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, text):
                return intent
    return "question"


def _matches_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


@dataclass
class InboundResult:
    relationship: Optional[Relationship]
    message: Message
    intent: str
    treated_as: str        # "opt_out" | "decline" | "reply" | "escalated" | "ignored" | "quarantined" | "deferred"
    responded: bool = False
    escalated: bool = False
    reason: str = ""
    disposition: Optional[InboundDisposition] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship.id if self.relationship else "",
            "message_id": self.message.id,
            "intent": self.intent,
            "treated_as": self.treated_as,
            "responded": self.responded,
            "escalated": self.escalated,
            "reason": self.reason,
            "disposition": self.disposition.value if self.disposition else "",
        }


def message_answered(store: OperationsStore, message: Message) -> bool:
    """True when an inbound message already has a successful outbound answer.

    Linkage is any of: a SENT response referencing its internal id or its
    transport external id (thread-reply convention), or a recorded
    ``response:`` evidence link from batch answering. FAILED sends never
    count: a failed attempt leaves the message still needing an answer.
    Used to skip reprocessing without ever manufacturing a reply.
    """
    for item in store.list_messages():
        if (item.direction is MessageDirection.OUTBOUND
                and item.status is MessageStatus.SENT
                and item.in_reply_to in (message.id, message.external_id)):
            return True
    return any(isinstance(p, str) and p.startswith("response:")
               for p in (message.evidence or []))


class ConversationMonitor:
    """Associates inbound messages with relationships and decides responses."""

    def __init__(
        self,
        composer: OutreachComposer,
        *,
        transport: Any = None,
        identity: Any = None,
        adapter: Any = None,
        self_address: str = "",
    ) -> None:
        self._composer = composer
        self._transport = transport
        self._identity = identity
        self._adapter = adapter
        self._self_address = self_address or self._derive_self_address(transport)

    @staticmethod
    def _derive_self_address(transport: Any) -> str:
        sender = ""
        if transport is not None:
            backend = getattr(transport, "backend", None)
            if backend is not None:
                sender = str(getattr(backend, "sender", "") or "")
            if not sender:
                sender = str(getattr(transport, "sender", "") or "")
        return sender

    def ingest(
        self,
        store: OperationsStore,
        *,
        sender_email: str,
        body: str,
        subject: str = "",
        thread_id: str = "",
        external_id: str = "",
        in_reply_to: str = "",
        references: Optional[list[str]] = None,
        raw_body: str = "",
        need_rules: Optional[list] = None,
        project_facts: Optional[list] = None,
        principal_domains: Optional[list] = None,
        job: Any = None,
        worker_id: str = "",
    ) -> InboundResult:
        if external_id:
            existing = store.find_message_by_external_id(external_id)
            if (existing is not None and existing.direction is MessageDirection.INBOUND
                    and message_answered(store, existing)):
                relationship = store.get_relationship(existing.relationship_id)
                return InboundResult(
                    relationship, existing, "answered", "answered",
                    disposition=None,
                    reason="already answered in this or an earlier tick; see linked response",
                )
        message_ids = [i for i in [*list(references or []), in_reply_to] if i]
        disposition, relationship = self._classify(
            store, sender_email=sender_email, thread_id=thread_id,
            message_ids=message_ids, subject=subject, body=body,
        )
        # Set when the unsolicited branch below already recorded the inbound
        # message (relevant first contact); the trusted block must not record
        # it a second time.
        recorded_message = None

        if disposition in (
            InboundDisposition.SELF_COPY, InboundDisposition.BOUNCE,
            InboundDisposition.AUTOMATED, InboundDisposition.SPAM_OR_BULK,
        ):
            message = self._record_inbound(
                store, relationship_id=relationship.id if relationship else "",
                sender_email=sender_email, body=body, subject=subject,
                thread_id=thread_id, external_id=external_id, in_reply_to=in_reply_to,
                references=references, raw_body=raw_body,
            )
            return InboundResult(
                relationship, message, "ignored", "ignored",
                disposition=disposition,
                reason=f"{disposition.value}: excluded from autonomous response",
            )

        if disposition is InboundDisposition.UNSOLICITED_UNKNOWN:
            message = self._record_inbound(
                store, relationship_id="", sender_email=sender_email, body=body,
                subject=subject, thread_id=thread_id, external_id=external_id,
                in_reply_to=in_reply_to, references=references, raw_body=raw_body,
            )
            text = f"{subject or ''}\n{body or ''}"
            if _matches_any(_OPT_OUT_PATTERNS, text):
                return InboundResult(
                    None, message, "opt_out", "opt_out",
                    disposition=disposition,
                    reason="unsolicited sender opted out: honored, no relationship, no reply",
                )
            if _matches_any(_DECLINE_PATTERNS, text):
                return InboundResult(
                    None, message, "decline", "declined",
                    disposition=disposition,
                    reason="unsolicited sender declined: closed, no relationship, no reply",
                )
            relevance = self._assess_relevance(
                subject, body, need_rules=need_rules,
                project_facts=project_facts, principal_domains=principal_domains,
            )
            if not relevance.relevant:
                return InboundResult(
                    None, message, "unsolicited", "quarantined",
                    disposition=disposition,
                    reason="unsolicited unknown sender: quarantined, no autonomous reply",
                )
            # Relevant first contact: create the relationship and rejoin the
            # trusted flow below. Intent classification, authority policy,
            # budgets, and outbound mode still govern everything downstream —
            # relevance only opens the door, never sends.
            relationship = Relationship(
                display_name=(sender_email or "").split("@")[0] if sender_email else "unknown",
                email=sender_email,
                purpose="unsolicited_first_contact",
                status=RelationshipStatus.ENGAGED,
                notes=[f"first contact; relevance: {', '.join(relevance.matched_terms)}"],
                first_contacted_at=message.received_at,
                last_inbound_at=message.received_at,
                next_action="evaluate inbound normally",
            )
            store.add_relationship(relationship)
            message.relationship_id = relationship.id
            store.update_message(message)
            relationship.message_ids.append(message.id)
            if thread_id and thread_id not in relationship.thread_ids:
                relationship.thread_ids.append(thread_id)
            relationship.last_inbound_at = message.received_at
            store.update_relationship(relationship)
            store.append_provenance(ProvenanceEntry(
                phase=ProvenancePhase.MONITOR,
                summary=f"relevant first contact from '{sender_email}': joining normal evaluation",
                action="relevance_accept",
                result="; ".join(relevance.reasons) or "work-related",
                evidence=[f"matched:{term}" for term in relevance.matched_terms[:6]],
                refs={"relationship_id": relationship.id, "message_id": message.id},
            ))
            recorded_message = message

        # Trusted scope from here on.
        if relationship is None:
            relationship = Relationship(
                display_name=sender_email.split("@")[0] if sender_email else "unknown",
                email=sender_email,
                status=RelationshipStatus.ENGAGED,
            )
            store.add_relationship(relationship)
        if recorded_message is None:
            message = self._record_inbound(
                store, relationship_id=relationship.id, sender_email=sender_email,
                body=body, subject=subject, thread_id=thread_id, external_id=external_id,
                in_reply_to=in_reply_to, references=list(references or []), raw_body=raw_body,
            )
            relationship.message_ids.append(message.id)
            if thread_id and thread_id not in relationship.thread_ids:
                relationship.thread_ids.append(thread_id)
            relationship.last_inbound_at = message.received_at
            relationship.touch()
            store.update_relationship(relationship)
        else:
            message = recorded_message

        return self.respond_to_recorded(
            store, relationship, message, sender_email=sender_email,
            body=body, subject=subject, disposition=disposition,
            job=job, worker_id=worker_id,
        )

    def respond_to_recorded(
        self, store: OperationsStore, relationship: Relationship, message: Message,
        *, sender_email: str, body: str, subject: str = "",
        disposition: Any = None, job: Any = None, worker_id: str = "",
    ) -> InboundResult:
        """Run intent, policy, composition, and dispatch for an already-recorded
        inbound message. Used by ingest() after classification/recording and by
        explicit deferred-reply retries. Never records a second copy.

        Coalescing policy (documented product behavior): when several human
        messages in one conversation await answers, ONE response goes to the
        NEWEST message with the older ones attached as marked thread history;
        every included inbound is then marked answered. The newest content
        always wins; quoted older text is history, never a new instruction.

        When ``job`` (an EmailJob) is provided, every lifecycle transition is
        persisted: GENERATING with a context fingerprint, READY_TO_SEND with
        the response hash, SENT with the outbound id the moment transport
        accepts, then COMPLETED — or FAILED with reason and retryability.
        """

        # A message inside a trusted thread but written by a *different* sender is
        # still not autonomous scope: we record it for context but never reply to
        # the intruder on the principal's behalf.
        if relationship.email and _norm(relationship.email) != _norm(sender_email):
            store.update_relationship(relationship)
            return InboundResult(
                relationship, message, "unsolicited", "quarantined",
                disposition=disposition,
                reason=f"unexpected sender '{sender_email}' in an existing thread: quarantined",
            )

        # Empty-body gate: if no readable text could be extracted, do NOT
        # classify, do NOT fall back to a template, and do NOT reply — defer
        # and notify the principal instead.
        if not (body or "").strip():
            relationship.status = RelationshipStatus.ENGAGED
            relationship.next_action = "deferred: message body unavailable"
            store.update_relationship(relationship)
            store.append_notification(
                NotificationEntry(
                    kind="inbound_body_unavailable",
                    summary="An inbound message had no readable text body; deferred with no reply.",
                    refs={"message_id": message.id, "relationship_id": relationship.id,
                          "recipient": relationship.email, "external_id": message.external_id},
                )
            )
            store.append_provenance(
                ProvenanceEntry(
                    phase=ProvenancePhase.MONITOR,
                    summary="inbound message with no readable text body; no reply sent",
                    action="monitor",
                    result="inbound_body_unavailable",
                    refs={"message_id": message.id, "relationship_id": relationship.id},
                )
            )
            self._mark_deferred(store, message, "inbound_body_unavailable")
            return InboundResult(
                relationship, message, "unknown", "deferred",
                reason="inbound_body_unavailable: message body could not be extracted",
                disposition=disposition,
            )

        text = body or ""
        if _matches_any(_OPT_OUT_PATTERNS, text):
            relationship.opted_out = True
            relationship.status = RelationshipStatus.OPTED_OUT
            relationship.next_action = "closed (opt-out)"
            relationship.follow_up_due_at = None
            store.update_relationship(relationship)
            return InboundResult(relationship, message, "opt_out", "opt_out", disposition=disposition, reason="sender opted out")

        if _matches_any(_DECLINE_PATTERNS, text):
            relationship.status = RelationshipStatus.DECLINED
            relationship.next_action = "closed (declined)"
            relationship.follow_up_due_at = None
            store.update_relationship(relationship)
            intent = classify_intent(text)
            subject_out, reply_body, reply_mode = self._composer.compose_reply(
                relationship, text, intent="decline",
                facts=self._verified_facts(store),
                adapter=self._adapter, identity=self._identity,
                inbound_subject=subject or "",
            )
            dispatch_record, transmitted, escalated = self._dispatch_reply(
                store, relationship, subject_out, reply_body, kind="decline", source=message,
                mode=reply_mode,
            )
            relationship.status = RelationshipStatus.DECLINED
            store.update_relationship(relationship)
            if transmitted:
                reason = "respectful close sent; no further outreach"
            elif (dispatch_record is not None
                    and dispatch_record.status is MessageStatus.FAILED):
                error = (dispatch_record.generation or {}).get("error", "send failed")
                reason = f"respectful close send failed: {error}"
            elif not escalated:
                reason = "respectful close drafted (observe mode); no further outreach"
            else:
                reason = "respectful close requires human approval"
            return InboundResult(
                relationship, message, intent, "decline", responded=transmitted,
                escalated=escalated, reason=reason, disposition=disposition,
            )

        # Batch unanswered thread-siblings into this reply: one response may
        # answer several messages, but every message gets answered. An opt-out
        # anywhere in the unanswered chain closes the whole thread instead.
        siblings = self._unanswered_siblings(store, relationship, message)
        if any(_matches_any(_OPT_OUT_PATTERNS, s.body or "")
               for s in siblings if (s.body or "").strip()):
            relationship.opted_out = True
            relationship.status = RelationshipStatus.OPTED_OUT
            relationship.next_action = "closed (opt-out in unanswered thread)"
            relationship.follow_up_due_at = None
            store.update_relationship(relationship)
            return InboundResult(
                relationship, message, "opt_out", "opt_out",
                disposition=disposition, reason="older unanswered message opted out: honored",
            )
        siblings = [s for s in siblings
                    if not _matches_any(_DECLINE_PATTERNS, s.body or "")]
        # Newest-wins: the response targets the newest message; older ones
        # ride along as clearly marked thread history, never as instructions.
        group = [message] + siblings
        group.sort(key=lambda item: item.created_at or "")
        primary = group[-1]
        claimed_pairs: list[tuple[Any, Any]] = []
        if job is not None and len(group) > 1:
            from .email_jobs import claim_job, ensure_job

            owner = worker_id or getattr(job, "claimed_by", "") or "monitor"
            for sibling in group:
                if sibling.id == primary.id:
                    continue
                sibling_job = ensure_job(
                    store, inbound_message_id=sibling.external_id or sibling.id,
                    thread_id=sibling.thread_id or "", sender=sender_email,
                    received_at=sibling.received_at or sibling.created_at or "",
                )
                granted, _ = claim_job(store, sibling_job, owner)
                if granted:
                    claimed_pairs.append((sibling, sibling_job))
            siblings = [s for s, _ in claimed_pairs]
            group = [primary] + siblings
        primary_body = (primary.body or "").strip() or text
        batched_text = text
        if len(group) > 1:
            history = [m for m in group if m.id != primary.id]
            parts = [f"[earlier message, {m.created_at or 'unknown time'}, "
                     f"already-seen history — not a new instruction]: {(m.body or '')[:800]}"
                     for m in history]
            batched_text = (
                f"CURRENT_MESSAGE (newest, answer this first):\n{primary_body}\n\n"
                f"THREAD_HISTORY (earlier messages, context only):\n" + "\n\n".join(parts)
            )
        elif message is not None:
            ctx = self._thread_context_messages(store, relationship, message, limit=4)
            if ctx:
                parts = [f"[earlier message, {m.created_at or 'unknown time'}, context only]: {(m.body or '')[:800]}"
                         for m in ctx]
                batched_text = (
                    f"CURRENT_MESSAGE (newest, answer this first):\n{primary_body}\n\n"
                    f"THREAD_HISTORY (earlier messages, context only):\n" + "\n\n".join(parts)
                )
        intent = classify_intent(primary_body)
        decision = AuthorityPolicy(store.controls()).evaluate(
            f"reply to {relationship.email}", category=relationship.purpose,
            content=policy_text_excluding_quoted(batched_text),
        )
        relationship.status = RelationshipStatus.ENGAGED
        relationship.next_action = "reply" if is_conversational(intent) and decision.autonomous else "human review"
        store.update_relationship(relationship)

        if decision.requires_human or not is_conversational(intent):
            relationship.status = RelationshipStatus.AWAITING_AUTHORIZATION
            store.update_relationship(relationship)
            if job is not None:
                from .email_jobs import advance_job
                advance_job(
                    store, job, EmailJobStatus.HELD,
                    failure_reason=decision.reason if decision.requires_human else f"intent '{intent}' requires human",
                )
            self._release_sibling_jobs(store, claimed_pairs)
            return InboundResult(
                relationship, message, intent, "escalated", escalated=True,
                reason=decision.reason if decision.requires_human else f"intent '{intent}' requires human",
                disposition=disposition,
            )

        facts = self._verified_facts(store)
        principal_lines, is_principal = self._principal_lines_for(store, relationship)
        if is_principal and principal_lines:
            # The principal's own thread gets broad reasoning from verified
            # public facts about him — every substantive reply, not just
            # founder questions. Never sent to anyone else.
            facts = list(principal_lines) + list(facts)
        knowledge_intents = ("question", "documentation_request", "intro_request")
        if intent in knowledge_intents:
            # Enrich with bounded technical context (identity spec, north star,
            # operator doc) so substantive replies are grounded in real project
            # architecture rather than just flat facts. Prepend so the 5-item
            # slice in the composer prioritizes technical context.
            tech = self._technical_context(store)
            facts = tech + list(facts)
        if intent in knowledge_intents and not facts:
            # Knowledge-readiness gate: without verified project facts any
            # substantive reply would be an ungrounded acknowledgement. Defer
            # instead of pretending.
            relationship.status = RelationshipStatus.ENGAGED
            relationship.next_action = "deferred: project context unavailable"
            store.update_relationship(relationship)
            store.append_notification(
                NotificationEntry(
                    kind="project_context_unavailable",
                    summary="Inbound request could not be answered: no verified project facts observed",
                    refs={"message_id": message.id, "relationship_id": relationship.id, "recipient": relationship.email},
                )
            )
            self._mark_deferred(store, message, "project_context_unavailable")
            if job is not None:
                from .email_jobs import fail_job

                fail_job(store, job, "project_context_unavailable", retryable=True)
            self._release_sibling_jobs(store, claimed_pairs)
            return InboundResult(
                relationship, message, intent, "deferred",
                reason="project_context_unavailable: no verified project facts",
                disposition=disposition,
            )

        if job is not None:
            from .email_jobs import advance_job, context_version as _context_version

            project_fp = ""
            try:
                import hashlib as _hashlib

                state = store.project_state()
                project_fp = _hashlib.sha256(
                    "\n".join(sorted(state.facts or [])).encode()
                ).hexdigest()[:16] if state else ""
            except Exception:
                project_fp = ""
            advance_job(
                store, job, EmailJobStatus.GENERATING,
                context_version_value=_context_version(
                    body_sha256=message.body_sha256 or "",
                    project_fingerprint=project_fp,
                    model_id=str(getattr(self._adapter, "model", "") or "")),
            )
            try:
                from .email_jobs import stamp_job

                stamp_job(
                    store, job, "generation_started_at",
                    provider=type(self._adapter).__name__,
                    model=str(getattr(self._adapter, "model", "") or ""),
                )
            except Exception:
                pass
        subject_out, reply_body, reply_mode = self._composer.compose_reply(
            relationship, batched_text, intent=intent, facts=facts,
            adapter=self._adapter, identity=self._identity,
            principal=is_principal,
            inbound_subject=(primary.subject or subject or ""),
        )
        if reply_mode == "unavailable":
            # Substantive reply requires a working model runtime. Never send a
            # canned answer as if it were a model answer — fail explicitly by
            # deferring and notifying the principal.
            relationship.status = RelationshipStatus.ENGAGED
            relationship.next_action = "deferred: reply generation unavailable (no model runtime)"
            store.update_relationship(relationship)
            store.append_notification(
                NotificationEntry(
                    kind="reply_generation_unavailable",
                    summary="A substantive inbound could not be answered: no model runtime is configured for reply generation.",
                    refs={"message_id": message.id, "relationship_id": relationship.id,
                          "recipient": relationship.email},
                )
            )
            self._mark_deferred(store, message, "reply_generation_unavailable")
            if job is not None:
                from .email_jobs import fail_job

                fail_job(store, job, "reply_generation_unavailable", retryable=True)
            self._release_sibling_jobs(store, claimed_pairs)
            return InboundResult(
                relationship, message, intent, "deferred",
                reason="reply_generation_unavailable: no model runtime for substantive reply",
                disposition=disposition,
            )
        from .email_jobs import advance_job, fail_job, response_hash

        # Threading binds to the PRIMARY (newest) message: In-Reply-To names
        # it and References chains the group, so Gmail keeps one thread.
        if job is not None:
            advance_job(
                store, job, EmailJobStatus.READY_TO_SEND,
                generated_response_hash=response_hash(reply_body),
            )
        dispatch_record, transmitted, escalated = self._dispatch_reply(
            store, relationship, subject_out, reply_body, kind="reply", source=primary,
            mode=reply_mode, policy_reason=decision.reason, job=job,
        )
        if escalated:
            # A held reply is NOT a completed one. The ledger must never claim
            # a message was handled when no outbound was ever sent, so the
            # job stays held (visible, retryable) and the inbound stays owed.
            if job is not None:
                advance_job(
                    store, job, EmailJobStatus.HELD,
                    failure_reason=(dispatch_record is None and "escalated"
                                    or "escalated: approval required"),
                )
            self._release_sibling_jobs(store, claimed_pairs)
            return InboundResult(
                relationship, message, intent, "escalated", escalated=True,
                reason="outbound mode requires human approval", disposition=disposition,
            )
        if not transmitted:
            if (dispatch_record is not None
                    and dispatch_record.status is MessageStatus.FAILED):
                error = (dispatch_record.generation or {}).get("error", "send failed")
                if job is not None:
                    fail_job(store, job, f"send failed: {error}", retryable=True)
                # A failed send stays retryable like any deferral: the marker
                # routes it back through the retry scanner next tick.
                self._mark_deferred(store, message, "send_failed")
                self._release_sibling_jobs(store, claimed_pairs)
                return InboundResult(
                    relationship, message, intent, "failed", responded=False,
                    reason=f"reply send failed: {error}", disposition=disposition,
                )
            if job is not None:
                # Observe mode: a reply exists but was withheld by policy, so
                # the inbound is still owed one. Held, never completed.
                advance_job(
                    store, job, EmailJobStatus.HELD,
                    failure_reason="observe mode: reply drafted, not sent",
                )
            self._release_sibling_jobs(store, claimed_pairs)
            return InboundResult(
                relationship, message, intent, "reply", responded=False,
                reason="observe mode: reply drafted, not sent", disposition=disposition,
            )
        response_id = dispatch_record.id if dispatch_record is not None else ""
        if job is not None:
            advance_job(
                store, job, EmailJobStatus.SENT,
                outbound_message_id=(dispatch_record.external_id
                                     if dispatch_record is not None else ""),
            )
            advance_job(store, job, EmailJobStatus.COMPLETED)
        for sibling in siblings:
            sibling.evidence = list(sibling.evidence or []) + [f"response:{response_id}"]
            store.update_message(sibling)
        self._complete_sibling_jobs(store, claimed_pairs)
        if siblings:
            store.append_provenance(ProvenanceEntry(
                phase=ProvenancePhase.MONITOR,
                summary=f"reply batched {len(siblings) + 1} unanswered message(s) into one response",
                action="batch_reply",
                result=f"answered: {primary.id} + {[s.id for s in siblings if s.id != primary.id]}",
                refs={"relationship_id": relationship.id, "message_id": primary.id,
                      "response_id": response_id},
            ))
        return InboundResult(
            relationship, message, intent, "reply", responded=True,
            reason=decision.reason, disposition=disposition,
        )


    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _complete_sibling_jobs(store: OperationsStore, claimed_pairs: list) -> None:
        """Complete coalesced sibling jobs after their shared response sent."""
        from .email_jobs import advance_job
        from .models import EmailJobStatus

        for _, sibling_job in claimed_pairs:
            try:
                fresh = store.get_email_job(sibling_job.id)
                if fresh is not None and not fresh.is_terminal():
                    advance_job(store, fresh, EmailJobStatus.COMPLETED)
            except Exception:
                continue

    @staticmethod
    def _release_sibling_jobs(store: OperationsStore, claimed_pairs: list) -> None:
        """Return coalesced siblings to DISCOVERED when the batch does not send.

        Used on defer/escalate/fail paths so siblings remain independently
        processable instead of stranding in a claimed state.
        """
        from .email_jobs import advance_job
        from .models import EmailJobStatus

        for _, sibling_job in claimed_pairs:
            try:
                fresh = store.get_email_job(sibling_job.id)
                if fresh is not None and not fresh.is_terminal():
                    advance_job(store, fresh, EmailJobStatus.DISCOVERED)
            except Exception:
                continue

    @staticmethod
    def _mark_deferred(store: OperationsStore, message: Message, reason: str) -> None:
        """Flag a deferred inbound message for automatic retry on later ticks.

        The marker lives on the persisted record (survives restarts), so mail
        deferred during a model outage — or fetched just before shutdown —
        is re-driven through the exact same respond path instead of stranding.
        Only explicit deferrals set it; ignored/quarantined/answered mail
        never carries it and is never retried.
        """
        message.authorization = f"deferred:{reason}"[:200]
        store.update_message(message)

    def _record_inbound(
        self, store: OperationsStore, *, relationship_id: str, sender_email: str,
        body: str, subject: str, thread_id: str, external_id: str,
        in_reply_to: str, references: Optional[list[str]], raw_body: str = "",
    ) -> Message:
        # Idempotent: a pre-recorded batch entry (or a reprocessed fetch) is
        # adopted instead of duplicated. Fresh entries record normally.
        if external_id:
            existing = store.find_message_by_external_id(external_id)
            if existing is not None and existing.direction is MessageDirection.INBOUND:
                return existing
        raw = raw_body or body or ""
        message = Message(
            relationship_id=relationship_id,
            direction=MessageDirection.INBOUND,
            subject=subject,
            body=body,
            raw_body=raw,
            body_sha256=hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest() if raw else "",
            received_at=utcnow().isoformat(),
            external_id=external_id,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=list(references or []),
            status=MessageStatus.RECEIVED,
        )
        store.append_message(message)
        return message

    def _assess_relevance(
        self,
        subject: str,
        body: str,
        *,
        need_rules: Optional[list] = None,
        project_facts: Optional[list] = None,
        principal_domains: Optional[list] = None,
    ):
        """Decide whether an unsolicited message is valid, work-related mail.

        Deterministic signals first (need domains, project vocabulary,
        principal terms). When those miss and a model runtime exists, Aster
        reasons about it herself under a strict verdict protocol; anything
        ambiguous, failing, or model-less stays quarantined. Relevance is
        opt-in evidence, never a default.
        """
        from .relevance import assess_inbound_relevance, project_vocabulary

        terms = project_vocabulary(project_facts or [])
        verdict = assess_inbound_relevance(
            subject, body,
            need_rules=need_rules or [],
            project_terms=terms,
            principal_domains=principal_domains or [],
        )
        if verdict.relevant or self._adapter is None:
            return verdict
        return self._model_relevance_verdict(subject, body, need_rules or [], terms)

    def _model_relevance_verdict(
        self, subject: str, body: str, need_rules: list, terms: list
    ):
        """Ask the model whether strange mail concerns the principal's work.

        Strict protocol: the first line must be exactly RELEVANT or
        NOT_RELEVANT, followed by a short reason quoting evidence. Anything
        else — including any failure — means quarantine.
        """
        from .relevance import Relevance
        from .voice import repair_outbound

        domains = sorted({str(getattr(rule, "category", "")) for rule in need_rules if getattr(rule, "category", "")})
        context = (
            "You triage inbound mail for Aster, an AI operator. Her principal's work spans: "
            f"{', '.join(domains) or 'general collaboration'}. "
            "Project vocabulary includes: " + (", ".join(terms[:15]) or "none observed") + ". "
            "Reply with EXACTLY this shape and nothing else on the first line: "
            "RELEVANT: <reason under 15 words quoting one short phrase from the mail> "
            "or NOT_RELEVANT: <reason under 15 words>."
        )
        user_input = f"Subject: {(subject or '')[:200]}\nBody:\n{(body or '')[:1500]}"
        try:
            raw = (self._adapter.generate(context, user_input, self._identity) or "").strip()
        except Exception:
            return Relevance(False, [], ["model verdict unavailable"])
        first, _, rest = raw.partition("\n")
        label = first.strip().upper()
        reason = (rest.strip().split("\n")[0] if rest.strip() else first[len(label):].strip(" :"))[:200]
        if label.startswith("RELEVANT") and not label.startswith("NOT_"):
            repaired, clean = repair_outbound(reason)
            return Relevance(True, ["model:work-related"],
                             [repaired if clean else "model judged work-related"])
        return Relevance(False, [], ["model judged not work-related"])

    def _thread_context_messages(self, store: OperationsStore, relationship: Any, message: Message, limit: int = 5) -> list[Message]:
        """Recent inbound in the same conversation, oldest first, context only.

        Unlike :meth:`_unanswered_siblings` this includes messages that have
        already been answered, because a follow-up like "what did I just ask"
        requires the full thread, not only the still-open tail.
        """
        items: list[Message] = []
        for item in store.list_messages():
            if item.direction is not MessageDirection.INBOUND:
                continue
            item_rel = item.relationship_id or ""
            my_rel = (relationship.id or "") if relationship is not None else ""
            same = False
            if item_rel and my_rel and item_rel == my_rel:
                same = True
            elif (item.thread_id or "") and (message.thread_id or "") and item.thread_id == message.thread_id:
                same = True
            if not same:
                continue
            items.append(item)
        items.sort(key=lambda x: x.created_at or "")
        items = [i for i in items if i.id != message.id]
        if limit <= 0:
            return items
        return items[-limit:]

    def _unanswered_siblings(self, store: OperationsStore, relationship: Any, message: Message) -> list[Message]:
        """Other inbound in the same conversation still awaiting an answer.

        Same relationship (or same shared thread for not-yet-linked
        pre-records), still RECEIVED, with no outbound answer linked either
        by ``in_reply_to`` or by a recorded ``response:`` evidence link (see
        :func:`message_answered`). These are the messages a single reply is
        allowed to coalesce and mark handled; already-answered history is
        excluded here and supplied as context by
        :meth:`_thread_context_messages` instead.
        """
        pending = []
        for item in self._thread_context_messages(store, relationship, message, limit=0):
            if item.status is not MessageStatus.RECEIVED:
                continue
            if message_answered(store, item):
                continue
            pending.append(item)
        return pending[:5]

    def _principal_lines_for(self, store: OperationsStore, relationship: Any) -> tuple[list[str], bool]:
        """Cached public principal facts for principal threads.

        Only the builder relationship ever receives them: principal facts
        must never leak into replies to anyone else. Reads the durable
        cache (refreshed weekly by the operator tick), so both fresh ingest
        and deferred retries see the same verified facts. Leads with the
        founder fact so identity questions are answered, never deflected.
        """
        try:
            from .principal import BUILDER_PURPOSE
            from .principal_knowledge import load_profile, principal_context_lines

            if getattr(relationship, "purpose", "") != BUILDER_PURPOSE:
                return [], False
            storage = getattr(store, "_storage", None)
            identity_id = getattr(store, "identity_id", "")
            if storage is None or not identity_id:
                return [], True
            from .aster import ASTER_FOUNDED_PROJECT, ASTER_FOUNDER_NAME

            lines = principal_context_lines(
                load_profile(storage, identity_id),
                founder=ASTER_FOUNDER_NAME, project=ASTER_FOUNDED_PROJECT,
            )
            return lines, True
        except Exception:
            return [], False

    def _classify(
        self, store: OperationsStore, *, sender_email: str, thread_id: str,
        message_ids: list[str], subject: str, body: str,
    ) -> tuple[InboundDisposition, Optional[Relationship]]:
        sender = _norm(sender_email)
        self_addr = _norm(self._self_address)
        if self_addr and sender and sender == self_addr:
            return InboundDisposition.SELF_COPY, None
        if sender and _is_automated_sender(sender_email):
            return InboundDisposition.AUTOMATED, None
        if _matches_any(_BOUNCE_SUBJECT_RE, subject) or _matches_any(_BOUNCE_BODY_RE, body):
            return InboundDisposition.BOUNCE, None
        if _matches_any(_AUTOMATED_SUBJECT_RE, subject) or _matches_any(_AUTOMATED_BODY_RE, body):
            return InboundDisposition.AUTOMATED, None
        if _matches_any(_SPAM_SUBJECT_RE, subject) or _matches_any(_SPAM_BODY_RE, body):
            return InboundDisposition.SPAM_OR_BULK, None
        # Primary boundary: trusted thread / known relationship scope.
        relationship = self._resolve_thread_relationship(store, thread_id, message_ids)
        if relationship is not None:
            return InboundDisposition.TRUSTED_THREAD, relationship
        if sender:
            relationship = store.find_relationship_by_email(sender_email)
            if relationship is not None:
                return InboundDisposition.APPROVED_SENDER, relationship
        return InboundDisposition.UNSOLICITED_UNKNOWN, None

    def _resolve_thread_relationship(
        self, store: OperationsStore, thread_id: str, message_ids: list[str],
    ) -> Optional[Relationship]:
        for candidate in ([thread_id] if thread_id else []) + list(message_ids or []):
            if not candidate:
                continue
            found = store.find_relationship_by_thread(candidate)
            if found is not None:
                return found
        return None

    def _verified_facts(self, store: OperationsStore) -> list[str]:
        """Verified project facts a reply may reference (from real observation)."""
        state = store.project_state()
        return list(state.facts) if state else []

    def _technical_context(self, store: OperationsStore) -> list[str]:
        """Retrieve bounded excerpts from key project docs for grounded replies.

        Reads identity spec, north star, operator doc, and spec from the resolved
        project root. Each excerpt is labeled with its source so the model can
        cite it. Total chars bounded to ~2500 so it fits in context.
        """
        state = store.project_state()
        if not state:
            return []
        root = state.metadata.get("resolved_root", "")
        if not root:
            return []

        from pathlib import Path
        root_path = Path(root)
        docs_dir = root_path / "docs"
        spec_dir = root_path / "spec"

        excerpts: list[str] = []
        MAX_PER_DOC = 500
        MAX_TOTAL = 2500
        total = 0

        # Priority docs that explain the architecture/portability model
        for doc_name in (
            "07-identity-spec.md",
            "01-north-star.md",
            "ASTER-OPERATOR.md",
            "SPEC.md",
            "spec/identity.schema.json",
        ):
            if doc_name.startswith("spec/"):
                p = spec_dir / doc_name.split("/", 1)[1]
            else:
                p = docs_dir / doc_name
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # Take first meaningful lines (skip title/badges)
            lines = [ln for ln in text.splitlines()
                     if ln.strip() and not ln.strip().startswith("#")
                     and not ln.strip().startswith("[!")
                     and not ln.strip().startswith("![")
                     and not ln.strip().startswith("<")
                     and not ln.strip().startswith("```")]
            excerpt = " ".join(lines)[:MAX_PER_DOC]
            if excerpt:
                excerpts.append(f"[{doc_name}] {excerpt}")
                total += len(excerpt)
                if total >= MAX_TOTAL:
                    break
        return excerpts

    def _dispatch_reply(
        self, store: OperationsStore, relationship: Relationship, subject: str, body: str,
        *, kind: str, source: Optional[Message] = None,
        mode: str = "template_fallback", policy_reason: str = "", job: Any = None,
    ) -> tuple[Optional[Message], bool, bool]:
        """Route a composed reply through the outbound operating mode.

        Returns ``(message, transmitted, escalated)``.  ``observe`` records a
        WOULD_SEND draft without transmitting; ``approval_required`` records an
        escalation; ``autonomous`` transmits through the transport.  Every
        record carries ``generation`` metadata so a template reply can never be
        confused with a model-backed one.
        """
        outbound_mode = normalize_outbound_mode(store.controls().outbound_mode)
        thread, in_reply_to, references = self._threading_for(source, relationship)
        generation = self._generation_meta(store, mode, source, policy_reason=policy_reason)

        if outbound_mode == "approval_required":
            if self._has_draft(store, relationship.id, kind):
                return None, False, False
            record = Message(
                relationship_id=relationship.id,
                direction=MessageDirection.OUTBOUND,
                subject=subject,
                body=body,
                status=MessageStatus.AWAITING_AUTHORIZATION,
                authorization=f"awaiting_human_authorization:{kind}",
                thread_id=thread,
                in_reply_to=in_reply_to,
                references=list(references),
                generation=generation,
            )
            store.append_message(record)
            relationship.message_ids.append(record.id)
            relationship.status = RelationshipStatus.AWAITING_AUTHORIZATION
            relationship.next_action = "human authorization required"
            store.update_relationship(relationship)
            store.append_notification(
                NotificationEntry(
                    kind="escalation",
                    summary=f"reply requires human authorization: {kind}",
                    refs={"message_id": record.id, "relationship_id": relationship.id,
                          "recipient": relationship.email},
                )
            )
            return record, False, True

        if outbound_mode == "observe":
            if self._has_draft(store, relationship.id, kind):
                return None, False, False
            record = Message(
                relationship_id=relationship.id,
                direction=MessageDirection.OUTBOUND,
                subject=subject,
                body=body,
                status=MessageStatus.WOULD_SEND,
                authorization=f"observe_would_send:{kind}",
                thread_id=thread,
                in_reply_to=in_reply_to,
                references=list(references),
                generation=generation,
            )
            store.append_message(record)
            relationship.message_ids.append(record.id)
            store.update_relationship(relationship)
            store.append_provenance(
                ProvenanceEntry(
                    phase=ProvenancePhase.PLAN,
                    summary=f"reply drafted in observation mode (not sent): {kind}",
                    action="would_send",
                    result=f"to={relationship.email} mode=observe",
                    refs={"relationship_id": relationship.id, "message_id": record.id},
                )
            )
            return record, False, False

        record, transmitted = self._send_reply(
            store, relationship, subject, body,
            thread=thread, in_reply_to=in_reply_to, references=list(references),
            generation=generation, job=job,
        )
        if not transmitted and record is not None and record.status is MessageStatus.FAILED:
            return record, False, False
        return record, transmitted, False

    def _generation_meta(
        self, store: OperationsStore, reply_mode: str, source: Optional[Message],
        *, policy_reason: str = "",
    ) -> dict[str, Any]:
        """Provenance for how an outbound reply was produced.

        Distinguishes ``identity_model_generation`` (model adapter wrote it)
        from ``template_fallback`` and records provider/model, the inbound
        messages being answered, the policy decision, and the verified facts
        the model could cite.  Compact keys only (no credentials).
        """
        meta: dict[str, Any] = {
            "mode": reply_mode,
            "adapter": type(self._adapter).__name__ if self._adapter is not None else "",
            "model": str(getattr(self._adapter, "model", "") or "") if self._adapter is not None else "",
            "inbound_message_ids": [source.id] if source else [],
            "inbound_external_ids": [source.external_id] if source and source.external_id else [],
        }
        # A fallback chain reports which provider actually generated the reply;
        # never present the chain head as the author (live-test finding).
        selection = getattr(self._adapter, "last_selection", None) or {}
        if selection.get("provider"):
            meta["provider"] = selection["provider"]
        if selection.get("model"):
            meta["model"] = selection["model"]
        if selection.get("latency_ms") is not None:
            meta["latency_ms"] = selection["latency_ms"]
        if policy_reason:
            meta["policy"] = policy_reason
        state = store.project_state()
        if state is not None:
            meta["verified_fact_ids"] = [f.id for f in state.fact_details]
        return {k: v for k, v in meta.items() if v not in ("", [], None)}

    def _threading_for(self, source: Optional[Message], relationship: Relationship) -> tuple[str, str, list[str]]:
        """Derive the RFC threading context for a reply to ``source``."""
        if source is not None:
            thread = source.thread_id or (relationship.thread_ids[-1] if relationship.thread_ids else "")
            in_reply_to = source.external_id or source.in_reply_to or source.thread_id
            references = [r for r in [*list(source.references or []), in_reply_to] if r]
            return thread, in_reply_to, references
        thread = relationship.thread_ids[-1] if relationship.thread_ids else ""
        return thread, "", []

    def _has_draft(self, store: OperationsStore, relationship_id: str, kind: str) -> bool:
        for existing in store.list_messages():
            if existing.relationship_id != relationship_id:
                continue
            if existing.status not in (MessageStatus.WOULD_SEND, MessageStatus.AWAITING_AUTHORIZATION):
                continue
            if kind in (existing.authorization or ""):
                return True
        return False

    def _send_reply(
        self, store: OperationsStore, relationship: Relationship, subject: str, body: str,
        *, thread: str = "", in_reply_to: str = "", references: Optional[list[str]] = None,
        generation: Optional[dict[str, Any]] = None, job: Any = None,
    ) -> tuple[Optional[Message], bool]:
        """Transmit a reply through the transport, so 'sent' is runtime-verified.

        Returns ``(record, transmitted)``. The record is persisted on every
        path — SENT on success, FAILED with the real error otherwise — so a
        failure is observable and distinguishable from a drafted-but-unsent
        reply, never silently 'sent' and never mislabeled.

        A standards-compliant RFC 5322 ``Message-ID`` is generated *before*
        transmission and persisted verbatim in ``Message.external_id`` (falling
        back to the transport's identifier only if it overrides ours).  The
        relationship is re-persisted on every path so ``message_ids`` and
        ``last_outbound_at`` are never lost to an unsaved in-memory append.
        """
        if not thread:
            thread = relationship.thread_ids[-1] if relationship.thread_ids else ""
        generation = dict(generation or {})

        def _stamp(stage: str) -> None:
            if job is None:
                return
            try:
                from .email_jobs import stamp_job

                stamp_job(store, job, stage)
            except Exception:
                pass

        def _record_failure(reason: str) -> Message:
            record = Message(
                relationship_id=relationship.id,
                direction=MessageDirection.OUTBOUND,
                subject=subject,
                body=body,
                status=MessageStatus.FAILED,
                authorization=reason,
                thread_id=thread,
                in_reply_to=in_reply_to,
                references=list(references or []),
                generation=generation,
            )
            store.append_message(record)
            relationship.message_ids.append(record.id)
            store.update_relationship(relationship)
            return record

        if self._transport is None:
            return _record_failure("dry_run_no_transport"), False

        from core.capabilities.email.backends import generate_message_id

        message_id = generate_message_id()
        _stamp("send_started_at")
        try:
            result = self._transport.send(
                to=relationship.email, subject=subject, body=body, thread_id=thread,
                in_reply_to=in_reply_to, references=list(references or []),
                message_id=message_id,
            )
        except Exception as exc:
            generation["error"] = f"{type(exc).__name__}: {exc}"
            return _record_failure("conversational_autonomous"), False
        if isinstance(result, dict) and not result.get("ok"):
            generation["error"] = str(result.get("error", "send_not_ok"))
            return _record_failure("conversational_autonomous"), False
        _stamp("smtp_accepted_at")

        external_id = str(result.get("external_id") or message_id)
        generation["mode"] = generation.get("mode") or "identity_model_generation"
        generation["message_id"] = external_id
        message = Message(
            relationship_id=relationship.id,
            direction=MessageDirection.OUTBOUND,
            subject=subject,
            body=body,
            sent_at=utcnow().isoformat(),
            status=MessageStatus.SENT,
            authorization="conversational_autonomous",
            external_id=external_id,
            thread_id=result.get("thread_id") or thread,
            in_reply_to=result.get("in_reply_to") or in_reply_to,
            references=references or [],
            generation=generation,
        )
        store.append_message(message)
        relationship.message_ids.append(message.id)
        resolved_thread = message.thread_id
        if resolved_thread and resolved_thread not in relationship.thread_ids:
            relationship.thread_ids.append(resolved_thread)
        relationship.last_outbound_at = message.sent_at
        store.update_relationship(relationship)
        store.record_usage("replies")
        return message, True