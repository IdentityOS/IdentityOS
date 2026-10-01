from __future__ import annotations

import re
from typing import Dict, List, Optional

from core.prometheus.models import CapabilityNeed

# Mapping of capability IDs to keyword patterns that indicate a need for them
_CAPABILITY_KEYWORDS: Dict[str, List[str]] = {
    "github": [
        "github", "repository", "repo", "pull request", "pr", "commit",
        "branch", "issue", "release", "star", "fork", "git hub",
    ],
    "weather": [
        "weather", "temperature", "forecast", "rain", "humidity",
        "wind", "climate", "sunny", "cloudy",
    ],
    "calc": [
        "calculate", "computation", "math", "expression", "evaluate",
        "plus", "minus", "times", "divided by", "square root",
    ],
    "web": [
        "search", "browse", "website", "web page", "url", "http",
        "fetch", "scrape", "crawl", "look up",
    ],
    "datetime": [
        "current time", "what time", "what date", "what day",
        "today", "now", "timestamp", "time zone",
    ],
    "filesystem": [
        "file", "read file", "write file", "directory", "folder",
        "list files", "save to", "load from",
    ],
    "text": [
        "summarize", "translate", "format", "parse", "extract",
        "convert text", "transform",
    ],
    "system_info": [
        "system", "os", "memory", "cpu", "disk", "hardware",
        "platform", "environment",
    ],
    "architecture_analysis": [
        "architecture", "architectural", "module structure", "layer separation",
        "coupling", "codebase structure", "package dependency",
    ],
    "code_review": [
        "review", "code review", "pull request review", "pr review",
        "merge readiness", "change review",
    ],
    "changelog_gen": [
        "changelog", "release notes", "what changed", "version history",
        "git log", "commit history",
    ],
    "repo_health": [
        "repository health", "repo health", "code quality", "test health",
        "benchmark trend", "project health",
    ],
    "dependency_graph": [
        "dependency graph", "dependency map", "module dependency",
        "circular dependency", "import graph",
    ],
}

# Patterns in LLM responses that indicate a missing capability
_RESPONSE_GAP_PATTERNS = [
    re.compile(r"(?:I don'?t|I do not|I lack|I'm? not able to).{0,50}(?:capability|skill|access|tool)", re.IGNORECASE),
    re.compile(r"(?:don'?t have|do not have|missing|not installed|not available).{0,30}(?:capability|skill|access)", re.IGNORECASE),
    re.compile(r"(?:cannot|cannot|can'?t|unable to).{0,30}(?:without|need|require)", re.IGNORECASE),
    re.compile(r"(?:capability|skill|tool|plugin|integration).{0,20}(?:not|missing|lacking|unavailable)", re.IGNORECASE),
    re.compile(r"(?:you need to install|please install|install the|try installing)", re.IGNORECASE),
]

_ABILITY_REQUEST_PATTERNS = [
    re.compile(
        r"\b(?:i\s+)?(?:would\s+like|want|need)\s+(?:you\s+)?to\s+be\s+able\s+to\s+([^.!?]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:give|add)\s+(?:you|the\s+identity)\s+(?:an?\s+)?(?:ability|capability)\s+to\s+([^.!?]+)",
        re.IGNORECASE,
    ),
]

_ABILITY_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "for", "from", "i", "in", "it",
    "me", "my", "of", "on", "or", "the", "to", "with", "you", "your",
}


def infer_requested_capability_id(text: str) -> Optional[str]:
    """Infer a stable slug only from explicit requests for a missing ability.

    This is capability-agnostic: it does not map domains such as speech or OCR
    to special implementations.  The behavioral goal remains the full user
    request and Skill Forge is responsible for proving the resulting behavior.
    """
    for pattern in _ABILITY_REQUEST_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        words = [
            word
            for word in re.findall(r"[a-z0-9]+", match.group(1).lower())
            if word not in _ABILITY_STOPWORDS
        ]
        if not words:
            return None
        slug = "_".join(words[:5])[:63].rstrip("_")
        if len(slug) >= 2 and slug[0].isalpha():
            return slug
    return None


def detect_need_from_input(user_input: str) -> Optional[CapabilityNeed]:
    input_lower = user_input.lower()
    matched_keywords: List[str] = []
    suggested_cap_ids: List[str] = []

    for cap_id, keywords in _CAPABILITY_KEYWORDS.items():
        for kw in keywords:
            if kw in input_lower:
                matched_keywords.append(kw)
                if cap_id not in suggested_cap_ids:
                    suggested_cap_ids.append(cap_id)

    if not suggested_cap_ids:
        inferred = infer_requested_capability_id(user_input)
        if inferred is None:
            return None
        suggested_cap_ids.append(inferred)
        matched_keywords.append(inferred.replace("_", " "))

    return CapabilityNeed(
        skill_keywords=matched_keywords,
        confidence=min(1.0, len(matched_keywords) * 0.25),
        source="user_input",
        original_request=user_input,
        suggested_capability_ids=suggested_cap_ids,
    )


def detect_need_from_response(
    response: str,
    original_request: str,
) -> Optional[CapabilityNeed]:
    response_lower = response.lower()

    gap_detected = any(p.search(response_lower) for p in _RESPONSE_GAP_PATTERNS)
    if not gap_detected:
        return None

    matched_keywords: List[str] = []
    suggested_cap_ids: List[str] = []

    for cap_id, keywords in _CAPABILITY_KEYWORDS.items():
        for kw in keywords:
            if kw in response_lower or kw in original_request.lower():
                matched_keywords.append(kw)
                if cap_id not in suggested_cap_ids:
                    suggested_cap_ids.append(cap_id)

    if not suggested_cap_ids:
        suggest_from_request = detect_need_from_input(original_request)
        if suggest_from_request:
            return suggest_from_request
        inferred = infer_requested_capability_id(original_request)
        if inferred is None:
            return None
        suggested_cap_ids.append(inferred)
        matched_keywords.append(inferred.replace("_", " "))

    return CapabilityNeed(
        skill_keywords=matched_keywords,
        confidence=0.8,
        source="response",
        original_request=original_request,
        suggested_capability_ids=suggested_cap_ids,
    )
