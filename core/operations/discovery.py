"""
core/operations/discovery.py

Opportunity discovery.

A :class:`CandidateSource` turns a need into zero or more raw candidates.
Sources are pluggable: a static list for a curated directory, a callable that
queries an API, or a web-search adapter.  The discoverer normalizes candidates
into evidence-carrying :class:`Opportunity` records and never re-proposes a
target that is already known or already contacted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .models import Need, Opportunity, OpportunityStatus
from .store import OperationsStore, _norm


@dataclass
class Candidate:
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
    risks: list[str] = field(default_factory=list)
    confidence: float = 0.0
    test_candidate: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        return cls(
            target_name=str(data.get("target_name", data.get("name", ""))),
            organization=str(data.get("organization", data.get("org", ""))),
            contact_email=str(data.get("contact_email", data.get("email", ""))),
            contact_url=str(data.get("contact_url", data.get("url", ""))),
            channel=str(data.get("channel", "email")),
            category=str(data.get("category", "")),
            relevant_work=list(data.get("relevant_work", [])),
            evidence=list(data.get("evidence", [])),
            fit_reason=str(data.get("fit_reason", "")),
            value_proposition=str(data.get("value_proposition", "")),
            potential_ask=str(data.get("potential_ask", "")),
            risks=list(data.get("risks", [])),
            confidence=float(data.get("confidence", 0.0)),
            test_candidate=bool(data.get("test_candidate", False)),
        )


class CandidateSource:
    """Base class for anything that can propose candidates for a need."""

    name = "candidate-source"
    required_skill: Optional[str] = None

    def search(self, need: Need) -> list[Candidate]:  # pragma: no cover - interface
        raise NotImplementedError


class StaticCandidateSource(CandidateSource):
    """A fixed, curated set of candidates (supplied by config or a test)."""

    def __init__(self, candidates: Iterable[Candidate | dict], name: str = "static") -> None:
        self.name = name
        self._candidates = [
            c if isinstance(c, Candidate) else Candidate.from_dict(c) for c in candidates
        ]

    def search(self, need: Need) -> list[Candidate]:
        return list(self._candidates)


class CallableCandidateSource(CandidateSource):
    """Delegates to a caller-supplied function ``fn(need) -> candidates``."""

    def __init__(self, fn: Callable[[Need], Iterable[Candidate | dict]], name: str = "callable") -> None:
        self.name = name
        self._fn = fn

    def search(self, need: Need) -> list[Candidate]:
        raw = self._fn(need) or []
        return [c if isinstance(c, Candidate) else Candidate.from_dict(c) for c in raw]


class SearchCandidateSource(CandidateSource):
    """Builds candidates from web search results using a search callable.

    The callable receives a query string and returns a list of result dicts with
    at least ``title``/``url``/``snippet``.  Results become *uncontacted*
    candidates: they still require a reachable contact before outreach.
    """

    name = "web-search"

    def __init__(
        self,
        search_fn: Callable[[str], list[dict]],
        *,
        query_template: str = "{category} program accepting applications {need}",
        required_skill: str = "web.search",
    ) -> None:
        self._search_fn = search_fn
        self._query_template = query_template
        self.required_skill = required_skill

    def search(self, need: Need) -> list[Candidate]:
        query = self._query_template.format(category=need.category, need=need.description)
        results = self._search_fn(query) or []
        candidates: list[Candidate] = []
        for result in results:
            url = str(result.get("url", ""))
            title = str(result.get("title", ""))
            snippet = str(result.get("snippet", result.get("snippet_text", "")))
            if not (title or url):
                continue
            candidates.append(
                Candidate(
                    target_name=title or url,
                    organization=title,
                    contact_url=url,
                    category=need.category,
                    relevant_work=[snippet] if snippet else [],
                    evidence=[f"search:{query}", f"result:{title}", f"url:{url}"],
                    fit_reason=snippet,
                    confidence=0.25,
                )
            )
        return candidates


def candidate_from_mapping(data: dict[str, Any]) -> Candidate:
    return Candidate.from_dict(data)


class OpportunityDiscoverer:
    """Normalizes candidates from sources into deduplicated opportunities."""

    def __init__(self, sources: Iterable[CandidateSource]) -> None:
        self.sources = list(sources)

    def discover(self, store: OperationsStore, need: Need) -> list[Opportunity]:
        created: list[Opportunity] = []
        seen: set[tuple[str, str]] = set()
        for source in self.sources:
            for candidate in source.search(need):
                if not (candidate.target_name or candidate.organization):
                    continue
                key = (_norm(candidate.target_name), _norm(candidate.organization))
                if key in seen:
                    continue
                seen.add(key)
                if store.find_opportunity(candidate.target_name, candidate.organization) is not None:
                    continue
                # Never propose someone already contacted.
                if candidate.contact_email and store.has_contacted(candidate.contact_email, candidate.organization):
                    continue
                evidence = list(dict.fromkeys([*candidate.evidence, f"source:{source.name}"]))
                opportunity = Opportunity(
                    need_id=need.id,
                    target_name=candidate.target_name,
                    organization=candidate.organization,
                    contact_email=candidate.contact_email,
                    contact_url=candidate.contact_url,
                    channel=candidate.channel,
                    category=candidate.category or need.category,
                    relevant_work=list(candidate.relevant_work),
                    evidence=evidence,
                    fit_reason=candidate.fit_reason,
                    value_proposition=candidate.value_proposition,
                    potential_ask=candidate.potential_ask,
                    confidence=candidate.confidence,
                    test_candidate=candidate.test_candidate,
                    risks=list(candidate.risks),
                    status=OpportunityStatus.DISCOVERED,
                )
                store.add_opportunity(opportunity)
                created.append(opportunity)
        return created
