"""
core/operations/principal.py

Principal messaging for operator identities (Aster Control).

Arsène (builder / human principal) communicates with the SAME persistent
operator identity through a private control interface. This module owns:

- the builder relationship (created once, deduped by purpose marker — never
  duplicated);
- inbound principal message submission with size/rate limits;
- deterministic command classification (OBSERVE / COMMUNICATE / PROPOSE /
  EXECUTE / CONVERSE). Classification is triage only: existing IdentityOS
  permissions, capability gates, budgets, and escalation rules remain
  authoritative at execution time;
- principal response composition through the configured model adapter with
  the operator's identity context. A substantive reply REQUIRES a working
  model runtime; without one the message is DEFERRED, never faked.

No secrets are stored in message metadata. Message bodies belong to the
principal↔operator conversation and are readable only through the private,
authenticated control surface — never through public or logged channels.
"""

from __future__ import annotations

import enum
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .models import (
    Message,
    MessageDirection,
    MessageStatus,
    ProvenanceEntry,
    ProvenancePhase,
    Relationship,
    RelationshipStatus,
    utcnow,
)

#: Purpose marker identifying the builder relationship. Dedupe key.
BUILDER_PURPOSE = "principal:builder"

#: Control-surface channel for phone-dashboard messages.
CONTROL_CHANNEL = "aster-control"

#: Bounds for inbound principal messages.
MAX_MESSAGE_CHARS = 4000
MAX_PENDING_MESSAGES = 5
RATE_WINDOW_SECONDS = 300
MAX_MESSAGES_PER_WINDOW = 10


class CommandClass(str, enum.Enum):
    """Bounded instruction categories for principal messages."""

    CONVERSE = "converse"
    OBSERVE = "observe"
    COMMUNICATE = "communicate"
    PROPOSE = "propose"
    EXECUTE = "execute"

    @classmethod
    def coerce(cls, value: Any) -> "CommandClass":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        return cls.CONVERSE


_EXECUTE_RE = re.compile(
    r"\b(install|uninstall|delete|remove|acquire|post|publish|speak|authorize|"
    r"approve|grant|revoke|pause|resume|stop|restart|sign|rise|enter|override|"
    r"escalate|send\s+(an?\s+)?email|change|update|fix|deploy|merge|commit)\b",
    re.IGNORECASE,
)
_COMMUNICATE_RE = re.compile(
    r"\b(reply|respond|message|tell|notify|email|write\s+to|contact|reach\s+out)\b"
    r".{0,40}\b(to|selah|them|him|her|everyone|all)\b"
    r"|\b(reply\s+to|respond\s+to)\b",
    re.IGNORECASE,
)
_OBSERVE_RE = re.compile(
    r"\b(check|look|search|find|inspect|inspecting|summar|report|status|what(\'s| is| are| did| have)|"
    r"show|list|how\s+is|how\s+are|did\s+you|have\s+you|are\s+you|observe|monitor|read)\b",
    re.IGNORECASE,
)
_PROPOSE_RE = re.compile(
    r"\b(plan|propose|proposal|design|draft|figure\s+out|how\s+(could|would|should)|"
    r"don\'t\s+change|do\s+not\s+change|without\s+(changing|doing|executing)|idea|option|consider)\b",
    re.IGNORECASE,
)


_NO_CHANGE_RE = re.compile(
    r"\b(do\s+not\s+change|don\'t\s+change|without\s+changing|no\s+changes?|"
    r"don\'t\s+do\s+anything|just\s+(look|check|report))\b",
    re.IGNORECASE,
)


def classify_command(text: str) -> CommandClass:
    """Deterministically classify a principal message. Triage, not policy."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return CommandClass.CONVERSE
    if _NO_CHANGE_RE.search(lowered):
        return CommandClass.PROPOSE
    if _EXECUTE_RE.search(lowered):
        return CommandClass.EXECUTE
    if _COMMUNICATE_RE.search(lowered):
        return CommandClass.COMMUNICATE
    if _PROPOSE_RE.search(lowered):
        return CommandClass.PROPOSE
    if _OBSERVE_RE.search(lowered):
        return CommandClass.OBSERVE
    return CommandClass.CONVERSE


def find_builder_relationship(store: Any) -> Optional[Relationship]:
    """Return the existing builder relationship, if any. Never duplicates."""
    for rel in store.list_relationships():
        if (rel.purpose or "") == BUILDER_PURPOSE:
            return rel
    return None


def ensure_builder_relationship(store: Any, *, display_name: str = "Arsène Manzi") -> Relationship:
    """Return the builder relationship, creating it exactly once."""
    existing = find_builder_relationship(store)
    if existing is not None:
        return existing
    now = utcnow().isoformat()
    rel = Relationship(
        display_name=display_name,
        organization="",
        role="builder & human principal",
        purpose=BUILDER_PURPOSE,
        status=RelationshipStatus.ENGAGED,
        notes=["Builder relationship for the private Aster Control interface."],
        first_contacted_at=now,
        last_inbound_at=now,
        next_action="awaiting principal message",
    )
    return store.add_relationship(rel)


def pending_principal_messages(store: Any, *, limit: int = 20) -> list[Message]:
    """Inbound control-channel messages awaiting operator processing, oldest first.

    DEFERRED messages are picked up again on later ticks: the blocking
    condition (no model runtime, transient failure) may have resolved.
    PERMISSION_REQUIRED / FAILED / COMPLETED are terminal until the principal
    acts or sends a new message.
    """
    items = [
        m for m in store.list_messages()
        if m.direction is MessageDirection.INBOUND
        and m.channel == CONTROL_CHANNEL
        and m.status in (MessageStatus.RECEIVED, MessageStatus.QUEUED, MessageStatus.DEFERRED)
    ]
    items.sort(key=lambda m: m.created_at or "")
    return items[: max(1, limit)]


def thread_messages(store: Any, *, limit: int = 50) -> list[Message]:
    """Full principal conversation history (inbound + responses), oldest first."""
    items = [m for m in store.list_messages() if m.channel == CONTROL_CHANNEL]
    items.sort(key=lambda m: m.created_at or "")
    return items[-max(1, limit):]


def _principal_provenance(
    store: Any,
    summary: str,
    *,
    action: str = "",
    result: str = "",
    refs: Optional[dict[str, Any]] = None,
    scrub: Any = None,
) -> None:
    if scrub is not None:
        try:
            summary = scrub(summary)
            result = scrub(result)
        except Exception:
            pass
    store.append_provenance(
        ProvenanceEntry(
            phase=ProvenancePhase.PRINCIPAL,
            summary=summary,
            action=action,
            result=result,
            refs=dict(refs or {}),
        )
    )


def submit_principal_message(
    store: Any,
    text: str,
    *,
    thread_id: str = "",
    scrub: Any = None,
) -> Message:
    """Persist an inbound principal message. Returns it with status RECEIVED.

    Raises ValueError on validation failure (empty / oversize) and
    RuntimeError when the principal already has too many unprocessed messages
    (backpressure, not silent loss).
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("message is empty")
    if len(body) > MAX_MESSAGE_CHARS:
        raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} characters")

    pending = pending_principal_messages(store, limit=MAX_PENDING_MESSAGES + 1)
    if len(pending) >= MAX_PENDING_MESSAGES:
        raise RuntimeError(
            f"{len(pending)} principal message(s) still queued; "
            "wait for Aster to process them before sending more"
        )
    now = datetime.now(timezone.utc)
    window_start = (now.timestamp() - RATE_WINDOW_SECONDS)
    recent = 0
    for message in thread_messages(store, limit=MAX_MESSAGES_PER_WINDOW + 5):
        if message.direction is not MessageDirection.INBOUND:
            continue
        try:
            created = datetime.fromisoformat(message.created_at or "")
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created.timestamp() >= window_start:
            recent += 1
    if recent >= MAX_MESSAGES_PER_WINDOW:
        raise RuntimeError("rate limit: too many principal messages in a short window")

    rel = ensure_builder_relationship(store)
    message = Message(
        relationship_id=rel.id,
        direction=MessageDirection.INBOUND,
        channel=CONTROL_CHANNEL,
        subject="",
        body=body,
        received_at=utcnow().isoformat(),
        thread_id=thread_id or "principal",
        status=MessageStatus.RECEIVED,
        authorization="principal:builder",
    )
    store.append_message(message)
    rel.message_ids.append(message.id)
    rel.last_inbound_at = message.received_at
    rel.next_action = "process principal message"
    store.update_relationship(rel)
    _principal_provenance(
        store,
        "principal message received via Aster Control",
        action="principal.receive",
        result=f"queued for operator processing ({len(body)} chars)",
        refs={"message_id": message.id, "relationship_id": rel.id},
        scrub=scrub,
    )
    return message


def build_principal_context(
    *,
    identity_name: str,
    objective: str,
    history: list[Message],
    command: CommandClass,
    presence_summary: str = "",
    principal_lines: Optional[list[str]] = None,
) -> tuple[str, str]:
    """Build (system_context, user_input) for a genuine model response.

    The response is generated as the persistent operator identity: name,
    objective, live presence facts, and the principal conversation history.
    No chain-of-thought is requested or stored.
    """
    from .voice import style_constraint_prompt

    lines = [
        f"You are {identity_name}, a persistent autonomous operator identity running on IdentityOS, "
        "acting under delegated authority for your builder and human principal Arsène Manzi.",
        "You are replying to your principal through a private control interface. "
        "Be direct, specific, and honest about uncertainty. Never claim an action happened unless it did. "
        "Everyday conversation is welcome; for consequential or binding decisions, escalate instead of deciding alone.",
        f"Your current objective: {objective or 'not set'}.",
        style_constraint_prompt(),
    ]
    if presence_summary:
        lines.append(f"Live operational facts (verified runtime state, not claims): {presence_summary}")
    verified = [str(line) for line in (principal_lines or []) if str(line).strip()][:8]
    if verified:
        lines.append("Verified public facts about Arsène Manzi (cite only what is written here; "
                     "never invent achievements; one warm sentence of genuine appreciation is welcome):")
        lines.extend(f"- {line}" for line in verified)
    history_lines: list[str] = []
    for item in history[-8:]:
        who = "Arsène" if item.direction is MessageDirection.INBOUND else identity_name
        snippet = (item.body or "")[:1200]
        history_lines.append(f"{who}: {snippet}")
    user_input = (
        f"Instruction category (triage): {command.value}.\n"
        + ("\n".join(history_lines) if history_lines else "(no prior conversation)")
    )
    return "\n".join(lines), user_input
