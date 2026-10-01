"""Deterministic Skill Forge artifact encoding and verification."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

PAYLOAD_NAMES = ("capability.py", "manifest.json", "acceptance.json")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def payload_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in PAYLOAD_NAMES:
        data = files[name]
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(len(data)).encode())
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def write_artifact(
    path: Path,
    *,
    source: str,
    manifest: dict[str, Any],
    acceptance: list[dict[str, Any]],
) -> str:
    files = {
        "capability.py": source.encode(),
        "manifest.json": canonical_json(manifest),
        "acceptance.json": canonical_json(acceptance),
    }
    digest = payload_digest(files)
    attestation = canonical_json({
        "schema_version": 1,
        "artifact_sha256": digest,
        "tested_payload": list(PAYLOAD_NAMES),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in (*PAYLOAD_NAMES, "attestation.json"):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, attestation if name == "attestation.json" else files[name])
    return digest


def read_artifact(path: str | Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        expected = {*PAYLOAD_NAMES, "attestation.json"}
        if set(names) != expected or len(names) != len(expected):
            raise ValueError(f"artifact must contain exactly: {', '.join(sorted(expected))}")
        files = {name: archive.read(name) for name in PAYLOAD_NAMES}
        attestation = json.loads(archive.read("attestation.json"))
    observed = payload_digest(files)
    if attestation.get("artifact_sha256") != observed:
        raise ValueError("artifact payload does not match its attestation")
    return files, attestation


def package_record(path: str | Path) -> dict[str, Any]:
    files, attestation = read_artifact(path)
    return {
        "schema_version": 1,
        "artifact_sha256": attestation["artifact_sha256"],
        "source": files["capability.py"].decode(),
        "manifest": json.loads(files["manifest.json"]),
        "acceptance": json.loads(files["acceptance.json"]),
    }


def verify_package_record(record: dict[str, Any]) -> str:
    files = {
        "capability.py": str(record.get("source", "")).encode(),
        "manifest.json": canonical_json(record.get("manifest", {})),
        "acceptance.json": canonical_json(record.get("acceptance", [])),
    }
    observed = payload_digest(files)
    if record.get("artifact_sha256") != observed:
        raise ValueError("persisted forged package failed its content digest")
    return observed
