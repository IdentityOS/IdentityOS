from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class AcquisitionMode(str, Enum):
    AUTOMATIC = "automatic"
    APPROVAL_REQUIRED = "approval_required"
    READ_ONLY = "read_only"
    ENTERPRISE = "enterprise"


class AcquisitionStatus(str, Enum):
    NEED_DETECTED = "need_detected"
    SEARCHING = "searching"
    CANDIDATES_FOUND = "candidates_found"
    TRUST_VERIFIED = "trust_verified"
    INSTALLING = "installing"
    INSTALLED = "installed"
    VALIDATING = "validating"
    VALIDATED = "validated"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class CapabilityNeed:
    capability_id: Optional[str] = None
    skill_keywords: List[str] = field(default_factory=list)
    confidence: float = 0.0
    source: str = ""
    original_request: str = ""
    suggested_capability_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "capability_id": self.capability_id,
            "skill_keywords": self.skill_keywords,
            "confidence": self.confidence,
            "source": self.source,
            "original_request": self.original_request,
            "suggested_capability_ids": self.suggested_capability_ids,
        }


@dataclass
class RegistryCandidate:
    cap_id: str
    name: str
    version: str
    author: str
    description: str
    skills: List[dict]
    permissions: Dict[str, bool]
    manifest_url: str
    dependencies: List[str] = field(default_factory=list)
    trust_score: float = 0.0
    relevance_score: float = 0.0
    behaviorally_verified: bool = False
    artifact_sha256: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "cap_id": self.cap_id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "skills": self.skills,
            "permissions": self.permissions,
            "manifest_url": self.manifest_url,
            "dependencies": self.dependencies,
            "trust_score": self.trust_score,
            "relevance_score": self.relevance_score,
            "behaviorally_verified": self.behaviorally_verified,
            "artifact_sha256": self.artifact_sha256,
        }


@dataclass
class AcquisitionRecord:
    need: CapabilityNeed = field(default_factory=CapabilityNeed)
    status: AcquisitionStatus = AcquisitionStatus.NEED_DETECTED
    candidates_found: List[RegistryCandidate] = field(default_factory=list)
    chosen_candidate: Optional[RegistryCandidate] = None
    trust_score: float = 0.0
    installation_success: bool = False
    validation_success: bool = False
    retry_success: bool = False
    performance_gain: float = 0.0
    duration_ms: float = 0.0
    error: Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    identity_id: str = ""
    mode: AcquisitionMode = AcquisitionMode.AUTOMATIC
    # Stable provenance for exactly-once reconciliation of durable Executive
    # tasks. Older inline Prometheus records leave this unset.
    source_task_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "need": self.need.to_dict(),
            "status": self.status.value,
            "candidates_found": [c.to_dict() for c in self.candidates_found],
            "chosen_candidate": self.chosen_candidate.to_dict() if self.chosen_candidate else None,
            "trust_score": self.trust_score,
            "installation_success": self.installation_success,
            "validation_success": self.validation_success,
            "retry_success": self.retry_success,
            "performance_gain": self.performance_gain,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "timestamp": self.timestamp,
            "identity_id": self.identity_id,
            "mode": self.mode.value,
            "source_task_id": self.source_task_id,
        }


@dataclass
class EvolutionResult:
    success: bool = False
    acquired: bool = False
    acquisition_record: Optional[AcquisitionRecord] = None
    retry_response: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class PrometheusConfig:
    mode: AcquisitionMode = AcquisitionMode.AUTOMATIC
    max_candidates: int = 5
    max_acquisitions_per_interaction: int = 1
    min_trust_score: float = 0.5
    max_duration_ms: float = 30000.0
    enable_pre_check: bool = True
    enable_post_check: bool = True
    enable_learning: bool = True
    registry_path: str = "registry/capabilities/index.json"
    storage_namespace: str = "prometheus"
