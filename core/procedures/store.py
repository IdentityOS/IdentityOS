"""Persistent procedure and protected held-out-suite storage."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .models import HeldOutExample, Procedure


class ProcedureStore:
    LIBRARY_NAMESPACE = "procedure.library"
    SUITE_NAMESPACE = "procedure.held_out_suites"

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def get(self, identity_id: str, procedure_id: str) -> Optional[Procedure]:
        raw = self.storage.load(identity_id, self.LIBRARY_NAMESPACE) or {}
        item = (raw.get("procedures") or {}).get(procedure_id)
        return Procedure.from_dict(item) if isinstance(item, dict) else None

    def list(self, identity_id: str) -> list[Procedure]:
        raw = self.storage.load(identity_id, self.LIBRARY_NAMESPACE) or {}
        return [
            Procedure.from_dict(item)
            for item in (raw.get("procedures") or {}).values()
            if isinstance(item, dict)
        ]

    def save(self, procedure: Procedure) -> None:
        raw = self.storage.load(procedure.identity_id, self.LIBRARY_NAMESPACE) or {}
        procedures = dict(raw.get("procedures") or {})
        procedures[procedure.procedure_id] = procedure.to_dict()
        self.storage.save(
            procedure.identity_id,
            self.LIBRARY_NAMESPACE,
            {"schema_version": 1, "procedures": procedures},
        )

    def register_suite(
        self,
        identity_id: str,
        suite_id: str,
        examples: list[HeldOutExample],
    ) -> str:
        if not suite_id.strip() or len(examples) < 2:
            raise ValueError("a held-out suite requires an id and at least two examples")
        ids = [example.example_id for example in examples]
        if len(ids) != len(set(ids)):
            raise ValueError("held-out example ids must be unique")
        serialized = [example.to_dict() for example in examples]
        digest = hashlib.sha256(
            json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        raw = self.storage.load(identity_id, self.SUITE_NAMESPACE) or {}
        suites = dict(raw.get("suites") or {})
        existing = suites.get(suite_id)
        if existing and existing.get("sha256") != digest:
            raise ValueError("held-out suite ids are immutable; register a new suite id")
        suites[suite_id] = {"sha256": digest, "examples": serialized}
        self.storage.save(
            identity_id,
            self.SUITE_NAMESPACE,
            {"schema_version": 1, "suites": suites},
        )
        return digest

    def load_suite(self, identity_id: str, suite_id: str) -> tuple[str, list[HeldOutExample]]:
        raw = self.storage.load(identity_id, self.SUITE_NAMESPACE) or {}
        suite = (raw.get("suites") or {}).get(suite_id)
        if not isinstance(suite, dict):
            raise ValueError(f"unknown held-out suite: {suite_id}")
        examples = [HeldOutExample.from_dict(item) for item in suite.get("examples", [])]
        observed = hashlib.sha256(
            json.dumps(
                [example.to_dict() for example in examples],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if observed != suite.get("sha256"):
            raise ValueError("held-out suite failed its content digest")
        return observed, examples
