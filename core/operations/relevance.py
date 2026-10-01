"""
core/operations/relevance.py

Work-relevance assessment for unsolicited inbound mail.

Aster quarantines unknown senders by default. When the principal asks her to
also hear strangers out, this module decides — deterministically and with
recorded evidence — whether an unsolicited message is valid and related to
the principal's work (IdentityOS or otherwise):

- need-rule probes: the operator's own declared work domains, reused as
  matchers (a rule that fires on project state also recognizes mail about it);
- project vocabulary: distinctive terms from actually observed project facts;
- principal domains: explicit terms Arsène maintains himself (persisted on
  the operator controls, settable via ``aster override``), covering work
  outside IdentityOS.

A relevant verdict never sends anything by itself: the message rejoins the
normal trusted flow (intent classification, authority policy, budgets,
outbound mode), so opt-outs, sensitive content, and unapproved sends are
still governed exactly as before. Thread intruders, automation, bounces,
and spam patterns never reach this assessment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

MAX_SCAN_CHARS = 4000
MIN_BODY_CHARS = 12

_STOPWORDS = frozenset("""
a an the and or but if then else for with without within from to of in on at by
is are was were be been being have has had do does did will would could should
may might must can this that these those it its we you he she they them his her
our your their my me him us as so than too very just about into over after
before between through during per each other more most such no not only own
same also how what when where which who whom why i ii iii hello hi hey dear
there here today tomorrow yesterday please thanks thank regards best sincerely
email mail message sent using get got make made take took come came know
would like look forward hearing hope well kind etc vs via per re fw fwd
""".split())


@dataclass
class Relevance:
    relevant: bool
    matched_terms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def project_vocabulary(facts: Iterable[str], *, limit: int = 40) -> list[str]:
    """Distinctive project terms from observed fact statements.

    Deterministic: lowercase alphanumeric tokens of length >= 4, stopwords
    removed, ordered by frequency then alphabetically, capped.
    """
    counts: dict[str, int] = {}
    for fact in facts or []:
        for token in re.findall(r"[a-z0-9][a-z0-9\-]{3,24}", str(fact or "").lower()):
            token = token.strip("-")
            if len(token) < 4 or token in _STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [term for term, _ in ranked[: max(1, limit)]]


def assess_inbound_relevance(
    subject: str,
    body: str,
    *,
    need_rules: Optional[Iterable[Any]] = None,
    project_terms: Optional[Iterable[str]] = None,
    principal_domains: Optional[Iterable[str]] = None,
) -> Relevance:
    """Decide whether an unsolicited message is valid, work-related mail.

    Returns matched terms and human-readable reasons in every case so the
    decision is auditable in provenance. Never raises on odd input.
    """
    text = f"{subject or ''}\n{body or ''}".strip()
    if len((body or "").strip()) < MIN_BODY_CHARS:
        return Relevance(False, [], ["body too short to evaluate"])
    scanned = text[:MAX_SCAN_CHARS]
    lowered = scanned.lower()

    matched: list[str] = []
    reasons: list[str] = []

    for rule in need_rules or []:
        probe = getattr(rule, "probe", "") or ""
        category = getattr(rule, "category", "") or "work"
        if not probe:
            continue
        try:
            found = re.search(probe, scanned, re.IGNORECASE)
        except re.error:
            continue
        if found:
            matched.append(f"need:{category}")
            reasons.append(f"matches {category} work domain")

    for term in project_terms or []:
        term = str(term or "").strip().lower()
        if len(term) >= 4 and term in lowered and f"project:{term}" not in matched:
            matched.append(f"project:{term}")
            reasons.append(f"mentions project term '{term}'")

    for domain in principal_domains or []:
        domain = str(domain or "").strip().lower()
        if len(domain) >= 3 and domain in lowered:
            matched.append(f"principal:{domain}")
            reasons.append(f"matches principal work term '{domain}'")

    if not matched:
        return Relevance(False, [], ["no work-domain, project, or principal term matched"])
    return Relevance(True, matched[:8], reasons[:4])
