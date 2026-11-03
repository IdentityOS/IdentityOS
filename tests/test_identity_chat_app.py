"""Executable acceptance tests for the flagship Identity Chat web app."""

import asyncio
import importlib
import json

import httpx
import pytest


@pytest.fixture()
def chat_app(tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITYOS_HOME", str(tmp_path / "identityos-home"))
    module = importlib.import_module("runtime.playground.app")
    previous = module.manager
    module.manager = module.RuntimeManager()
    try:
        yield module.app, module.manager
    finally:
        module.manager = previous


def request(app, method, path, data=None):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=data)

    return asyncio.run(run())


def create_identity(app, identity_id="chat-bot"):
    response = request(app, "POST", "/playground/api/identities", {
        "identity_id": identity_id,
        "name": identity_id.title(),
        "persona": "A test identity",
    })
    assert response.status_code == 200


def test_goal_intention_session_and_restart_lifecycle(chat_app):
    app, _manager = chat_app
    create_identity(app)

    goal = request(app, "POST", "/playground/api/goals", {
        "identity_id": "chat-bot", "title": "Ship Identity Chat", "priority": "high",
    })
    intention = request(app, "POST", "/playground/api/intentions", {
        "identity_id": "chat-bot", "description": "Review the UI", "hours": 24,
    })
    assert goal.status_code == intention.status_code == 200

    for mode in ("normal", "roleplay", "simulation", "dream", "hypothetical"):
        session = request(app, "POST", "/playground/api/session", {
            "identity_id": "chat-bot", "mode": mode,
        })
        assert session.status_code == 200
        assert session.json()["mode"] == mode

    complete_goal = request(
        app, "POST", f"/playground/api/goals/{goal.json()['id']}/complete", {"identity_id": "chat-bot"},
    )
    complete_intention = request(
        app, "POST", f"/playground/api/intentions/{intention.json()['id']}/complete", {"identity_id": "chat-bot"},
    )
    assert complete_goal.json()["status"] == "completed"
    assert complete_intention.json()["status"] == "completed"

    restarted = request(app, "POST", "/playground/api/restart", {"identity_id": "chat-bot"})
    assert restarted.status_code == 200
    state = request(app, "GET", "/playground/api/identity/chat-bot").json()
    assert any(item["title"] == "Ship Identity Chat" for item in state["goals"])
    assert any(item["description"] == "Review the UI" for item in state["intentions"])


def test_stream_export_constitution_and_multiple_identities(chat_app):
    app, manager = chat_app
    create_identity(app, "alpha")
    create_identity(app, "beta")
    request(app, "POST", "/playground/api/goals", {
        "identity_id": "alpha", "title": "Alpha-only goal",
    })
    request(app, "POST", "/playground/api/goals", {
        "identity_id": "beta", "title": "Beta-only goal",
    })

    alpha = request(app, "GET", "/playground/api/identity/alpha").json()
    assert {goal["title"] for goal in alpha["goals"]} == {"Alpha-only goal"}
    assert "Beta-only goal" not in manager.get_runtime().goal_engine.to_prompt_summary("alpha")

    stream = request(app, "POST", "/playground/api/chat/stream", {
        "identity_id": "alpha", "user_input": "Hello",
    })
    assert stream.status_code == 200
    events = [json.loads(line) for line in stream.text.splitlines()]
    assert any(item["type"] == "event" for item in events)
    assert any(item["type"] == "chunk" for item in events)
    assert events[-1]["type"] == "done"

    portable = request(app, "GET", "/playground/api/export/alpha")
    assert portable.status_code == 200
    assert "attachment" in portable.headers["content-disposition"]
    assert portable.json()["identity"]["id"] == "alpha"

    constitution = request(app, "GET", "/playground/api/constitution/alpha")
    assert constitution.status_code == 200
    assert "constitution" in constitution.json()
    assert isinstance(constitution.json()["laws"], dict)


def test_page_exposes_all_identity_subsystems(chat_app):
    app, _manager = chat_app
    page = request(app, "GET", "/playground")
    assert page.status_code == 200
    for element_id in (
        "session-mode",
        "btn-export",
        "panel-timeline-body",
        "panel-goals-body",
        "panel-intentions-body",
        "panel-relationships-body",
        "panel-memories-body",
        "panel-constitution-body",
    ):
        assert f'id="{element_id}"' in page.text
