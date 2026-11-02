"""Offline, approval-gated publication of independently verified capabilities."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .artifacts import package_record
from .models import ForgeRequest, ForgeResult
from .service import SkillForge, SkillForgeError

_INVITATION_NS = "skill_forge.push_invitation"
_RECEIPTS_NS = "skill_forge.self_improvement.receipts"
_LOCK = threading.RLock()


class PushApprovalRequired(SkillForgeError):
    """The candidate has no valid external push invitation."""


@dataclass(frozen=True)
class PushInvitationCredential:
    """Opaque bearer credential returned once to the external pusher."""

    invitation_id: str
    token: str

    def public_reference(self) -> dict[str, str]:
        """Return the safe reference that may be logged or persisted."""
        return {"invitation_id": self.invitation_id}


class PushInvitationAuthority:
    """Issue and consume exact-artifact, one-use offline approvals.

    Only a SHA-256 digest of the bearer token is persisted. The token itself is
    random and encodes no identity, capability, path, or artifact metadata.
    """

    def __init__(
        self,
        storage: Any,
        *,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.storage = storage
        self.clock = clock
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def issue(
        self,
        *,
        identity_id: str,
        capability_id: str,
        artifact_sha256: str,
        ttl_seconds: float = 900,
    ) -> PushInvitationCredential:
        if ttl_seconds <= 0 or ttl_seconds > 86_400:
            raise ValueError("push invitation TTL must be between 0 and 86400 seconds")
        if not _is_digest(artifact_sha256):
            raise ValueError("artifact_sha256 must be a lowercase SHA-256 digest")
        invitation_id = uuid.uuid4().hex
        token = self.token_factory()
        if not isinstance(token, str) or len(token.encode("utf-8")) < 32:
            raise ValueError("push invitation token must contain at least 32 bytes")
        now = self.clock()
        record = {
            "schema_version": 1,
            "invitation_id": invitation_id,
            "token_sha256": _secret_digest(token),
            "identity_id": identity_id,
            "capability_id": capability_id,
            "artifact_sha256": artifact_sha256,
            "created_at_epoch": now,
            "expires_at_epoch": now + float(ttl_seconds),
            "status": "issued",
            "failed_attempts": 0,
        }
        self.storage.save(identity_id, self._namespace(invitation_id), record)
        return PushInvitationCredential(invitation_id=invitation_id, token=token)

    def consume(
        self,
        credential: PushInvitationCredential,
        *,
        identity_id: str,
        capability_id: str,
        artifact_sha256: str,
    ) -> dict[str, Any]:
        """Consume a matching invitation before any publication side effect."""
        with _LOCK:
            namespace = self._namespace(credential.invitation_id)
            record = self.storage.load(identity_id, namespace)
            if not isinstance(record, dict):
                raise PushApprovalRequired("push invitation was not found")
            if record.get("status") != "issued":
                raise PushApprovalRequired("push invitation is no longer usable")
            if self.clock() >= float(record.get("expires_at_epoch", 0)):
                record["status"] = "expired"
                self.storage.save(identity_id, namespace, record)
                raise PushApprovalRequired("push invitation has expired")
            supplied = _secret_digest(credential.token)
            if not hmac.compare_digest(supplied, str(record.get("token_sha256", ""))):
                record["failed_attempts"] = int(record.get("failed_attempts", 0)) + 1
                if record["failed_attempts"] >= 5:
                    record["status"] = "revoked"
                self.storage.save(identity_id, namespace, record)
                raise PushApprovalRequired("push invitation token is invalid")
            scope = (identity_id, capability_id, artifact_sha256)
            expected = (
                record.get("identity_id"),
                record.get("capability_id"),
                record.get("artifact_sha256"),
            )
            if scope != expected:
                raise PushApprovalRequired("push invitation does not match this exact artifact scope")
            record["status"] = "consumed"
            record["consumed_at_epoch"] = self.clock()
            record.pop("token_sha256", None)
            self.storage.save(identity_id, namespace, record)
            return {
                "invitation_id": credential.invitation_id,
                "scope": {
                    "identity_id": identity_id,
                    "capability_id": capability_id,
                    "artifact_sha256": artifact_sha256,
                },
                "credential_count": 1,
                "long_lived_credentials": 0,
            }

    @staticmethod
    def _namespace(invitation_id: str) -> str:
        if not invitation_id or any(char not in "0123456789abcdef" for char in invitation_id):
            raise PushApprovalRequired("push invitation reference is invalid")
        return f"{_INVITATION_NS}.{invitation_id}"


class SelfImprovementPipeline:
    """Forge offline, then publish and install only an externally invited artifact."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        forge: SkillForge,
        authority: PushInvitationAuthority,
        storage: Any,
        capability_registry: Any,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.forge = forge
        self.authority = authority
        self.storage = storage
        self.capability_registry = capability_registry
        self.clock = clock

    def prepare(self, request: ForgeRequest, *, identity: Any = None) -> ForgeResult:
        """Author and verify a candidate without publishing or installing it."""
        return self.forge.forge_candidate(request, identity=identity)

    def publish_and_install(
        self,
        candidate: ForgeResult,
        *,
        identity_id: str,
        credential: PushInvitationCredential | None,
    ) -> dict[str, Any]:
        started = self.clock()
        if credential is None:
            raise PushApprovalRequired("an external push invitation is required")
        record = package_record(candidate.artifact_path)
        cap_id = str(record.get("manifest", {}).get("id", ""))
        digest = str(record.get("artifact_sha256", ""))
        if cap_id != candidate.capability_id or digest != candidate.artifact_sha256:
            raise SkillForgeError("candidate metadata does not match packaged artifact")

        approval = self.authority.consume(
            credential,
            identity_id=identity_id,
            capability_id=cap_id,
            artifact_sha256=digest,
        )
        approved_at = self.clock()
        destination = self._publication_path(identity_id, cap_id, digest)
        credential_evidence = {
            "used": approval["credential_count"],
            "kind": "one_time_push_invitation",
            "long_lived": approval["long_lived_credentials"],
            "invitation_id": approval["invitation_id"],
            "token_persisted": False,
        }
        published_at = approved_at
        try:
            if destination.exists():
                raise SkillForgeError(f"refusing to overwrite published artifact: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    delete=False,
                ) as handle:
                    temporary = handle.name
                    with Path(candidate.artifact_path).open("rb") as source:
                        shutil.copyfileobj(source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            published_at = self.clock()

            installed = self.forge.install_artifact(
                destination,
                identity_id=identity_id,
                capability_registry=self.capability_registry,
                storage=self.storage,
            )
        except Exception as exc:
            failed_at = self.clock()
            failure_receipt = {
                "schema_version": 1,
                "status": "failed_after_approval",
                "identity_id": identity_id,
                "capability_id": cap_id,
                "artifact_sha256": digest,
                "changed_paths": [str(destination)] if destination.exists() else [],
                "bytes_published": destination.stat().st_size if destination.exists() else 0,
                "installed_skills": [],
                "credentials": credential_evidence,
                "network_calls": 0,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "overhead_ms": {
                    "approval": round((approved_at - started) * 1000, 3),
                    "publication_and_installation": round((failed_at - approved_at) * 1000, 3),
                    "total": round((failed_at - started) * 1000, 3),
                },
            }
            self._save_receipt(identity_id, failure_receipt)
            raise
        completed_at = self.clock()
        receipt = {
            "schema_version": 1,
            "status": "published_installed_verified",
            "identity_id": identity_id,
            "capability_id": cap_id,
            "artifact_sha256": digest,
            "changed_paths": [str(destination)],
            "bytes_published": destination.stat().st_size,
            "installed_skills": [skill.name for skill in installed.skills()],
            "credentials": credential_evidence,
            "network_calls": 0,
            "overhead_ms": {
                "approval": round((approved_at - started) * 1000, 3),
                "publication": round((published_at - approved_at) * 1000, 3),
                "installation_verification": round((completed_at - published_at) * 1000, 3),
                "total": round((completed_at - started) * 1000, 3),
            },
        }
        self._save_receipt(identity_id, receipt)
        return receipt

    def _publication_path(self, identity_id: str, cap_id: str, digest: str) -> Path:
        identity_key = hashlib.sha256(identity_id.encode("utf-8")).hexdigest()
        destination = (
            self.workspace_root
            / ".identity_forge"
            / "published"
            / identity_key
            / cap_id
            / digest
            / "capability.idcap"
        ).resolve()
        root = (self.workspace_root / ".identity_forge" / "published").resolve()
        if not destination.is_relative_to(root):
            raise SkillForgeError("publication path escaped the workspace")
        return destination

    def _save_receipt(self, identity_id: str, receipt: dict[str, Any]) -> None:
        raw = self.storage.load(identity_id, _RECEIPTS_NS) or {}
        receipts = list(raw.get("receipts", []))
        receipts.append(json.loads(json.dumps(receipt)))
        self.storage.save(identity_id, _RECEIPTS_NS, {"receipts": receipts[-100:]})


def _secret_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)
