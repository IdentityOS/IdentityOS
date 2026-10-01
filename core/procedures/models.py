"""Serializable procedure-learning records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class HeldOutExample:
    example_id: str
    bindings: dict[str, Any]
    expected_steps: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "bindings": self.bindings,
            "expected_steps": list(self.expected_steps),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HeldOutExample":
        example_id = str(raw.get("example_id", "")).strip()
        bindings = raw.get("bindings", {})
        expected = raw.get("expected_steps", [])
        if not example_id or not isinstance(bindings, dict) or not isinstance(expected, list):
            raise ValueError("held-out examples require id, bindings object, and expected_steps list")
        return cls(example_id, dict(bindings), tuple(dict(step) for step in expected))


@dataclass
class ProcedureVersion:
    version: int
    source_task_id: str
    template: list[dict[str, Any]]
    parameter_schema: dict[str, Any]
    template_sha256: str
    suite_id: str
    suite_sha256: str
    held_out_score: float
    evaluations: list[dict[str, Any]]
    status: str
    created_at: str
    rejection_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_task_id": self.source_task_id,
            "template": self.template,
            "parameter_schema": self.parameter_schema,
            "template_sha256": self.template_sha256,
            "suite_id": self.suite_id,
            "suite_sha256": self.suite_sha256,
            "held_out_score": self.held_out_score,
            "evaluations": self.evaluations,
            "status": self.status,
            "created_at": self.created_at,
            "rejection_reason": self.rejection_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProcedureVersion":
        return cls(
            version=int(raw["version"]),
            source_task_id=str(raw["source_task_id"]),
            template=list(raw.get("template", [])),
            parameter_schema=dict(raw.get("parameter_schema", {})),
            template_sha256=str(raw.get("template_sha256", "")),
            suite_id=str(raw.get("suite_id", "")),
            suite_sha256=str(raw.get("suite_sha256", "")),
            held_out_score=float(raw.get("held_out_score", 0.0)),
            evaluations=list(raw.get("evaluations", [])),
            status=str(raw.get("status", "rejected")),
            created_at=str(raw.get("created_at", "")),
            rejection_reason=raw.get("rejection_reason"),
        )


@dataclass
class Procedure:
    procedure_id: str
    identity_id: str
    name: str
    versions: list[ProcedureVersion] = field(default_factory=list)
    champion_version: Optional[int] = None

    @property
    def champion(self) -> Optional[ProcedureVersion]:
        return next(
            (version for version in self.versions if version.version == self.champion_version),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "procedure_id": self.procedure_id,
            "identity_id": self.identity_id,
            "name": self.name,
            "versions": [version.to_dict() for version in self.versions],
            "champion_version": self.champion_version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Procedure":
        return cls(
            procedure_id=str(raw["procedure_id"]),
            identity_id=str(raw["identity_id"]),
            name=str(raw.get("name", raw["procedure_id"])),
            versions=[ProcedureVersion.from_dict(item) for item in raw.get("versions", [])],
            champion_version=raw.get("champion_version"),
        )
