"""
core/operations/voice.py

Aster's communication identity and outbound style enforcement — the shared
rendering/validation layer for every Aster-authored outbound boundary.

Two responsibilities:

1. Communication identity: who Aster is in external communication (name,
   display name, canonical email, principal, disclosure lines, full/compact/
   HTML signatures, writing profile). Mission-specific values live in
   :mod:`core.operations.aster`; this module holds the generic machinery.

2. The em-dash invariant: ASTER-AUTHORED OUTBOUND TEXT MUST CONTAIN ZERO
   U+2014 CHARACTERS. This is an identity-level rule, not a prompt
   preference. Enforcement is layered:

   - generation constraints (the writing profile instructs the model);
   - deterministic validation at every covered boundary;
   - deterministic repair for a narrow set of meaning-safe patterns
     (numeric ranges, paired parentheticals) — never a blind
     ``text.replace("—", "-")``, which produces poor writing;
   - rejection with regeneration when safe repair is impossible.

Historical records and quoted third-party evidence are never mutated: the
rule governs Aster-authored OUTPUT only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

EM_DASH = "\u2014"

# Numeric ranges: "pages 10 — 20" -> "pages 10 to 20". Meaning-preserving.
_RANGE_RE = re.compile(r"(\d)\s+—\s+(\d)")

# Paired parenthetical: "everything — memory, relationships — across" ->
# "everything (memory, relationships) across". Only when the inner span has
# no sentence-enders and no nested em dash; parentheses are always
# grammatical here, commas would break on inner commas.
_PAIRED_RE = re.compile(r" — ([^.!?—\n]{1,200}) — ")

# Unpaired junction: "Address rename verified — same account, new name"
# -> "Address rename verified: same account, new name". A colon is the
# standard punctuation for a title/subject divider. Only fires when no
# paired parenthetical remains. The separator form has meaning
# (announcement followed by detail), which a colon preserves.
_SINGLE_JUNCTION_RE = re.compile(r" — ")


class OutboundStyleError(ValueError):
    """An Aster-authored artifact still contains U+2014 after repair."""


def find_em_dashes(text: str) -> list[int]:
    """Return the character offsets of every U+2014 in *text*."""
    return [match.start() for match in re.finditer(EM_DASH, text or "")]


def count_em_dashes(text: str) -> int:
    """Return the number of U+2014 characters in *text*."""
    return (text or "").count(EM_DASH)


@dataclass
class StyleValidation:
    ok: bool
    em_dash_count: int
    locations: list[int] = field(default_factory=list)


def validate_outbound(*parts: Any) -> StyleValidation:
    """Validate final rendered outbound text (subject, body, signature...).

    Inspects the joined artifact: a violation in ANY part fails the whole.
    """
    text = "\n".join("" if part is None else str(part) for part in parts)
    locations = find_em_dashes(text)
    return StyleValidation(ok=not locations, em_dash_count=len(locations), locations=locations)


def repair_outbound(text: str, *, header: bool = False) -> tuple[str, bool]:
    """Apply ONLY meaning-safe deterministic repairs.

    Returns ``(repaired_text, fully_clean)``. Ranges become "N to M";
    paired parentheticals become parentheses. Anything else is left
    untouched for the caller to reject/regenerate — never mangled with a
    blind hyphen replacement.

    ``header=True`` additionally converts a single junction separator
    ("X — Y") to a colon ("X: Y"). That is safe only for headers/subjects:
    a one-line subject uses the dash as a title divider, and neither mail
    headers nor the prose body should pretend to be a sentence.
    """
    if not text or EM_DASH not in text:
        return text, True
    repaired = _RANGE_RE.sub(r"\1 to \2", text)

    def _paren(match: re.Match) -> str:
        return f" ({match.group(1).strip()}) "

    repaired = _PAIRED_RE.sub(_paren, repaired)
    if header:
        # "Re: Address rename verified — same account, ..." is a header from
        # an inbound subject: a colon reads the same and the line stays one
        # header. Bodies keep the strict rule instead.
        repaired = _SINGLE_JUNCTION_RE.sub(": ", repaired)
        repaired = re.sub(r" :", ":", repaired)
    # Collapse any doubled spacing the substitutions may introduce, without
    # touching newlines or intentional indentation.
    repaired = re.sub(r"[ \t]{2,}", " ", repaired)
    return repaired, EM_DASH not in repaired


def assert_clean(*parts: Any, context: str = "outbound") -> None:
    """Raise OutboundStyleError if the joined artifact contains U+2014."""
    validation = validate_outbound(*parts)
    if not validation.ok:
        raise OutboundStyleError(
            f"{context} contains {validation.em_dash_count} em dash(es) "
            f"at offsets {validation.locations[:8]}"
        )


@dataclass
class CommunicationIdentity:
    """Reusable presentation profile for an operator identity."""

    identity_name: str = "Aster"
    identity_type: str = "AI identity"
    system: str = "IdentityOS"
    email: str = ""
    email_display_name: str = ""
    principal: str = ""
    short_description: str = ""
    disclosure_first_contact: str = ""
    disclosure_followup: str = ""
    signature_full: str = ""
    signature_compact: str = ""
    signature_html: str = ""
    never_em_dash: bool = True

    def display_sender(self) -> str:
        """Sender display string, e.g. ``Aster | IdentityOS``."""
        return self.email_display_name or self.identity_name

    def signature_for(self, *, first_contact: bool, formal: bool = False) -> str:
        """Deterministic signature selection.

        First contact and formal/consequential communication always carry
        the full signature; established threads may use the compact form.
        """
        if first_contact or formal:
            return self.signature_full
        return self.signature_compact or self.signature_full


def split_signature(body: str, identity: CommunicationIdentity) -> tuple[str, str]:
    """Split a trailing known signature off a composed body.

    Returns ``(body_without_signature, variant)`` where variant is "full",
    "compact", or "". Only exact suffix matches strip — never fuzzy edits.
    """
    text = body or ""
    full = (identity.signature_full or "").strip()
    compact = (identity.signature_compact or "").strip()
    stripped = text.rstrip()
    if full and stripped.endswith(full):
        return stripped[: -len(full)].rstrip(), "full"
    if compact and stripped.endswith(compact):
        return stripped[: -len(compact)].rstrip(), "compact"
    return text, ""


def render_html_body(body: str, identity: CommunicationIdentity) -> tuple[str, str]:
    """Render ``(html_body, signature_variant)`` for multipart email.

    The plain-text body stays canonical; the HTML alternative mirrors it
    (paragraphs + linked signature) without tracking, images, or styling
    beyond inherited text.
    """
    import html as _html

    content, variant = split_signature(body, identity)
    paragraphs = [f"<p>{_html.escape(block).replace(chr(10), '<br>')}</p>"
                  for block in content.split("\n\n") if block.strip()]
    # Append the linked signature block only when a matching plain signature
    # was actually stripped; otherwise the converted body is already complete
    # and appending would duplicate the sign-off.
    html_signature = ""
    if variant:
        html_signature = identity.signature_html or render_html_signature(identity)
    return "".join(paragraphs) + html_signature, variant or "unsigned"


def render_html_signature(identity: CommunicationIdentity) -> str:
    """Restrained HTML rendering of the canonical signature.

    No images, no tracking, no remote assets, no colors beyond inherited
    text, no titles. The plain-text alternative remains canonical.
    """
    import html as _html

    lines = [line for line in (identity.signature_full or "").splitlines() if line.strip()]
    if not lines:
        return ""
    parts = [f"<strong>{_html.escape(lines[0])}</strong>"]
    for line in lines[1:]:
        if line.startswith("http"):
            escaped = _html.escape(line)
            parts.append(f'<a href="{escaped}">{escaped}</a>')
        else:
            parts.append(_html.escape(line))
    rendered = "<br>".join(parts)
    return f'<div class="aster-signature">{rendered}</div>'


def select_formality(*, authorization: str = "", requires_human: bool = False) -> bool:
    """Formal (full-signature) communication when consequential.

    Escalations, authorizations, and approval-gated messages are formal;
    routine autonomous outreach is not.
    """
    marker = f"{authorization or ''}".lower()
    return bool(requires_human or "authoriz" in marker or "escalat" in marker)


RE_PREFIX_RE = re.compile(r"^\s*re\s*:\s*", re.IGNORECASE)
_FWD_PREFIX_RE = re.compile(r"^\s*((re|aw|fw|fwd|antw)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
DEFAULT_REPLY_SUBJECT = "Re: your message"


def reply_subject(original: str) -> str:
    """The subject a reply MUST carry, derived deterministically.

    Reply threading metadata is transport, not authorship. A model that
    invents a subject breaks Gmail conversation grouping: Gmail keeps a
    message in the referenced conversation only when the subject matches
    too, so a rewritten subject silently forks the thread and strands the
    rest of the conversation. So the subject is never generated.

    An already-``Re:`` subject is preserved byte for byte (Gmail clients
    stack prefixes, and re-prefixing produces "Re: Re: ..."). Anything
    else is prefixed once. Header newlines are removed because a subject is
    a single header field.
    """
    flat = re.sub(r"[\r\n\t]+", " ", (original or "")).strip()
    flat = re.sub(r"\s{2,}", " ", flat)
    if not flat:
        return DEFAULT_REPLY_SUBJECT
    if RE_PREFIX_RE.match(flat):
        return flat
    return "Re: " + flat


def normalize_reply_subject(text: str) -> str:
    """Collapse stacked reply prefixes on an *inbound* subject.

    Humans' mail clients chain "Re: Re: Re:". Normalizing before deriving
    the reply subject keeps a long conversation on one stable subject, so
    Gmail never fragments it.
    """
    flat = re.sub(r"[\r\n\t]+", " ", (text or "")).strip()
    if not flat:
        return flat
    stripped = _FWD_PREFIX_RE.sub("", flat)
    return (stripped or flat).strip()


def style_constraint_prompt() -> str:
    """Generation constraints for Aster-authored prose.

    Given to the model alongside the writing profile. This reduces
    violations at the source; the deterministic validator remains
    authoritative at every boundary.
    """
    return (
        "Punctuation constraint (identity rule, no exceptions): never use the em dash "
        "character (U+2014) anywhere, including subjects and signatures. When you want "
        "an interruption, contrast, aside, or parenthetical, restructure naturally with "
        "periods, commas, colons, semicolons, or parentheses instead. Examples of the "
        "required style: use a colon to introduce an explanation ('IdentityOS solves a "
        "different problem: continuity across models and interfaces'); use commas for "
        "asides ('I found something interesting, but I want to verify it first'); use "
        "parentheses for parenthetical lists. Never substitute a hyphen for an em dash."
    )
