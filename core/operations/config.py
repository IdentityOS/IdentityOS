"""
core/operations/config.py

Configuration for an operator identity.

An :class:`OperatorConfig` is the only place mission-specific data lives.  The
engine consumes it without knowing whether the operator is raising funds,
recruiting collaborators, or sourcing compute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .discovery import CandidateSource
from .needs import RequirementRule


@dataclass
class OperatorConfig:
    identity_id: str
    project_root: str = "."
    project_name: str = ""
    sender_name: str = ""
    sender_email: str = ""
    signature: str = ""
    transparency: str = ""
    purpose: str = "project collaboration"
    need_rules: list[RequirementRule] = field(default_factory=list)
    candidate_sources: list[CandidateSource] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=lambda: ["web.fetch"])
    pursue_threshold: float = 0.55
    hold_threshold: float = 0.4
    max_needs_per_tick: int = 5
    max_outreach_per_tick: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)
    poll_interval: float = 300.0
    adaptive_polling: bool = True

    def composer_defaults(self) -> dict[str, str]:
        return {
            "sender_name": self.sender_name,
            "project_name": self.project_name,
            "signature": self.signature,
            "transparency": self.transparency,
        }
