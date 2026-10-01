from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class EvidenceOrigin(Enum):
    USER_INPUT = "user_input"
    MEMORY = "memory"
    CAPABILITY = "capability"
    OBSERVATION = "observation"
    INFERENCE = "inference"
    SYSTEM = "system"
    SELF = "self"


def soft_failure_message(data: Any) -> Optional[str]:
    """Detect soft failures embedded in handler return values.

    Handlers historically returned ``{"error": "..."}`` while callers wrapped
    them in ``CapabilityResult.ok(confidence=1.0)``, producing false
    "verified" evidence.  Any non-empty ``error`` key, ``status == "error"``,
    or non-zero ``exit_code`` is a failure — never a verified fact.
    """
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if err is not None and err != "" and err is not False:
        return str(err)
    if data.get("status") == "error":
        return str(data.get("message") or data.get("reason") or "status=error")
    exit_code = data.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        stderr = data.get("stderr") or data.get("message") or ""
        return f"exit_code={exit_code}" + (f": {stderr}" if stderr else "")
    return None


@dataclass
class CapabilityResult:
    capability: str
    action: str
    success: bool
    confidence: float = 0.0
    source: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: Any = None
    error: Optional[dict] = None
    citations: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    metadata: dict = field(default_factory=dict)
    # Chain-of-custody: params used + observable post-conditions
    params: dict = field(default_factory=dict)
    custody: dict = field(default_factory=dict)

    @staticmethod
    def ok(capability: str, action: str, data: Any, *,
           source: str = "", confidence: float = 1.0,
           citations: list[str] | None = None,
           duration_ms: float = 0.0,
           params: dict | None = None,
           custody: dict | None = None) -> CapabilityResult:
        return CapabilityResult(
            capability=capability,
            action=action,
            success=True,
            confidence=confidence,
            source=source,
            data=data,
            citations=citations or [],
            duration_ms=duration_ms,
            params=params or {},
            custody=custody or {},
        )

    @staticmethod
    def fail(capability: str, action: str, error_type: str, message: str, *,
             source: str = "", duration_ms: float = 0.0,
             params: dict | None = None,
             data: Any = None) -> CapabilityResult:
        return CapabilityResult(
            capability=capability,
            action=action,
            success=False,
            confidence=0.0,
            source=source,
            data=data,
            error={"type": error_type, "message": message},
            duration_ms=duration_ms,
            params=params or {},
        )

    @staticmethod
    def from_data(
        capability: str,
        action: str,
        data: Any,
        *,
        source: str = "",
        confidence: float = 1.0,
        citations: list[str] | None = None,
        duration_ms: float = 0.0,
        params: dict | None = None,
        custody: dict | None = None,
    ) -> CapabilityResult:
        """Wrap handler output, converting soft-error dicts into failures.

        Never marks a result verified when the handler reported an error,
        non-zero exit code, or ``status == "error"``.
        """
        msg = soft_failure_message(data)
        if msg is not None:
            return CapabilityResult.fail(
                capability,
                action,
                "handler_error",
                msg,
                source=source,
                duration_ms=duration_ms,
                params=params,
                data=data,
            )
        built_custody = dict(custody or {})
        if isinstance(data, dict):
            for key in ("path", "bytes_written", "bytes_appended", "url", "name",
                        "exit_code", "count", "status"):
                if key in data and key not in built_custody:
                    built_custody[key] = data[key]
        return CapabilityResult.ok(
            capability,
            action,
            data,
            source=source,
            confidence=confidence,
            citations=citations,
            duration_ms=duration_ms,
            params=params,
            custody=built_custody,
        )

    def reclassify_soft_errors(self) -> CapabilityResult:
        """Defense-in-depth: convert an already-built ok() that embeds soft failure."""
        if not self.success:
            return self
        msg = soft_failure_message(self.data)
        if msg is None:
            return self
        return CapabilityResult.fail(
            self.capability,
            self.action,
            "handler_error",
            msg,
            source=self.source,
            duration_ms=self.duration_ms,
            params=self.params,
            data=self.data,
        )

    def to_dict(self) -> dict:
        """Full lossless serialization (data included), for evidence and tests."""
        return {
            "capability": self.capability,
            "action": self.action,
            "success": self.success,
            "confidence": self.confidence,
            "source": self.source,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "data": self.data,
            "citations": self.citations,
            "metadata": self.metadata,
            "params": self.params,
            "custody": self.custody,
        }

    def to_evidence_dict(self) -> dict:
        return {
            "capability": self.capability,
            "action": self.action,
            "success": self.success,
            "confidence": self.confidence,
            "source": self.source,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "params": self.params or {},
            "custody": self.custody or {},
        }


@dataclass
class Fact:
    content: str
    origin: EvidenceOrigin
    confidence: float
    source: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    citations: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @staticmethod
    def from_result(result: CapabilityResult) -> list[Fact]:
        if not result.success:
            return []
        text = str(result.data) if not isinstance(result.data, dict) else _summarize(result.data)
        if len(text) > 3000:
            text = text[:3000] + "\n... [truncated]"
        return [
            Fact(
                content=text,
                origin=EvidenceOrigin.CAPABILITY,
                confidence=result.confidence,
                source=result.source,
                timestamp=result.timestamp,
                citations=result.citations,
                metadata={"capability": result.capability, "action": result.action},
            )
        ]


def _summarize(data: dict, max_len: int = 2000) -> str:
    import json
    text = json.dumps(data, indent=2, default=str)
    if len(text) > max_len:
        text = text[:max_len] + "\n... [truncated]"
    return text
