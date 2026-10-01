"""Canonical, transport-neutral communication model.

IdentityOS already persists messages, but as a flat list keyed by ``Message``
rows that assume email. Terminal sessions and email share the same table, so a
reader cannot tell "an email Arsène sent" from "a command he typed", and there
is no conversation to group by.

This module adds the missing abstraction without a second source of truth:
:class:`Conversation` and :class:`CommunicationEvent` are a *view* assembled
from records that already exist (email ``Message`` rows plus the channel's own
event log). Nothing here writes. That keeps one timeline, adds channels
without touching business logic, and cannot drift from the durable state it
reads.

A channel declares how to enumerate its own events. Adding SMS, a phone call,
or Culture Commons means writing one :class:`Channel` implementation, not
editing the dashboard or the monitor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .models import utcnow

# ── channel registry ───────────────────────────────────────────────────────

#: Canonical channel ids. This is a vocabulary, not business logic: a channel
#: exists here only if something implements it.
CHANNEL_EMAIL = "email"
CHANNEL_TERMINAL = "terminal"
CHANNEL_A2A = "a2a"
CHANNEL_COMMONS = "commons"
CHANNEL_SMS = "sms"
CHANNEL_PHONE = "phone"
CHANNEL_MCP = "mcp"


@dataclass(frozen=True)
class Channel:
    """A transport Aster can be addressed on, and how to read its timeline."""

    id: str
    label: str
    #: ``(store, identity_id, limit) -> list[event dicts]``
    reader: Callable[..., list[dict[str, Any]]]
    #: True when the channel is backed by a real, currently configured
    #: transport. Channels without history and without a configured transport
    #: are hidden rather than rendered as empty.
    is_configured: Callable[[Any], bool] = lambda store: True
    icon: str = "•"
    description: str = ""


_REGISTRY: dict[str, Channel] = {}


def register_channel(channel: Channel) -> Channel:
    _REGISTRY[channel.id] = channel
    return channel


def get_channel(channel_id: str) -> Optional[Channel]:
    return _REGISTRY.get(channel_id)


def known_channel_ids() -> list[str]:
    return sorted(_REGISTRY)


# ── canonical records ──────────────────────────────────────────────────────


@dataclass
class CommunicationEvent:
    """One thing that happened on a channel, in one shape for every transport.

    Fields left empty by a transport are honestly empty rather than invented:
    SMS has no subject and no thread id, and the model must not pretend
    otherwise by borrowing the email's.
    """

    id: str
    conversation_id: str
    channel: str
    direction: str  # "inbound" | "outbound"
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    external_message_id: str = ""
    external_thread_id: str = ""
    rfc_message_id: str = ""
    reply_to_event_id: str = ""
    received_at: str = ""
    sent_at: str = ""
    created_at: str = ""
    status: str = ""
    provenance_id: str = ""
    operation_job_id: str = ""
    provider: str = ""
    model: str = ""
    error: str = ""
    #: Channel-specific fields kept intact rather than flattened away.
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "channel": self.channel,
            "direction": self.direction,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "subject": self.subject,
            "body": self.body,
            "external_message_id": self.external_message_id,
            "external_thread_id": self.external_thread_id,
            "rfc_message_id": self.rfc_message_id,
            "reply_to_event_id": self.reply_to_event_id,
            "received_at": self.received_at,
            "sent_at": self.sent_at,
            "created_at": self.created_at,
            "status": self.status,
            "provenance_id": self.provenance_id,
            "operation_job_id": self.operation_job_id,
            "provider": self.provider,
            "model": self.model,
            "error": self.error,
            "attributes": dict(self.attributes),
        }


@dataclass
class Conversation:
    """A thread of events with one counterpart, on one channel."""

    id: str
    channel: str
    external_conversation_id: str = ""
    title: str = ""
    participants: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    events: list[CommunicationEvent] = field(default_factory=list)
    #: Summary state, derived from the events and the durable job ledger.
    state: str = "idle"
    awaiting_aster: bool = False
    last_error: str = ""

    def to_summary(self) -> dict[str, Any]:
        latest = self.events[-1] if self.events else None
        return {
            "id": self.id,
            "channel": self.channel,
            "external_conversation_id": self.external_conversation_id,
            "title": self.title,
            "participants": list(self.participants),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "event_count": len(self.events),
            "state": self.state,
            "awaiting_aster": self.awaiting_aster,
            "last_error": self.last_error,
            "latest": latest.to_dict() if latest is not None else None,
        }


# ── derivation ─────────────────────────────────────────────────────────────


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _message_created(message: Any) -> str:
    return (getattr(message, "received_at", "") or getattr(message, "created_at", "")
            or getattr(message, "sent_at", "") or "")


def _counterparty_address(store: Any, relationship_id: str, direction: str) -> str:
    """The human on the other end of a message.

    ``Message`` does not persist a sender column, so the address is resolved
    from the relationship the message is linked to. Returning "" is honest
    when there is no relationship; the UI shows an unnamed counterpart rather
    than inventing an address.
    """
    if not relationship_id:
        return ""
    try:
        getter = getattr(store, "get_relationship", None)
        relationship = getter(relationship_id) if getter else None
    except Exception:
        relationship = None
    if relationship is None:
        return ""
    return (getattr(relationship, "email", "") or "").strip()


def _email_conversation_id(thread_id: str, sender: str, recipient: str = "") -> str:
    """Stable conversation key for email.

    Gmail's immutable thread id is authoritative when the transport captured
    one. Otherwise fall back to the RFC thread, and only then to the
    counterpart pair, so a conversation never silently splits just because one
    message lacked a References header.
    """
    if thread_id:
        return f"email:{thread_id}"
    return f"email:pair:{_norm(sender)}|{_norm(recipient)}"


def _email_events(store: Any, *, limit: int = 500) -> list[dict[str, Any]]:
    """Email events, one row per real message, Gmail ids attached."""
    messages = list(store.list_messages())
    gmail_by_rfc: dict[str, Any] = {}
    for job in store.list_email_jobs():
        # New jobs record rfc_message_id explicitly; jobs written before that
        # field existed keyed the RFC id into inbound_message_id. Both hold the
        # same value, so index both and never lose a legacy message.
        for key in (job.rfc_message_id, job.inbound_message_id):
            if key:
                gmail_by_rfc.setdefault(key, job)

    events: list[dict[str, Any]] = []
    for message in messages:
        direction = (getattr(message.direction, "value", str(message.direction))
                     or "").lower()
        if direction not in ("inbound", "outbound"):
            continue
        # Only email rows belong to the email channel. Terminal/control
        # messages live in the same table and must not be folded in.
        if getattr(message, "channel", "email") not in ("email", "", None):
            continue
        rfc = (getattr(message, "external_id", "") or "").strip()
        job = gmail_by_rfc.get(rfc)
        generation = dict(getattr(message, "generation", {}) or {})
        relationship_id = getattr(message, "relationship_id", "") or ""
        counterparty = _counterparty_address(store, relationship_id, direction)
        rfc_id = rfc
        gmail_message_id = ""
        gmail_thread_id = ""
        job_rfc = ""
        if job is not None:
            gmail_message_id = job.gmail_message_id or ""
            gmail_thread_id = job.gmail_thread_id or ""
            job_rfc = job.rfc_message_id or rfc
        events.append(CommunicationEvent(
            id=message.id,
            conversation_id="",
            channel=CHANNEL_EMAIL,
            direction=direction,
            sender=(counterparty if direction == "inbound"
                    else generation.get("to", "") or counterparty),
            recipients=[counterparty] if direction == "outbound" and counterparty else [],
            subject=message.subject or "",
            body=message.body or "",
            external_message_id=gmail_message_id,
            external_thread_id=gmail_thread_id,
            rfc_message_id=job_rfc or rfc,
            reply_to_event_id=getattr(message, "in_reply_to", "") or "",
            received_at=getattr(message, "received_at", "") or "",
            sent_at=getattr(message, "sent_at", "") or "",
            created_at=_message_created(message),
            status=(getattr(message.status, "value", str(message.status)) or ""),
            provenance_id=(getattr(message, "generation", {}) or {}).get("provenance_id", ""),
            operation_job_id=(job.id if job is not None else ""),
            provider=generation.get("adapter", "") or generation.get("provider", ""),
            model=generation.get("model", ""),
            error=generation.get("error", ""),
            attributes={
                "relationship_id": relationship_id,
                "authorization": getattr(message, "authorization", "") or "",
                "references": list(getattr(message, "references", []) or []),
                "body_sha256": getattr(message, "body_sha256", "") or "",
                # The RFC thread id is a durable grouping fallback for mail
                # that predates Gmail immutable-id capture.
                "thread_id": getattr(message, "thread_id", "") or "",
            },
        ).to_dict())
    events.sort(key=lambda e: e.get("created_at") or "")
    return events[-limit:]


def _terminal_events(store: Any, *, limit: int = 500) -> list[dict[str, Any]]:
    """Terminal / CLI sessions, kept strictly separate from email.

    Aster Control messages share the Message table with email, so the channel
    is identified by the control channel marker rather than by row order.
    """
    try:
        from .principal import CONTROL_CHANNEL
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for message in store.list_messages():
        if getattr(message, "channel", "") != CONTROL_CHANNEL:
            continue
        generation = dict(getattr(message, "generation", {}) or {})
        out.append(CommunicationEvent(
            id=message.id,
            conversation_id="",
            channel=CHANNEL_TERMINAL,
            direction=(getattr(message.direction, "value", str(message.direction)) or "").lower(),
            sender=getattr(message, "authorization", "") or "",
            subject="",
            body=message.body or "",
            created_at=_message_created(message),
            received_at=getattr(message, "received_at", "") or "",
            sent_at=getattr(message, "sent_at", "") or "",
            status=(getattr(message.status, "value", str(message.status)) or ""),
            provider=generation.get("adapter", ""),
            model=generation.get("model", ""),
            attributes={"session_id": getattr(message, "thread_id", "") or ""},
        ).to_dict())
    out.sort(key=lambda e: e.get("created_at") or "")
    return out[-limit:]


register_channel(Channel(
    id=CHANNEL_EMAIL, label="Email", reader=_email_events, icon="✉",
    description="Inbound and outbound mail, threaded by conversation",
))
register_channel(Channel(
    id=CHANNEL_TERMINAL, label="Terminal / CLI", reader=_terminal_events,
    icon="▸", description="Interactive sessions and Aster Control messages",
))

_JOB_AWAITING = {"discovered", "claimed", "generating", "ready_to_send", "held"}


def conversations_for(store: Any, channel_id: str, *,
                      limit: int = 100) -> list[Conversation]:
    """Group one channel's events into conversations, newest activity first."""
    channel = get_channel(channel_id)
    if channel is None:
        return []
    try:
        raw_events = list(channel.reader(store, limit=limit * 4))
    except Exception:
        return []

    # Map Gmail message id -> event id so a reply can point at what it answers.
    by_gmail: dict[str, str] = {}
    for event in raw_events:
        if event.get("external_message_id"):
            by_gmail[event["external_message_id"]] = event["id"]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in raw_events:
        if channel_id == CHANNEL_EMAIL:
            attributes = event.get("attributes") or {}
            key = _email_conversation_id(
                event.get("external_thread_id")
                or attributes.get("thread_id", "")
                or event.get("thread_id_hint", ""),
                attributes.get("sender", ""), attributes.get("recipient", ""))
        else:
            key = f"terminal:{event.get('attributes', {}).get('session_id', '') or 'default'}"
        event["conversation_id"] = key
        reply_to = event.get("reply_to_event_id") or ""
        if reply_to in by_gmail:
            event["reply_to_event_id"] = by_gmail[reply_to]
        elif reply_to:
            event["reply_to_event_id"] = ""
        grouped.setdefault(key, []).append(event)

    jobs_by_rfc: dict[str, Any] = {}
    if channel_id == CHANNEL_EMAIL:
        for job in getattr(store, "list_email_jobs", lambda: [])():
            for key in (job.rfc_message_id, job.inbound_message_id):
                if key:
                    jobs_by_rfc.setdefault(key, job)

    out: list[Conversation] = []
    for key, events in grouped.items():
        events.sort(key=lambda e: e.get("created_at") or "")
        participants: list[str] = []
        for event in events:
            if event.get("sender") and event["sender"] not in participants:
                participants.append(event["sender"])
            for address in event.get("recipients") or []:
                if address and address not in participants:
                    participants.append(address)
        title = ""
        for event in events:
            if event.get("subject"):
                title = event["subject"]
                break
        conversation = Conversation(
            id=key,
            channel=channel_id,
            external_conversation_id=events[0].get("external_thread_id", "") or "",
            title=title,
            participants=participants,
            created_at=events[0].get("created_at", "") or "",
            updated_at=events[-1].get("created_at", "") or "",
            events=[CommunicationEvent(**_event_kwargs(e)) for e in events],
        )
        # Awaiting Aster is decided by the durable ledger, never by guessing
        # from timestamps: a job that is held or in flight means a reply is
        # genuinely still owed.
        awaiting = False
        error = ""
        for event in events:
            rfc = event.get("rfc_message_id") or ""
            job = jobs_by_rfc.get(rfc)
            if job is None:
                continue
            if job.status.value in _JOB_AWAITING:
                awaiting = True
            if job.failure_reason:
                error = job.failure_reason
        conversation.awaiting_aster = awaiting
        conversation.last_error = error
        conversation.state = _conversation_state(conversation)
        out.append(conversation)
    out.sort(key=lambda c: c.updated_at or "", reverse=True)
    return out[:limit]


def _event_kwargs(data: dict[str, Any]) -> dict[str, Any]:
    known = {f for f in CommunicationEvent.__dataclass_fields__}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in known}


def _conversation_state(conversation: Conversation) -> str:
    if conversation.last_error:
        return "error"
    if conversation.awaiting_aster:
        return "awaiting_aster"
    latest = conversation.events[-1] if conversation.events else None
    if latest is not None and latest.direction == "inbound":
        return "waiting_for_aster"
    return "replied"


def channels_for(store: Any) -> list[dict[str, Any]]:
    """Channels that are actually configured or have real history.

    An empty channel is not shown: a communications center listing A2A and SMS
    with nothing in them is noise, and it implies capabilities Aster does not
    have.
    """
    out: list[dict[str, Any]] = []
    for channel_id in known_channel_ids():
        channel = get_channel(channel_id)
        if channel is None:
            continue
        try:
            events = list(channel.reader(store, limit=1))
        except Exception:
            events = []
        if not events:
            try:
                if not channel.is_configured(store):
                    continue
            except Exception:
                continue
        conversation_count = 0
        try:
            conversation_count = len(conversations_for(store, channel_id, limit=200))
        except Exception:
            conversation_count = 0
        if not events and not conversation_count:
            continue
        out.append({
            "id": channel.id,
            "label": channel.label,
            "icon": channel.icon,
            "description": channel.description,
            "conversation_count": conversation_count,
        })
    return out
