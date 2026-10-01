"""Serializable records for deterministic procedure reflexes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ReflexBinding:
    reflex_id: str
    identity_id: str
    procedure_id: str
    procedure_version: int
    trigger_template: str
    trigger_sha256: str
    procedure_sha256: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reflex_id": self.reflex_id,
            "identity_id": self.identity_id,
            "procedure_id": self.procedure_id,
            "procedure_version": self.procedure_version,
            "trigger_template": self.trigger_template,
            "trigger_sha256": self.trigger_sha256,
            "procedure_sha256": self.procedure_sha256,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReflexBinding":
        return cls(
            reflex_id=str(raw["reflex_id"]),
            identity_id=str(raw["identity_id"]),
            procedure_id=str(raw["procedure_id"]),
            procedure_version=int(raw["procedure_version"]),
            trigger_template=str(raw["trigger_template"]),
            trigger_sha256=str(raw["trigger_sha256"]),
            procedure_sha256=str(raw["procedure_sha256"]),
            created_at=str(raw["created_at"]),
        )


@dataclass
class ReflexRun:
    run_id: str
    reflex_id: str
    identity_id: str
    task_id: str
    procedure_id: str
    procedure_version: int
    expected_plan_sha256: str
    trigger_sha256: str
    status: str
    dispatched_at: str
    timings_ms: dict[str, float] = field(default_factory=dict)
    model_planning_calls: int = 0
    verified_at: Optional[str] = None
    verification: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "reflex_id": self.reflex_id,
            "identity_id": self.identity_id,
            "task_id": self.task_id,
            "procedure_id": self.procedure_id,
            "procedure_version": self.procedure_version,
            "expected_plan_sha256": self.expected_plan_sha256,
            "trigger_sha256": self.trigger_sha256,
            "status": self.status,
            "dispatched_at": self.dispatched_at,
            "timings_ms": self.timings_ms,
            "model_planning_calls": self.model_planning_calls,
            "verified_at": self.verified_at,
            "verification": self.verification,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReflexRun":
        return cls(
            run_id=str(raw["run_id"]),
            reflex_id=str(raw["reflex_id"]),
            identity_id=str(raw["identity_id"]),
            task_id=str(raw["task_id"]),
            procedure_id=str(raw["procedure_id"]),
            procedure_version=int(raw["procedure_version"]),
            expected_plan_sha256=str(raw["expected_plan_sha256"]),
            trigger_sha256=str(raw["trigger_sha256"]),
            status=str(raw["status"]),
            dispatched_at=str(raw["dispatched_at"]),
            timings_ms=dict(raw.get("timings_ms", {})),
            model_planning_calls=int(raw.get("model_planning_calls", 0)),
            verified_at=raw.get("verified_at"),
            verification=dict(raw.get("verification", {})),
        )
