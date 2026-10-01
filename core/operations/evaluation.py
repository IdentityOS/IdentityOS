"""
core/operations/evaluation.py

Target evaluation and duplicate-contact prevention.

Evaluation produces an evidence-backed :class:`Evaluation` rather than a magic
score: every factor is derived from something observable (whether the target's
work overlaps the need, whether we have a contactable address, whether we have
already contacted them).  The duplicate-contact policy is the single gate that
prevents a target from ever receiving more than one cold introduction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Evaluation, Need, Opportunity, Relationship
from .store import OperationsStore, _norm


@dataclass
class ContactDecision:
    allowed: bool
    reason: str
    code: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


class TargetEvaluator:
    """Derives an explainable evaluation from observable opportunity facts."""

    def __init__(self, *, pursue_threshold: float = 0.55, hold_threshold: float = 0.4) -> None:
        self.pursue_threshold = pursue_threshold
        self.hold_threshold = hold_threshold

    def evaluate(self, opportunity: Opportunity, need: Need) -> Evaluation:
        factors: dict[str, float] = {}
        evidence: list[str] = []
        risks: list[str] = []

        # Need alignment: shared category / explicit link to the need.
        alignment = 0.4
        if opportunity.need_id == need.id:
            alignment += 0.2
        if _norm(opportunity.category) and _norm(opportunity.category) == _norm(need.category):
            alignment += 0.25
        factors["need_alignment"] = min(1.0, alignment)
        evidence.append(f"opportunity.need_id={opportunity.need_id or 'unset'} need.id={need.id}")

        # Evidence quality: how many independent facts support the target.
        evidence_score = min(1.0, len(opportunity.evidence) * 0.25)
        factors["evidence_quality"] = evidence_score
        evidence.extend(opportunity.evidence[:5])

        # Mission fit: overlap between the target's work and the project need.
        overlap = 0.0
        need_terms = set(_norm(need.description).split())
        for work in opportunity.relevant_work:
            work_terms = set(_norm(work).split())
            if need_terms and work_terms:
                overlap = max(overlap, len(need_terms & work_terms) / len(need_terms))
        if opportunity.fit_reason:
            overlap = max(overlap, 0.5)
        factors["mission_fit"] = min(1.0, overlap)
        if opportunity.relevant_work:
            evidence.append(f"relevant_work={'; '.join(opportunity.relevant_work[:3])}")

        # Reachability: can we actually send a permitted, individual message?
        reachable = 0.0
        if opportunity.contact_email:
            reachable += 0.7
        if opportunity.contact_url:
            reachable += 0.3
        factors["reachability"] = min(1.0, reachable)
        if not opportunity.contact_email and not opportunity.contact_url:
            risks.append("no contact channel discovered")

        # Risk (higher is safer).
        risk = 0.7
        if opportunity.risks:
            risk -= 0.15 * min(len(opportunity.risks), 3)
            risks.extend(opportunity.risks)
        factors["risk"] = max(0.1, risk)

        # Source confidence: how confident the discovery process was. A zero
        # confidence candidate carries no evidentiary weight on its own; source
        # confidence only *adds* when it is positive, so existing zero-confidence
        # flow is unchanged.
        source_confidence = min(1.0, max(0.0, float(opportunity.confidence or 0.0)))
        if source_confidence > 0:
            factors["source_confidence"] = round(source_confidence, 4)
            evidence.append(f"source_confidence={source_confidence:.2f}")

        result = Evaluation(
            opportunity_id=opportunity.id,
            factors=factors,
            evidence=evidence,
            rationale="",
            recommendation="hold",
            confidence=round(sum(factors.values()) / len(factors), 4),
        )
        score = result.score
        if score >= self.pursue_threshold:
            result.recommendation = "pursue"
        elif score >= self.hold_threshold:
            result.recommendation = "hold"
        else:
            result.recommendation = "reject"

        # A zero-confidence candidate is never pursued autonomously unless it was
        # explicitly marked as a manual test candidate (test_candidate=true).
        if float(opportunity.confidence or 0.0) <= 0 and not opportunity.test_candidate:
            result.recommendation = "hold"
            result.rationale = (
                f"{result.rationale} | source_confidence_zero: candidate has no assigned "
                "confidence and was not marked test_candidate=true; not pursued"
            )

        rationale_parts = [
            f"need '{need.description}' (priority {need.priority})",
            f"target '{opportunity.target_name or opportunity.organization}'",
            f"score {score} from " + ", ".join(f"{k}={v:.2f}" for k, v in factors.items()),
        ]
        if risks:
            rationale_parts.append("risks: " + "; ".join(risks))
        result.rationale = " | ".join(rationale_parts)
        return result


class DuplicateContactPolicy:
    """Guarantees a cold introduction is sent at most once per target."""

    def __init__(self, store: OperationsStore) -> None:
        self._store = store

    def evaluate(self, opportunity: Opportunity) -> ContactDecision:
        controls = self._store.controls()

        email = _norm(opportunity.contact_email)
        org = _norm(opportunity.organization)

        for blocked in controls.never_contact:
            if _norm(blocked) in {email, org, _norm(opportunity.target_name)}:
                return ContactDecision(False, f"target is on the never-contact list: {blocked}", "never_contact")

        existing = self._store.find_relationship_by_email(opportunity.contact_email)
        if existing is None:
            for rel in self._store.list_relationships():
                if org and _norm(rel.organization) == org and _norm(rel.email) == email:
                    existing = rel
                    break

        if existing is not None:
            if existing.opted_out:
                return ContactDecision(False, "target has opted out", "opted_out")
            if existing.status.value in ("outreach_sent", "engaged", "declined", "opted_out", "awaiting_human_authorization"):
                return ContactDecision(False, "target already has an open or closed relationship", "already_contacted")

        if self._store.has_contacted(opportunity.contact_email, opportunity.organization):
            return ContactDecision(False, "target was already contacted previously", "already_contacted")

        return ContactDecision(True, "no prior contact recorded", "ok")

    def note_relationship(self, relationship: Optional[Relationship]) -> None:
        """Hook for callers that want to assert a relationship already exists."""
        if relationship is not None:
            self._store.update_relationship(relationship)
