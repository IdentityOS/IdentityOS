"""Serializable Skill Forge contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True)
class AcceptanceCase:
    """An independently supplied behavioral example.

    ``expected`` is matched recursively as a subset of the observed data.  It
    deliberately describes output behavior without importing or inspecting
    implementation details.
    """

    name: str
    skill: str
    params: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    expect_success: bool = True
    grants: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "skill": self.skill,
            "params": self.params,
            "expected": self.expected,
            "expect_success": self.expect_success,
            "grants": list(self.grants),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AcceptanceCase":
        if not isinstance(raw, dict):
            raise ValueError("acceptance case must be an object")
        name = str(raw.get("name", "")).strip()
        skill = str(raw.get("skill", "")).strip()
        params = raw.get("params", {})
        expected = raw.get("expected", {})
        grants = raw.get("grants", [])
        if not name or not skill:
            raise ValueError("acceptance cases require non-empty name and skill")
        if not isinstance(params, dict) or not isinstance(expected, dict):
            raise ValueError("acceptance params and expected values must be objects")
        if not isinstance(grants, list) or not all(isinstance(item, str) for item in grants):
            raise ValueError("acceptance grants must be a list of strings")
        return cls(
            name=name,
            skill=skill,
            params=dict(params),
            expected=dict(expected),
            expect_success=bool(raw.get("expect_success", True)),
            grants=tuple(grants),
        )


@dataclass(frozen=True)
class ForgeRequest:
    capability_id: str
    goal: str
    identity_id: str
    name: str = ""
    description: str = ""
    allowed_permissions: tuple[str, ...] = ()
    allowed_dependencies: tuple[str, ...] = ()
    acceptance_cases: tuple[AcceptanceCase, ...] = ()

    def __post_init__(self) -> None:
        if not _CAPABILITY_ID.fullmatch(self.capability_id):
            raise ValueError(
                "capability_id must start with a letter and contain only "
                "lowercase letters, digits, and underscores"
            )
        if not self.goal.strip():
            raise ValueError("forge goal must not be empty")


@dataclass(frozen=True)
class ForgeProposal:
    source: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ForgeResult:
    capability_id: str
    artifact_path: str
    artifact_sha256: str
    source_path: str
    manifest: dict[str, Any]
    acceptance_cases: tuple[AcceptanceCase, ...]
    isolation_report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "source_path": self.source_path,
            "manifest": self.manifest,
            "acceptance_cases": [case.to_dict() for case in self.acceptance_cases],
            "isolation_report": self.isolation_report,
        }
