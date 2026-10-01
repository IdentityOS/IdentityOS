"""Durable storage for reflex definitions, executions, and measurements."""

from __future__ import annotations

from typing import Any, Optional

from .models import ReflexBinding, ReflexRun


class ReflexStore:
    NAMESPACE = "reflex.state"

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def _load(self, identity_id: str) -> dict[str, Any]:
        return self.storage.load(identity_id, self.NAMESPACE) or {
            "schema_version": 1,
            "bindings": {},
            "runs": {},
            "benchmarks": [],
        }

    def binding(self, identity_id: str, reflex_id: str) -> Optional[ReflexBinding]:
        raw = self._load(identity_id)
        item = (raw.get("bindings") or {}).get(reflex_id)
        return ReflexBinding.from_dict(item) if isinstance(item, dict) else None

    def bindings(self, identity_id: str) -> list[ReflexBinding]:
        return [
            ReflexBinding.from_dict(item)
            for item in (self._load(identity_id).get("bindings") or {}).values()
            if isinstance(item, dict)
        ]

    def save_binding(self, binding: ReflexBinding) -> None:
        raw = self._load(binding.identity_id)
        bindings = dict(raw.get("bindings") or {})
        bindings[binding.reflex_id] = binding.to_dict()
        raw["bindings"] = bindings
        self.storage.save(binding.identity_id, self.NAMESPACE, raw)

    def run(self, identity_id: str, run_id: str) -> Optional[ReflexRun]:
        item = (self._load(identity_id).get("runs") or {}).get(run_id)
        return ReflexRun.from_dict(item) if isinstance(item, dict) else None

    def save_run(self, run: ReflexRun) -> None:
        raw = self._load(run.identity_id)
        runs = dict(raw.get("runs") or {})
        runs[run.run_id] = run.to_dict()
        raw["runs"] = runs
        self.storage.save(run.identity_id, self.NAMESPACE, raw)

    def save_benchmark(self, identity_id: str, report: dict[str, Any]) -> None:
        raw = self._load(identity_id)
        reports = list(raw.get("benchmarks") or [])
        reports.append(report)
        raw["benchmarks"] = reports[-100:]
        self.storage.save(identity_id, self.NAMESPACE, raw)
