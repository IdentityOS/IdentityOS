"""Truthful, portable capability generation.

Skill Forge treats generated source as untrusted until an independent runner
has executed its declared behavioral contract.  Only the exact tested bytes
are packaged and eligible for installation.
"""

from .models import AcceptanceCase, ForgeProposal, ForgeRequest, ForgeResult
from .service import (
    CapabilityAuthor,
    CapabilityTestDesigner,
    ModelCapabilityAuthor,
    ModelCapabilityTestDesigner,
    SkillForge,
    SkillForgeError,
)

__all__ = [
    "AcceptanceCase",
    "CapabilityAuthor",
    "CapabilityTestDesigner",
    "ForgeProposal",
    "ForgeRequest",
    "ForgeResult",
    "ModelCapabilityAuthor",
    "ModelCapabilityTestDesigner",
    "SkillForge",
    "SkillForgeError",
]
