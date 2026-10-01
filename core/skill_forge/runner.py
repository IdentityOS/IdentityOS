"""Subprocess entry point for independent forged-capability acceptance tests."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

from core.capabilities.registry import CapabilityRegistry
from runtime.persistence import InMemoryBackend

from .loader import capability_class_from_source
from .models import AcceptanceCase


def _contains(observed: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and _contains(observed[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(observed, list) and len(observed) == len(expected) and all(
            _contains(actual, wanted) for actual, wanted in zip(observed, expected)
        )
    return observed == expected


def run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    source = str(payload["source"])
    cap_id = str(payload["capability_id"])
    digest = str(payload["artifact_sha256"])
    manifest = payload["manifest"]
    if not isinstance(manifest, dict) or manifest.get("id") != cap_id:
        raise ValueError("runner manifest does not match capability id")
    cases = [AcceptanceCase.from_dict(item) for item in payload["acceptance"]]
    capture = io.StringIO()
    started = time.monotonic()
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
        cap_cls = capability_class_from_source(
            source,
            cap_id,
            digest,
            allowed_permissions=set(manifest.get("permissions", [])),
            allowed_dependencies=set(manifest.get("dependencies", [])),
        )
        storage = InMemoryBackend()
        registry = CapabilityRegistry(storage)
        cap = cap_cls(config={})
        cap.install("forge-isolation", storage)
        registry._loaded["forge-isolation"] = {cap_id: cap}
        results = []
        for case in cases:
            declared_permissions = {skill.permission for skill in cap.skills()}
            for permission in case.grants:
                if permission not in declared_permissions:
                    raise ValueError(
                        f"case '{case.name}' grants undeclared permission '{permission}'"
                    )
                registry.grant("forge-isolation", cap_id, permission)
            result = registry.call("forge-isolation", case.skill, **case.params)
            success = bool(getattr(result, "success", False))
            data = getattr(result, "data", {})
            passed = success == case.expect_success and _contains(data, case.expected)
            results.append({
                "name": case.name,
                "skill": case.skill,
                "passed": passed,
                "success": success,
                "expected": case.expected,
                "observed": data,
                "error": getattr(result, "error", None),
            })
    return {
        "passed": bool(results) and all(item["passed"] for item in results),
        "case_count": len(results),
        "cases": results,
        "captured_output": capture.getvalue()[-2000:],
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"passed": False, "error": "expected payload path"}))
        return 2
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        report = run_payload(payload)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] else 1
    except Exception as exc:
        print(json.dumps({
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
