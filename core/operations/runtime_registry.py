"""
core/operations/runtime_registry.py

Process-local registry connecting a live :class:`OperationsEngine` to the
operations capability and CLI.  Mirrors the Executive runtime's pattern: an
engine is registered once per storage backend and reused.
"""

from __future__ import annotations

from typing import Any, Optional

from .engine import OperationsEngine

_ENGINES: dict[int, OperationsEngine] = {}


def register_engine(engine: OperationsEngine) -> OperationsEngine:
    _ENGINES[id(engine.storage)] = engine
    return engine


def get_engine_for(storage: Any, identity_id: Optional[str] = None) -> Optional[OperationsEngine]:
    engine = _ENGINES.get(id(storage))
    if engine is None:
        return None
    if identity_id and engine.config.identity_id != identity_id:
        return None
    return engine


def clear_engines() -> None:
    _ENGINES.clear()
