from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Set

from core.prometheus.models import CapabilityNeed, RegistryCandidate


def _load_registry_index(registry_path: str = "registry/capabilities/index.json") -> List[dict]:
    path = Path(registry_path)
    if not path.exists():
        alt_path = Path(__file__).resolve().parent.parent.parent.parent / registry_path
        path = alt_path

    if not path.exists():
        return []

    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("capabilities", [])
    except (json.JSONDecodeError, IOError):
        return []


def _load_manifest(cap_id: str, base_path: str = "registry/capabilities") -> Optional[dict]:
    path = Path(base_path) / cap_id / "manifest.json"
    if not path.exists():
        alt_path = Path(__file__).resolve().parent.parent.parent.parent / base_path / cap_id / "manifest.json"
        path = alt_path
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _score_relevance(candidate: dict, need: CapabilityNeed) -> float:
    score = 0.0
    name = (candidate.get("name") or "").lower()
    desc = (candidate.get("description") or "").lower()
    cap_id = (candidate.get("id") or "").lower()
    skills = candidate.get("skills", [])

    for keyword in need.suggested_capability_ids:
        if keyword.lower() in cap_id:
            score += 0.4
        if keyword.lower() in name:
            score += 0.3

    for kw in need.skill_keywords:
        kw_lower = kw.lower()
        if kw_lower in desc:
            score += 0.2
        if kw_lower in cap_id:
            score += 0.3
        for skill in skills:
            sname = (skill.get("name") or "").lower()
            sdesc = (skill.get("description") or "").lower()
            if kw_lower in sname or kw_lower in sdesc:
                score += 0.15

    return min(1.0, score)


_cached_registry: Optional[List[dict]] = None


def search_registry(
    need: CapabilityNeed,
    max_candidates: int = 5,
    registry_path: str = "registry/capabilities/index.json",
    installed_ids: Optional[Set[str]] = None,
) -> List[RegistryCandidate]:
    global _cached_registry
    if _cached_registry is None:
        _cached_registry = _load_registry_index(registry_path)

    entries = _cached_registry or []
    installed_ids = installed_ids or set()
    candidates: List[RegistryCandidate] = []

    for entry in entries:
        cap_id = entry.get("id", "")
        if cap_id in installed_ids:
            continue

        manifest = _load_manifest(cap_id) or entry
        relevance = _score_relevance(manifest, need)

        if relevance > 0:
            candidate = RegistryCandidate(
                cap_id=cap_id,
                name=manifest.get("name", cap_id),
                version=manifest.get("version", "0.0.0"),
                author=manifest.get("author", "unknown"),
                description=manifest.get("description", ""),
                skills=manifest.get("skills", []),
                permissions=manifest.get("permissions", {}),
                dependencies=manifest.get("dependencies", []),
                manifest_url=entry.get("url", ""),
                relevance_score=round(relevance, 3),
                behaviorally_verified=bool(manifest.get("behaviorally_verified")),
                artifact_sha256=manifest.get("artifact_sha256"),
            )
            candidates.append(candidate)

    candidates.sort(key=lambda c: c.relevance_score, reverse=True)
    return candidates[:max_candidates]


def clear_cache() -> None:
    global _cached_registry
    _cached_registry = None
