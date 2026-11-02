"""Versioned provenance for comparable IdentityBench results."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional


BENCHMARK_SCHEMA_VERSION = 3


def suite_fingerprint(package_root: Optional[Path] = None) -> str:
    """Return a deterministic digest of the executable benchmark suite."""
    root = package_root or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    executable_suffixes = {".py", ".json", ".toml", ".yaml", ".yml"}
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and candidate.suffix in executable_suffixes
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_comparison_signature(config: dict[str, Any]) -> str:
    """Hash every configuration dimension that changes score meaning."""
    dimensions = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "suite_fingerprint": config.get("suite_fingerprint"),
        "evaluator_digest": config.get("evaluator_digest"),
        "protected_suite_digest": config.get("protected_suite_digest"),
        "lane": config.get("lane"),
        "seed": config.get("seed"),
        "worlds": config.get("worlds"),
        "adapter": config.get("adapter"),
        "context_tokens": config.get("context_tokens"),
        "response_tokens": config.get("response_tokens"),
        "tool_result_chars": config.get("tool_result_chars"),
        "tools_per_request": config.get("tools_per_request"),
        "tool_rounds": config.get("tool_rounds"),
        "request_interval_seconds": config.get("request_interval_seconds"),
        "cooldown_wait_seconds": config.get("cooldown_wait_seconds"),
        "request_timeout_seconds": config.get("request_timeout_seconds"),
    }
    encoded = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def capability_manifest_fingerprint(runtime: Any, identity_id: str) -> str:
    """Hash the installed capability contracts without exposing configuration.

    Capability configuration can contain credentials.  The digest covers it,
    while the public manifest records only behaviorally relevant contracts.
    """
    registry = getattr(runtime, "capability_registry", None)
    capabilities = registry.list(identity_id) if registry is not None else []
    manifest = []
    for capability in capabilities:
        public = capability.to_dict()
        public["config_digest"] = hashlib.sha256(
            json.dumps(
                getattr(capability, "_config", {}),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        manifest.append(public)
    encoded = json.dumps(
        sorted(manifest, key=lambda item: (item.get("id", ""), item.get("version", ""))),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def comparison_signature(run: Optional[dict[str, Any]]) -> Optional[str]:
    if not run:
        return None
    config = run.get("config")
    if not isinstance(config, dict):
        return None
    value = config.get("comparison_signature")
    return value if isinstance(value, str) and value else None


def runs_are_comparable(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Reject comparisons across scoring schemas, suites, models, or budgets."""
    previous_signature = comparison_signature(previous)
    current_signature = comparison_signature(current)
    if previous_signature is None and current_signature is None:
        return True  # Legacy-to-legacy reports retain their historical behavior.
    return previous_signature is not None and previous_signature == current_signature
