"""Governed service artifact format: bounded local transformations, never Python.

The grammar has no filesystem, network, imports, eval, prompts or credentials.
Unsupported engineering requires a future sandbox adapter, not relaxed validation.
"""

import hashlib
import re
from pathlib import Path

from core.services.store import encode, fingerprint

OPS = {"strip", "lower", "upper", "split_lines", "sort", "unique", "count", "sha256"}


def validate(document):
    if not isinstance(document, dict) or set(document) != {"format", "skill", "steps", "permissions"}:
        raise ValueError("invalid artifact schema")
    if document["format"] != "local-transform-v1" or document["permissions"] != ["local"]:
        raise PermissionError("only bounded local artifacts are supported")
    if not isinstance(document["skill"], str) or not re.fullmatch(
        r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", document["skill"]
    ):
        raise ValueError("invalid skill")
    steps = document["steps"]
    if (
        not isinstance(steps, list)
        or not 1 <= len(steps) <= 16
        or any(not isinstance(s, str) or s not in OPS for s in steps)
    ):
        raise ValueError("unsupported operation; code and prompt instructions are not executable")
    return fingerprint(document)


def execute(document, value):
    validate(document)
    if not isinstance(value, str) or len(value.encode()) > 65536:
        raise ValueError("input must be text, at most 64 KiB")
    for op in document["steps"]:
        if op == "strip":
            value = value.strip()
        elif op == "lower":
            value = value.lower()
        elif op == "upper":
            value = value.upper()
        elif op == "split_lines":
            value = value.splitlines()
        elif op == "sort":
            if not isinstance(value, list):
                raise ValueError("sort requires lines")
            value = sorted(value)
        elif op == "unique":
            if not isinstance(value, list):
                raise ValueError("unique requires lines")
            value = list(dict.fromkeys(value))
        elif op == "count":
            value = len(value)
        elif op == "sha256":
            value = hashlib.sha256(value.encode()).hexdigest()
    return {"value": value}


def build(workspace, job_id, skill, steps):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("invalid workspace id")
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / job_id
    if target.is_symlink():
        raise ValueError("workspace escape")
    target.mkdir(exist_ok=True)
    document = {"format": "local-transform-v1", "skill": skill, "steps": steps, "permissions": ["local"]}
    digest = validate(document)
    path = target / (digest + ".json")
    if path.is_symlink():
        raise ValueError("artifact escape")
    path.write_text(encode(document), encoding="utf-8")
    return document


def install(registry, identity, document, digest):
    if validate(document) != digest:
        raise ValueError("artifact substitution")
    # Never shadow an already-installed skill, especially a denied one.
    for cap in registry.list(identity):
        if any(s.name == document["skill"] for s in cap.skills()) and cap.id != "service_artifacts":
            raise PermissionError("existing capability cannot be replaced through service delivery")
    cap = registry.get(identity, "service_artifacts")
    bundles = dict(cap._config.get("bundles", {})) if cap else {}
    bundles[document["skill"]] = {"document": document, "fingerprint": digest}
    registry.install(identity, "service_artifacts", config={"bundles": bundles})
