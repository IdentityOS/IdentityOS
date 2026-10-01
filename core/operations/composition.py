"""
core/operations/composition.py

Individualized message composition.

Messages are derived from a structured :class:`OutreachBrief` built out of real
evidence (the target's actual work, the specific need, the concrete ask).  A
model adapter may be plugged in to polish prose, but the *content* is always
grounded in the brief and the deterministic renderer is the fallback.  Nothing
here is a hard-coded template for a specific target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Need, Opportunity, Relationship


@dataclass
class OutreachBrief:
    recipient_name: str
    organization: str = ""
    role: str = ""
    relevant_work: list[str] = field(default_factory=list)
    need_description: str = ""
    value_proposition: str = ""
    potential_ask: str = ""
    fit_reason: str = ""
    sender_name: str = ""
    transparency: str = ""
    signature: str = ""
    subject_hint: str = ""

    @classmethod
    def from_opportunity(
        cls,
        opportunity: Opportunity,
        need: Need,
        *,
        sender_name: str = "",
        transparency: str = "",
        signature: str = "",
    ) -> "OutreachBrief":
        return cls(
            recipient_name=opportunity.target_name or opportunity.organization,
            organization=opportunity.organization,
            relevant_work=list(opportunity.relevant_work),
            need_description=need.description,
            value_proposition=opportunity.value_proposition,
            potential_ask=opportunity.potential_ask,
            fit_reason=opportunity.fit_reason,
            sender_name=sender_name,
            transparency=transparency,
            signature=signature,
        )


class OutreachComposer:
    """Builds individualized outreach and replies, optionally via a model."""

    def __init__(
        self,
        *,
        sender_name: str = "",
        project_name: str = "",
        signature: str = "",
        transparency: str = "",
    ) -> None:
        self.sender_name = sender_name
        self.project_name = project_name
        self.signature = signature
        self.transparency = transparency

    # ── cold outreach ─────────────────────────────────────────────────

    def compose(
        self,
        brief: OutreachBrief,
        *,
        adapter: Any = None,
        identity: Any = None,
    ) -> tuple[str, str]:
        """Return ``(subject, body)`` for an individualized introduction.

        Model-generated text passes through the voice invariant: safe repairs
        apply, and anything still containing U+2014 falls back to the clean
        deterministic renderer rather than shipping a violation.
        """
        from .voice import repair_outbound, validate_outbound

        if adapter is not None:
            generated = self._via_adapter(brief, adapter, identity)
            if generated:
                subject, body = generated
                if validate_outbound(subject, body).ok:
                    return subject, body
                repaired_subject, subject_clean = repair_outbound(subject)
                repaired_body, body_clean = repair_outbound(body)
                if subject_clean and body_clean:
                    return repaired_subject, repaired_body
        subject, body = self._structured(brief)
        if validate_outbound(subject, body).ok:
            return subject, body
        # Evidence-derived fields can carry dashes (e.g. a name). Repair safe
        # cases; anything still dirty is returned for the transport gate to
        # fail closed with a clear reason rather than shipping a violation.
        repaired_subject, subject_clean = repair_outbound(subject)
        repaired_body, body_clean = repair_outbound(body)
        if subject_clean and body_clean:
            return repaired_subject, repaired_body
        return subject, body

    def _structured(self, brief: OutreachBrief) -> tuple[str, str]:
        first_name = (brief.recipient_name or "there").split()[0]
        focus = brief.relevant_work[0] if brief.relevant_work else (brief.organization or "your work")
        subject = brief.subject_hint or self._default_subject(focus)
        work_line = self._work_line(brief)
        such_as = f" {work_line}" if work_line else ""

        paragraphs = [
            f"Dear {first_name},",
            (
                f"I'm {self.sender_name or 'an operator'} reaching out on behalf of "
                f"{self.project_name or 'our project'}. I came across your work{such_as} "
                f"and it connects directly to a problem we are actively working on: "
                f"{brief.need_description or 'a resourcing gap on our project'}."
            ),
        ]

        if brief.fit_reason:
            paragraphs.append(f"What stood out: {brief.fit_reason}")
        if brief.value_proposition:
            paragraphs.append(f"What we bring: {brief.value_proposition}")
        if brief.potential_ask:
            paragraphs.append(f"What I'd like to ask: {brief.potential_ask}")
        if brief.transparency or self.transparency:
            paragraphs.append(brief.transparency or self.transparency)
        paragraphs.append("If this isn't relevant, a one-line reply is enough and I won't follow up.")
        paragraphs.append(brief.signature or self.signature or f"Warmly,\n{self.sender_name}")

        body = "\n\n".join(p for p in paragraphs if p)
        return subject, body

    def _default_subject(self, focus: str) -> str:
        focus = (focus or "your work").strip().rstrip(".")
        if len(focus) > 60:
            focus = focus[:57] + "..."
        return f"Connecting {self.project_name or 'our project'} with your work on {focus}"

    def _work_line(self, brief: OutreachBrief) -> str:
        if not brief.relevant_work:
            return ""
        works = [w.strip().rstrip(".") for w in brief.relevant_work if w.strip()]
        if len(works) == 1:
            return f"on {works[0]}"
        if len(works) == 2:
            return f"on {works[0]} and {works[1]}"
        return f"on {works[0]}, {works[1]}, and related efforts"

    # ── replies ───────────────────────────────────────────────────────

    def compose_reply(
        self,
        relationship: Relationship,
        inbound_body: str,
        *,
        intent: str = "question",
        facts: Optional[list[str]] = None,
        adapter: Any = None,
        identity: Any = None,
        principal: bool = False,
        inbound_subject: str = "",
    ) -> tuple[str, str, str]:
        """Return ``(subject, body, generation_mode)`` for a reply.

        The subject is DERIVED from ``inbound_subject`` by
        :func:`core.operations.voice.reply_subject` and is never model
        output: Gmail only keeps a reply in the referenced conversation when
        the subject matches, so a model-invented subject forks the thread.
        Any ``Subject:`` line the model emits is discarded before the body is
        read.

        ``generation_mode`` is one of:
          * ``identity_model_generation`` — written by a model adapter
            (the only mode allowed for substantive/knowledge replies);
          * ``template_fallback`` — a scripted close for status-only intents
            (decline / thanks / scheduling);
          * ``unavailable`` — a substantive intent but no working model
            runtime, or model output that still violates the voice invariant
            after safe repair.  The monitor must *defer* (never send a canned
            answer masquerading as a model one, and never ship U+2014).
        """
        from .voice import repair_outbound, reply_subject, validate_outbound
        from .voice import normalize_reply_subject

        subject = reply_subject(normalize_reply_subject(inbound_subject))
        if adapter is not None:
            generated = self._reply_via_adapter(relationship, inbound_body, intent, facts or [], adapter, identity,
                                                principal=principal)
            if generated:
                _model_subject, body = generated
                body = self._ensure_signature(body)
                if validate_outbound(subject, body).ok:
                    return subject, body, "identity_model_generation"
                repaired_body, body_clean = repair_outbound(body)
                if body_clean:
                    return subject, repaired_body, "identity_model_generation"
        if intent in ("question", "documentation_request", "intro_request", "interest"):
            return subject, "", "unavailable"
        _t_subject, template_body = self._structured_reply(relationship, inbound_body, intent, facts or [])
        return subject, template_body, "template_fallback"

    def _structured_reply(
        self,
        relationship: Relationship,
        inbound_body: str,
        intent: str,
        facts: list[str],
    ) -> tuple[str, str]:
        first_name = (relationship.display_name or "there").split()[0]
        subject = f"Re: {relationship.purpose or 'our conversation'}"
        lines: list[str] = [f"Hi {first_name},", ""]

        if intent == "thanks":
            lines.append("Glad this was useful. No action needed on your side.")
        elif intent == "scheduling":
            lines.append(
                "Happy to find a time. I can do a short call in the next week; "
                "send a couple of windows that suit you and I'll confirm."
            )
        elif intent == "decline":
            lines.append(
                "Understood, and thank you for the candid reply. I'll close this out and "
                "won't reach out again about it."
            )
        else:
            # Knowledge/substantive intents are answered only through a model
            # adapter (see compose_reply); this is a truthful stand-in for
            # direct callers that must not pretend to be a model answer.
            lines.append(
                "Thanks for the note. I'll confirm the details from our verified "
                "material and come back to you rather than guess."
            )

        lines.extend(["", "Best,"])
        lines.append(self.sender_name or "Aster")
        if self.signature:
            lines.append(self.signature)
        return subject, "\n".join(lines)

    def compose_follow_up(
        self,
        relationship: Relationship,
        *,
        adapter: Any = None,
        identity: Any = None,
    ) -> tuple[str, str]:
        """A short, polite nudge when a first message went unanswered."""
        from .voice import repair_outbound, validate_outbound

        angle = ""
        for note in reversed(relationship.notes or []):
            if note.startswith("re-engagement angle:"):
                angle = note.split(":", 1)[1].strip()
                break
        if adapter is not None:
            brief = OutreachBrief(
                recipient_name=relationship.display_name,
                organization=relationship.organization,
                need_description="a brief follow-up on my earlier note",
                potential_ask="a quick yes/no on whether this is relevant",
                signature=self.signature,
                fit_reason=f"Re-engagement angle: {angle}" if angle else "",
            )
            generated = self._via_adapter(brief, adapter, identity)
            if generated:
                subject, body = generated
                if validate_outbound(subject, body).ok:
                    return subject, body
                repaired_subject, subject_clean = repair_outbound(subject)
                repaired_body, body_clean = repair_outbound(body)
                if subject_clean and body_clean:
                    return repaired_subject, repaired_body
        first_name = (relationship.display_name or "there").split()[0]
        subject = f"Re: {relationship.purpose or 'my earlier note'}"
        body_lines = [
            f"Hi {first_name},",
            "",
            "Following up once on my earlier note in case it landed at a busy time.",
            "If it isn't relevant, a one-line reply is all I need and I'll leave it there.",
        ]
        if angle:
            body_lines += ["", f"One concrete thought: {angle}"]
        body_lines += [
            "",
            "Best,",
        ]
        body_lines.append(self.sender_name or "Aster")
        if self.signature:
            body_lines.append(self.signature)
        body = "\n".join(body_lines)
        return subject, body

    # ── adapters ──────────────────────────────────────────────────────

    def _via_adapter(self, brief: OutreachBrief, adapter: Any, identity: Any) -> Optional[tuple[str, str]]:
        from .voice import style_constraint_prompt

        system = (
            "You write a single, respectful, specific cold outreach email. "
            "Use ONLY the facts in the brief. Never invent credentials, funding, or claims. "
            "Keep it under 180 words. Include the transparency note and signature exactly. "
            "Return the email as 'Subject: <subject>' on the first line then the body. "
            + style_constraint_prompt()
        )
        payload = {
            "recipient": brief.recipient_name,
            "organization": brief.organization,
            "relevant_work": brief.relevant_work,
            "need": brief.need_description,
            "value_proposition": brief.value_proposition,
            "ask": brief.potential_ask,
            "fit_reason": brief.fit_reason,
            "transparency": brief.transparency or self.transparency,
            "signature": brief.signature or self.signature,
        }
        text = self._generate(system, payload, adapter, identity)
        return self._parse_email(text)

    def _reply_via_adapter(
        self, relationship: Relationship, inbound_body: str, intent: str, facts: list[str], adapter: Any, identity: Any,
        *, principal: bool = False, max_tokens: int = 1000,
    ) -> Optional[tuple[str, str]]:
        from .voice import style_constraint_prompt

        system = (
            "You draft the BODY of a short, polite reply. Answer directly and fully "
            "from the verified facts provided: questions showing interest in the "
            "project, the identity, or the principal are engagement to nurture, so "
            "give everything the facts support and close by inviting continued "
            "conversation. "
            "Only when the verified facts genuinely lack the answer, say so plainly in "
            "one sentence and offer a concrete next step instead of deflecting "
            "preemptively. "
            "If the message asks for anything binding (money, contracts, legal, access, "
            "employment, ownership), say you will check with the principal instead of "
            "answering. "
            "Return the reply body only. Never write a Subject line: the subject is "
            "fixed transport metadata and any Subject line you produce is discarded. "
            "Never restate the quoted thread history; answer what the current message "
            "actually asks. "
            + style_constraint_prompt()
        )
        if principal:
            system += (
                " You are writing to Arsène Manzi himself, your builder and principal. "
                "You may warmly acknowledge verified facts about him from the provided "
                "context (a sentence of genuine appreciation is welcome); never invent "
                "achievements, titles, or history, and never be sycophantic."
            )
        payload = {
            "from": relationship.display_name,
            "intent": intent,
            "inbound": inbound_body[:1500],
            "verified_facts": facts[:5],
            "signature": self.signature,
        }
        text = self._generate(system, payload, adapter, identity, max_tokens=max_tokens)
        return self._parse_email(text)

    def _ensure_signature(self, body: str) -> str:
        """Guarantee model-generated replies carry the sender signature.

        The model is asked to sign, but token ceilings can cut the sign-off:
        if the configured signature text is absent, append sender + signature
        exactly once (never duplicated).
        """
        text = body or ""
        if self.signature and self.signature.strip() in text:
            return text
        suffix = "\n\n" + (self.sender_name or "Aster")
        if self.signature:
            suffix += "\n" + self.signature
        return text.rstrip() + suffix

    def _generate(self, system: str, payload: dict, adapter: Any, identity: Any,
                  *, max_tokens: int = 500) -> str:
        import json

        try:
            return adapter.generate(
                system,
                json.dumps(payload, ensure_ascii=False, indent=2),
                identity,
                temperature=0.3,
                max_tokens=max_tokens,
            )
        except Exception:
            return ""

    def _parse_email(self, text: str) -> Optional[tuple[str, str]]:
        """Split model output into ``(model_subject, body)``.

        The model subject is returned only so callers can observe that it was
        discarded: the reply subject is derived from the triggering message
        by :func:`core.operations.voice.reply_subject`, never from the model.
        A stray ``Subject:`` line in the body is stripped so it can never
        leak into sent mail.
        """
        if not text or not text.strip():
            return None
        subject = ""
        body_lines: list[str] = []
        for line in text.splitlines():
            if not subject and line.lower().startswith("subject:"):
                subject = line.split(":", 1)[1].strip()
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if not body:
            return None
        return subject, body

    def signature_line(self) -> str:
        return self.signature or (self.sender_name or "Aster")
