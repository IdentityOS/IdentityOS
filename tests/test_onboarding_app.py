"""Deterministic onboarding regressions; these mocks are not acceptance inference."""

from __future__ import annotations

import asyncio
import importlib
import json

import httpx
import pytest

from adapters.base import BaseAdapter


class _DeterministicAdapter(BaseAdapter):
    def generate(self, context, user_input, identity, **kwargs):
        return f"Verified response for {identity.name}: {user_input}"


class _RejectedCredentialAdapter(BaseAdapter):
    def generate(self, context, user_input, identity, **kwargs):
        raise RuntimeError("synthetic 401 credential rejection")


@pytest.fixture
def onboarding(tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITYOS_HOME", str(tmp_path / "isolated-installation"))
    module = importlib.import_module("runtime.playground.app")
    previous = module.manager
    module.manager = module.RuntimeManager()
    fake = _DeterministicAdapter(model="deterministic-regression-model")
    monkeypatch.setattr(module, "build_configured_adapter", lambda paths: fake)
    try:
        yield module
    finally:
        module.manager.get_runtime().shutdown()
        module.manager = previous


def run_client(app, operation):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await operation(client)

    return asyncio.run(run())


def test_onboarding_requires_csrf_and_never_returns_credentials(onboarding):
    async def scenario(client):
        page = await client.get("/onboarding")
        assert page.status_code == 200
        assert "Create an AI identity that persists" in page.text

        rejected = await client.post("/onboarding/api/identity", json={"name": "Ada"})
        assert rejected.status_code == 403

        headers = {"X-IdentityOS-CSRF": onboarding._CSRF_TOKEN}
        created = await client.post(
            "/onboarding/api/identity",
            headers=headers,
            json={"name": "Ada", "purpose": "A persistent building partner"},
        )
        assert created.status_code == 200

        configured = await client.post(
            "/onboarding/api/provider",
            headers=headers,
            json={
                "provider": "groq",
                "model": "regression-model",
                "api_key": "credential-canary-never-return",
            },
        )
        assert configured.status_code == 200
        assert "credential-canary-never-return" not in configured.text
        assert "secret_handle" not in configured.text

        verified = await client.post("/onboarding/api/verify", headers=headers, json={})
        assert verified.status_code == 200
        body = verified.json()
        assert body["ready"] is True
        assert body["provenance"] == {
            "generation_mode": "identity_model_generation",
            "provider": "_DeterministicAdapter",
            "model": "deterministic-regression-model",
            "latency_ms": body["provenance"]["latency_ms"],
        }

        state = await client.get("/onboarding/api/state")
        assert state.json()["onboarding"]["ready"] is True
        assert "credential-canary-never-return" not in state.text

    run_client(onboarding.app, scenario)
    assert "credential-canary-never-return" not in onboarding.manager._paths.config_file.read_text()


def test_first_chat_and_history_survive_runtime_manager_restart(onboarding, monkeypatch):
    async def setup(client):
        await client.get("/onboarding")
        headers = {"X-IdentityOS-CSRF": onboarding._CSRF_TOKEN}
        await client.post("/onboarding/api/identity", headers=headers, json={"name": "Continuity"})
        await client.post(
            "/onboarding/api/provider",
            headers=headers,
            json={"provider": "ollama", "model": "local-regression"},
        )
        await client.post("/onboarding/api/verify", headers=headers, json={})
        chat = await client.post(
            "/onboarding/api/chat",
            headers=headers,
            json={"message": "Remember restart marker 417"},
        )
        assert chat.status_code == 200

    run_client(onboarding.app, setup)
    old_identity_id = json.loads(onboarding.manager._paths.config_file.read_text())["identity_id"]
    onboarding.manager.get_runtime().shutdown()
    onboarding.manager = onboarding.RuntimeManager()
    monkeypatch.setattr(
        onboarding,
        "build_configured_adapter",
        lambda paths: _DeterministicAdapter(model="deterministic-regression-model"),
    )

    async def verify_restart(client):
        history = await client.get("/onboarding/api/history")
        assert history.status_code == 200
        text = json.dumps(history.json())
        assert "restart marker 417" in text
        state = (await client.get("/onboarding/api/state")).json()
        assert state["identity_id"] == old_identity_id
        assert state["onboarding"]["ready"] is True

    run_client(onboarding.app, verify_restart)


def test_remote_host_and_cross_origin_requests_are_rejected(onboarding):
    async def remote_host():
        transport = httpx.ASGITransport(app=onboarding.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://example.com") as client:
            return await client.get("/onboarding")

    assert asyncio.run(remote_host()).status_code == 400

    async def cross_origin(client):
        return await client.get("/onboarding/api/state", headers={"Origin": "https://evil.example"})

    assert run_client(onboarding.app, cross_origin).status_code == 403


def test_failed_provider_request_never_marks_identity_ready(onboarding, monkeypatch):
    monkeypatch.setattr(
        onboarding,
        "build_configured_adapter",
        lambda paths: _RejectedCredentialAdapter(model="rejected-model"),
    )

    async def scenario(client):
        await client.get("/onboarding")
        headers = {"X-IdentityOS-CSRF": onboarding._CSRF_TOKEN}
        await client.post("/onboarding/api/identity", headers=headers, json={"name": "Not Ready"})
        configured = await client.post(
            "/onboarding/api/provider",
            headers=headers,
            json={"provider": "groq", "model": "rejected-model", "api_key": "invalid-synthetic-key"},
        )
        assert configured.status_code == 200
        verified = await client.post("/onboarding/api/verify", headers=headers, json={})
        assert verified.status_code == 422
        assert verified.json()["ready"] is False
        state = (await client.get("/onboarding/api/state")).json()
        assert state["onboarding"]["ready"] is False
        assert "invalid-synthetic-key" not in verified.text

    run_client(onboarding.app, scenario)
