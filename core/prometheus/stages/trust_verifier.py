from __future__ import annotations

from typing import Dict, List, Optional, Set

from core.prometheus.models import AcquisitionMode, RegistryCandidate

_TRUSTED_AUTHORS: Set[str] = {"IdentityOS", "identityos", "lacebx"}
_BLOCKED_AUTHORS: Set[str] = set()
_MIN_TRUST_SCORE: float = 0.3


def verify_trust(
    candidate: RegistryCandidate,
    mode: AcquisitionMode = AcquisitionMode.AUTOMATIC,
    min_score: float = 0.5,
) -> float:
    score = 0.0

    if candidate.author in _TRUSTED_AUTHORS:
        score += 0.5
    elif candidate.author not in _BLOCKED_AUTHORS:
        score += 0.2

    if candidate.version:
        try:
            parts = candidate.version.split(".")
            major = int(parts[0]) if parts else 0
            if major >= 1:
                score += 0.15
            elif major >= 0 and len(parts) > 1:
                minor = int(parts[1]) if len(parts) > 1 else 0
                if minor >= 5:
                    score += 0.1
        except (ValueError, IndexError):
            pass

    if candidate.permissions.get("network"):
        score -= 0.05
    if candidate.permissions.get("filesystem"):
        score -= 0.05

    num_skills = len(candidate.skills)
    if num_skills >= 2:
        score += 0.1
    if num_skills >= 5:
        score += 0.05

    # A digest alone is not trust. Give limited credit only when the manifest
    # binds it to independently executed behavioral cases.
    if candidate.behaviorally_verified and candidate.artifact_sha256:
        score += 0.2

    score = max(0.0, min(1.0, score))
    candidate.trust_score = round(score, 3)
    return score


def is_trusted(
    candidate: RegistryCandidate,
    mode: AcquisitionMode,
    min_score: float = 0.5,
) -> bool:
    score = verify_trust(candidate, mode, min_score)
    if mode == AcquisitionMode.AUTOMATIC:
        return score >= min_score
    if mode == AcquisitionMode.APPROVAL_REQUIRED:
        return score >= _MIN_TRUST_SCORE
    if mode == AcquisitionMode.READ_ONLY:
        return False
    if mode == AcquisitionMode.ENTERPRISE:
        return score >= min_score + 0.2
    return score >= min_score
