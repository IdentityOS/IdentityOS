from __future__ import annotations

from .base import Capability, Skill
from .proxy import CapabilityProxy
from .registry import CapabilityRegistry
from .result import CapabilityResult, EvidenceOrigin, Fact
from .evidence import EvidenceManager, EvidenceReport

# Import built-in capabilities so their @register decorators fire
from . import github  # noqa: F401
from . import weather  # noqa: F401
from . import calc  # noqa: F401
from . import datetime  # noqa: F401
from . import web  # noqa: F401
from . import browser  # noqa: F401
from . import filesystem  # noqa: F401
from . import text  # noqa: F401
from . import system_info  # noqa: F401
from . import daedalus  # noqa: F401
from . import file_tools  # noqa: F401
from . import registry_manager  # noqa: F401
from . import skill_validator  # noqa: F401
from . import task_planner  # noqa: F401
from . import command_exec  # noqa: F401
from . import executive  # noqa: F401
from . import worldmonitor  # noqa: F401
from . import procedure_learning  # noqa: F401
from . import reflex  # noqa: F401
from . import embodiment  # noqa: F401

__all__ = [
    "Capability",
    "Skill",
    "CapabilityProxy",
    "CapabilityRegistry",
    "CapabilityResult",
    "EvidenceOrigin",
    "Fact",
    "EvidenceManager",
    "EvidenceReport",
]
