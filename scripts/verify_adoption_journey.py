#!/usr/bin/env python3
"""Real-provider acceptance phases for a clean IdentityOS installation.

This driver imports the installed package, not repository internals.  Each phase
runs in a separate process so persistence cannot be satisfied by an in-memory
object.  It emits one final JSON evidence object and never prints response text
or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from core.identity import create_identity
from core.secrets.store import SecretStore
from identityos.adoption import (
    build_configured_adapter,
    configure_provider,
    initialize,
    record_verification,
)
from identityos.paths import get_paths
from identityos.portability import export_identity, import_identity
from runtime.orchestrator import IdentityRuntime, InteractionRequest
from runtime.persistence import JSONFileBackend


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_path() -> Path:
    paths = get_paths().ensure()
    return paths.state_dir / "adoption-acceptance.json"


def save_state(value: dict) -> None:
    path = state_path()
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def load_state() -> dict:
    path = state_path()
    if not path.is_file():
        raise RuntimeError("inference phase evidence is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("acceptance state is invalid")
    return value


def actual_provenance(response) -> dict:
    provenance = dict(response.metadata.get("generation_provenance") or {})
    if (
        provenance.get("generation_mode") != "identity_model_generation"
        or not provenance.get("provider")
        or not provenance.get("model")
        or not str(response.output or "").strip()
    ):
        raise RuntimeError("provider did not produce runtime-observed model generation")
    return {
        "generation_mode": provenance["generation_mode"],
        "provider": str(provenance["provider"]),
        "model": str(provenance["model"]),
        "latency_ms": provenance.get("latency_ms"),
    }


def inference_phase(args: argparse.Namespace) -> dict:
    paths = get_paths().ensure()
    initialize(paths)
    configure_provider(
        args.provider,
        model=args.model,
        base_url=args.base_url,
        paths=paths,
    )
    backend = JSONFileBackend(str(paths.identity_store))
    adapter = build_configured_adapter(paths)
    if hasattr(adapter, "max_tokens"):
        adapter.max_tokens = 128
    runtime = IdentityRuntime(storage=backend, adapter=adapter, max_context_tokens=512)
    runtime.prometheus = None
    identity_id = f"acceptance-{args.nonce.lower()}"
    spec = create_identity(name="Adoption Verifier", identity_id=identity_id)
    runtime.register(spec)
    started = time.monotonic_ns()
    try:
        response = runtime.process(InteractionRequest(
            identity_id=identity_id,
            user_id="acceptance-user",
            user_input=f"Reply briefly and include this verification marker: {args.nonce}",
        ))
        provenance = actual_provenance(response)
        response_sha256 = digest(response.output)
        record_verification(
            identity_id=identity_id,
            provenance=provenance,
            response_digest=response_sha256,
            paths=paths,
        )
    finally:
        runtime.shutdown()
    elapsed_ms = round((time.monotonic_ns() - started) / 1_000_000, 3)
    save_state({
        "identity_id": identity_id,
        "nonce": args.nonce,
        "provider": args.provider,
        "model": args.model,
        "response_sha256": response_sha256,
    })
    return {
        "verified": True,
        "identity_id": identity_id,
        "response_sha256": response_sha256,
        "generation_provenance": provenance,
        "elapsed_ms": elapsed_ms,
    }


def restart_phase(args: argparse.Namespace) -> dict:
    del args
    paths = get_paths()
    state = load_state()
    adapter = build_configured_adapter(paths)
    if hasattr(adapter, "max_tokens"):
        adapter.max_tokens = 128
    runtime = IdentityRuntime(
        storage=JSONFileBackend(str(paths.identity_store)),
        adapter=adapter,
        max_context_tokens=512,
    )
    runtime.prometheus = None
    started = time.monotonic_ns()
    try:
        runtime.load_persisted()
        spec = runtime.load(state["identity_id"])
        if spec is None or spec.id != state["identity_id"]:
            raise RuntimeError("identity did not survive process restart")
        memories = runtime.memory_store.by_identity(spec.id)
        previous_available = any(state["nonce"] in memory.content for memory in memories)
        if not previous_available:
            raise RuntimeError("prior conversation was not available after restart")
        response = runtime.process(InteractionRequest(
            identity_id=spec.id,
            user_id="acceptance-user",
            user_input="Confirm in one sentence that this is a new runtime process.",
        ))
        provenance = actual_provenance(response)
    finally:
        runtime.shutdown()
    return {
        "verified": True,
        "identity_id": state["identity_id"],
        "identity_stable": True,
        "prior_conversation_available": True,
        "response_sha256": digest(response.output),
        "generation_provenance": provenance,
        "elapsed_ms": round((time.monotonic_ns() - started) / 1_000_000, 3),
    }


def portability_phase(args: argparse.Namespace) -> dict:
    source_paths = get_paths()
    state = load_state()
    archive = Path(args.archive).expanduser().resolve(strict=False)
    destination_home = Path(args.destination_home).expanduser().resolve(strict=False)
    canary = f"portable-secret-{state['nonce']}"
    SecretStore(source_paths.secrets_dir).put("providers/acceptance/canary", canary)
    backend = JSONFileBackend(str(source_paths.identity_store))
    backend.save(state["identity_id"], "debug.interaction:portability-canary", {
        "identity_id": state["identity_id"],
        "diagnostic": canary,
        "api_key": canary,
    })
    started = time.monotonic_ns()
    exported = export_identity(state["identity_id"], archive, paths=source_paths)
    archive_text = archive.read_text(encoding="utf-8")
    if canary in archive_text:
        raise RuntimeError("credential canary leaked into portable archive")

    prior_home = os.environ.get("IDENTITYOS_HOME")
    os.environ["IDENTITYOS_HOME"] = str(destination_home)
    try:
        destination_paths = get_paths().ensure()
        initialize(destination_paths)
        imported = import_identity(archive, paths=destination_paths)
        fresh = IdentityRuntime(storage=JSONFileBackend(str(destination_paths.identity_store)))
        try:
            fresh.load_persisted()
            spec = fresh.load(state["identity_id"])
            if spec is None or spec.id != state["identity_id"]:
                raise RuntimeError("portable identity ID was not preserved")
            if not any(state["nonce"] in item.content for item in fresh.memory_store.by_identity(spec.id)):
                raise RuntimeError("conversation memory was not preserved by portability")
        finally:
            fresh.shutdown()
    finally:
        if prior_home is None:
            os.environ.pop("IDENTITYOS_HOME", None)
        else:
            os.environ["IDENTITYOS_HOME"] = prior_home
    return {
        "verified": True,
        "identity_id": state["identity_id"],
        "identity_stable": True,
        "secret_canary_excluded": True,
        "memory_preserved": True,
        "archive_sha256": exported["sha256"],
        "content_sha256": imported["content_sha256"],
        "elapsed_ms": round((time.monotonic_ns() - started) / 1_000_000, 3),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("inference", "restart", "portability"))
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--model", default="llama3.2:1b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--nonce", default="IDOS417")
    parser.add_argument("--archive", default="identityos-acceptance.identityos.json")
    parser.add_argument("--destination-home", default="identityos-portability-destination")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.phase == "inference":
        result = inference_phase(args)
    elif args.phase == "restart":
        result = restart_phase(args)
    else:
        result = portability_phase(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
