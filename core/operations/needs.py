"""
core/operations/needs.py

Evidence-backed need detection.

A :class:`RequirementRule` describes a signal that *should* or *should not* be
present in the observed project state.  When the observed reality violates the
expectation, the detector records a :class:`Need` whose evidence explains
exactly what was or was not found.  This is intentionally generic: funding,
collaborators, compute, validation, documentation, and any other need are all
expressed as data, never as bespoke branches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .models import Need, NeedStatus, ProjectState, utcnow
from .store import OperationsStore


@dataclass
class RequirementRule:
    category: str
    description: str
    probe: str                     # regex searched against project state text
    expect: str = "absent"         # "absent" -> need when missing, "present" -> need when found
    urgency: float = 0.5
    impact: float = 0.5
    rationale: str = ""

    def _matches(self, haystack: str) -> bool:
        return re.search(self.probe, haystack, flags=re.IGNORECASE) is not None


class NeedDetector:
    """Turns observed project state into deduplicated, evidence-backed needs."""

    def __init__(self, rules: Iterable[RequirementRule]) -> None:
        self.rules = list(rules)

    def _text(self, state: ProjectState) -> str:
        parts = [state.name, state.summary, *state.facts, *state.metadata.get("search_text", [])]
        return "\n".join(str(p) for p in parts if p)

    def detect(self, store: OperationsStore, state: ProjectState) -> list[Need]:
        """Create needs for unmet requirements; return only the newly-created ones."""
        haystack = self._text(state)
        created: list[Need] = []
        for rule in self.rules:
            found = rule._matches(haystack)
            triggered = (rule.expect == "absent" and not found) or (
                rule.expect == "present" and found
            )
            if not triggered:
                continue
            if store.find_need(rule.category, rule.description) is not None:
                continue
            evidence = [
                f"rule:{rule.category}:probe={rule.probe!r}:expect={rule.expect}",
                f"observed:{'match' if found else 'no match'} in project state",
            ]
            if rule.rationale:
                evidence.append(f"rationale:{rule.rationale}")
            evidence.extend(state.evidence[:5])
            need = Need(
                category=rule.category,
                description=rule.description,
                evidence=evidence,
                urgency=rule.urgency,
                impact=rule.impact,
            )
            store.add_need(need)
            created.append(need)
        return created

    @staticmethod
    def ensure_need(
        store: OperationsStore,
        *,
        category: str,
        description: str,
        evidence: list[str],
        urgency: float = 0.5,
        impact: float = 0.5,
    ) -> Need:
        """Idempotently record a need discovered outside the rule engine."""
        existing = store.find_need(category, description)
        if existing is not None:
            if existing.status is NeedStatus.DISMISSED:
                existing.status = NeedStatus.OPEN
            existing.evidence = list(dict.fromkeys([*existing.evidence, *evidence]))
            existing.urgency = max(existing.urgency, urgency)
            existing.impact = max(existing.impact, impact)
            store.update_need(existing)
            return existing
        need = Need(
            category=category,
            description=description,
            evidence=list(dict.fromkeys(evidence)),
            urgency=urgency,
            impact=impact,
        )
        store.add_need(need)
        return need
