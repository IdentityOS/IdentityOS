"""End-to-end evidence for approval-gated offline capability expansion."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.capabilities.registry import CapabilityRegistry
from core.skill_forge import (
    ForgeRequest,
    PushApprovalRequired,
    PushInvitationAuthority,
    PushInvitationCredential,
    SelfImprovementPipeline,
)
from runtime.persistence import JSONFileBackend
from tests.test_skill_forge import build_forge


def _pipeline(tmp_path: Path, cap_id: str, *, now=None):
    workspace = tmp_path / "workspace"
    storage = JSONFileBackend(root_dir=str(tmp_path / "identity-store"))
    registry = CapabilityRegistry(storage)
    forge = build_forge(tmp_path, cap_id)
    authority = PushInvitationAuthority(
        storage,
        clock=now or (lambda: 1000.0),
        token_factory=lambda: "opaque-push-token-with-at-least-32-bytes",
    )
    pipeline = SelfImprovementPipeline(
        workspace,
        forge=forge,
        authority=authority,
        storage=storage,
        capability_registry=registry,
    )
    return pipeline, forge, authority, storage, registry


def test_offline_self_improvement_requires_invitation_and_survives_restart(tmp_path):
    cap_id = "offline_echo"
    pipeline, forge, authority, storage, registry = _pipeline(tmp_path, cap_id)
    candidate = pipeline.prepare(
        ForgeRequest(cap_id, "echo text and report its length", "persistent-identity")
    )

    # Authoring and isolated tests do not make the capability importable or installed.
    assert not (tmp_path / "workspace" / "core" / "capabilities" / cap_id).exists()
    assert registry.get("persistent-identity", cap_id) is None
    with pytest.raises(PushApprovalRequired, match="external push invitation"):
        pipeline.publish_and_install(
            candidate, identity_id="persistent-identity", credential=None
        )

    credential = authority.issue(
        identity_id="persistent-identity",
        capability_id=cap_id,
        artifact_sha256=candidate.artifact_sha256,
    )

    # Recreate every runtime-facing object to prove the approval and artifact are durable.
    fresh_storage = JSONFileBackend(root_dir=str(tmp_path / "identity-store"))
    fresh_registry = CapabilityRegistry(fresh_storage)
    fresh_authority = PushInvitationAuthority(fresh_storage, clock=lambda: 1001.0)
    fresh_pipeline = SelfImprovementPipeline(
        tmp_path / "workspace",
        forge=forge,
        authority=fresh_authority,
        storage=fresh_storage,
        capability_registry=fresh_registry,
    )
    receipt = fresh_pipeline.publish_and_install(
        candidate,
        identity_id="persistent-identity",
        credential=credential,
    )

    assert receipt["status"] == "published_installed_verified"
    assert receipt["credentials"] == {
        "used": 1,
        "kind": "one_time_push_invitation",
        "long_lived": 0,
        "invitation_id": credential.invitation_id,
        "token_persisted": False,
    }
    assert receipt["network_calls"] == 0
    assert receipt["bytes_published"] > 0
    assert len(receipt["changed_paths"]) == 1
    assert Path(receipt["changed_paths"][0]).is_file()

    # A third registry instance reloads and executes the installed package.
    restarted_registry = CapabilityRegistry(
        JSONFileBackend(root_dir=str(tmp_path / "identity-store"))
    )
    observed = restarted_registry.call(
        "persistent-identity", f"{cap_id}.echo", text="after restart"
    )
    assert observed.success is True
    assert observed.data == {"echo": "after restart", "length": 13}

    persisted = json.dumps(
        {
            namespace: fresh_storage.load("persistent-identity", namespace)
            for namespace in fresh_storage.list_namespaces("persistent-identity")
        }
    )
    assert credential.token not in persisted
    with pytest.raises(PushApprovalRequired, match="no longer usable"):
        fresh_pipeline.publish_and_install(
            candidate,
            identity_id="persistent-identity",
            credential=credential,
        )


def test_push_invitation_is_exact_scope_and_expires(tmp_path):
    now = [2000.0]
    cap_id = "scoped_echo"
    pipeline, _, authority, _, registry = _pipeline(
        tmp_path, cap_id, now=lambda: now[0]
    )
    candidate = pipeline.prepare(ForgeRequest(cap_id, "echo text", "owner"))
    credential = authority.issue(
        identity_id="owner",
        capability_id=cap_id,
        artifact_sha256=candidate.artifact_sha256,
        ttl_seconds=10,
    )

    with pytest.raises(PushApprovalRequired, match="exact artifact scope"):
        authority.consume(
            credential,
            identity_id="owner",
            capability_id="other_capability",
            artifact_sha256=candidate.artifact_sha256,
        )
    assert registry.get("owner", cap_id) is None

    now[0] = 2011.0
    with pytest.raises(PushApprovalRequired, match="expired"):
        pipeline.publish_and_install(
            candidate,
            identity_id="owner",
            credential=credential,
        )


def test_invalid_token_is_rate_limited_and_never_published(tmp_path):
    cap_id = "rate_limited_echo"
    pipeline, _, authority, storage, registry = _pipeline(tmp_path, cap_id)
    candidate = pipeline.prepare(ForgeRequest(cap_id, "echo text", "owner"))
    credential = authority.issue(
        identity_id="owner",
        capability_id=cap_id,
        artifact_sha256=candidate.artifact_sha256,
    )
    wrong = PushInvitationCredential(credential.invitation_id, "x" * 40)

    for _ in range(5):
        with pytest.raises(PushApprovalRequired):
            pipeline.publish_and_install(
                candidate, identity_id="owner", credential=wrong
            )

    invitation = storage.load(
        "owner", f"skill_forge.push_invitation.{credential.invitation_id}"
    )
    assert invitation["status"] == "revoked"
    assert invitation["failed_attempts"] == 5
    assert registry.get("owner", cap_id) is None


def test_failure_after_approval_is_durable_and_does_not_invite_replay(tmp_path):
    cap_id = "failed_install_echo"
    pipeline, _, authority, storage, _ = _pipeline(tmp_path, cap_id)
    candidate = pipeline.prepare(ForgeRequest(cap_id, "echo text", "owner"))
    credential = authority.issue(
        identity_id="owner",
        capability_id=cap_id,
        artifact_sha256=candidate.artifact_sha256,
    )

    with patch.object(
        pipeline.forge, "install_artifact", side_effect=RuntimeError("install failed")
    ):
        with pytest.raises(RuntimeError, match="install failed"):
            pipeline.publish_and_install(
                candidate, identity_id="owner", credential=credential
            )

    receipts = storage.load("owner", "skill_forge.self_improvement.receipts")
    assert receipts["receipts"][-1]["status"] == "failed_after_approval"
    assert receipts["receipts"][-1]["credentials"]["used"] == 1
    with pytest.raises(PushApprovalRequired, match="no longer usable"):
        pipeline.publish_and_install(
            candidate, identity_id="owner", credential=credential
        )
