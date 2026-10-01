"""Evidence classifications, never inferred from a user's requested workaround."""

from enum import Enum


class GapKind(str, Enum):
    CAPABILITY_GAP = "CAPABILITY_GAP"
    AUTHORITY_GAP = "AUTHORITY_GAP"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    BUG = "BUG"
    EXTERNAL_LIMITATION = "EXTERNAL_LIMITATION"
    UNKNOWN = "UNKNOWN"


ERROR_KINDS = {
    "permission_denied": GapKind.AUTHORITY_GAP,
    "policy_denied": GapKind.AUTHORITY_GAP,
    "skill_not_found": GapKind.CAPABILITY_GAP,
    "provider_unavailable": GapKind.PROVIDER_UNAVAILABLE,
    "timeout": GapKind.PROVIDER_UNAVAILABLE,
    "dependency_unavailable": GapKind.DEPENDENCY_UNAVAILABLE,
    "configuration_error": GapKind.CONFIGURATION_ERROR,
    "test_failed": GapKind.BUG,
    "quota_exceeded": GapKind.EXTERNAL_LIMITATION,
    "rate_limit": GapKind.EXTERNAL_LIMITATION,
}


def classify_result(result):
    """Accept an actual SkillResult, not model-supplied labels."""
    if result.success:
        return None
    return ERROR_KINDS.get((result.error or {}).get("type"), GapKind.UNKNOWN)
