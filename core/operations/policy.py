"""
core/operations/policy.py

Authority policy — the hard boundary between autonomous action and escalation.

The operator may act autonomously on low-risk, reversible communication.  It may
*never* autonomously commit the principal to anything consequential.  This module
turns that boundary into an inspectable decision so the engine can mark work as
``AWAITING_HUMAN_AUTHORIZATION`` instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .models import ControlState


class Authority(str, Enum):
    AUTONOMOUS = "autonomous"
    AWAITING_HUMAN_AUTHORIZATION = "awaiting_human_authorization"


@dataclass
class AuthorizationDecision:
    authority: Authority
    reason: str
    category: str = ""
    matched_terms: list[str] = field(default_factory=list)

    @property
    def autonomous(self) -> bool:
        return self.authority is Authority.AUTONOMOUS

    @property
    def requires_human(self) -> bool:
        return self.authority is Authority.AWAITING_HUMAN_AUTHORIZATION


# Categories that always require explicit human authorization, regardless of
# operator configuration.  These are commitments that bind the principal.
# ``require_approval_categories`` (a human control) may only ever ADD to this
# baseline; no operator configuration can disable these.
BASELINE_CONSEQUENTIAL_CATEGORIES: tuple[str, ...] = (
    "investment",
    "equity",
    "contracts",
    "legal",
    "money",
    "credentials",
    "access",
    "ip ownership",
    "repository access",
    "employment",
    "exclusivity",
)

# Deterministic conservative pre-policy: a regex signal scan that runs BEFORE
# any model-based classification.  A single strong signal is enough to escalate;
# this intentionally errs toward the principal's review.
CONSEQUENTIAL_SIGNALS: dict[str, tuple[str, ...]] = {
    "investment": (
        r"\binvest(?:or|ment|ed|ing|s)?\b", r"\bvaluation\b", r"\bterm ?sheet\b",
        r"\bacquisit\w*\b", r"\b(?:merge|merger)\b",
    ),
    "equity": (
        r"\bequity\b", r"\bstake\b", r"\bownership\b", r"\bshare(?:s|holder)? of\b",
        r"\b\d{1,3}\s*%\s*of\b", r"\b(?:acqui|buy)\b",
    ),
    "money": (
        r"\$\s?\d", r"(?:€|£)\s?\d", r"\d[\d,\.]*\s*(?:usd|gbp|eur|dollars?|euros?|pounds?)",
        r"\b(?:wire|transfer funds?|invoice|payment|paying)\b",
    ),
    "credentials": (
        r"\b(?:password|api\s?key|access token|secret|credential|ssh\s?key|private key)\b",
    ),
    "access": (
        r"\b(?:repo(?:sitory)? access|admin access|push access|merge access|"
        r"write access|grant(?:ing)? access|root access|superuser)\b",
    ),
    "contracts": (
        r"\bcontract\b", r"\bagreement\b", r"\bterms?\s+and\s+conditions\b", r"\bnda\b",
        r"\bindemn\w*\b", r"\bwarrant\w*\b", r"\bsigning\b", r"\bbinding\b",
    ),
    "legal": (
        r"\blegal\b", r"\bjurisdiction\b", r"\blitigation\b", r"\batto?rney\b", r"\blawsuit\b",
    ),
    "employment": (
        r"\b(?:job offer|salary|compensation|employment|hiring|hire|onboarding|contractor agreement)\b",
    ),
    "ip ownership": (
        r"\bintellectual property\b", r"\b(?:assign|copyright)\b (?:copyright|ownership)",
        r"\bpatent\b", r"\blicens\w*\b", r"\btransfer(?:ring)? (?:ownership|rights|ip)\b",
    ),
    "exclusivity": (
        r"\bexclusiv\w*\b", r"\bsole (?:distribution|rights|agent)\b",
        r"\bno other .{0,30}(?:partners?|vendors?)\b",
    ),
}

# Compound signals: no single word is decisive, but money PLUS an equity/deal
# shape is a strong, deterministic indicator of a binding proposal.
CONSEQUENTIAL_COMPOUND_SIGNALS: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        (r"\$\s?\d", r"(?:€|£)\s?\d", r"\d[\d,\.]*\s*(?:usd|gbp|eur|dollars?|euros?|pounds?)"),
        (
            r"\d{1,3}\s*%", r"\bequity\b", r"\bstake\b", r"\bownership\b",
            r"\binvest(?:or|ment|ed|ing)?\b", r"\bterm ?sheet\b", r"\bconvertible\b",
            r"\bsafe\b", r"\bfor \d{1,3}\s*%", r"\boffer(?:ed|ing)?\b",
        ),
        "investment",
    ),
)


# Explicit acts that bind the principal or disclose protected material.  These
# are scanned in *every* mode, including routine outreach, because merely asking
# about a funding program is allowed but agreeing to terms is not.
COMMITMENT_PHRASES = (
    "we commit", "we guarantee", "we promise", "we will pay", "we agree to pay",
    "our price is", "we will invest", "we will fund", "wire the funds",
    "transfer funds", "sign the contract", "enter into an agreement",
    "we accept the terms", "binding agreement", "here is the api key",
    "here is the password", "the password is", "the token is", "ssh key is",
    "we assign copyright", "transfer ownership", "grant exclusive",
    "exclusive license", "we offer you the position", "salary of",
    "we will deliver by", "service level agreement", "i commit", "i guarantee",
    "i promise", "i accept the terms", "i will sign",
)


class AuthorityPolicy:
    """Decides whether an action is autonomous or must be escalated."""

    def __init__(self, controls: ControlState | None = None) -> None:
        self._controls = controls or ControlState()

    def update_controls(self, controls: ControlState) -> None:
        self._controls = controls

    def evaluate(
        self,
        action: str,
        *,
        category: str = "",
        content: str = "",
        mode: str = "content",
    ) -> AuthorizationDecision:
        """Return the authority decision for an action.

        ``mode="content"`` scans the full text through the deterministic
        consequential pre-policy (money, equity, contracts, credentials, access,
        legal, employment, exclusivity).  ``mode="commitment"`` scans only
        explicit binding/disclosure phrases (used for outbound outreach, where
        asking about money or programs is permitted but committing is not).

        The baseline consequential categories are enforced in every case: human
        ``require_approval_categories`` configure extra categories, but no
        configuration can disable the baseline.
        """
        haystack = f"{action}\n{category}\n{content}".lower()

        if category and category.lower() in {
            c.lower() for c in self._controls.require_approval_categories
        }:
            return AuthorizationDecision(
                Authority.AWAITING_HUMAN_AUTHORIZATION,
                f"category '{category}' is set to require human approval",
                category=category,
            )

        commitment_matches = [p for p in COMMITMENT_PHRASES if p in haystack]
        if commitment_matches:
            return AuthorizationDecision(
                Authority.AWAITING_HUMAN_AUTHORIZATION,
                "message makes a binding commitment or discloses protected material",
                category=category,
                matched_terms=sorted(set(commitment_matches)),
            )

        if mode == "commitment":
            return AuthorizationDecision(
                Authority.AUTONOMOUS,
                "outreach asks about a program without committing the principal",
                category=category,
            )

        matched: list[str] = []
        matched_categories: list[str] = []
        for cat, patterns in CONSEQUENTIAL_SIGNALS.items():
            hits = [p for p in patterns if re.search(p, haystack)]
            if hits:
                matched.extend(hits)
                matched_categories.append(cat)
        for money_patterns, shape_patterns, deal_category in CONSEQUENTIAL_COMPOUND_SIGNALS:
            money_hits = [p for p in money_patterns if re.search(p, haystack)]
            shape_hits = [p for p in shape_patterns if re.search(p, haystack)]
            if money_hits and shape_hits:
                matched.extend(money_hits)
                matched.extend(shape_hits)
                matched_categories.append(deal_category)
        if matched:
            default_cat = matched_categories[0] if matched_categories else ""
            return AuthorizationDecision(
                Authority.AWAITING_HUMAN_AUTHORIZATION,
                "message touches consequential commitments: "
                + ", ".join(sorted(set(matched_categories))),
                category=default_cat,
                matched_terms=sorted(set(matched)),
            )

        return AuthorizationDecision(
            Authority.AUTONOMOUS,
            "low-risk communication within delegated authority",
            category=category,
        )


_UNTRUSTED_BLOCK_RE = re.compile(
    r"-*\s*BEGIN UNTRUSTED(?: CONTENT| BLOCK| DATA)?\s*-*\n?.+?"
    r"-*\s*END UNTRUSTED(?: CONTENT| BLOCK| DATA)?\s*-*",
    re.IGNORECASE | re.DOTALL,
)
# A sender may also declare an *unterminated* untrusted region: "everything
# below is data I want you to inspect". The declaration is the signal, so
# the remainder is quoted material. Still requires the explicit marker, so a
# bare first-person consequential request keeps full policy force.
_UNTRUSTED_OPEN_RE = re.compile(
    r"-*\s*BEGIN UNTRUSTED(?: CONTENT| BLOCK| DATA)?\s*-*\n?.*",
    re.IGNORECASE | re.DOTALL,
)
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def policy_text_excluding_quoted(text: str) -> str:
    """Text for the escalation decision with quoted hostile content removed.

    A sender quoting an attack (forwarded phishing, a pasted override block,
    ``>`` replies) is DISCUSSING it, not issuing it: the quoted words must
    not by themselves force escalation, or any quoted credential-word would
    denial-of-service legitimate security Q&A. The full text still reaches
    composition (answered conversationally, never executed), and genuine
    first-person requests keep full policy force.

    Untrusted regions are recognised with or without the ``---`` fences
    people actually type, and an unterminated one drops to the end of the
    message. Both forms still require an explicit ``BEGIN UNTRUSTED`` marker,
    so escalation is never skipped for an unmarked request.
    """
    cleaned = _UNTRUSTED_BLOCK_RE.sub(" ", text or "")
    cleaned = _UNTRUSTED_OPEN_RE.sub(" ", cleaned)
    cleaned = _FENCED_BLOCK_RE.sub(" ", cleaned)
    kept = [line for line in cleaned.splitlines()
            if not line.strip().startswith(">")]
    return "\n".join(kept)


# Intent classes a reply can fall into. Conversational/reversible intents are
# autonomous; the rest are escalated.
CONVERSATIONAL_INTENTS = {
    "question": "answered from verified project facts",
    "thanks": "acknowledgement",
    "scheduling": "proposing times",
    "documentation_request": "sharing public links",
    "intro_request": "routing to a public link",
    "interest": "expressing appreciation and offering next step",
    "decline": "acknowledging decision and closing respectfully",
}


def is_conversational(intent: str) -> bool:
    return intent in CONVERSATIONAL_INTENTS
